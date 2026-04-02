"""
skills/makerworld_skill.py — MakerWorld Skill (Phase E)

Adapts MakerWorldController (reference: browser_automation v14) to the
browser_control Skills architecture.

Design:
  - MakerWorld is a React/MUI SPA: most interactions are via evaluate_js().
  - Selector JSON (skills/selectors/makerworld.json) is used only for
    wait_for() page-load detection; all interactive clicks are JS-based.
  - All JS is adapted from MakerWorldController v14 (browser_automation).
  - Like detection uses: aria-pressed -> aria-label -> CSS color -> SVG fill
    -> MUI class names (multi-strategy, locale-independent).
  - Collection dialog uses MuiFormControlLabel rows, PrivateSwitchBase-input.
  - Model card extraction uses a[href*="/models/"] anchor walking.

Actions:
  Navigation:     navigate_to, search
  Data extract:   get_model_info, get_search_results, get_model_performance
  Engagement:     like, unlike, toggle_like
  Collections:    collect, uncollect, get_collections
  Download:       download (format: "3mf" | "stl" | "bambu"),
                  download_3mf, download_stl
  Profile:        get_my_uploads, get_my_likes
  Analysis:       get_popular_searches, compare_models

Selectors: skills/selectors/makerworld.json
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

import config
from core.actions import Actions, ActionError
from skills.base_skill import BaseSkill, Result

logger = logging.getLogger(__name__)

_BASE = "https://makerworld.com"

# ── Shared JS fragments (adapted from MakerWorldController v14) ─────────────

_JS_GET_LOCALE = r"""
() => {
    const m = window.location.pathname.match(/\/([a-z]{2})\//);
    return m ? m[1] : "en";
}
"""

_JS_IS_LIKED = r"""
() => {
    const candidates = [
        document.querySelector('[class*="like-icon-box"]'),
        document.querySelector('button[aria-label*="like" i]'),
        document.querySelector('[data-testid*="like"]'),
    ];
    const el = candidates.find(Boolean);
    if (!el) return null;
    const btn = el.closest('button') || el;

    // Strategy 1: aria-pressed (most reliable when present)
    const pressed = btn.getAttribute('aria-pressed') ?? el.getAttribute('aria-pressed');
    if (pressed === 'true')  return true;
    if (pressed === 'false') return false;

    // Strategy 2: aria-label text
    const label = (btn.getAttribute('aria-label') || el.getAttribute('aria-label') || '').toLowerCase();
    if (label.includes('unlike') || label.includes('entfernen')) return true;
    if (label.includes('add like')) return false;

    // Strategy 3: CSS color (MUI sets color on button, not SVG fill)
    for (const target of [btn, el, ...el.querySelectorAll('*')]) {
        const c = window.getComputedStyle(target).color;
        const m = c && c.match(/rgb[a]?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/);
        if (!m) continue;
        const r = +m[1], g = +m[2], b = +m[3];
        if (r > 180 && r > g + 60 && r > b + 60) return true;
        if (Math.max(r,g,b) - Math.min(r,g,b) < 40 && r > 60) return false;
    }

    // Strategy 4: SVG fill
    for (const p of el.querySelectorAll('svg path, svg circle')) {
        const fill = p.getAttribute('fill') || window.getComputedStyle(p).fill || '';
        if (!fill || fill === 'none' || fill === 'currentColor') continue;
        const m2 = fill.match(/rgb[a]?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/);
        if (m2) {
            const r = +m2[1], g = +m2[2], b = +m2[3];
            if (r > 180 && r > g + 60 && r > b + 60) return true;
            if (Math.max(r,g,b) - Math.min(r,g,b) < 35) return false;
        }
    }

    // Strategy 5: MUI class names
    const allCls = [el, ...el.querySelectorAll('*')]
        .map(n => (n.className || '').toString()).join(' ');
    if (/\bgrey(500|600|700|800)\b/.test(allCls)) return false;
    if (/\b(primary|liked|active)\b/.test(allCls)) return true;

    return null;
}
"""

_JS_CLICK_LIKE = r"""
() => {
    const candidates = [
        document.querySelector('[class*="like-icon-box"]'),
        document.querySelector('button[aria-label*="like" i]'),
    ];
    const el = candidates.find(e => e && e.getBoundingClientRect().width > 0);
    if (!el) return null;
    // Simulate hover first (some React components need it)
    el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true, cancelable: true}));
    el.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true, cancelable: true}));
    el.click();
    return true;
}
"""

_JS_IS_COLLECTED = r"""
() => {
    const el = document.querySelector('[class*="collection-entry"]');
    if (!el) return null;
    const num = parseInt((el.innerText || '').trim(), 10);
    return isNaN(num) ? null : num > 0;
}
"""

_JS_CLICK_COLLECT = r"""
() => {
    for (const el of document.querySelectorAll('*')) {
        if (!(el.className || '').toString().includes('collection-entry')) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 1 || rect.height < 1) continue;
        el.click();
        return true;
    }
    return null;
}
"""

_JS_GET_COLLECTIONS = r"""
() => {
    function findDialog() {
        for (const el of document.querySelectorAll('[class*="MuiDialog-root"]')) {
            if (!el.className.toString().includes('MuiModal-hidden')) return el;
        }
        return null;
    }
    const BADGE_EXACT = new Set([
        'privat','private','public','exklusiv','exclusive',
        'fertig','done','ok','cancel','abbrechen','schliessen',
        'neue kollektion','new collection','add new collection',
    ]);
    const BADGE_SUFFIX = ['modelle','models','items','prints'];
    function isValid(t) {
        t = (t || '').trim();
        if (t.length < 2 || t.length > 80) return false;
        if (/^\d+[KkMm]?$/.test(t)) return false;
        if (BADGE_EXACT.has(t.toLowerCase())) return false;
        if (BADGE_SUFFIX.some(s => t.toLowerCase().endsWith(' ' + s))) return false;
        return true;
    }
    const dialog = findDialog();
    if (!dialog) return '[]';
    const names = [];
    // Primary: MuiFormControlLabel rows
    for (const el of dialog.querySelectorAll('label,[class*="MuiFormControlLabel"]')) {
        const line = (el.innerText || '').trim().split('\n')[0].trim();
        if (isValid(line)) names.push(line);
    }
    // Fallback A: MuiFormGroup direct children
    if (names.length === 0) {
        for (const el of dialog.querySelectorAll('[class*="MuiFormGroup"] > *')) {
            const line = (el.innerText || '').trim().split('\n')[0].trim();
            if (isValid(line)) names.push(line);
        }
    }
    // Fallback B: <li>
    if (names.length === 0) {
        for (const el of dialog.querySelectorAll('li')) {
            const line = (el.innerText || '').trim().split('\n')[0].trim();
            if (isValid(line)) names.push(line);
        }
    }
    return JSON.stringify([...new Set(names)].slice(0, 30));
}
"""

_JS_CONFIRM_DIALOG = r"""
() => {
    function findDialog() {
        for (const el of document.querySelectorAll('[class*="MuiDialog-root"]')) {
            if (!el.className.toString().includes('MuiModal-hidden')) return el;
        }
        return null;
    }
    const dialog = findDialog();
    if (!dialog) return null;
    const kws = ['fertig','done','ok','confirm','speichern','save','apply','bestaetigen'];
    const btns = Array.from(dialog.querySelectorAll('button'))
        .filter(b => b.getBoundingClientRect().width > 10);
    for (const btn of btns) {
        const text = (btn.innerText || '').toLowerCase().trim();
        if (kws.some(k => text === k || text.includes(k))) { btn.click(); return text; }
    }
    // Fallback: bottom-right button (most MUI dialogs place confirm bottom-right)
    if (btns.length > 0) {
        btns.sort((a, b) => {
            const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
            return (rb.x + rb.y) - (ra.x + ra.y);
        });
        btns[0].click(); return 'bottom-right-fallback';
    }
    return null;
}
"""

_JS_CLOSE_DIALOG = r"""
() => {
    function findDialog() {
        for (const el of document.querySelectorAll('[class*="MuiDialog-root"]')) {
            if (!el.className.toString().includes('MuiModal-hidden')) return el;
        }
        return null;
    }
    const dialog = findDialog();
    if (dialog) {
        for (const btn of dialog.querySelectorAll('button')) {
            const t = (btn.innerText || '').toLowerCase().trim();
            const c = btn.className.toString();
            if (['abbrechen','cancel','schliessen','close'].includes(t) ||
                c.includes('close') || c.includes('Close')) { btn.click(); return; }
        }
    }
    document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape',bubbles:true}));
}
"""

_JS_GET_MODEL_INFO = r"""
() => {
    const info = {};
    info.url   = window.location.href;
    info.title = ((document.querySelector('h1') || {}).innerText || '').trim();
    const aLinks = document.querySelectorAll('a[href*="/@"],a[href*="/user/"],a[href*="/u/"]');
    info.author = aLinks.length ? (aLinks[0].innerText || '').trim().split('\n')[0] : '';
    let longest = '';
    for (const p of document.querySelectorAll('p')) {
        const t = (p.innerText || '').trim();
        if (t.length > longest.length && t.length < 1000) longest = t;
    }
    info.description = longest.slice(0, 300);
    const tags = Array.from(document.querySelectorAll('a[href*="tag="],a[href*="/tag/"]'))
        .map(el => (el.innerText || '').trim()).filter(Boolean).slice(0, 15);
    info.tags = tags.join(', ');
    return JSON.stringify(info);
}
"""

_JS_EXTRACT_MODEL_CARDS = r"""
(maxResults) => {
    const SKIP = new Set(['like','unlike','download','collect','save','print','open',
        'share','report','edit','delete','gif','mp4','jpg','jpeg','png','webp','svg']);
    function isSkip(t) {
        t = (t || '').toLowerCase().trim();
        return t.length <= 3 || /^\d+[KkMm]?$/.test(t) || SKIP.has(t);
    }
    function extractTitle(card) {
        for (const sel of ['h1','h2','h3','h4','h5',
            '[class*="title"]','[class*="Title"]',
            '[class*="name"]','[class*="Name"]','strong']) {
            for (const el of card.querySelectorAll(sel)) {
                if (el.closest('a[href*="/@"]') || el.closest('a[href*="/u/"]')) continue;
                if (el.childElementCount > 2) continue;
                const t = (el.innerText || '').trim().split('\n')[0].trim();
                if (!isSkip(t) && t.length <= 160) return t;
            }
        }
        for (const el of card.querySelectorAll('p,span,div')) {
            if (el.closest('a[href*="/@"]') || el.closest('a[href*="/u/"]')) continue;
            if (el.childElementCount > 3) continue;
            const t = (el.innerText || '').trim().split('\n')[0].trim();
            if (!isSkip(t) && t.length >= 5 && t.length <= 160) return t;
        }
        return '';
    }
    const seen = new Set();
    const results = [];
    for (const link of document.querySelectorAll('a[href*="/models/"]')) {
        const href = link.href || '';
        const slug = href.split('/models/')[1] || '';
        if (!/\d/.test(slug) || seen.has(href)) continue;
        const card = link.closest('article') || link.closest('li') ||
            link.closest('[class*="card"]') || link.closest('[class*="Card"]') ||
            link.closest('[class*="item"]') || link.closest('[class*="Item"]') ||
            (link.parentElement && link.parentElement.parentElement);
        const title = card ? extractTitle(card)
            : (link.innerText || '').trim().split('\n')[0].trim();
        if (!title || isSkip(title)) continue;
        seen.add(href);
        const authorLink = card && (
            card.querySelector('a[href*="/@"]') ||
            card.querySelector('a[href*="/user/"]'));
        const author = authorLink
            ? (authorLink.innerText || '').trim().split('\n')[0] : '';
        results.push({title, url: href, author, likes:'', downloads:'', views:''});
        if (results.length >= maxResults) break;
    }
    return results;
}
"""

_JS_OPEN_DOWNLOAD_MENU = r"""
() => {
    // Strategy 1: icon-box element in the expected Y range
    for (const el of document.querySelectorAll('*')) {
        const cls = (el.className || '').toString();
        if (!cls.includes('icon-box')) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 1 || rect.height < 1) continue;
        if (rect.y < 250 || rect.y > 480) continue;
        el.click();
        return 'icon-box';
    }
    // Strategy 2: smallest button in the action bar area
    const btns = Array.from(document.querySelectorAll('button'))
        .filter(b => {
            const r = b.getBoundingClientRect();
            return r.y > 250 && r.y < 480 && r.x > 800 && r.width > 0;
        });
    if (btns.length >= 2) {
        btns.sort((a, b) =>
            a.getBoundingClientRect().width - b.getBoundingClientRect().width);
        btns[0].click();
        return 'smallest-button';
    }
    return null;
}
"""


class MakerWorldSkill(BaseSkill):
    """
    Skill for MakerWorld (makerworld.com) — Phase E.

    All interactive operations use evaluate_js() / safe_evaluate_js() because
    MakerWorld is a React/MUI SPA that requires synthetic events. Selector JSON
    (skills/selectors/makerworld.json) is used only for wait_for() page-load
    guards — never for clicking.

    Like/unlike detection uses 5 strategies in order:
        1. aria-pressed attribute (canonical, when present)
        2. aria-label text (locale-independent subset)
        3. CSS color on button element (MUI primary color = liked)
        4. SVG fill color (fallback for icon-only buttons)
        5. MUI class names (grey = unliked, primary/liked/active = liked)

    Collection dialog detection uses:
        - MuiFormControlLabel rows (primary, confirmed from DOM)
        - PrivateSwitchBase-input checkbox state
        - Mui-checked class name on checkbox span
    """

    name: str = "MakerWorld"
    base_url: str = "makerworld.com"

    def __init__(self) -> None:
        self._selectors = self._load_selectors("makerworld")
        logger.info("[%s] Skill initialized.", self.name)

    def can_handle(self, url: str) -> bool:
        return "makerworld.com" in url

    def get_action(self, name: str) -> Callable | None:
        _map: dict[str, Callable] = {
            # Navigation
            "navigate_to":           self._action_navigate_to,
            "search":                self._action_search,
            # Data extraction
            "get_search_results":    self._action_get_search_results,
            "get_model_info":        self._action_get_model_info,
            "get_model_performance": self._action_get_model_performance,
            # Engagement (idempotent)
            "like":                  self._action_like,
            "unlike":                self._action_unlike,
            "toggle_like":           self._action_toggle_like,
            # Collections
            "collect":               self._action_collect,
            "uncollect":             self._action_uncollect,
            "get_collections":       self._action_get_collections,
            # Download
            "download":              self._action_download,
            "download_3mf":          self._action_download_3mf,
            "download_stl":          self._action_download_stl,
            # Profile
            "get_my_uploads":        self._action_get_my_uploads,
            "get_my_likes":          self._action_get_my_likes,
            # Analysis
            "get_popular_searches":  self._action_get_popular_searches,
            "compare_models":        self._action_compare_models,
            # ── mw_* Aliases (Planner-facing names) ────────────────────────────
            "mw_search":             self._action_search,
            "mw_open_top":           self._action_get_search_results,
            "mw_get_info":           self._action_get_model_info,
            "mw_get_results":        self._action_get_search_results,
            "mw_like":               self._action_like,
            "mw_unlike":             self._action_unlike,
            "mw_toggle_like":        self._action_toggle_like,
            "mw_collect":            self._action_collect,
            "mw_uncollect":          self._action_uncollect,
            "mw_download":           self._action_download,
            "mw_download_3mf":       self._action_download_3mf,
            "mw_download_stl":       self._action_download_stl,
            "mw_navigate_to_model":  self._action_navigate_to_model,
        }
        action = _map.get(name)
        if action is None:
            logger.warning("[%s] Unknown action: '%s'", self.name, name)
        return action

    # ═══════════════════════════════════════════════════════════════════
    # NAVIGATION
    # ═══════════════════════════════════════════════════════════════════

    def _action_navigate_to(self, actions: Actions, url: str = "") -> Result:
        """Navigate to a MakerWorld URL and wait for main content."""
        if not url:
            url = _BASE
        logger.info("[%s] navigate_to('%s')", self.name, url)
        try:
            actions.navigate(url)
            actions.wait_for(
                selectors=self._selectors["model_page_root"], timeout=15.0
            )
            return Result.ok(data={"url": url})
        except ActionError as e:
            return Result.fail(error=f"navigate_to('{url}'): {e}")
        except Exception as e:
            return Result.fail(error=f"navigate_to(): {type(e).__name__}: {e}")

    def _action_search(self, actions: Actions, query: str = "") -> Result:
        """Navigate to MakerWorld search results for the given query."""
        logger.info("[%s] search('%s')", self.name, query)
        try:
            locale = actions.safe_evaluate_js(_JS_GET_LOCALE, default="en") or "en"
            url = f"{_BASE}/{locale}/3d-models?keyword={query.replace(' ', '+')}"
            actions.navigate(url)
            actions.wait_for(
                selectors=self._selectors["search_result_root"], timeout=15.0
            )
            return Result.ok(data={"query": query, "url": url})
        except ActionError as e:
            return Result.fail(error=f"search('{query}'): {e}")
        except Exception as e:
            return Result.fail(error=f"search(): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # DATA EXTRACTION
    # ═══════════════════════════════════════════════════════════════════

    def _action_get_search_results(self, actions: Actions, n: int = 10) -> Result:
        """Extract up to n model cards from the current search results page."""
        logger.info("[%s] get_search_results(n=%d)", self.name, n)
        try:
            # Scroll to trigger lazy-loaded cards, then scroll back
            for _ in range(3):
                actions.scroll("down", 400)
                time.sleep(0.4)
            actions.scroll("up", 9999)
            results = actions.safe_evaluate_js(
                f"({_JS_EXTRACT_MODEL_CARDS})({n})", default=[]
            ) or []
            logger.info("[%s] get_search_results -> %d cards", self.name, len(results))
            return Result.ok(data={"results": results, "count": len(results)})
        except ActionError as e:
            return Result.fail(error=f"get_search_results(): {e}")
        except Exception as e:
            return Result.fail(error=f"get_search_results(): {type(e).__name__}: {e}")

    def _action_get_model_info(self, actions: Actions) -> Result:
        """Extract metadata from the currently open model page."""
        logger.info("[%s] get_model_info()", self.name)
        try:
            raw = actions.safe_evaluate_js(_JS_GET_MODEL_INFO, default="{}")
            info = json.loads(raw or "{}")
            # Augment with current engagement state
            info["is_liked"] = str(
                actions.safe_evaluate_js(_JS_IS_LIKED, default=None)
            )
            info["is_collected"] = str(
                actions.safe_evaluate_js(_JS_IS_COLLECTED, default=None)
            )
            return Result.ok(data=info)
        except Exception as e:
            return Result.fail(error=f"get_model_info(): {type(e).__name__}: {e}")

    def _action_get_model_performance(self, actions: Actions) -> Result:
        """Navigate to data-overview/model and scrape performance stats table."""
        logger.info("[%s] get_model_performance()", self.name)
        try:
            locale = (
                actions.safe_evaluate_js(_JS_GET_LOCALE, default="en") or "en"
            )
            url = f"{_BASE}/{locale}/my/data-overview/model"
            actions.navigate(url)
            actions.wait_for(selectors=["main", "tbody", "table"], timeout=15.0)
            for _ in range(4):
                actions.scroll("down", 400)
                time.sleep(0.5)
            _JS_PERF = r"""
            () => {
                const result = {headers: [], rows: []};
                const thead = document.querySelector('thead');
                if (thead)
                    result.headers = Array.from(thead.querySelectorAll('th,td'))
                        .map(h => (h.innerText||'').trim().toLowerCase()).filter(Boolean);
                for (const row of (document.querySelector('tbody')||document).querySelectorAll('tr')) {
                    if (row.closest('thead')) continue;
                    const cells = Array.from(row.querySelectorAll('td,th'))
                        .map(c => (c.innerText||'').trim());
                    if (cells.length < 2 || !cells.some(c => /\d/.test(c))) continue;
                    const link = row.querySelector('a[href*="/models/"]');
                    result.rows.push({
                        title: link ? (link.innerText||'').trim() : cells[0],
                        url:   link ? link.href : '',
                        cells,
                    });
                    if (result.rows.length >= 50) break;
                }
                return JSON.stringify(result);
            }
            """
            raw = actions.safe_evaluate_js(_JS_PERF, default="{}")
            parsed = json.loads(raw or "{}")
            return Result.ok(data=parsed)
        except ActionError as e:
            return Result.fail(error=f"get_model_performance(): {e}")
        except Exception as e:
            return Result.fail(
                error=f"get_model_performance(): {type(e).__name__}: {e}"
            )

    # ═══════════════════════════════════════════════════════════════════
    # LIKE / UNLIKE (idempotent — adapted from MakerWorldController v14)
    # ═══════════════════════════════════════════════════════════════════

    def _action_like(self, actions: Actions) -> Result:
        """Like the current model. Idempotent — skips if already liked."""
        logger.info("[%s] like()", self.name)
        try:
            current = actions.safe_evaluate_js(_JS_IS_LIKED, default=None)
            if current is True:
                logger.info("[%s] already liked — skipping", self.name)
                return Result.ok(
                    data={"liked": True, "action": "skipped_already_liked"}
                )
            clicked = actions.safe_evaluate_js(_JS_CLICK_LIKE, default=None)
            if not clicked:
                return Result.fail(error="like(): like button not found on page")
            new_state = self._poll_like_state(actions, current, timeout=6.0)
            logger.info("[%s] like() -> state=%s", self.name, new_state)
            return Result.ok(data={"liked": new_state, "action": "liked"})
        except Exception as e:
            return Result.fail(error=f"like(): {type(e).__name__}: {e}")

    def _action_unlike(self, actions: Actions) -> Result:
        """Remove like from current model. Idempotent — skips if not liked."""
        logger.info("[%s] unlike()", self.name)
        try:
            current = actions.safe_evaluate_js(_JS_IS_LIKED, default=None)
            if current is False:
                logger.info("[%s] not liked — skipping", self.name)
                return Result.ok(
                    data={"liked": False, "action": "skipped_not_liked"}
                )
            clicked = actions.safe_evaluate_js(_JS_CLICK_LIKE, default=None)
            if not clicked:
                return Result.fail(error="unlike(): like button not found on page")
            new_state = self._poll_like_state(actions, current, timeout=6.0)
            logger.info("[%s] unlike() -> state=%s", self.name, new_state)
            return Result.ok(data={"liked": new_state, "action": "unliked"})
        except Exception as e:
            return Result.fail(error=f"unlike(): {type(e).__name__}: {e}")

    def _action_toggle_like(self, actions: Actions) -> Result:
        """Toggle the like state on the current model."""
        logger.info("[%s] toggle_like()", self.name)
        try:
            before = actions.safe_evaluate_js(_JS_IS_LIKED, default=None)
            clicked = actions.safe_evaluate_js(_JS_CLICK_LIKE, default=None)
            if not clicked:
                return Result.fail(error="toggle_like(): like button not found")
            new_state = self._poll_like_state(actions, before, timeout=6.0)
            logger.info("[%s] toggle_like() %s -> %s", self.name, before, new_state)
            return Result.ok(data={"liked": new_state, "was": before})
        except Exception as e:
            return Result.fail(error=f"toggle_like(): {type(e).__name__}: {e}")

    def _poll_like_state(
        self, actions: Actions, before: Any, timeout: float = 6.0
    ) -> Any:
        """
        Poll is_liked() until it changes from `before`, with timeout.
        Returns final state (may be `before` if no change observed).
        """
        interval = 0.4
        steps = int(timeout / interval)
        for _ in range(steps):
            time.sleep(interval)
            state = actions.safe_evaluate_js(_JS_IS_LIKED, default=None)
            if state is not None and state != before:
                return state
        return actions.safe_evaluate_js(_JS_IS_LIKED, default=before)

    # ═══════════════════════════════════════════════════════════════════
    # COLLECTIONS (adapted from MakerWorldController v14)
    # ═══════════════════════════════════════════════════════════════════

    def _action_get_collections(self, actions: Actions) -> Result:
        """Open collection dialog and return list of collection names."""
        logger.info("[%s] get_collections()", self.name)
        try:
            actions.safe_evaluate_js(
                "() => document.body.click()", default=None
            )
            time.sleep(0.2)
            clicked = actions.safe_evaluate_js(_JS_CLICK_COLLECT, default=None)
            if not clicked:
                return Result.fail(
                    error="get_collections(): collection button not found"
                )
            time.sleep(1.5)
            raw = actions.safe_evaluate_js(_JS_GET_COLLECTIONS, default="[]")
            names = json.loads(raw or "[]")
            # Always close the dialog
            actions.safe_evaluate_js(_JS_CLOSE_DIALOG, default=None)
            time.sleep(0.4)
            return Result.ok(data={"collections": names, "count": len(names)})
        except Exception as e:
            return Result.fail(
                error=f"get_collections(): {type(e).__name__}: {e}"
            )

    def _action_collect(
        self, actions: Actions, collection_name: str = ""
    ) -> Result:
        """
        Add current model to a collection.
        Opens the collection dialog; selects named collection if provided.
        Idempotent: if already in named collection, skips click and confirms.
        """
        logger.info("[%s] collect(collection='%s')", self.name, collection_name)
        try:
            # Idempotency pre-check: if already collected and no specific collection
            # is requested, skip the dialog entirely (aria-pressed / class-based check
            # via _JS_IS_COLLECTED which inspects the collection-entry element).
            if not collection_name:
                already = actions.safe_evaluate_js(_JS_IS_COLLECTED, default=None)
                if already is True:
                    logger.info("[%s] already collected — skipping", self.name)
                    return Result.ok(
                        data={"collected": True, "action": "skipped_already_collected"}
                    )
            actions.safe_evaluate_js(
                "() => document.body.click()", default=None
            )
            time.sleep(0.2)
            clicked = actions.safe_evaluate_js(_JS_CLICK_COLLECT, default=None)
            if not clicked:
                return Result.fail(
                    error="collect(): collection button not found"
                )
            time.sleep(1.5)

            # Check if dialog appeared at all
            _JS_HAS_DIALOG = """
            () => {
                for (const el of document.querySelectorAll('[class*="MuiDialog-root"]')) {
                    if (!el.className.toString().includes('MuiModal-hidden')) return true;
                }
                return false;
            }
            """
            dialog_present = actions.safe_evaluate_js(
                _JS_HAS_DIALOG, default=False
            )
            if not dialog_present:
                # No dialog — item was added directly (single-collection case)
                return Result.ok(
                    data={"collected": True, "action": "collected_no_dialog"}
                )

            if collection_name:
                target = collection_name.lower()
                _JS_SELECT_COLL = f"""
                () => {{
                    function findDialog() {{
                        for (const el of document.querySelectorAll('[class*="MuiDialog-root"]')) {{
                            if (!el.className.toString().includes('MuiModal-hidden')) return el;
                        }}
                        return null;
                    }}
                    const dialog = findDialog();
                    if (!dialog) return null;
                    const cands = [
                        ...dialog.querySelectorAll('label,[class*="MuiFormControlLabel"]'),
                        ...dialog.querySelectorAll('li'),
                    ];
                    for (const row of cands) {{
                        if (!(row.innerText||'').toLowerCase().includes({json.dumps(target)})) continue;
                        const inp = row.querySelector(
                            'input[type="checkbox"],input.PrivateSwitchBase-input');
                        const alreadyChecked = (
                            (inp && inp.checked) ||
                            !!row.querySelector('.Mui-checked,[class*="Mui-checked"]')
                        );
                        if (alreadyChecked) {{
                            return 'already:' +
                                (row.innerText||'').trim().split('\\n')[0].slice(0,60);
                        }}
                        row.click();
                        return 'added:' +
                            (row.innerText||'').trim().split('\\n')[0].slice(0,60);
                    }}
                    return null;
                }}
                """
                res = actions.safe_evaluate_js(_JS_SELECT_COLL, default=None)
                if not res:
                    actions.safe_evaluate_js(_JS_CLOSE_DIALOG, default=None)
                    return Result.fail(
                        error=f"collect(): collection '{collection_name}' not found"
                    )
                logger.info("[%s] collect: %r", self.name, res)
                time.sleep(0.4)

            confirmed = actions.safe_evaluate_js(_JS_CONFIRM_DIALOG, default=None)
            time.sleep(0.5)
            return Result.ok(
                data={
                    "collected": True,
                    "action": "collected",
                    "collection": collection_name or "default",
                    "confirmed": str(confirmed),
                }
            )
        except Exception as e:
            return Result.fail(error=f"collect(): {type(e).__name__}: {e}")

    def _action_uncollect(
        self, actions: Actions, collection_name: str = ""
    ) -> Result:
        """
        Remove current model from a collection.
        Finds checked row in dialog (by name if provided, else first checked row).
        """
        logger.info("[%s] uncollect(collection='%s')", self.name, collection_name)
        try:
            actions.safe_evaluate_js(
                "() => document.body.click()", default=None
            )
            time.sleep(0.2)
            clicked = actions.safe_evaluate_js(_JS_CLICK_COLLECT, default=None)
            if not clicked:
                return Result.fail(
                    error="uncollect(): collection button not found"
                )
            time.sleep(1.5)

            target = (collection_name or "").lower()
            _JS_UNCHECK = f"""
            () => {{
                function findDialog() {{
                    for (const el of document.querySelectorAll('[class*="MuiDialog-root"]')) {{
                        if (!el.className.toString().includes('MuiModal-hidden')) return el;
                    }}
                    return null;
                }}
                const dialog = findDialog();
                if (!dialog) return null;
                function isChecked(row) {{
                    const inp = row.querySelector(
                        'input[type="checkbox"],input.PrivateSwitchBase-input');
                    if (inp && inp.checked) return true;
                    const cb = row.querySelector('[class*="MuiCheckbox"]');
                    if (cb && (cb.className||'').includes('Mui-checked')) return true;
                    if (row.querySelector('.Mui-checked,[class*="Mui-checked"]')) return true;
                    return false;
                }}
                for (const sel of ['label,[class*="MuiFormControlLabel"]','li']) {{
                    for (const row of dialog.querySelectorAll(sel)) {{
                        const text = (row.innerText||'').toLowerCase();
                        if ({json.dumps(target)} && !text.includes({json.dumps(target)})) continue;
                        if (isChecked(row)) {{
                            row.click();
                            return (row.innerText||'').trim().split('\\n')[0].slice(0,60);
                        }}
                    }}
                }}
                return null;
            }}
            """
            toggled = actions.safe_evaluate_js(_JS_UNCHECK, default=None)
            if not toggled:
                # Tier 2: if model is still collected, try clicking row directly
                is_coll = actions.safe_evaluate_js(_JS_IS_COLLECTED, default=None)
                if is_coll:
                    _JS_CLICK_ROW = f"""
                    () => {{
                        function findDialog() {{
                            for (const el of document.querySelectorAll('[class*="MuiDialog-root"]')) {{
                                if (!el.className.toString().includes('MuiModal-hidden')) return el;
                            }}
                            return null;
                        }}
                        const dialog = findDialog();
                        if (!dialog) return null;
                        const cands = [
                            ...dialog.querySelectorAll('label,[class*="MuiFormControlLabel"]'),
                            ...dialog.querySelectorAll('li'),
                        ];
                        for (const row of cands) {{
                            const text = (row.innerText||'').toLowerCase();
                            if (!{json.dumps(target)} || text.includes({json.dumps(target)})) {{
                                row.click();
                                return (row.innerText||'').trim().split('\\n')[0].slice(0,60);
                            }}
                        }}
                        return null;
                    }}
                    """
                    toggled = actions.safe_evaluate_js(_JS_CLICK_ROW, default=None)

            if not toggled:
                actions.safe_evaluate_js(_JS_CLOSE_DIALOG, default=None)
                return Result.fail(
                    error=f"uncollect(): no checked row found for '{collection_name}'"
                )

            logger.info("[%s] uncollect: unchecked '%s'", self.name, toggled)
            time.sleep(0.4)
            confirmed = actions.safe_evaluate_js(_JS_CONFIRM_DIALOG, default=None)
            time.sleep(0.5)
            return Result.ok(
                data={
                    "uncollected": True,
                    "collection": toggled,
                    "confirmed": str(confirmed),
                }
            )
        except Exception as e:
            return Result.fail(error=f"uncollect(): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════════════════
    # DOWNLOAD (adapted from MakerWorldController v14)
    # ═══════════════════════════════════════════════════════════════════

    def _action_download(
        self, actions: Actions, format: str = "3mf"
    ) -> Result:
        """
        Download the current model in the given format.

        Args:
            format: "3mf" (default) | "stl" | "bambu"
        """
        logger.info("[%s] download(format='%s')", self.name, format)
        try:
            opened = actions.safe_evaluate_js(
                _JS_OPEN_DOWNLOAD_MENU, default=None
            )
            if not opened:
                return Result.fail(
                    error="download(): could not open download dropdown"
                )
            time.sleep(0.8)

            kw_map: dict[str, list[str]] = {
                "3mf":   ["3mf herunterladen", "download 3mf", "3mf"],
                "stl":   ["stl/cad herunterladen", "stl herunterladen",
                          "download stl", "stl"],
                "bambu": ["im bambu studio oeffnen", "open in bambu studio",
                          "bambu studio", "bambu"],
            }
            keywords = kw_map.get(format.lower(), [format.lower()])

            _JS_CLICK_FORMAT = f"""
            () => {{
                const kws = {json.dumps(keywords)};
                const scope =
                    document.querySelector('[class*="MuiPopper-root"]') ||
                    document.querySelector('[class*="MuiMenu-root"]')   ||
                    document.querySelector('[class*="MuiPopover-root"]') ||
                    document;
                for (const el of scope.querySelectorAll(
                    'li,[role="menuitem"],div,span,a')) {{
                    const text = (el.innerText || '').toLowerCase().trim();
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 5 || rect.height < 5) continue;
                    if (kws.some(k => text === k || text.includes(k))) {{
                        el.click();
                        return text.slice(0, 60);
                    }}
                }}
                return null;
            }}
            """
            clicked_item = actions.safe_evaluate_js(
                _JS_CLICK_FORMAT, default=None
            )
            if not clicked_item:
                return Result.fail(
                    error=f"download(): format '{format}' not found in dropdown"
                )
            time.sleep(1.5)
            logger.info(
                "[%s] download('%s') via '%s' OK",
                self.name, format, clicked_item,
            )
            return Result.ok(data={"format": format, "menu_item": clicked_item})
        except Exception as e:
            return Result.fail(error=f"download(): {type(e).__name__}: {e}")

    def _action_download_3mf(self, actions: Actions) -> Result:
        return self._action_download(actions, format="3mf")

    def _action_download_stl(self, actions: Actions) -> Result:
        return self._action_download(actions, format="stl")

    def _action_navigate_to_model(self, actions: Actions, url: str = "") -> Result:
        """Thin wrapper: navigate to a model URL (alias for navigate_to)."""
        return self._action_navigate_to(actions, url=url)

    # ═══════════════════════════════════════════════════════════════════
    # PROFILE (adapted from MakerWorldController v14)
    # ═══════════════════════════════════════════════════════════════════

    def _action_get_my_uploads(
        self, actions: Actions, username: str = ""
    ) -> Result:
        """Navigate to user's uploads page and scrape model cards."""
        logger.info("[%s] get_my_uploads(username='%s')", self.name, username)
        if not username:
            # Auto-detect from current URL (handles '/@user/', '/user/', '/profile/')
            username = actions.safe_evaluate_js(
                "() => { const p = window.location.pathname; "
                "const a = p.match(/\\/@([^/]+)/); if(a) return a[1]; "
                "const b = p.match(/\\/user\\/([^/]+)/); if(b) return b[1]; "
                "const c = p.match(/\\/profile\\/([^/]+)/); return c ? c[1] : ''; }",
                default=""
            )
        if not username:
            return Result.fail("get_my_uploads(): Username nicht erkennbar")
        return self._scrape_profile_list(actions, username, "upload")

    def _action_get_my_likes(
        self, actions: Actions, username: str = ""
    ) -> Result:
        """Navigate to user's liked models page and scrape model cards."""
        logger.info("[%s] get_my_likes(username='%s')", self.name, username)
        if not username:
            # Auto-detect from current URL (handles '/@user/', '/user/', '/profile/')
            username = actions.safe_evaluate_js(
                "() => { const p = window.location.pathname; "
                "const a = p.match(/\\/@([^/]+)/); if(a) return a[1]; "
                "const b = p.match(/\\/user\\/([^/]+)/); if(b) return b[1]; "
                "const c = p.match(/\\/profile\\/([^/]+)/); return c ? c[1] : ''; }",
                default=""
            )
        if not username:
            return Result.fail("get_my_likes(): Username nicht erkennbar")
        return self._scrape_profile_list(actions, username, "likes")

    def _scrape_profile_list(
        self, actions: Actions, username: str, tab: str
    ) -> Result:
        """
        Incremental-scroll model card scraper for profile pages.
        Adapted from MakerWorldController v14._scrape_profile_model_list():
        inner-container scroll removed (breaks virtual-scroll lists);
        collects incrementally during window-scroll.
        """
        try:
            locale = (
                actions.safe_evaluate_js(_JS_GET_LOCALE, default="en") or "en"
            )
            url = f"{_BASE}/{locale}/@{username}/{tab}"
            actions.navigate(url)
            actions.wait_for(
                selectors=self._selectors["search_result_root"], timeout=15.0
            )
            seen: set = set()
            results = []
            for step in range(10):
                batch = (
                    actions.safe_evaluate_js(
                        f"({_JS_EXTRACT_MODEL_CARDS})(100)", default=[]
                    )
                    or []
                )
                for item in batch:
                    href = item.get("url", "")
                    if href and href not in seen:
                        seen.add(href)
                        results.append(item)
                if len(results) >= 100:
                    break
                prev = len(seen)
                actions.scroll("down", 600)
                time.sleep(0.9)
                if step >= 2 and len(seen) == prev:
                    break
            actions.scroll("up", 9999)
            return Result.ok(data={"results": results, "count": len(results)})
        except ActionError as e:
            return Result.fail(error=f"profile list ({tab}): {e}")
        except Exception as e:
            return Result.fail(
                error=f"profile list ({tab}): {type(e).__name__}: {e}"
            )

    # ═══════════════════════════════════════════════════════════════════
    # ANALYSIS
    # ═══════════════════════════════════════════════════════════════════

    def _action_get_popular_searches(self, actions: Actions) -> Result:
        """Navigate to popular-searches page and extract word-cloud keywords."""
        logger.info("[%s] get_popular_searches()", self.name)
        try:
            locale = (
                actions.safe_evaluate_js(_JS_GET_LOCALE, default="en") or "en"
            )
            url = f"{_BASE}/{locale}/my/creator-center/popular-searches"
            actions.navigate(url)
            time.sleep(2.0)  # word-cloud JS needs render time
            _JS_CLOUD = r"""
            () => {
                const results = [];
                const seen = new Set();
                for (const el of document.querySelectorAll(
                    '.js-word-item,.word-cloud-item,' +
                    '[class*="word-cloud"],[class*="word-item"],[class*="wordCloud"]'
                )) {
                    const text = (el.innerText || el.textContent || '').trim();
                    if (!text || text.length < 2 || seen.has(text)) continue;
                    seen.add(text);
                    const fs = parseFloat(window.getComputedStyle(el).fontSize) || 0;
                    results.push({keyword: text, font_size: String(Math.round(fs))});
                }
                results.sort((a,b) =>
                    parseFloat(b.font_size) - parseFloat(a.font_size));
                results.forEach((r, i) => { r.rank = String(i + 1); });
                return results.slice(0, 100);
            }
            """
            results = (
                actions.safe_evaluate_js(_JS_CLOUD, default=[]) or []
            )
            return Result.ok(data={"keywords": results, "count": len(results)})
        except ActionError as e:
            return Result.fail(error=f"get_popular_searches(): {e}")
        except Exception as e:
            return Result.fail(
                error=f"get_popular_searches(): {type(e).__name__}: {e}"
            )

    def _action_compare_models(
        self, actions: Actions, url1: str = "", url2: str = ""
    ) -> Result:
        """Open two model pages in sequence and compare their metadata."""
        logger.info(
            "[%s] compare_models('%s', '%s')",
            self.name, url1[:60], url2[:60],
        )
        try:
            if not url1 or not url2:
                return Result.fail(
                    error="compare_models(): url1 and url2 are required"
                )
            actions.navigate(url1)
            actions.wait_for(
                selectors=self._selectors["model_page_root"], timeout=12.0
            )
            raw_a = actions.safe_evaluate_js(_JS_GET_MODEL_INFO, default="{}")
            info_a = json.loads(raw_a or "{}")

            actions.navigate(url2)
            actions.wait_for(
                selectors=self._selectors["model_page_root"], timeout=12.0
            )
            raw_b = actions.safe_evaluate_js(_JS_GET_MODEL_INFO, default="{}")
            info_b = json.loads(raw_b or "{}")

            return Result.ok(data={"model_a": info_a, "model_b": info_b})
        except ActionError as e:
            return Result.fail(error=f"compare_models(): {e}")
        except Exception as e:
            return Result.fail(
                error=f"compare_models(): {type(e).__name__}: {e}"
            )
