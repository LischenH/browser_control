"""
core/interrupts.py — Global Interrupt Handler (Phase 10).

Automatically detects and dismisses UI interruptions before actions execute,
without ever blocking or breaking the main execution flow.

Interrupt types (handled in priority order):
  1. Blocking overlays & modals   — highest priority: sit on top of everything
  2. Cookie banners               — prevent interaction with page content
  3. YouTube ads                  — skip-button or overlay close

Design principles (consistent with core philosophy):
  - Selector LISTS — not single selectors; tries all, uses first match
  - is_visible() pre-check before every blocking call (~1ms, never times out)
  - Non-fatal: all exceptions are caught silently; flow is never interrupted
  - Priority order: overlays → cookies → ads
  - Fast path: if nothing is visible, entire check costs ~10–30ms total
  - Idempotent: safe to call before every action, even on already-clean pages

Integration points (core/actions.py):
  - Before click()
  - Before type_text()
  - After navigate()
  - On retry inside _try_selector()

Public API:
  handler = InterruptHandler()
  handler.handle(page)   # returns True if anything was dismissed
"""

import logging
import time
from typing import Optional

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

# ── Cache constants ───────────────────────────────────────────────────────────
# MAJOR-9: Skip the full interrupt scan if the page URL has not changed and
# the last clean-pass happened within this window (seconds).
_INTERRUPT_CACHE_TTL: float = 2.0

logger = logging.getLogger(__name__)

# ── Interrupt Selector Groups ─────────────────────────────────────────────────

# Priority 1 — Blocking overlays & modals
# These sit on top of everything else and must be cleared first.
# SAFETY: Only ARIA close/dismiss labels are used here — exact matches only.
# Removed: button.close, button.btn-close, [data-dismiss='modal'],
#          .modal-close, .close-button, .popup-close, .lightbox-close, .overlay-close
# These are too generic and match Amazon checkout confirmation dialogs,
# Bootstrap modals on order pages, and other legitimate UI elements.
# Removed: button[aria-label*='lose'] partial match — matches 'foreclose', 'disclose' etc.
# Removed: button:has-text('OK'), button:has-text('Continue') — match checkout flows.
_OVERLAY_SELECTORS: list[str] = [
    # Generic ARIA close buttons — EXACT label matches only (no partial matching)
    "button[aria-label='Close']",
    "button[aria-label='close']",
    "button[aria-label='Dismiss']",
    "button[aria-label='dismiss']",
    "button[aria-label='Schließen']",         # German exact
    "button[aria-label='Fermer']",             # French exact
    # data-testid patterns (widely used in modern SPAs) — canonical data attributes
    "[data-testid='close-button']",
    "[data-testid='modal-close']",
    "[data-testid='dialog-close']",
    # Compound class patterns — require BOTH modal/dialog/popup AND close in class names.
    # More specific than single-class patterns; unlikely to match Amazon checkout.
    "[class*='modal'] button[class*='close']",
    "[class*='dialog'] button[class*='close']",
    "[class*='popup'] button[class*='close']",
    # role=dialog/alertdialog — EXACT aria-label only (no *='lose' partial matching)
    "[role='dialog'] button[aria-label='Close']",
    "[role='dialog'] button[aria-label='close']",
    "[role='dialog'] button[aria-label='Dismiss']",
    "[role='dialog'] button[aria-label='dismiss']",
    "[role='alertdialog'] button[aria-label='Close']",
    "[role='alertdialog'] button[aria-label='close']",
]

# Priority 2 — Cookie banners & consent dialogs
# Accept/agree selectors — dismissing by acceptance (not rejection) to avoid
# sites hiding content behind consent walls.
# Selector priority: ID > data-* > aria-* > class/structure > YouTube-specific > text
_COOKIE_SELECTORS: list[str] = [
    # ── Gruppe 1: ID-basiert (höchste Priorität) ─────────────────────────────
    # Named IDs — well-known Consent Management Platforms (CMPs).
    # IDs are the most specific and stable selectors available.
    "#onetrust-accept-btn-handler",                          # OneTrust
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll", # Cookiebot
    "#accept-all-cookies",
    "#cookie-accept",
    "#cookie-consent-accept",
    "#cookies-accept",
    "#gdpr-cookie-accept",
    "#accept_all",
    # ── Gruppe 2: data-Attribute ──────────────────────────────────────────────
    # data-* attributes are explicit CMP API surface — very reliable.
    "[data-cookiebanner='accept_button']",
    # ── Gruppe 3: aria-Attribute ──────────────────────────────────────────────
    # Exact aria-label matches — no partial matching to avoid false positives.
    "[aria-label='Accept cookies']",
    "[aria-label='Accept all cookies']",
    # ── Gruppe 4: Klassen/Struktur ────────────────────────────────────────────
    # Class-based and compound structural selectors.
    ".cookie-accept-all",
    ".js-accept-cookies",
    "button[class*='cookie'][class*='accept']",
    "button[id*='cookie'][id*='accept']",
    "button[class*='consent'][class*='accept']",
    "button[id*='consent'][id*='accept']",
    # ── Gruppe 5: YouTube-spezifisch (Komponenten-Selektoren) ─────────────────
    # YouTube consent dialogs use custom web components — scoped selectors.
    "ytd-consent-bump-v2-renderer button.VfPpkd-LgbsSe",
    "ytd-consent-bump-v2-renderer button[aria-label*='Accept']",
    "[aria-label='Accept the use of cookies and other data for the purposes described']",
    "tp-yt-paper-dialog .ytd-button-renderer:has-text('Accept all')",
    "ytd-consent-bump-v2-renderer form button:last-of-type",
    # ── Gruppe 6: Text-basiert (niedrigste Priorität) ─────────────────────────
    # Text matching is locale-dependent and prone to false positives on
    # checkout/ToS pages. Used only as last resort.
    # English variants — multi-word or cookie-context-specific ONLY.
    # REMOVED: 'Accept', 'Agree', 'Got it', 'I agree', 'I Accept', 'Confirm all'
    # Reason: Amazon checkout uses these exact words on payment/ToS acceptance buttons.
    # Only multi-word phrases or well-known cookie-specific phrases are retained.
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept all cookies')",
    "button:has-text('Accept cookies')",
    "button:has-text('Accept Cookies')",
    "button:has-text('Agree and proceed')",
    "button:has-text('Agree to all')",
    "button:has-text('Allow all')",
    "button:has-text('Allow All')",
    "button:has-text('Allow cookies')",
    "button:has-text('Allow all cookies')",
    # German variants — multi-word only.
    # REMOVED: 'Akzeptieren', 'Zustimmen', 'Einverstanden' — single-word, too generic.
    "button:has-text('Alle akzeptieren')",
    "button:has-text('Alle zulassen')",
    # French variants — multi-word only.
    # REMOVED: 'Accepter' — single-word, too generic.
    "button:has-text('Tout accepter')",
    "button:has-text('Accepter tout')",
]

# Priority 3 — YouTube ads
# Skip buttons + overlay close.
# Selector priority: data-*/class-based (stable) → text-based (locale fallback, end)
_AD_SELECTORS: list[str] = [
    # YouTube-specific DOM selectors (stable class names — try first)
    ".ytp-skip-ad-button",
    ".ytp-ad-skip-button",
    ".ytp-ad-skip-button-modern",
    "button.ytp-skip-ad-button",
    "[class*='ytp-skip-ad']",
    # Ad overlay dismiss
    ".ytp-ad-overlay-close-button",
    ".ytp-ad-overlay-slot .ytp-ad-overlay-close-button",
    ".ytp-ad-text-overlay .ytp-ad-overlay-close-container button",
    # Text-based selectors at END — locale-agnostic but lower priority
    "button:has-text('Skip Ads')",
    "button:has-text('Skip Ad')",
    "button:has-text('Skip ads')",
    "button:has-text('Überspringen')",         # German
    "button:has-text('Passer')",               # French
    "button:has-text('Omitir')",               # Spanish
    "button:has-text('Preskočit')",            # Slovak/Czech
]

# Master list in priority order (group_name, selectors)
_INTERRUPT_GROUPS: list[tuple[str, list[str]]] = [
    ("overlay", _OVERLAY_SELECTORS),
    ("cookie",  _COOKIE_SELECTORS),
    ("ad",      _AD_SELECTORS),
]

# ── Interrupt Handler ─────────────────────────────────────────────────────────

class InterruptHandler:
    """
    Detects and dismisses UI interruptions without blocking execution.

    Runs all interrupt groups in priority order. For each group it tries
    every selector using a non-blocking is_visible() pre-check. Only if
    an element IS visible does it attempt a click.

    Performance:
        Fast path  (nothing to dismiss): ~10–30ms total
                   (one is_visible() call per selector, all non-blocking ~1ms each)
        Slow path  (interrupt found):    ~100–400ms
                   (click + 3s max timeout per interrupt)

    Usage:
        handler = InterruptHandler()
        handler.handle(page)            # before an action
        was_cleared = handler.handle(page)  # True if something was dismissed
    """

    def __init__(self) -> None:
        # Track the last handled selector per group (useful for debugging / logging)
        self._last_dismissed: dict[str, str] = {}
        # MAJOR-9 — Interrupt cache
        # Skips the full selector scan when the URL is unchanged and the last
        # clean pass was < _INTERRUPT_CACHE_TTL seconds ago.
        self._last_clean_url: str = ""
        self._last_clean_time: float = 0.0

    def handle(self, page: Page) -> bool:
        """
        Scans for and dismisses all active interruptions on the page.

        Iterates all groups in priority order. Continues even after finding
        one interruption — there may be multiple layers (e.g. cookie banner
        behind a modal). Each dismissal is independent.

        Args:
            page: Current Playwright Page to scan.

        Returns:
            True if at least one interrupt was dismissed, False if page was clean.
        """
        # ── MAJOR-9: Cache fast-path ──────────────────────────────────────────
        try:
            current_url = page.url
        except Exception:
            current_url = ""

        now = time.monotonic()
        if (
            current_url
            and current_url == self._last_clean_url
            and (now - self._last_clean_time) < _INTERRUPT_CACHE_TTL
        ):
            logger.debug(
                "[interrupts] Cache hit — skipping scan "
                f"({now - self._last_clean_time:.3f}s since last clean pass)"
            )
            return False
        # ── End cache fast-path ───────────────────────────────────────────────

        any_dismissed = False
        for group_name, selectors in _INTERRUPT_GROUPS:
            dismissed = self._try_dismiss(page, group_name, selectors)
            if dismissed:
                any_dismissed = True
        if any_dismissed:
            logger.info("[interrupts] ✓ Interrupts cleared.")
            # Invalidate cache — a dismissal means the page state changed.
            self._last_clean_url = ""
            self._last_clean_time = 0.0
        else:
            logger.debug("[interrupts] No interrupts detected.")
            # Store clean-pass result so the next call within TTL is skipped.
            self._last_clean_url = current_url
            self._last_clean_time = now
        return any_dismissed

    def invalidate_cache(self) -> None:
        """
        Explicitly invalidate the URL-keyed interrupt scan cache.

        Call this whenever the active page changes (e.g. after a tab switch or
        navigation) so that the next handle() call runs a full interrupt scan
        on the new page rather than relying on a stale cached result.

        Used by Executor after a page-sync event.
        """
        self._last_clean_url = ""
        self._last_clean_time = 0.0
        logger.debug("[interrupts] Cache invalidated (page changed).")

    def _try_dismiss(self, page: Page, group: str, selectors: list[str]) -> bool:
        """
        Tries to dismiss one interrupt group by clicking the first visible selector.

        Strategy:
          1. is_visible(selector) — non-blocking, ~1ms, returns False if absent
          2. If visible: page.click(selector, timeout=3000)
          3. Log success or failure, move on
          4. Return True on first successful click; False if nothing found

        All exceptions are caught. This method NEVER raises.

        Args:
            page:      Playwright Page to inspect.
            group:     Human-readable group name used in log messages.
            selectors: Ordered list of CSS selectors to try.

        Returns:
            True if a selector was found and successfully clicked.
        """
        for selector in selectors:
            try:
                # Non-blocking visibility check — ~1ms, never stalls
                if not page.is_visible(selector):
                    continue  # Not present or hidden — skip immediately

                # Element is visible — attempt to dismiss it
                logger.info(
                    f"[interrupt:{group}] Detected '{selector}' — dismissing"
                )
                page.click(selector, timeout=3000)  # 3s max, don't stall main flow
                logger.info(
                    f"[interrupt:{group}] ✓ Dismissed via '{selector}'"
                )
                self._last_dismissed[group] = selector
                return True  # One per group per handle() call is enough

            except PlaywrightTimeoutError:
                logger.debug(
                    f"[interrupt:{group}] Click timeout on '{selector}' — skipping"
                )
            except Exception as exc:
                logger.debug(
                    f"[interrupt:{group}] Error on '{selector}': "
                    f"{type(exc).__name__}: {exc}"
                )

        return False
