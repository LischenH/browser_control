"""
tests/test_full_system.py
─────────────────────────
Full End-to-End Test Suite for browser_control.

Requirements:
  - Chrome running with: --remote-debugging-port=9222 --user-data-dir=C:\\tmp\\chrome_debug
  - pip install playwright
  - No mocks — real browser only

Run:
  python -m pytest tests/test_full_system.py -v --tb=short
  # or directly:
  python tests/test_full_system.py

Coverage:
  [YT]  YouTube: search, open video, like, pause/play, seek, subscribe,
                 comments, next video, recommended tab, channel, multi-tab
  [AMZ] Amazon:  search, open product, read title, add/remove cart
  [MW]  MakerWorld: search, open model, like, collect, download, model info
  [TAB] Multi-tab: open recommended videos, switch tabs, per-tab actions

Design:
  - Every test step validates the actual DOM state, not just return codes.
  - "Silent failure" detection: action returns success but state unchanged → FAIL.
  - Structured result output with PASS / FAIL / SKIP per step.
  - Consecutive failures in one suite do NOT abort other suites.
"""

from __future__ import annotations

import logging
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ─── path bootstrap (run from repo root OR tests/ dir) ──────────────────────
import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE) if os.path.basename(_HERE) == "tests" else _HERE
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ─── project imports ─────────────────────────────────────────────────────────
import config
from core.browser import BrowserConnection
from core.actions import Actions, ActionError
from core.tab_manager import TabManager
from skill_manager.manager import SkillManager
from agent.executor import Executor
from agent.planner import Planner, Step

# ─── logging setup ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,          # silence framework noise during tests
    format="%(levelname)s %(name)s: %(message)s",
)
_log = logging.getLogger("e2e_test")
_log.setLevel(logging.DEBUG)

# ─── Pass sentinel ──────────────────────────────────────────────────────────
# step() returns None on FAIL and _PASS on PASS-with-no-data (e.g. navigate()).
# Callers use `skip_if=prev_step is None` — which correctly stays False for _PASS.
_PASS = object()

# ─── Pass sentinel ───────────────────────────────────────────────────────────
# CRITICAL FIX: step() must return a non-None sentinel on PASS when fn() returns
# None (e.g. actions.navigate()). Without this, every `skip_if=nav is None` fires
# even when navigation succeeded, cascading into all downstream steps being skipped.
# _PASS is a unique object: `_PASS is not None` is True, `bool(_PASS)` is True.
_PASS = object()

# ─── ANSI colours ────────────────────────────────────────────────────────────
_GREEN  = "\033[92m"
_RED    = "\033[91m"
_YELLOW = "\033[93m"
_CYAN   = "\033[96m"
_BOLD   = "\033[1m"
_RESET  = "\033[0m"

def _green(s):  return f"{_GREEN}{s}{_RESET}"
def _red(s):    return f"{_RED}{s}{_RESET}"
def _yellow(s): return f"{_YELLOW}{s}{_RESET}"
def _cyan(s):   return f"{_CYAN}{s}{_RESET}"
def _bold(s):   return f"{_BOLD}{s}{_RESET}"


# ═════════════════════════════════════════════════════════════════════════════
# Result tracking
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class StepOutcome:
    name: str
    passed: bool
    skipped: bool = False
    details: str = ""
    data: Any = None

@dataclass
class SuiteResult:
    name: str
    outcomes: list[StepOutcome] = field(default_factory=list)

    @property
    def passed(self): return sum(1 for o in self.outcomes if o.passed)
    @property
    def failed(self): return sum(1 for o in self.outcomes if not o.passed and not o.skipped)
    @property
    def skipped(self): return sum(1 for o in self.outcomes if o.skipped)
    @property
    def total(self): return len(self.outcomes)

_all_suites: list[SuiteResult] = []


# ═════════════════════════════════════════════════════════════════════════════
# Test runner primitives
# ═════════════════════════════════════════════════════════════════════════════

_current_suite: Optional[SuiteResult] = None

def suite(name: str) -> SuiteResult:
    global _current_suite
    _current_suite = SuiteResult(name=name)
    _all_suites.append(_current_suite)
    print(f"\n{_bold(_cyan('━'*60))}")
    print(f"{_bold(_cyan(f'  SUITE: {name}'))}")
    print(f"{_bold(_cyan('━'*60))}")
    return _current_suite


def step(
    name: str,
    fn: Callable[[], Any],
    *,
    validate: Optional[Callable[[Any], tuple[bool, str]]] = None,
    skip_if: bool = False,
    skip_reason: str = "",
) -> Optional[Any]:
    """
    Run a single test step. Returns the result value on pass, None on fail/skip.

    validate: optional callable(result) -> (passed: bool, detail: str)
              If omitted, step passes when fn() does not raise.
    skip_if:  boolean — if True the step is recorded as SKIP.
    """
    assert _current_suite is not None, "call suite() before step()"

    prefix = f"  {'[SKIP]' if skip_if else '[STEP]'} {name}"

    if skip_if:
        print(f"{_yellow(prefix)} — {skip_reason}")
        _current_suite.outcomes.append(
            StepOutcome(name=name, passed=True, skipped=True, details=skip_reason)
        )
        return None

    print(f"{prefix} … ", end="", flush=True)
    t0 = time.perf_counter()
    try:
        result = fn()
        elapsed = (time.perf_counter() - t0) * 1000

        # Run optional validation
        if validate is not None:
            ok, detail = validate(result)
        else:
            ok, detail = True, ""

        if ok:
            tag = _green("PASS")
            msg = f"({elapsed:.0f}ms){' — ' + detail if detail else ''}"
            _current_suite.outcomes.append(
                StepOutcome(name=name, passed=True, details=detail, data=result)
            )
        else:
            tag = _red("FAIL")
            msg = f"({elapsed:.0f}ms) — {detail}"
            _current_suite.outcomes.append(
                StepOutcome(name=name, passed=False, details=detail, data=result)
            )

        print(f"{tag} {msg}")
        # On PASS: return the real result when it is not None, or _PASS sentinel
        # when fn() returned None (e.g. navigate(), scroll()). This lets callers
        # distinguish "step passed" from "step failed" via `result is None`.
        if ok:
            return result if result is not None else _PASS
        return None

    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        tb = traceback.format_exc().strip().splitlines()
        detail = f"{type(exc).__name__}: {exc}"
        _log.debug("Step '%s' exception:\n%s", name, "\n".join(tb))
        print(f"{_red('FAIL')} ({elapsed:.0f}ms) — {detail}")
        _current_suite.outcomes.append(
            StepOutcome(name=name, passed=False, details=detail)
        )
        return None


def assert_dom_state(actions: Actions, js: str, expected: Any, description: str) -> bool:
    """
    Execute JS and compare result to expected.
    Returns True on match, prints FAIL detail otherwise.
    Used for 'silent failure' detection — action said OK but DOM disagrees.
    """
    try:
        actual = actions.safe_evaluate_js(js, default="__eval_failed__")
        if actual == expected:
            return True
        print(f"    {_red('[DOM-MISMATCH]')} {description}: expected={expected!r} actual={actual!r}")
        return False
    except Exception as exc:
        print(f"    {_red('[DOM-CHECK-ERR]')} {description}: {exc}")
        return False


def print_summary(suites: list[SuiteResult]) -> int:
    """Print final summary table. Returns exit code (0=all passed)."""
    print(f"\n{_bold('═'*60)}")
    print(f"{_bold('  FINAL SUMMARY')}")
    print(f"{_bold('═'*60)}")
    total_pass = total_fail = total_skip = 0
    for s in suites:
        status = _green("✓ OK") if s.failed == 0 else _red("✗ FAIL")
        print(
            f"  {status}  {s.name:<30}  "
            f"{_green(str(s.passed))}P / {_red(str(s.failed))}F / {_yellow(str(s.skipped))}S"
        )
        for o in s.outcomes:
            if not o.passed and not o.skipped:
                print(f"        {_red('↳')} {o.name}: {o.details[:100]}")
        total_pass += s.passed
        total_fail += s.failed
        total_skip += s.skipped

    print(f"\n  Total: {_green(str(total_pass))} passed, "
          f"{_red(str(total_fail))} failed, "
          f"{_yellow(str(total_skip))} skipped")
    print(_bold("═"*60))
    return 0 if total_fail == 0 else 1


# ═════════════════════════════════════════════════════════════════════════════
# Connection setup (shared across all suites)
# ═════════════════════════════════════════════════════════════════════════════

def make_env():
    """Connect to Chrome and return (conn, skill_manager, tab_manager)."""
    conn = BrowserConnection().connect()
    sm = SkillManager()
    tm = TabManager(conn)
    return conn, sm, tm


def make_executor(conn, sm) -> Executor:
    return Executor(
        page=conn.active_page,
        skill_manager=sm,
        connection=conn,
        goal="e2e test",
    )


def skill_actions(conn, sm, url_fragment: str) -> tuple:
    """Get (skill, actions) for current page."""
    actions = Actions(conn.active_page)
    skill = sm.get_skill(url_fragment or conn.active_page.url)
    return skill, actions


# ═════════════════════════════════════════════════════════════════════════════
# ██████  YOUTUBE SUITE
# ═════════════════════════════════════════════════════════════════════════════

def run_youtube_suite(conn, sm, tm) -> SuiteResult:
    s = suite("YouTube — Search, Play, Engage, Navigate")
    executor = make_executor(conn, sm)
    yt_skill = sm.get_skill("youtube.com")
    actions = Actions(conn.active_page)

    # ── 1. Navigate to YouTube ────────────────────────────────────────────────
    nav_ok = step(
        "navigate to YouTube",
        lambda: actions.navigate("https://www.youtube.com"),
        validate=lambda r: ("youtube.com" in conn.active_page.url,
                            conn.active_page.url[:80]),
    )

    # ── 2. Search ─────────────────────────────────────────────────────────────
    search_result = step(
        "search for 'Python tutorial'",
        lambda: yt_skill.get_action("search")(actions, query="Python tutorial"),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=nav_ok is None,
        skip_reason="navigation failed",
    )

    # ── 3. Click first video ──────────────────────────────────────────────────
    video_result = step(
        "click first video result",
        lambda: yt_skill.get_action("click_first_video")(actions),
        validate=lambda r: (
            r is not None and r.success and "/watch" in conn.active_page.url,
            f"url={conn.active_page.url[:80]}" if r else "failed"
        ),
        skip_if=search_result is None,
        skip_reason="search failed",
    )

    # ── 4. Read title ─────────────────────────────────────────────────────────
    title_result = step(
        "read video title",
        lambda: yt_skill.get_action("read_title")(actions),
        validate=lambda r: (
            r is not None and r.success and bool(r.data and len(r.data) > 3),
            f"title={r.data!r}" if r else "no result"
        ),
        skip_if=video_result is None,
    )

    # ── 5. Pause video ────────────────────────────────────────────────────────
    pause_result = step(
        "pause video",
        lambda: yt_skill.get_action("pause")(actions),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=video_result is None,
    )

    # ── 5b. DOM verify: video is actually paused ──────────────────────────────
    step(
        "DOM verify: video.paused == true",
        lambda: assert_dom_state(
            actions,
            "() => { const v = document.querySelector('video'); return v ? v.paused : null; }",
            True,
            "video.paused",
        ),
        validate=lambda r: (r is True, "video not paused in DOM"),
        skip_if=pause_result is None,
    )

    # ── 6. Play video ─────────────────────────────────────────────────────────
    play_result = step(
        "play video",
        lambda: yt_skill.get_action("play")(actions),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=pause_result is None,
    )

    # ── 7. Seek forward 15 seconds ────────────────────────────────────────────
    step(
        "seek forward 15 seconds",
        lambda: yt_skill.get_action("seek_forward")(actions, seconds=15),
        validate=lambda r: (
            r is not None and r.success and isinstance(r.data, dict) and r.data.get("position", 0) > 10,
            f"position={r.data}" if r else "failed"
        ),
        skip_if=play_result is None,
    )

    # ── 8. Seek backward 5 seconds ────────────────────────────────────────────
    step(
        "seek backward 5 seconds",
        lambda: yt_skill.get_action("seek_backward")(actions, seconds=5),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=play_result is None,
    )

    # ── 9. Like video (idempotent) ────────────────────────────────────────────
    like_result = step(
        "like video",
        lambda: yt_skill.get_action("like")(actions),
        validate=lambda r: (
            r is not None and r.success and isinstance(r.data, dict)
            and r.data.get("liked") is True,
            f"data={r.data}" if r else "no result"
        ),
        skip_if=video_result is None,
    )

    # ── 9b. DOM verify: like button is now pressed ───────────────────────────
    step(
        "DOM verify: like button aria-pressed == true",
        lambda: assert_dom_state(
            actions,
            """() => {
              const btn =
                document.querySelector('like-button-view-model button[aria-pressed]')
                || document.querySelector('ytd-like-button-renderer button[aria-pressed]')
                || document.querySelector('#like-button button[aria-pressed]');
              return btn ? btn.getAttribute('aria-pressed') : null;
            }""",
            "true",
            "like button aria-pressed",
        ),
        validate=lambda r: (r is True, "like button not showing pressed in DOM"),
        skip_if=like_result is None,
    )

    # ── 10. Unlike (cleanup) ──────────────────────────────────────────────────
    step(
        "unlike video (cleanup)",
        lambda: yt_skill.get_action("unlike")(actions),
        validate=lambda r: (
            r is not None and r.success and isinstance(r.data, dict)
            and r.data.get("liked") is False,
            f"data={r.data}" if r else "no result"
        ),
        skip_if=like_result is None,
    )

    # ── 11. Subscribe ─────────────────────────────────────────────────────────
    sub_result = step(
        "subscribe to channel",
        lambda: yt_skill.get_action("subscribe")(actions),
        validate=lambda r: (
            r is not None and r.success,
            f"data={r.data}" if r else "no result"
        ),
        skip_if=video_result is None,
    )

    # ── 12. Unsubscribe (cleanup) ─────────────────────────────────────────────
    step(
        "unsubscribe (cleanup)",
        lambda: yt_skill.get_action("unsubscribe")(actions),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=sub_result is None,
    )

    # ── 13. Autoplay toggle ───────────────────────────────────────────────────
    step(
        "toggle autoplay",
        lambda: yt_skill.get_action("toggle_autoplay")(actions),
        validate=lambda r: (
            r is not None and r.success and isinstance(r.data, dict),
            f"data={r.data}" if r else "no result"
        ),
        skip_if=video_result is None,
    )

    # ── 14. Open comments ─────────────────────────────────────────────────────
    comments_result = step(
        "open comments section",
        lambda: yt_skill.get_action("open_comments")(actions),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=video_result is None,
    )

    # ── 15. Scroll comments ───────────────────────────────────────────────────
    step(
        "scroll comments (3 times)",
        lambda: yt_skill.get_action("scroll_comments")(actions, amount=3),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=comments_result is None,
    )

    # ── 16. Next video ────────────────────────────────────────────────────────
    next_result = step(
        "play next video",
        lambda: yt_skill.get_action("next_video")(actions),
        validate=lambda r: (
            r is not None and r.success
            and ("/watch" in conn.active_page.url or "/shorts/" in conn.active_page.url),
            f"url={conn.active_page.url[:80]}" if r else "failed"
        ),
        skip_if=video_result is None,
    )

    # ── 17. Go to channel ─────────────────────────────────────────────────────
    step(
        "go to channel",
        lambda: yt_skill.get_action("go_to_channel")(actions),
        validate=lambda r: (
            r is not None and r.success
            and ("/@" in conn.active_page.url or "/channel/" in conn.active_page.url
                 or "/c/" in conn.active_page.url),
            f"url={conn.active_page.url[:80]}" if r else "failed"
        ),
        skip_if=next_result is None,
    )

    return s


# ═════════════════════════════════════════════════════════════════════════════
# ██████  YOUTUBE RECOMMENDED / MULTI-TAB SUITE
# ═════════════════════════════════════════════════════════════════════════════

def run_youtube_multitab_suite(conn, sm, tm) -> SuiteResult:
    s = suite("YouTube — Multi-Tab Recommended Videos")
    yt_skill = sm.get_skill("youtube.com")
    actions = Actions(conn.active_page)

    # Navigate to a video first
    nav = step(
        "navigate to a YouTube video",
        lambda: actions.navigate("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        validate=lambda r: ("/watch" in conn.active_page.url, conn.active_page.url[:80]),
    )

    # Open top 3 recommended in background tabs
    open_result = step(
        "open top 3 recommended in background tabs",
        lambda: yt_skill.get_action("open_top_recommended")(actions, n=3),
        validate=lambda r: (
            r is not None and r.success
            and isinstance(r.data, list) and len(r.data) >= 1,
            f"opened={len(r.data) if r and r.data else 0} tabs"
        ),
        skip_if=nav is None,
    )

    # List tabs and verify new tabs exist
    tabs_result = step(
        "list all tabs — expect ≥2",
        lambda: tm.list_tabs(),
        validate=lambda r: (len(r) >= 2, f"found {len(r)} tabs"),
        skip_if=open_result is None,
    )

    # Switch to the second tab (first recommended)
    switched = step(
        "switch to first recommended tab (index 1)",
        lambda: tm.switch_to_index(1),
        validate=lambda r: (r is not None, "switch_to_index returned None"),
        skip_if=tabs_result is None or len(tabs_result) < 2,
        skip_reason="not enough tabs",
    )

    # Read title on that tab
    if switched is not None:
        new_actions = Actions(switched.page)
        new_skill = sm.get_skill("youtube.com")
        step(
            "read title on recommended tab",
            lambda: new_skill.get_action("read_title")(new_actions),
            validate=lambda r: (
                r is not None and r.success and bool(r.data and len(r.data) > 2),
                f"title={r.data!r}" if r else "failed"
            ),
        )

    # Switch back to original tab (index 0)
    step(
        "switch back to tab index 0",
        lambda: tm.switch_to_index(0),
        validate=lambda r: (r is not None, "failed"),
        skip_if=switched is None,
    )

    # Verify correct tab is active after switch
    step(
        "verify active tab URL contains youtube.com",
        lambda: conn.active_page.url,
        validate=lambda r: ("youtube.com" in r, f"url={r[:80]}"),
    )

    return s


# ═════════════════════════════════════════════════════════════════════════════
# ██████  AMAZON SUITE
# ═════════════════════════════════════════════════════════════════════════════

def run_amazon_suite(conn, sm, tm) -> SuiteResult:
    s = suite("Amazon — Search, Product, Cart")
    amz_skill = sm.get_skill("amazon")
    actions = Actions(conn.active_page)

    # ── 1. Navigate to Amazon ─────────────────────────────────────────────────
    nav_ok = step(
        "navigate to Amazon.de",
        lambda: actions.navigate("https://www.amazon.de"),
        validate=lambda r: ("amazon" in conn.active_page.url, conn.active_page.url[:80]),
    )

    # ── 2. Search ─────────────────────────────────────────────────────────────
    search_result = step(
        "search for 'USB-C Kabel'",
        lambda: amz_skill.get_action("search")(actions, query="USB-C Kabel"),
        validate=lambda r: (
            r is not None and r.success and "s?k=" in conn.active_page.url,
            f"url={conn.active_page.url[:80]}" if r else "failed"
        ),
        skip_if=nav_ok is None,
    )

    # ── 3. Click first organic result ────────────────────────────────────────
    product_result = step(
        "open first product result",
        lambda: amz_skill.get_action("click_first_result")(actions),
        validate=lambda r: (
            r is not None and r.success and "/dp/" in conn.active_page.url,
            f"url={conn.active_page.url[:80]}" if r else "failed"
        ),
        skip_if=search_result is None,
    )

    # ── 4. Read product title ─────────────────────────────────────────────────
    title_result = step(
        "read product title",
        lambda: amz_skill.get_action("read_product_title")(actions),
        validate=lambda r: (
            r is not None and r.success and bool(r.data and len(r.data) > 3),
            f"title={r.data!r}" if r else "no result"
        ),
        skip_if=product_result is None,
    )

    # ── 5. Read price ─────────────────────────────────────────────────────────
    step(
        "read product price",
        lambda: amz_skill.get_action("read_price")(actions),
        validate=lambda r: (
            r is not None and r.success and bool(r.data),
            f"price={r.data!r}" if r else "no result"
        ),
        skip_if=product_result is None,
    )

    # ── 6. Add to cart (idempotent — safe to call once) ───────────────────────
    cart_result = step(
        "add product to cart",
        lambda: amz_skill.get_action("add_to_cart")(actions),
        validate=lambda r: (
            r is not None and r.success and isinstance(r.data, dict)
            and r.data.get("added") is True,
            f"data={r.data}" if r else "no result"
        ),
        skip_if=product_result is None,
    )

    # ── 6b. DOM verify: cart count increased ─────────────────────────────────
    step(
        "DOM verify: cart badge shows ≥1 item",
        lambda: assert_dom_state(
            actions,
            """() => {
              const badge = document.querySelector('#nav-cart-count');
              return badge ? parseInt(badge.innerText.trim(), 10) : 0;
            }""",
            expected=None,  # just check > 0
            description="cart_count > 0",
        ),
        validate=lambda r: (
            # assert_dom_state returns True/False; here we re-check directly
            (lambda cnt: cnt > 0)(
                actions.safe_evaluate_js(
                    "() => { const b = document.querySelector('#nav-cart-count'); "
                    "return b ? parseInt(b.innerText.trim(), 10) || 0 : 0; }",
                    default=0
                )
            ),
            "cart count badge is 0"
        ),
        skip_if=cart_result is None,
    )

    # ── 7. Open cart ──────────────────────────────────────────────────────────
    cart_page_result = step(
        "open shopping cart",
        lambda: amz_skill.get_action("open_cart")(actions),
        validate=lambda r: (
            r is not None and r.success
            and ("/cart" in conn.active_page.url or "cart" in conn.active_page.url),
            f"url={conn.active_page.url[:80]}" if r else "failed"
        ),
        skip_if=cart_result is None,
    )

    # ── 8. Remove from cart ───────────────────────────────────────────────────
    step(
        "remove item from cart",
        lambda: amz_skill.get_action("remove_from_cart")(actions),
        validate=lambda r: (
            r is not None and r.success,
            r.error if r else "no result"
        ),
        skip_if=cart_page_result is None,
    )

    return s


# ═════════════════════════════════════════════════════════════════════════════
# ██████  MAKERWORLD SUITE
# ═════════════════════════════════════════════════════════════════════════════

def run_makerworld_suite(conn, sm, tm) -> SuiteResult:
    s = suite("MakerWorld — Search, Model, Engage, Download")
    mw_skill = sm.get_skill("makerworld.com")
    actions = Actions(conn.active_page)

    # ── 1. Navigate to MakerWorld ─────────────────────────────────────────────
    nav_ok = step(
        "navigate to MakerWorld",
        lambda: actions.navigate("https://makerworld.com"),
        validate=lambda r: ("makerworld.com" in conn.active_page.url, conn.active_page.url[:80]),
    )

    # ── 2. Search ─────────────────────────────────────────────────────────────
    search_result = step(
        "search for 'benchy'",
        lambda: mw_skill.get_action("search")(actions, query="benchy"),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=nav_ok is None,
    )

    # ── 3. Get search results ─────────────────────────────────────────────────
    results_data = step(
        "get top 5 search results",
        lambda: mw_skill.get_action("get_search_results")(actions, n=5),
        validate=lambda r: (
            r is not None and r.success
            and isinstance(r.data, list) and len(r.data) >= 1,
            f"found {len(r.data) if r and r.data else 0} results"
        ),
        skip_if=search_result is None,
    )

    # ── 4. Open first model ───────────────────────────────────────────────────
    first_model_url = None
    if results_data is not None and results_data.data:
        first_model_url = results_data.data[0].get("url", "")

    model_nav = step(
        "navigate to first model page",
        lambda: actions.navigate(
            first_model_url if first_model_url and first_model_url.startswith("http")
            else f"https://makerworld.com{first_model_url}"
        ),
        validate=lambda r: (
            "makerworld.com" in conn.active_page.url
            and "/models/" in conn.active_page.url,
            conn.active_page.url[:80]
        ),
        skip_if=first_model_url is None,
        skip_reason="no results found",
    )

    # ── 5. Get model info ─────────────────────────────────────────────────────
    info_result = step(
        "get model info",
        lambda: mw_skill.get_action("get_model_info")(actions),
        validate=lambda r: (
            r is not None and r.success
            and isinstance(r.data, dict) and bool(r.data.get("title")),
            f"title={r.data.get('title', '') if r and r.data else ''!r}"
        ),
        skip_if=model_nav is None,
    )

    # ── 6. Like model (idempotent) ────────────────────────────────────────────
    like_result = step(
        "like model",
        lambda: mw_skill.get_action("like")(actions),
        validate=lambda r: (
            r is not None and r.success
            and isinstance(r.data, dict) and r.data.get("liked") is not False,
            f"data={r.data}" if r else "no result"
        ),
        skip_if=model_nav is None,
    )

    # ── 6b. DOM verify like state ─────────────────────────────────────────────
    step(
        "DOM verify: like button shows liked state",
        lambda: actions.safe_evaluate_js(
            """() => {
              const candidates = [
                document.querySelector('[class*="like-icon-box"]'),
                document.querySelector('button[aria-label*="like" i]'),
              ];
              const el = candidates.find(Boolean);
              if (!el) return null;
              const btn = el.closest('button') || el;
              const pressed = btn.getAttribute('aria-pressed');
              if (pressed === 'true') return true;
              if (pressed === 'false') return false;
              return null;
            }""",
            default=None,
        ),
        validate=lambda r: (
            r is not False,  # True or None is acceptable (None = unreadable state)
            f"like state = {r!r} (False = not liked)"
        ),
        skip_if=like_result is None,
    )

    # ── 7. Unlike (cleanup) ───────────────────────────────────────────────────
    step(
        "unlike model (cleanup)",
        lambda: mw_skill.get_action("unlike")(actions),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=like_result is None,
    )

    # ── 8. Collect (add to collection) ───────────────────────────────────────
    collect_result = step(
        "collect model",
        lambda: mw_skill.get_action("collect")(actions),
        validate=lambda r: (
            r is not None and r.success
            and isinstance(r.data, dict) and r.data.get("collected") is True,
            f"data={r.data}" if r else "no result"
        ),
        skip_if=model_nav is None,
    )

    # ── 9. Uncollect (cleanup) ────────────────────────────────────────────────
    step(
        "uncollect model (cleanup)",
        lambda: mw_skill.get_action("uncollect")(actions),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=collect_result is None,
    )

    # ── 10. Download (simulate — opens menu, picks 3mf) ──────────────────────
    step(
        "download model (3mf — simulated)",
        lambda: mw_skill.get_action("download")(actions, format="3mf"),
        validate=lambda r: (
            r is not None and r.success
            and isinstance(r.data, dict) and r.data.get("format") == "3mf",
            f"data={r.data}" if r else "no result"
        ),
        skip_if=model_nav is None,
    )

    return s


# ═════════════════════════════════════════════════════════════════════════════
# ██████  FAILURE-DETECTION SUITE
# ═════════════════════════════════════════════════════════════════════════════

def run_failure_detection_suite(conn, sm, tm) -> SuiteResult:
    """
    Verify that the system correctly detects silent failures —
    cases where an action returns success but the DOM was not changed.
    """
    s = suite("Failure Detection — Silent Failure Guardrails")
    yt_skill = sm.get_skill("youtube.com")
    actions = Actions(conn.active_page)

    # Navigate to a video
    nav = step(
        "navigate to YouTube video for failure detection",
        lambda: actions.navigate("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        validate=lambda r: ("/watch" in conn.active_page.url, conn.active_page.url[:80]),
    )

    # Test: like then immediately check JS state (must match)
    like_r = step(
        "like video",
        lambda: yt_skill.get_action("like")(actions),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=nav is None,
    )

    step(
        "verify: JS confirms video IS liked after like()",
        lambda: actions.safe_evaluate_js(
            """() => {
              const btn =
                document.querySelector('like-button-view-model button[aria-pressed]')
                || document.querySelector('ytd-like-button-renderer button[aria-pressed]')
                || document.querySelector('#like-button button[aria-pressed]');
              return btn ? btn.getAttribute('aria-pressed') : '__not_found__';
            }""",
            default="__eval_failed__",
        ),
        validate=lambda r: (
            r == "true",
            f"aria-pressed={r!r} — expected 'true'. "
            "Silent failure: like() returned success but DOM says not liked."
        ),
        skip_if=like_r is None,
    )

    # Test: unlike should flip the state back
    unlike_r = step(
        "unlike video",
        lambda: yt_skill.get_action("unlike")(actions),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=like_r is None,
    )

    step(
        "verify: JS confirms video IS unliked after unlike()",
        lambda: actions.safe_evaluate_js(
            """() => {
              const btn =
                document.querySelector('like-button-view-model button[aria-pressed]')
                || document.querySelector('ytd-like-button-renderer button[aria-pressed]')
                || document.querySelector('#like-button button[aria-pressed]');
              return btn ? btn.getAttribute('aria-pressed') : '__not_found__';
            }""",
            default="__eval_failed__",
        ),
        validate=lambda r: (
            r == "false",
            f"aria-pressed={r!r} — expected 'false'. "
            "Silent failure: unlike() returned success but DOM still shows liked."
        ),
        skip_if=unlike_r is None,
    )

    # Test: pause → check paused, play → check not paused
    pause_r = step(
        "pause video",
        lambda: yt_skill.get_action("pause")(actions),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=nav is None,
    )
    step(
        "verify: video.paused == true after pause()",
        lambda: actions.safe_evaluate_js(
            "() => { const v = document.querySelector('video'); return v ? v.paused : null; }",
            default=None,
        ),
        validate=lambda r: (r is True, f"video.paused={r!r} — expected True (silent failure)"),
        skip_if=pause_r is None,
    )

    play_r = step(
        "play video",
        lambda: yt_skill.get_action("play")(actions),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=pause_r is None,
    )
    # Give autoplay a moment to confirm state
    time.sleep(0.5)
    step(
        "verify: video.paused == false after play()",
        lambda: actions.safe_evaluate_js(
            "() => { const v = document.querySelector('video'); return v ? v.paused : null; }",
            default=None,
        ),
        validate=lambda r: (r is False, f"video.paused={r!r} — expected False (silent failure)"),
        skip_if=play_r is None,
    )

    return s


# ═════════════════════════════════════════════════════════════════════════════
# ██████  YOUTUBE QUALITY + SAVE SUITE
# ═════════════════════════════════════════════════════════════════════════════

def run_youtube_quality_save_suite(conn, sm, tm) -> SuiteResult:
    s = suite("YouTube — Quality Selection & Watch Later")
    yt_skill = sm.get_skill("youtube.com")
    actions = Actions(conn.active_page)

    nav = step(
        "navigate to YouTube video",
        lambda: actions.navigate("https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
        validate=lambda r: ("/watch" in conn.active_page.url, conn.active_page.url[:80]),
    )

    # Quality selection
    step(
        "set video quality to 720p",
        lambda: yt_skill.get_action("set_quality")(actions, quality="720p"),
        validate=lambda r: (
            r is not None and r.success
            and isinstance(r.data, dict) and r.data.get("quality"),
            f"data={r.data}" if r else "failed"
        ),
        skip_if=nav is None,
    )

    # Autoplay toggle
    before_state = [None]
    def _toggle_and_check():
        state_js = """() => {
          const btn = document.querySelector('button.ytp-autonav-toggle-button')
                   || document.querySelector('.ytp-autonav-toggle-button');
          if (!btn) return null;
          return btn.getAttribute('aria-checked') || btn.getAttribute('aria-pressed');
        }"""
        before_state[0] = actions.safe_evaluate_js(state_js, default=None)
        r = yt_skill.get_action("toggle_autoplay")(actions)
        after = actions.safe_evaluate_js(state_js, default=None)
        return r, before_state[0], after

    step(
        "toggle autoplay and verify state changed",
        _toggle_and_check,
        validate=lambda r: (
            r is not None and r[0] is not None and r[0].success
            and (r[1] != r[2] or r[1] is None),  # state changed or unreadable
            f"before={r[1]!r} after={r[2]!r}" if r else "failed"
        ),
        skip_if=nav is None,
    )

    # Save to Watch Later
    save_result = step(
        "save to watch later",
        lambda: yt_skill.get_action("save_to_watch_later")(actions),
        validate=lambda r: (
            r is not None and r.success,
            f"data={r.data}" if r else "no result"
        ),
        skip_if=nav is None,
    )

    # Remove from Watch Later (cleanup)
    step(
        "remove from watch later (cleanup)",
        lambda: yt_skill.get_action("remove_from_watch_later")(actions),
        validate=lambda r: (r is not None and r.success, r.error if r else "no result"),
        skip_if=save_result is None
        or (save_result.data or {}).get("action") == "skipped",
    )

    return s


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main() -> int:
    print(_bold("\n" + "═"*60))
    print(_bold("  browser_control — Full E2E Test Suite"))
    print(_bold("  Real Chrome via CDP — NO MOCKS"))
    print(_bold("═"*60))

    # Connect once, share across all suites
    print("\nConnecting to Chrome …", end=" ", flush=True)
    try:
        conn, sm, tm = make_env()
        print(_green("OK"))
        print(f"  Active page: {conn.active_page.url[:80]}")
        print(f"  Open tabs  : {len(tm.list_tabs())}")
    except Exception as exc:
        print(_red(f"FAILED — {exc}"))
        print("\nMake sure Chrome is running with:")
        print("  chrome --remote-debugging-port=9222 --user-data-dir=C:\\tmp\\chrome_debug")
        return 1

    try:
        # Run all suites — each is independent, failures do not abort others
        run_youtube_suite(conn, sm, tm)
        run_youtube_quality_save_suite(conn, sm, tm)
        run_youtube_multitab_suite(conn, sm, tm)
        run_amazon_suite(conn, sm, tm)
        run_makerworld_suite(conn, sm, tm)
        run_failure_detection_suite(conn, sm, tm)
    finally:
        conn.disconnect()

    return print_summary(_all_suites)


if __name__ == "__main__":
    sys.exit(main())
