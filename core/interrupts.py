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
from typing import Optional

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger(__name__)

# ── Interrupt Selector Groups ─────────────────────────────────────────────────

# Priority 1 — Blocking overlays & modals
# These sit on top of everything else and must be cleared first.
_OVERLAY_SELECTORS: list[str] = [
    # Generic ARIA close buttons
    "button[aria-label='Close']",
    "button[aria-label='close']",
    "button[aria-label='Dismiss']",
    "button[aria-label='dismiss']",
    "button[aria-label='Schließen']",         # German
    "button[aria-label='Fermer']",             # French
    # data-testid patterns (widely used in modern SPAs)
    "[data-testid='close-button']",
    "[data-testid='modal-close']",
    "[data-testid='dialog-close']",
    # Bootstrap / common CSS frameworks
    "button.close",
    "button.btn-close",
    "[data-dismiss='modal']",
    # Class-name patterns
    ".modal-close",
    ".close-button",
    ".popup-close",
    ".lightbox-close",
    ".overlay-close",
    "[class*='modal'] button[class*='close']",
    "[class*='dialog'] button[class*='close']",
    "[class*='popup'] button[class*='close']",
    # role=dialog close buttons (ARIA-compliant patterns)
    "[role='dialog'] button[aria-label*='lose']",
    "[role='dialog'] button[aria-label*='ismiss']",
    "[role='alertdialog'] button[aria-label*='lose']",
]

# Priority 2 — Cookie banners & consent dialogs
# Accept/agree selectors — dismissing by acceptance (not rejection) to avoid
# sites hiding content behind consent walls.
_COOKIE_SELECTORS: list[str] = [
    # English variants (most common)
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Accept all cookies')",
    "button:has-text('Accept cookies')",
    "button:has-text('Accept Cookies')",
    "button:has-text('Accept')",
    "button:has-text('Agree')",
    "button:has-text('Agree and proceed')",
    "button:has-text('Agree to all')",
    "button:has-text('Allow all')",
    "button:has-text('Allow All')",
    "button:has-text('Allow cookies')",
    "button:has-text('Allow all cookies')",
    "button:has-text('Got it')",
    "button:has-text('I agree')",
    "button:has-text('I Accept')",
    "button:has-text('Confirm all')",
    # German variants
    "button:has-text('Alle akzeptieren')",
    "button:has-text('Akzeptieren')",
    "button:has-text('Zustimmen')",
    "button:has-text('Einverstanden')",
    "button:has-text('Alle zulassen')",
    # French variants
    "button:has-text('Tout accepter')",
    "button:has-text('Accepter tout')",
    "button:has-text('Accepter')",
    # YouTube-specific consent dialogs
    "ytd-consent-bump-v2-renderer button.VfPpkd-LgbsSe",
    "ytd-consent-bump-v2-renderer button[aria-label*='Accept']",
    "[aria-label='Accept the use of cookies and other data for the purposes described']",
    "tp-yt-paper-dialog .ytd-button-renderer:has-text('Accept all')",
    "ytd-consent-bump-v2-renderer form button:last-of-type",
    # Named IDs — well-known Consent Management Platforms (CMPs)
    "#onetrust-accept-btn-handler",                          # OneTrust
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll", # Cookiebot
    "#accept-all-cookies",
    "#cookie-accept",
    "#cookie-consent-accept",
    "#cookies-accept",
    "#gdpr-cookie-accept",
    "#accept_all",
    ".cookie-accept-all",
    ".js-accept-cookies",
    "[data-cookiebanner='accept_button']",
    "[aria-label='Accept cookies']",
    "[aria-label='Accept all cookies']",
    # Generic class/ID pattern matching
    "button[class*='cookie'][class*='accept']",
    "button[id*='cookie'][id*='accept']",
    "button[class*='consent'][class*='accept']",
    "button[id*='consent'][id*='accept']",
]

# Priority 3 — YouTube ads
# Skip buttons + overlay close. Text-based selectors are locale-agnostic.
_AD_SELECTORS: list[str] = [
    # Skip button — text-based (works across all YouTube locales)
    "button:has-text('Skip Ads')",
    "button:has-text('Skip Ad')",
    "button:has-text('Skip ads')",
    "button:has-text('Überspringen')",         # German
    "button:has-text('Passer')",               # French
    "button:has-text('Omitir')",               # Spanish
    "button:has-text('Preskočit')",            # Slovak/Czech
    # YouTube-specific DOM selectors (may change with YouTube UI updates)
    ".ytp-skip-ad-button",
    ".ytp-ad-skip-button",
    ".ytp-ad-skip-button-modern",
    "button.ytp-skip-ad-button",
    "[class*='ytp-skip-ad']",
    # Ad overlay dismiss
    ".ytp-ad-overlay-close-button",
    ".ytp-ad-overlay-slot .ytp-ad-overlay-close-button",
    ".ytp-ad-text-overlay .ytp-ad-overlay-close-container button",
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
        any_dismissed = False
        for group_name, selectors in _INTERRUPT_GROUPS:
            dismissed = self._try_dismiss(page, group_name, selectors)
            if dismissed:
                any_dismissed = True
        if any_dismissed:
            logger.info("[interrupts] ✓ Interrupts cleared.")
        else:
            logger.debug("[interrupts] No interrupts detected.")
        return any_dismissed

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
