"""
core/browser.py — Chrome-Verbindung via Playwright CDP.

FIX HISTORY (Production Stability):
  - Added _health_check(): verifies CDP connection is alive after connect()
  - Added reconnect(): re-attaches to Chrome without requiring a new instance
  - Added resync_active_page(): recovers active_page after a tab is closed/crashed
  - connect() now raises an explicit, clear error if Chrome is not running with
    --remote-debugging-port (instead of a generic Playwright exception)
  - active_page property re-syncs automatically if the stored page is closed
"""

import logging
from typing import Optional

from playwright.sync_api import (
    Browser as PlaywrightBrowser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

import config

logger = logging.getLogger(__name__)


class BrowserConnection:
    """
    Holds the CDP connection to a running Chrome process.

    STABLE CONTRACT (never changes):
        conn = BrowserConnection()
        conn.connect()          → attaches to existing Chrome
        page = conn.active_page → currently active Page
        conn.disconnect()       → clean teardown (does NOT close Chrome)

    As context manager:
        with BrowserConnection() as conn:
            page = conn.active_page

    New reliability APIs (additive, do not break existing callers):
        conn.health_check()          → True if connection is alive
        conn.reconnect()             → re-attaches without restarting Chrome
        conn.resync_active_page()    → picks a live page if stored page is dead
    """

    def __init__(self) -> None:
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[PlaywrightBrowser] = None
        self._context: Optional[BrowserContext] = None
        self._active_page: Optional[Page] = None

    # ── Connection ─────────────────────────────────────────────────────────────

    def connect(self) -> "BrowserConnection":
        """
        Attaches to an already-running Chrome via CDP.

        Chrome MUST be started with:
            chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\tmp\\chrome_debug

        Raises:
            ConnectionError: explicit message if Chrome is not reachable or
                             has no debug port, or if the health-check fails.
        """
        cdp_url = config.CHROME_CDP_URL
        logger.info(f"[BrowserConnection] Connecting via CDP: {cdp_url}")

        try:
            self._playwright = sync_playwright().start()
            # connect_over_cdp ATTACHES — it does NOT launch a new process.
            self._browser = self._playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            # Stop playwright before re-raising so resources are released.
            if self._playwright:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
            self._playwright = None
            raise ConnectionError(
                f"\n"
                f"  ❌  Cannot connect to Chrome at {cdp_url}\n"
                f"\n"
                f"  Chrome must be running with remote debugging enabled.\n"
                f"  Start it like this:\n"
                f"\n"
                f"    Windows : chrome.exe --remote-debugging-port={config.CHROME_DEBUG_PORT} "
                f"--user-data-dir=C:\\tmp\\chrome_debug\n"
                f"    macOS   : /Applications/Google\\ Chrome.app/Contents/MacOS/"
                f"Google\\ Chrome --remote-debugging-port={config.CHROME_DEBUG_PORT}\n"
                f"    Linux   : google-chrome --remote-debugging-port={config.CHROME_DEBUG_PORT}\n"
                f"\n"
                f"  Original error: {exc}"
            ) from exc

        # ── Resolve context ────────────────────────────────────────────────────
        contexts = self._browser.contexts
        if not contexts:
            raise ConnectionError(
                f"Connected to Chrome at {cdp_url} but found NO browser contexts.\n"
                f"Make sure Chrome has at least one window/tab open."
            )

        self._context = contexts[0]
        logger.info(
            f"[BrowserConnection] Attached. "
            f"Contexts: {len(contexts)}, "
            f"Pages in context[0]: {len(self._context.pages)}"
        )

        # ── Resolve active page ────────────────────────────────────────────────
        self._active_page = self._pick_best_page()
        logger.info(
            f"[BrowserConnection] Active page: "
            f"'{self._active_page.title()[:60]}' | {self._active_page.url}"
        )

        # ── Health-check ────────────────────────────────────────────────────────
        if not self.health_check():
            raise ConnectionError(
                f"Health-check failed after connecting to Chrome at {cdp_url}.\n"
                f"CDP connection was established but JS evaluation failed — "
                f"Chrome may be in a bad state."
            )

        logger.info("[BrowserConnection] ✓ Health-check passed. Connection ready.")
        return self

    def disconnect(self) -> None:
        """
        Clean teardown. Does NOT close Chrome.
        Safe to call multiple times (idempotent).
        """
        logger.info("[BrowserConnection] Disconnecting.")
        if self._browser:
            try:
                self._browser.close()
            except Exception as exc:
                logger.debug(f"[BrowserConnection] browser.close() error (ignored): {exc}")
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception as exc:
                logger.debug(f"[BrowserConnection] playwright.stop() error (ignored): {exc}")
        self._browser = None
        self._playwright = None
        self._context = None
        self._active_page = None

    # ── Reliability APIs ───────────────────────────────────────────────────────

    def health_check(self) -> bool:
        """
        Verifies the CDP connection is alive by evaluating a trivial JS expression.

        Returns:
            True  — connection is alive and responsive
            False — connection is dead, unresponsive, or not yet established
        """
        if self._active_page is None:
            logger.debug("[BrowserConnection] Health-check: not connected (active_page is None)")
            return False
        try:
            result = self._active_page.evaluate("() => document.readyState")
            logger.debug(f"[BrowserConnection] Health-check OK (readyState={result!r})")
            return True
        except Exception as exc:
            logger.warning(f"[BrowserConnection] Health-check FAILED: {exc}")
            return False

    def reconnect(self) -> "BrowserConnection":
        """
        Tears down the existing connection and re-attaches to Chrome.

        Use this when health_check() returns False or after a transient
        CDP disconnect. Chrome itself must still be running.

        Returns self for chaining.

        Raises:
            ConnectionError: same as connect() if Chrome is not reachable.
        """
        logger.info("[BrowserConnection] Reconnecting ...")
        try:
            self.disconnect()
        except Exception:
            pass
        return self.connect()

    def resync_active_page(self) -> Page:
        """
        Recovers the active page reference after a tab close or crash.

        If the currently stored page is still alive, returns it unchanged.
        Otherwise, picks the best live page from the current context.

        Returns:
            A live Playwright Page object.

        Raises:
            RuntimeError: if there are no live pages at all.
        """
        # Check if current page is still alive
        try:
            _ = self._active_page.url  # accessing .url raises if page is closed
            _ = self._active_page.evaluate("() => 1")  # CDP round-trip
            logger.debug("[BrowserConnection] resync_active_page: current page is alive")
            return self._active_page
        except Exception:
            logger.warning(
                "[BrowserConnection] Stored active_page is dead — resyncing ..."
            )

        # Pick a new live page
        page = self._pick_best_page()
        self._active_page = page
        logger.info(
            f"[BrowserConnection] Resynced active page → "
            f"'{page.title()[:60]}' | {page.url}"
        )
        return page

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def active_page(self) -> Page:
        """The currently active Playwright Page."""
        if self._active_page is None:
            raise RuntimeError(
                "[BrowserConnection] Not connected. Call connect() first."
            )
        # Auto-recover if the page was closed externally
        try:
            _ = self._active_page.url
        except Exception:
            logger.warning(
                "[BrowserConnection] active_page was closed externally — resyncing"
            )
            self.resync_active_page()
        return self._active_page

    @active_page.setter
    def active_page(self, page: Page) -> None:
        """Allows TabManager to update the active page."""
        self._active_page = page
        try:
            logger.debug(
                f"[BrowserConnection] active_page → '{page.title()[:60]}' | {page.url}"
            )
        except Exception:
            logger.debug("[BrowserConnection] active_page updated (page details unavailable)")

    @property
    def context(self) -> BrowserContext:
        """The Playwright BrowserContext (= the Chrome window)."""
        if self._context is None:
            raise RuntimeError(
                "[BrowserConnection] Not connected. Call connect() first."
            )
        return self._context

    @property
    def browser(self) -> PlaywrightBrowser:
        """Raw Playwright Browser object (rarely needed directly)."""
        if self._browser is None:
            raise RuntimeError(
                "[BrowserConnection] Not connected. Call connect() first."
            )
        return self._browser

    # ── Context Manager ─────────────────────────────────────────────────────────

    def __enter__(self) -> "BrowserConnection":
        return self.connect()

    def __exit__(self, *_) -> None:
        self.disconnect()

    # ── Internal helpers ────────────────────────────────────────────────────────

    def _pick_best_page(self) -> Page:
        """
        Selects the best page from the current context.

        Priority:
          1. Last page in the context pages list (most recently activated/opened)
          2. Any page that is not about:blank
          3. If Chrome has no pages, opens a new blank page

        Returns:
            A Playwright Page object.
        """
        pages = self._context.pages

        if not pages:
            logger.warning(
                "[BrowserConnection] No pages found in context — opening a blank page."
            )
            return self._context.new_page()

        # Prefer pages with a real URL over about:blank / about:newtab
        non_blank = [
            p for p in pages
            if p.url not in ("about:blank", "chrome://newtab/", "")
        ]
        candidates = non_blank if non_blank else pages

        # Last in list = most recently focused/opened (Playwright convention)
        chosen = candidates[-1]
        logger.debug(
            f"[BrowserConnection] _pick_best_page: "
            f"{len(pages)} total page(s), chose index {pages.index(chosen)} "
            f"(url={chosen.url[:60]!r})"
        )
        return chosen
