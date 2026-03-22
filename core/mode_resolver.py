"""
core/mode_resolver.py -- Execution-Mode-Aufloesung (Phase 7c)

FIX HISTORY (Production Stability):
  - Amazon moved from HUMAN_DOMAINS to FAST_DOMAINS.
    Requirement: YouTube AND Amazon must run in FAST mode.
  - Unknown-site fallback changed from 'fast' to 'human'.
    Rationale: known-safe sites are in FAST_DOMAINS; anything else should
    default to the safer, slower mode rather than risk breaking fragile pages.
  - HUMAN_DOMAINS narrowed to sites with genuine bot-detection / challenge pages.

Resolution hierarchy:
  1. config.EXECUTION_MODE == "fast" | "human"  -> return directly
  2. config.EXECUTION_MODE == "auto":
       a. Known FAST_DOMAINS   -> "fast"
       b. Known HUMAN_DOMAINS  -> "human"
       c. Login/Form/Checkout URL patterns -> "human"
       d. Dynamic DOM analysis (if page provided) -> "fast" | "human"
       e. Fallback: unknown sites -> "human"   [CHANGED from "fast"]

Public API (stable contract):
  resolve_mode(url: str, page: Page | None = None) -> str
  ModeResolver (class with patterns as class constants, easily testable)
"""

from __future__ import annotations

import logging
from typing import Optional

from playwright.sync_api import Page

import config

logger = logging.getLogger(__name__)

# -- JS: DOM mutation rate detection ------------------------------------------
# Observes <body> mutations for OBSERVE_MS milliseconds.
# Returns true when many mutations occurred (-> HUMAN), false for quiet DOM (-> FAST).
_JS_DOM_DYNAMICS = """
() => new Promise(resolve => {
    const threshold = 10;
    let count = 0;
    const observer = new MutationObserver(mutations => {
        count += mutations.length;
    });
    observer.observe(document.body || document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        characterData: false,
    });
    setTimeout(() => {
        observer.disconnect();
        resolve(count > threshold);
    }, OBSERVE_MS);
})
""".strip()

# -- JS: Infinite-scroll / layout-shift detection ----------------------------
_JS_DETECT_INFINITE_SCROLL = """
() => {
    const hasInfiniteScroll = !!(
        document.querySelector('[infinite-scroll]') ||
        document.querySelector('[data-infinite-scroll]') ||
        document.querySelector('.infinite-scroll') ||
        document.querySelector('[data-component-type="s-search-results"]')
    );
    const hasDynamicGrid = document.querySelectorAll(
        '[data-asin], [data-component-type="s-search-result"]'
    ).length > 3;
    return hasInfiniteScroll || hasDynamicGrid;
}
""".strip()


class ModeResolver:
    """
    Determines the execution mode (fast / human) for a given URL.

    All patterns are class attributes -> easy to test and extend without
    constructor injection.
    """

    # Domains that always get FAST mode (direct DOM interaction, minimal delay)
    # FIX: Amazon moved here from HUMAN_DOMAINS.
    # Production requirement: YouTube AND Amazon must both run in FAST mode.
    FAST_DOMAINS: tuple[str, ...] = (
        "youtube.com",
        "google.com",
        "wikipedia.org",
        "github.com",
        "stackoverflow.com",
        "docs.",
        "developer.",
        # Shopping sites -- structure is consistent enough for direct interaction
        "amazon.",
        "ebay.",
        "etsy.",
        "walmart.",
        "aliexpress.",
    )

    # Domains that always get HUMAN mode (simulated mouse, randomized delays)
    # FIX: Narrowed to sites with genuine bot-detection / Cloudflare challenges.
    HUMAN_DOMAINS: tuple[str, ...] = (
        "shopify.",
        "cloudflare.",
        "ticketmaster.",
        "stubhub.",
    )

    # URL path patterns that force HUMAN mode (logins, forms, checkouts)
    HUMAN_PATH_PATTERNS: tuple[str, ...] = (
        "login",
        "signin",
        "sign-in",
        "sign_in",
        "checkout",
        "cart",
        "payment",
        "register",
        "password",
        "account",
        "auth",
        "oauth",
        "verify",
        "captcha",
    )

    def resolve(self, url: str, page: Optional[Page] = None) -> str:
        """
        Resolves the execution mode for the given URL.

        Args:
            url:  Current page URL.
            page: Playwright Page for dynamic DOM analysis (optional).

        Returns:
            "fast" or "human"
        """
        # -- 1. Explicit config override has highest priority ------------------
        global_mode = config.EXECUTION_MODE
        if global_mode in ("fast", "human"):
            logger.debug(f"[ModeResolver] Config override: '{global_mode}'")
            return global_mode

        # -- 2. URL-based pattern matching ------------------------------------
        url_lower = url.lower()

        # FIX: Check HUMAN_PATH_PATTERNS FIRST before FAST_DOMAINS.
        # Rationale: login/checkout/payment pages need HUMAN mode even on
        # otherwise-FAST domains like amazon.de or youtube.com.
        # (Previously FAST_DOMAINS was checked first, so amazon.de/signin
        # matched 'amazon.' and returned 'fast' -- incorrect.)
        for pattern in self.HUMAN_PATH_PATTERNS:
            if pattern in url_lower:
                logger.debug(f"[ModeResolver] HUMAN <- path match '{pattern}' in '{url}'")
                return "human"

        for pattern in self.FAST_DOMAINS:
            if pattern in url_lower:
                logger.debug(f"[ModeResolver] FAST <- domain match '{pattern}' in '{url}'")
                return "fast"

        for pattern in self.HUMAN_DOMAINS:
            if pattern in url_lower:
                logger.debug(f"[ModeResolver] HUMAN <- domain match '{pattern}' in '{url}'")
                return "human"

        # -- 3. Dynamic page analysis (only when page is provided) -------------
        if page is not None:
            try:
                detected = self._detect_from_page(page)
                logger.debug(f"[ModeResolver] DOM analysis -> '{detected}' for '{url}'")
                return detected
            except Exception as exc:
                logger.debug(f"[ModeResolver] DOM analysis failed: {exc} -> fallback 'human'")

        # -- 4. Unknown site -> HUMAN (safer default) -------------------------
        # FIX: Changed from 'fast' to 'human'.
        # Known-safe, well-structured sites are already listed in FAST_DOMAINS.
        # For any unknown site, HUMAN mode (slower but more robust) prevents
        # breaking fragile SPAs or triggering bot-detection.
        logger.debug(f"[ModeResolver] Unknown URL '{url}' -> fallback 'human'")
        return "human"

    def _detect_from_page(self, page: Page) -> str:
        """
        Dynamic detection via JavaScript analysis of the live page.

        Checks:
          - Infinite-scroll attributes (instant, synchronous)
          - DOM mutation rate over a short observation window

        Returns:
            "fast" for quiet DOM, "human" for highly dynamic pages.
        """
        # Instant check: infinite scroll / dynamic grid structures
        has_dynamic_structure = page.evaluate(_JS_DETECT_INFINITE_SCROLL)
        if has_dynamic_structure:
            logger.debug("[ModeResolver] DOM: infinite-scroll detected -> HUMAN")
            return "human"

        # Mutation rate check: is the DOM actively changing?
        observe_ms = config.DOM_STABILITY_OBSERVE_MS
        js = _JS_DOM_DYNAMICS.replace("OBSERVE_MS", str(observe_ms))
        is_dynamic = page.evaluate(js)

        if is_dynamic:
            logger.debug(
                f"[ModeResolver] DOM: >10 mutations in {observe_ms}ms -> HUMAN"
            )
            return "human"

        logger.debug("[ModeResolver] DOM: quiet -> FAST")
        return "fast"


# -- Module-level singleton ---------------------------------------------------
# Skills and actions can import `resolve_mode(url)` directly without managing
# a ModeResolver instance.

_resolver = ModeResolver()


def resolve_mode(url: str, page: Optional[Page] = None) -> str:
    """
    Convenience function: resolves execution mode for `url`.
    Delegates to the module-level singleton `_resolver`.

    Args:
        url:  Current or target URL.
        page: Playwright Page for DOM analysis (optional, only in "auto" mode).

    Returns:
        "fast" or "human"
    """
    return _resolver.resolve(url, page)
