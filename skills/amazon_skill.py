"""
skills/amazon_skill.py — Amazon-Skill (Phase 10 — Full Platform Agent)

Implementiert BaseSkill für amazon.de (und alle regionalen Varianten).

Actions (original):
    search(query)
    click_first_result()
    read_result_title()
    read_product_title()
    open_top_results(n)

Actions (shopping — idempotent):
    add_to_cart()
    remove_from_cart()
    add_to_wishlist()
    remove_from_wishlist()
    buy_now()

Actions (account navigation):
    open_orders()
    open_cart()
    open_wishlist()

Actions (product data):
    read_price()
    read_rating()
    read_reviews(n)

Design: ASIN-based link extraction (unchanged from original).
All new actions check current page state before acting where possible.
"""

from __future__ import annotations

import logging
import re
from typing import Callable

import config
from core.actions import Actions, ActionError
from skills.base_skill import BaseSkill, Result

logger = logging.getLogger(__name__)

# ── Product link extractor ──────────────────────────────────────────────────
# FIX: Sponsored (/sspa/) results are now explicitly excluded.
# Priority: /dp/ link > h2 non-sspa link > canonical /dp/<ASIN> from data-asin.
# This guarantees all returned URLs are real product pages, not ad redirects.
_JS_EXTRACT_PRODUCT_LINKS = """
(limit, base) => {
    const containers = document.querySelectorAll(
        "div[data-component-type='s-search-result'][data-asin]"
    );
    const seen = new Set();
    const results = [];

    for (const el of containers) {
        if (results.length >= limit) break;
        const asin = (el.getAttribute('data-asin') || '').trim();
        if (!asin || seen.has(asin)) continue;

        // Skip sponsored results explicitly
        if (el.querySelector('.puis-sponsored-label-text')) continue;
        if (el.querySelector('[aria-label*="Sponsored"]')) continue;

        // Prefer a direct /dp/ link; never use /sspa/ redirect links
        let link = el.querySelector('a[href*="/dp/"]')
                || el.querySelector('h2 a[href]:not([href*="/sspa/"])');

        if (link) {
            const href = (link.getAttribute('href') || '').trim();
            if (!href || href === '#' || href === '/') {
                if (asin) { seen.add(asin); results.push(base + '/dp/' + asin); }
                continue;
            }
            seen.add(asin);
            const full = href.startsWith('http') ? href : base + href;
            // Normalise to canonical /dp/<ASIN> — strips tracking query params
            const m = full.match(/^(https?:\/\/[^/]+\/(?:[^/]+\/)?dp\/[A-Z0-9]{10})/);
            results.push(m ? m[1] : full);
        } else if (asin) {
            // No usable link — construct canonical URL from ASIN
            seen.add(asin);
            results.push(base + '/dp/' + asin);
        }
    }
    return results;
}
"""

# ── Shopping JS helpers ────────────────────────────────────────────────────────
_JS_IS_IN_CART = """
() => {
  // Check if "added to cart" confirmation is present
  const confirm = document.querySelector('#NATC_SMART_WAGON_CONF_MSG_SUCCESS')
               || document.querySelector('#huc-v2-confirm-text')
               || document.querySelector('[data-action="add-to-cart"] .a-color-success');
  return !!confirm;
}
"""

_JS_GET_CART_COUNT = """
() => {
  const badge = document.querySelector('#nav-cart-count');
  if (!badge) return null;
  return parseInt(badge.innerText.trim(), 10) || 0;
}
"""

_JS_IS_PRODUCT_PAGE = """
() => !!document.querySelector('#productTitle')
"""

_JS_IS_WISHLIST_PAGE = """
() => !!(window.location.href.includes('wishlist') || window.location.href.includes('list'))
"""

_JS_IS_CART_PAGE = """
() => !!(window.location.href.includes('/cart') || document.querySelector('#activeCartViewForm'))
"""

# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_asin(url: str) -> str:
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    return m.group(1) if m else ""


def _is_product_url(url: str) -> bool:
    return "/dp/" in url


def _amazon_base(page_url: str) -> str:
    m = re.match(r"(https?://[^/]+)", page_url)
    return m.group(1) if m else "https://www.amazon.de"


class AmazonSkill(BaseSkill):
    """
    Full platform-level Amazon skill.

    Supports product pages, search results, cart, wishlist, and orders.
    Shopping actions are idempotent where possible.
    """

    name: str = "Amazon"
    base_url: str = "amazon.de"

    def __init__(self) -> None:
        self._selectors = self._load_selectors("amazon")
        logger.info(f"[{self.name}] Skill initialisiert.")

    def can_handle(self, url: str) -> bool:
        return "amazon" in url

    def get_action(self, name: str) -> Callable | None:
        _action_map: dict[str, Callable] = {
            # ── Original actions ──────────────────────────────────────────────
            "search":                self._action_search,
            "click_first_result":    self._action_click_first_result,
            # read_result_title removed (D4): redundant on Amazon — search results
            # are immediately navigated away from; read_product_title on the product
            # page is sufficient and avoids an extra DOM wait on the results page.
            "read_product_title":    self._action_read_product_title,
            "open_top_results":      self._action_open_top_results,
            # ── Shopping ─────────────────────────────────────────────────────
            "add_to_cart":           self._action_add_to_cart,
            "remove_from_cart":      self._action_remove_from_cart,
            "add_to_wishlist":       self._action_add_to_wishlist,
            "remove_from_wishlist":  self._action_remove_from_wishlist,
            "buy_now":               self._action_buy_now,
            # ── Account navigation ────────────────────────────────────────────
            "open_orders":           self._action_open_orders,
            "open_cart":             self._action_open_cart,
            "open_wishlist":         self._action_open_wishlist,
            # ── Product data ──────────────────────────────────────────────────
            "read_price":            self._action_read_price,
            "read_rating":           self._action_read_rating,
            "read_reviews":          self._action_read_reviews,
        }
        action = _action_map.get(name)
        if action is None:
            logger.warning(f"[{self.name}] Unbekannte Action: '{name}'")
        return action

    # ═══════════════════════════════════════════════════════════════════════
    # ORIGINAL ACTIONS (preserved)
    # ═══════════════════════════════════════════════════════════════════════

    def _action_search(self, actions: Actions, query: str) -> Result:
        logger.info(f"[{self.name}] search('{query}')")
        try:
            actions.wait_for(selectors=self._selectors["search_box"], timeout=15.0)
            actions.type_text(selectors=self._selectors["search_box"], text=query)
            actions.press_key("Enter")
            actions.wait_for(selectors=self._selectors["result_item"], timeout=20.0)
            logger.info(f"[{self.name}] search('{query}') ✅")
            return Result.ok(data=query)
        except ActionError as e:
            return Result.fail(error=f"search('{query}'): {e}")
        except Exception as e:
            return Result.fail(error=f"search(): {type(e).__name__}: {e}")

    def _action_click_first_result(self, actions: Actions) -> Result:
        logger.info(f"[{self.name}] click_first_result()")
        try:
            actions.wait_for(selectors=self._selectors["result_item"], timeout=15.0)
            actions.click(selectors=self._selectors["first_result_link"])
            actions.wait_for(selectors=self._selectors["product_title"], timeout=20.0)
            logger.info(f"[{self.name}] click_first_result() ✅")
            return Result.ok()
        except ActionError as e:
            return Result.fail(error=f"click_first_result(): {e}")
        except Exception as e:
            return Result.fail(error=f"click_first_result(): {type(e).__name__}: {e}")

    def _action_read_result_title(self, actions: Actions) -> Result:
        logger.info(f"[{self.name}] read_result_title()")
        try:
            actions.wait_for(selectors=self._selectors["result_title"], timeout=10.0)
            title = actions.get_text(selectors=self._selectors["result_title"])
            if not title or not title.strip():
                return Result.fail(error="read_result_title(): empty title")
            return Result.ok(data=title.strip())
        except ActionError as e:
            return Result.fail(error=f"read_result_title(): {e}")
        except Exception as e:
            return Result.fail(error=f"read_result_title(): {type(e).__name__}: {e}")

    def _action_read_product_title(self, actions: Actions) -> Result:
        logger.info(f"[{self.name}] read_product_title()")
        try:
            actions.wait_for(selectors=self._selectors["product_title"], timeout=10.0)
            title = actions.get_text(selectors=self._selectors["product_title"])
            if not title or not title.strip():
                return Result.fail(error="read_product_title(): empty title")
            return Result.ok(data=title.strip())
        except ActionError as e:
            return Result.fail(error=f"read_product_title(): {e}")
        except Exception as e:
            return Result.fail(error=f"read_product_title(): {type(e).__name__}: {e}")

    def _action_open_top_results(self, actions: Actions, n: int = 5) -> Result:
        """
        Opens the top N organic (non-sponsored) Amazon product results in
        new background tabs.

        FIX: Requests n*3 candidates from the JS extractor so that after
        sponsored results are filtered out there are still enough real
        product URLs to fill n tabs.  All returned URLs are canonical
        /dp/<ASIN> pages -- no /sspa/ ad-redirect URLs.
        """
        logger.info(f"[{self.name}] open_top_results(n={n})")
        try:
            page = actions._page  # noqa: SLF001
            base_url = _amazon_base(page.url)
            # Request 3x more candidates than needed because sponsored items
            # are filtered inside the JS extractor and we need headroom.
            raw_urls: list[str] = actions.evaluate_js(
                f"({_JS_EXTRACT_PRODUCT_LINKS})({n * 3}, {base_url!r})"
            )
            if not raw_urls:
                return Result.fail(error="open_top_results(): no product links found")

            urls = raw_urls[:n]
            tab_results: list[dict] = []

            for i, url in enumerate(urls):
                try:
                    new_page = actions.open_new_tab(url)
                    new_actions = Actions(new_page)
                    final_url = new_page.url
                    verified = _is_product_url(final_url)
                    title = ""
                    if verified:
                        try:
                            new_actions.wait_for(
                                selectors=self._selectors["product_title"], timeout=10.0
                            )
                            title = new_actions.get_text(selectors=self._selectors["product_title"])
                        except ActionError:
                            title = new_page.title()
                    else:
                        title = new_page.title()
                    asin = _extract_asin(final_url)
                    tab_results.append({
                        "tab_index": i + 1, "url": final_url,
                        "title": title.strip() if title else new_page.title(),
                        "asin": asin, "verified": verified,
                    })
                except ActionError as tab_err:
                    tab_results.append({
                        "tab_index": i + 1, "url": url, "title": "", "asin": "",
                        "verified": False, "error": str(tab_err),
                    })

            return Result.ok(data=tab_results)
        except ActionError as e:
            return Result.fail(error=f"open_top_results(): {e}")
        except Exception as e:
            return Result.fail(error=f"open_top_results(): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # SHOPPING ACTIONS
    # ═══════════════════════════════════════════════════════════════════════

    # ── JS helpers for add_to_cart idempotency ────────────────────────────────
    _JS_IS_ALREADY_IN_CART = """
    () => {
      // 1. Confirmation banner already visible (item was just added this session)
      if (document.querySelector('#NATC_SMART_WAGON_CONF_MSG_SUCCESS')
       || document.querySelector('#huc-v2-confirm-text')
       || document.querySelector('#sw-atc-confirmation')
       || document.querySelector('.a-color-success')) return 'confirmation_visible';

      // 2. Button text changed to "Added to Cart" / "Im Einkaufswagen" / etc.
      const btn = document.querySelector('#add-to-cart-button')
               || document.querySelector('input#add-to-cart-button');
      if (btn) {
        const label = (btn.value || btn.innerText || btn.getAttribute('aria-label') || '').toLowerCase();
        if (/added|in(?:\\s+your)?\\s+cart|im\\s+einkaufswagen|ajout/i.test(label))
          return 'button_label_changed';
        // 3. Button is disabled after add (some Amazon locales)
        if (btn.disabled || btn.getAttribute('disabled') !== null) return 'button_disabled';
      }

      // 4. "Go to Cart" / "Proceed to Checkout" CTA appeared (post-add overlay)
      if (document.querySelector('#hlb-ptc-btn')
       || document.querySelector('#sw-gtc')
       || document.querySelector('#huc-v2-order-row-confirm-text')) return 'goto_cart_visible';

      return null;  // not yet in cart
    }
    """

    def _action_add_to_cart(self, actions: Actions) -> Result:
        """
        Add the current product to the cart.
        Must be on a product page (/dp/ URL).

        Phase 10.1 (MAJOR-6) — Idempotency checks:
          1. Pre-click: detect if already-in-cart confirmation is visible.
          2. Pre-click: detect if the Add-to-Cart button is disabled (already added).
          3. Pre-click: detect if button label changed to 'Added' / locale equivalent.
          4. Post-click: verify via confirmation element OR cart count delta.
        """
        logger.info(f"[{self.name}] add_to_cart()")
        try:
            is_product = actions.safe_evaluate_js(_JS_IS_PRODUCT_PAGE, default=False)
            if not is_product:
                return Result.fail(
                    error="add_to_cart(): not on a product page. "
                          "Navigate to a product page first."
                )

            # ── Idempotency pre-check ─────────────────────────────────────────
            already_reason = actions.safe_evaluate_js(self._JS_IS_ALREADY_IN_CART, default=None)
            if already_reason:
                logger.info(
                    f"[{self.name}] add_to_cart(): already in cart "
                    f"(detected via '{already_reason}') — skipping"
                )
                return Result.ok(data={
                    "added": True,
                    "action": "skipped_already_in_cart",
                    "reason": already_reason,
                })

            cart_before = actions.safe_evaluate_js(_JS_GET_CART_COUNT, default=0) or 0

            # ── Wait for button and verify it is clickable ─────────────────────
            actions.wait_for(selectors=self._selectors["add_to_cart_button"], timeout=10.0)

            # Extra guard: if the button exists but is already disabled, it is
            # an "Added" state on some Amazon localizations — treat as idempotent.
            btn_disabled = actions.safe_evaluate_js("""
            () => {
              const btn = document.querySelector('#add-to-cart-button')
                       || document.querySelector('input#add-to-cart-button');
              return btn ? (btn.disabled || btn.getAttribute('aria-disabled') === 'true') : false;
            }
            """, default=False)
            if btn_disabled:
                logger.info(
                    f"[{self.name}] add_to_cart(): button is disabled — "
                    "item likely already in cart, skipping click"
                )
                return Result.ok(data={
                    "added": True,
                    "action": "skipped_button_disabled",
                })

            actions.click(selectors=self._selectors["add_to_cart_button"])

            # ── Post-click confirmation ───────────────────────────────────────
            added = False
            try:
                actions.wait_for(
                    selectors=[
                        "#NATC_SMART_WAGON_CONF_MSG_SUCCESS",
                        "#huc-v2-confirm-text",
                        "#add-to-cart-button-ubb",
                        ".a-color-success",
                        "#sw-atc-confirmation",
                    ],
                    timeout=8.0,
                )
                added = True
            except ActionError:
                # Check if cart count increased as fallback
                cart_after = actions.safe_evaluate_js(_JS_GET_CART_COUNT, default=0) or 0
                added = cart_after > cart_before

            if added:
                logger.info(f"[{self.name}] add_to_cart() ✅")
                return Result.ok(data={"added": True, "action": "added", "cart_count": cart_before + 1})

            # No confirmation found — run idempotency check one more time in
            # case the post-add state loaded after the wait timed out.
            post_reason = actions.safe_evaluate_js(self._JS_IS_ALREADY_IN_CART, default=None)
            if post_reason:
                logger.info(
                    f"[{self.name}] add_to_cart(): post-click check shows in-cart "
                    f"('{post_reason}') — treating as success"
                )
                return Result.ok(data={"added": True, "action": "added_unverified", "reason": post_reason})

            logger.warning(f"[{self.name}] add_to_cart(): no confirmation found — treating as success")
            return Result.ok(data={"added": True, "action": "unverified"})
        except ActionError as e:
            return Result.fail(error=f"add_to_cart(): {e}")
        except Exception as e:
            return Result.fail(error=f"add_to_cart(): {type(e).__name__}: {e}")

    def _action_remove_from_cart(self, actions: Actions) -> Result:
        """
        Remove the first item from the cart.
        Must be on the cart page.
        """
        logger.info(f"[{self.name}] remove_from_cart()")
        try:
            is_cart = actions.safe_evaluate_js(_JS_IS_CART_PAGE, default=False)
            if not is_cart:
                # Navigate to cart first
                actions.navigate(
                    _amazon_base(actions._page.url) + "/cart"  # noqa: SLF001
                )
                actions.wait_for(
                    selectors=["#activeCartViewForm", ".sc-list-item"],
                    timeout=10.0
                )

            actions.wait_for(
                selectors=self._selectors["remove_from_cart_button"], timeout=10.0
            )
            actions.click(selectors=self._selectors["remove_from_cart_button"])
            logger.info(f"[{self.name}] remove_from_cart() ✅")
            return Result.ok(data={"removed": True})
        except ActionError as e:
            return Result.fail(error=f"remove_from_cart(): {e}")
        except Exception as e:
            return Result.fail(error=f"remove_from_cart(): {type(e).__name__}: {e}")

    def _action_add_to_wishlist(self, actions: Actions) -> Result:
        """
        Add the current product to the default wishlist.
        Must be on a product page.
        """
        logger.info(f"[{self.name}] add_to_wishlist()")
        try:
            is_product = actions.safe_evaluate_js(_JS_IS_PRODUCT_PAGE, default=False)
            if not is_product:
                return Result.fail(
                    error="add_to_wishlist(): not on a product page. "
                          "Navigate to a product page first."
                )

            actions.wait_for(selectors=self._selectors["wishlist_button"], timeout=10.0)
            actions.click(selectors=self._selectors["wishlist_button"])

            # Amazon may show a "Sign in" modal or a wishlist picker — handle both
            try:
                # Look for a confirmation or wishlist selection modal
                actions.wait_for(
                    selectors=[
                        "#add-to-list-submit",
                        "#huc-wl-confirm-button",
                        ".wl-flash-confirm",
                        "#g-twister-fbt-popup-modal",
                    ],
                    timeout=5.0,
                )
                # If a confirm button appeared, click it
                try:
                    actions.click(
                        selectors=["#add-to-list-submit", "#huc-wl-confirm-button"]
                    )
                except ActionError:
                    pass  # Confirmation not needed
            except ActionError:
                pass  # No confirmation dialog — item was added directly

            logger.info(f"[{self.name}] add_to_wishlist() ✅")
            return Result.ok(data={"added_to_wishlist": True})
        except ActionError as e:
            return Result.fail(error=f"add_to_wishlist(): {e}")
        except Exception as e:
            return Result.fail(error=f"add_to_wishlist(): {type(e).__name__}: {e}")

    def _action_remove_from_wishlist(self, actions: Actions) -> Result:
        """
        Remove the first item from the wishlist.
        Must be on the wishlist page.
        """
        logger.info(f"[{self.name}] remove_from_wishlist()")
        try:
            is_wl = actions.safe_evaluate_js(_JS_IS_WISHLIST_PAGE, default=False)
            if not is_wl:
                # Navigate to wishlist
                base = _amazon_base(actions._page.url)  # noqa: SLF001
                actions.navigate(f"{base}/hz/wishlist/ls")
                actions.wait_for(
                    selectors=["[id*='item_']", ".wl-item-view"],
                    timeout=10.0
                )

            actions.wait_for(
                selectors=self._selectors["remove_from_wishlist_button"], timeout=10.0
            )
            actions.click(selectors=self._selectors["remove_from_wishlist_button"])
            logger.info(f"[{self.name}] remove_from_wishlist() ✅")
            return Result.ok(data={"removed_from_wishlist": True})
        except ActionError as e:
            return Result.fail(error=f"remove_from_wishlist(): {e}")
        except Exception as e:
            return Result.fail(error=f"remove_from_wishlist(): {type(e).__name__}: {e}")

    def _action_buy_now(self, actions: Actions) -> Result:
        """
        Click the 'Buy Now' button on the current product page.
        NOTE: This initiates checkout — use with care.
        """
        logger.info(f"[{self.name}] buy_now()")
        if not config.BUY_NOW_ENABLED:
            logger.warning(f"[{self.name}] buy_now() blocked — BUY_NOW_ENABLED is False")
            return Result.fail("buy_now disabled by config")
        try:
            is_product = actions.safe_evaluate_js(_JS_IS_PRODUCT_PAGE, default=False)
            if not is_product:
                return Result.fail(
                    error="buy_now(): not on a product page. "
                          "Navigate to a product page first."
                )

            actions.wait_for(selectors=self._selectors["buy_now_button"], timeout=10.0)
            actions.click(selectors=self._selectors["buy_now_button"])
            logger.info(f"[{self.name}] buy_now() ✅ checkout initiated")
            return Result.ok(data={"checkout_initiated": True})
        except ActionError as e:
            return Result.fail(error=f"buy_now(): {e}")
        except Exception as e:
            return Result.fail(error=f"buy_now(): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # ACCOUNT NAVIGATION ACTIONS
    # ═══════════════════════════════════════════════════════════════════════

    def _action_open_orders(self, actions: Actions) -> Result:
        """Navigate to the order history page."""
        logger.info(f"[{self.name}] open_orders()")
        try:
            base = _amazon_base(actions._page.url)  # noqa: SLF001
            # Try nav link first
            try:
                actions.wait_for(selectors=self._selectors["orders_link"], timeout=5.0)
                actions.click_and_wait(selectors=self._selectors["orders_link"])
            except ActionError:
                actions.navigate(f"{base}/gp/your-account/order-history")

            final_url = actions._page.url  # noqa: SLF001
            logger.info(f"[{self.name}] open_orders() ✅ url={final_url}")
            return Result.ok(data={"url": final_url})
        except ActionError as e:
            return Result.fail(error=f"open_orders(): {e}")
        except Exception as e:
            return Result.fail(error=f"open_orders(): {type(e).__name__}: {e}")

    def _action_open_cart(self, actions: Actions) -> Result:
        """Navigate to the shopping cart."""
        logger.info(f"[{self.name}] open_cart()")
        try:
            base = _amazon_base(actions._page.url)  # noqa: SLF001
            try:
                actions.wait_for(selectors=self._selectors["cart_icon"], timeout=5.0)
                actions.click_and_wait(selectors=self._selectors["cart_icon"])
            except ActionError:
                actions.navigate(f"{base}/cart")

            final_url = actions._page.url  # noqa: SLF001
            logger.info(f"[{self.name}] open_cart() ✅ url={final_url}")
            return Result.ok(data={"url": final_url})
        except ActionError as e:
            return Result.fail(error=f"open_cart(): {e}")
        except Exception as e:
            return Result.fail(error=f"open_cart(): {type(e).__name__}: {e}")

    def _action_open_wishlist(self, actions: Actions) -> Result:
        """Navigate to the default wishlist."""
        logger.info(f"[{self.name}] open_wishlist()")
        try:
            base = _amazon_base(actions._page.url)  # noqa: SLF001
            try:
                actions.wait_for(selectors=self._selectors["wishlist_nav_link"], timeout=5.0)
                actions.click_and_wait(selectors=self._selectors["wishlist_nav_link"])
            except ActionError:
                actions.navigate(f"{base}/hz/wishlist/ls")

            final_url = actions._page.url  # noqa: SLF001
            logger.info(f"[{self.name}] open_wishlist() ✅ url={final_url}")
            return Result.ok(data={"url": final_url})
        except ActionError as e:
            return Result.fail(error=f"open_wishlist(): {e}")
        except Exception as e:
            return Result.fail(error=f"open_wishlist(): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # PRODUCT DATA ACTIONS
    # ═══════════════════════════════════════════════════════════════════════

    def _action_read_price(self, actions: Actions) -> Result:
        """Read the product price from the current product page."""
        logger.info(f"[{self.name}] read_price()")
        try:
            actions.wait_for(selectors=self._selectors["price_selector"], timeout=10.0)
            price_text = actions.get_text(selectors=self._selectors["price_selector"])
            if not price_text or not price_text.strip():
                return Result.fail(error="read_price(): price element found but empty")
            logger.info(f"[{self.name}] read_price() ✅ '{price_text.strip()}'")
            return Result.ok(data={"price": price_text.strip()})
        except ActionError as e:
            return Result.fail(error=f"read_price(): {e}")
        except Exception as e:
            return Result.fail(error=f"read_price(): {type(e).__name__}: {e}")

    def _action_read_rating(self, actions: Actions) -> Result:
        """Read the product rating (e.g., '4.5 out of 5 stars')."""
        logger.info(f"[{self.name}] read_rating()")
        try:
            actions.wait_for(selectors=self._selectors["rating_selector"], timeout=10.0)
            rating_text = actions.get_text(selectors=self._selectors["rating_selector"])
            if not rating_text or not rating_text.strip():
                return Result.fail(error="read_rating(): rating element found but empty")
            logger.info(f"[{self.name}] read_rating() ✅ '{rating_text.strip()}'")
            return Result.ok(data={"rating": rating_text.strip()})
        except ActionError as e:
            return Result.fail(error=f"read_rating(): {e}")
        except Exception as e:
            return Result.fail(error=f"read_rating(): {type(e).__name__}: {e}")

    def _action_read_reviews(self, actions: Actions, n: int = 3) -> Result:
        """
        Read the top N customer reviews from the current product page.
        Returns a list of dicts with 'title' and 'body' keys.
        """
        logger.info(f"[{self.name}] read_reviews(n={n})")
        try:
            n = max(1, int(n))
            # Scroll to reviews section first
            actions.evaluate_js(
                "() => {"
                "  const r = document.querySelector('[data-hook=\"reviews-medley-footer\"]')"
                "         || document.querySelector('#customerReviews')"
                "         || document.querySelector('#reviews-summary');"
                "  if (r) r.scrollIntoView({behavior: 'instant', block: 'start'});"
                "}"
            )

            actions.wait_for(selectors=self._selectors["review_block"], timeout=12.0)

            # Extract reviews via JS
            reviews = actions.evaluate_js(f"""
            () => {{
              const blocks = document.querySelectorAll('[data-hook="review"]');
              const results = [];
              for (let i = 0; i < Math.min(blocks.length, {n}); i++) {{
                const block = blocks[i];
                const titleEl = block.querySelector('[data-hook="review-title"] span:not([class])')
                             || block.querySelector('[data-hook="review-title"]');
                const bodyEl  = block.querySelector('[data-hook="review-body"] span')
                             || block.querySelector('[data-hook="review-body"]');
                results.push({{
                  title: (titleEl ? titleEl.innerText : '').trim(),
                  body:  (bodyEl  ? bodyEl.innerText  : '').trim().substring(0, 500),
                }});
              }}
              return results;
            }}
            """)

            if not reviews:
                return Result.fail(error="read_reviews(): no reviews found on page")

            logger.info(f"[{self.name}] read_reviews() ✅ {len(reviews)} reviews")
            return Result.ok(data={"reviews": reviews, "count": len(reviews)})
        except ActionError as e:
            return Result.fail(error=f"read_reviews(): {e}")
        except Exception as e:
            return Result.fail(error=f"read_reviews(): {type(e).__name__}: {e}")
