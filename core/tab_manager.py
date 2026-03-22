"""
core/tab_manager.py — Tab-Lifecycle und Live-State.

FIX HISTORY (Production Stability):
  - Added internal sequential tab IDs (tab_id field on TabInfo)
  - Added _registry: dict[int, Page] — deterministic tab ownership tracking
  - open_tab() now returns a TabInfo WITH an assigned tab_id
  - switch_to_* methods always call bring_to_front() AND set conn.active_page
  - close_tab() removes the tab from _registry and resyncs active_page
  - get_tab_by_id(tab_id) — look up a tracked tab by its internal ID
  - All actions always operate on the correct Page via explicit focus switching

STABLE CONTRACT (never changes):
  manager.list_tabs()            → list[TabInfo]
  manager.switch_to_url(url)     → TabInfo
  manager.open_tab(url)          → TabInfo
  manager.close_tab(tab)         → None
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import Page

from core.browser import BrowserConnection

logger = logging.getLogger(__name__)

# Global sequential ID counter — survives the lifetime of a TabManager instance.
_TAB_ID_COUNTER: int = 0


def _next_tab_id() -> int:
    global _TAB_ID_COUNTER
    _TAB_ID_COUNTER += 1
    return _TAB_ID_COUNTER


@dataclass
class TabInfo:
    """
    Snapshot of a tab at query time, including a stable internal ID.

    page:    Playwright Page object — actions are executed through this.
    url:     Current URL.
    title:   Current <title> tag.
    index:   Position in the context's pages list (informational, not a stable key).
    tab_id:  Stable internal ID assigned at tab creation (never changes for this tab).
    """
    page: Page
    url: str
    title: str
    index: int
    tab_id: int = field(default_factory=_next_tab_id)

    def __repr__(self) -> str:
        return (
            f"TabInfo(id={self.tab_id}, index={self.index}, "
            f"title='{self.title[:40]}', url='{self.url[:60]}')"
        )


class TabManager:
    """
    Manages all tabs of the connected Chrome window.

    Tab IDs:
        Every tab opened via this manager receives a stable integer ID.
        The ID is stored in TabInfo.tab_id and can be used to retrieve
        the tab via get_tab_by_id(tab_id) regardless of its current index.

    Focus guarantee:
        Every switch_to_* method calls bring_to_front() AND sets
        conn.active_page — ensuring all subsequent actions land on the
        correct Page instance.

    Usage:
        manager = TabManager(connection)
        tab = manager.open_tab("https://youtube.com")
        print(tab.tab_id)            # stable ID
        manager.switch_to_index(0)   # switch back to first tab
        same_tab = manager.get_tab_by_id(tab.tab_id)  # retrieve by ID
    """

    def __init__(self, connection: BrowserConnection) -> None:
        self._conn = connection
        # Registry: tab_id → Page (for deterministic lookup by ID)
        self._registry: dict[int, Page] = {}
        # Seed the registry with already-open tabs
        self._seed_registry()

    # ── Registry Management ────────────────────────────────────────────────────

    def _seed_registry(self) -> None:
        """
        Assigns IDs to all pages that were already open before TabManager
        was instantiated (e.g. existing Chrome tabs).
        """
        pages = self._conn.context.pages
        for page in pages:
            tab_id = _next_tab_id()
            self._registry[tab_id] = page
        logger.debug(
            f"[TabManager] Seeded registry with {len(pages)} existing page(s)."
        )

    def _register(self, page: Page) -> int:
        """Assigns a new tab ID to a Page and stores it in the registry."""
        tab_id = _next_tab_id()
        self._registry[tab_id] = page
        logger.debug(f"[TabManager] Registered new tab (id={tab_id}): {page.url[:60]!r}")
        return tab_id

    def _cleanup_registry(self) -> None:
        """Removes entries for pages that are no longer in the context."""
        live_pages = set(self._conn.context.pages)
        dead_ids = [tid for tid, page in self._registry.items() if page not in live_pages]
        for tid in dead_ids:
            logger.debug(f"[TabManager] Removing closed tab from registry (id={tid})")
            del self._registry[tid]

    def get_tab_by_id(self, tab_id: int) -> Optional["TabInfo"]:
        """
        Retrieves a tracked tab by its stable internal ID.

        Args:
            tab_id: The tab_id returned by open_tab() or list_tabs().

        Returns:
            TabInfo if the tab is still alive, None if it was closed.
        """
        self._cleanup_registry()
        page = self._registry.get(tab_id)
        if page is None:
            logger.debug(f"[TabManager] get_tab_by_id({tab_id}): not found (closed?)")
            return None
        try:
            idx = self._get_page_index(page)
            return TabInfo(
                page=page,
                url=page.url,
                title=page.title(),
                index=idx,
                tab_id=tab_id,
            )
        except Exception as exc:
            logger.debug(f"[TabManager] get_tab_by_id({tab_id}): page error: {exc}")
            return None

    # ── Live Tab Queries ────────────────────────────────────────────────────────

    def list_tabs(self) -> list[TabInfo]:
        """
        Returns all open tabs — LIVE (no cache), always fresh from Playwright.

        Tab IDs:
          - Tabs that were opened via this manager retain their assigned IDs.
          - Tabs opened externally (user-created) receive new IDs if not already
            in the registry.
        """
        self._cleanup_registry()
        pages = self._conn.context.pages

        # Build a reverse map: page → tab_id
        page_to_id: dict[Page, int] = {v: k for k, v in self._registry.items()}

        tabs: list[TabInfo] = []
        for i, page in enumerate(pages):
            # Assign an ID if this page has never been seen before
            if page not in page_to_id:
                new_id = self._register(page)
                page_to_id[page] = new_id

            try:
                tabs.append(TabInfo(
                    page=page,
                    url=page.url,
                    title=page.title(),
                    index=i,
                    tab_id=page_to_id[page],
                ))
            except Exception as exc:
                logger.debug(f"[TabManager] list_tabs: skipping page #{i} ({exc})")

        logger.debug(f"[TabManager] list_tabs → {len(tabs)} tab(s)")
        for tab in tabs:
            logger.debug(f"  {tab}")
        return tabs

    def get_active_tab(self) -> TabInfo:
        """Returns the currently active tab (conn.active_page)."""
        page = self._conn.active_page
        page_to_id: dict[Page, int] = {v: k for k, v in self._registry.items()}
        tab_id = page_to_id.get(page)
        if tab_id is None:
            tab_id = self._register(page)
        return TabInfo(
            page=page,
            url=page.url,
            title=page.title(),
            index=self._get_page_index(page),
            tab_id=tab_id,
        )

    # ── Tab Switching ───────────────────────────────────────────────────────────

    def switch_to_url(self, url_fragment: str) -> TabInfo:
        """
        Activates the first tab whose URL contains url_fragment.
        Sets conn.active_page to the found Page (focus guarantee).
        """
        logger.info(f"[TabManager] switch_to_url: '{url_fragment}'")
        tabs = self.list_tabs()
        for tab in tabs:
            if url_fragment.lower() in tab.url.lower():
                self._activate(tab)
                logger.info(f"[TabManager] ✓ Switched → {tab}")
                return tab
        raise ValueError(
            f"[TabManager] No tab with URL fragment '{url_fragment}'. "
            f"Open tabs: {[t.url for t in tabs]}"
        )

    def switch_to_title(self, title_fragment: str) -> TabInfo:
        """Activates the first tab whose title contains title_fragment."""
        logger.info(f"[TabManager] switch_to_title: '{title_fragment}'")
        tabs = self.list_tabs()
        for tab in tabs:
            if title_fragment.lower() in tab.title.lower():
                self._activate(tab)
                logger.info(f"[TabManager] ✓ Switched → {tab}")
                return tab
        raise ValueError(
            f"[TabManager] No tab with title fragment '{title_fragment}'. "
            f"Open tabs: {[t.title for t in tabs]}"
        )

    def switch_to_index(self, index: int) -> TabInfo:
        """Activates the tab at the given 0-based index."""
        tabs = self.list_tabs()
        if index < 0 or index >= len(tabs):
            raise IndexError(
                f"[TabManager] Tab index {index} out of range [0, {len(tabs) - 1}]."
            )
        tab = tabs[index]
        self._activate(tab)
        logger.info(f"[TabManager] ✓ Switched to index {index} → {tab}")
        return tab

    def switch_to_tab_id(self, tab_id: int) -> TabInfo:
        """
        Activates the tab with the given internal tab_id.
        Raises ValueError if the tab has been closed.
        """
        tab = self.get_tab_by_id(tab_id)
        if tab is None:
            raise ValueError(
                f"[TabManager] Tab with id={tab_id} not found (may have been closed)."
            )
        self._activate(tab)
        logger.info(f"[TabManager] ✓ Switched to tab_id={tab_id} → {tab}")
        return tab

    # ── Tab Lifecycle ───────────────────────────────────────────────────────────

    def open_tab(self, url: Optional[str] = None) -> TabInfo:
        """
        Opens a new BACKGROUND tab, navigates to URL (if provided), and
        returns a TabInfo with a stable tab_id.

        ROOT-CAUSE NOTE — why window.open() instead of context.new_page():
          context.new_page() calls CDP Target.activateTarget, which triggers
          Chrome's autoplay unblock for ALL tabs in the context. This would
          cause background YouTube tabs to start playing spontaneously.
          window.open('url', '_blank') opens the tab in the background without
          activating it. Playwright captures the Page reference via expect_page().

        The new tab becomes conn.active_page so that the executor's next
        step automatically operates on it.

        Args:
            url: Optional URL to navigate to. None → about:blank.

        Returns:
            TabInfo with a stable tab_id.
        """
        target_url = url or "about:blank"
        logger.info(f"[TabManager] open_tab → {target_url}")

        with self._conn.context.expect_page() as page_event:
            self._conn.active_page.evaluate(
                f"window.open({target_url!r}, '_blank')"
            )

        new_page = page_event.value
        new_page.wait_for_load_state("domcontentloaded")

        # Register before setting active_page so the ID is available immediately
        tab_id = self._register(new_page)

        # Become the active page so that executor's next step uses this tab
        self._conn.active_page = new_page

        tab = TabInfo(
            page=new_page,
            url=new_page.url,
            title=new_page.title(),
            index=self._get_page_index(new_page),
            tab_id=tab_id,
        )
        logger.info(f"[TabManager] ✓ Opened background tab: {tab}")
        return tab

    def close_tab(self, tab: Optional[TabInfo] = None) -> None:
        """
        Closes a tab (default: current active tab).
        After closing, conn.active_page is set to the most recently opened
        remaining tab.

        Args:
            tab: TabInfo to close. None → closes the active tab.
        """
        target = tab or self.get_active_tab()
        logger.info(f"[TabManager] close_tab: {target}")

        # Remove from registry first
        self._cleanup_registry()
        dead_ids = [tid for tid, p in self._registry.items() if p is target.page]
        for tid in dead_ids:
            del self._registry[tid]

        target.page.close()

        # Resync active page
        remaining = self._conn.context.pages
        if remaining:
            self._conn.active_page = remaining[-1]
            try:
                logger.info(
                    f"[TabManager] ✓ Closed. New active tab: "
                    f"'{remaining[-1].title()[:40]}'"
                )
            except Exception:
                logger.info("[TabManager] ✓ Closed.")
        else:
            logger.warning("[TabManager] No tabs remaining after close.")

    # ── Internal Helpers ────────────────────────────────────────────────────────

    def _activate(self, tab: TabInfo) -> None:
        """
        Focus guarantee: brings the tab to the front AND sets conn.active_page.
        Both steps are required — bring_to_front() alone doesn't update the
        active_page that the executor uses for actions.
        """
        try:
            tab.page.bring_to_front()
        except Exception as exc:
            logger.debug(f"[TabManager] bring_to_front failed (non-fatal): {exc}")
        self._conn.active_page = tab.page

    def _get_page_index(self, page: Page) -> int:
        """Returns the 0-based index of a Page in the context's pages list."""
        try:
            return self._conn.context.pages.index(page)
        except ValueError:
            return -1
