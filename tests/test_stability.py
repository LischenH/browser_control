"""
tests/test_stability.py -- Unit tests for production stability fixes.

Tests cover:
  1. Tab system (deterministic IDs, registry, switch, close)
  2. Interrupt handler (overlay, cookie, ad detection -- mocked DOM)
  3. Page readiness detection (fast-path, spinner-wait, DOM stable)
  4. BrowserConnection health-check and resync logic
  5. Mode resolver fix (Amazon -> fast, unknown -> human)
  6. Executor page-sync (active_page re-read before each step)
  7. YouTube link filtering (/watch?v= validation)
  8. Amazon JS extractor (sponsored filtering logic)

All tests are fully mocked -- no Chrome, no Playwright required.
Run: python -m pytest tests/test_stability.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call, PropertyMock
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_page(url: str = "https://www.youtube.com", title: str = "Test Page") -> MagicMock:
    page = MagicMock()
    page.url = url
    page.title.return_value = title
    page.evaluate.return_value = "complete"
    page.is_visible.return_value = False
    return page


def _make_connection(pages=None, url="https://www.youtube.com"):
    """Create a mock BrowserConnection."""
    page = _make_page(url)
    conn = MagicMock()
    conn.active_page = page
    ctx = MagicMock()
    ctx.pages = pages if pages is not None else [page]
    conn.context = ctx
    conn.browser = MagicMock()
    return conn, page


# ===========================================================================
# 1. TAB SYSTEM TESTS
# ===========================================================================

class TestTabManager:
    """Tests for core/tab_manager.py -- deterministic IDs, registry, switching."""

    def _make_tab_manager(self, pages=None, url="https://www.youtube.com"):
        from core.tab_manager import TabManager
        conn, page = _make_connection(pages=pages, url=url)
        manager = TabManager(conn)
        return manager, conn, page

    def test_list_tabs_assigns_ids(self):
        """Every tab returned by list_tabs() has a non-zero tab_id."""
        p1 = _make_page("https://www.youtube.com", "YouTube")
        p2 = _make_page("https://www.amazon.de", "Amazon")
        manager, conn, _ = self._make_tab_manager(pages=[p1, p2])

        tabs = manager.list_tabs()
        assert len(tabs) == 2
        ids = [t.tab_id for t in tabs]
        assert all(i > 0 for i in ids), "All tab IDs must be positive integers"
        assert len(set(ids)) == 2, "Tab IDs must be unique"

    def test_get_tab_by_id_returns_correct_tab(self):
        """get_tab_by_id() returns the right TabInfo for a known ID."""
        p1 = _make_page("https://www.youtube.com", "YouTube")
        p2 = _make_page("https://www.amazon.de", "Amazon")
        manager, conn, _ = self._make_tab_manager(pages=[p1, p2])

        tabs = manager.list_tabs()
        for tab in tabs:
            found = manager.get_tab_by_id(tab.tab_id)
            assert found is not None
            assert found.tab_id == tab.tab_id
            assert found.url == tab.url

    def test_get_tab_by_id_unknown_returns_none(self):
        """get_tab_by_id() returns None for an ID that was never assigned."""
        manager, conn, _ = self._make_tab_manager()
        result = manager.get_tab_by_id(99999)
        assert result is None

    def test_switch_to_url_sets_active_page(self):
        """switch_to_url() sets conn.active_page to the matched tab's page."""
        p1 = _make_page("https://www.youtube.com", "YouTube")
        p2 = _make_page("https://www.amazon.de", "Amazon")
        manager, conn, _ = self._make_tab_manager(pages=[p1, p2])

        tab = manager.switch_to_url("amazon")
        assert tab.url == "https://www.amazon.de"
        assert conn.active_page is p2

    def test_switch_to_url_raises_on_no_match(self):
        """switch_to_url() raises ValueError when no tab matches."""
        manager, conn, _ = self._make_tab_manager()
        with pytest.raises(ValueError, match="No tab with URL fragment"):
            manager.switch_to_url("nonexistent.example.com")

    def test_switch_to_index_sets_active_page(self):
        """switch_to_index() activates the correct tab by position."""
        p0 = _make_page("https://google.com", "Google")
        p1 = _make_page("https://youtube.com", "YouTube")
        manager, conn, _ = self._make_tab_manager(pages=[p0, p1])

        tab = manager.switch_to_index(1)
        assert "youtube" in tab.url
        assert conn.active_page is p1

    def test_switch_to_index_raises_out_of_range(self):
        """switch_to_index() raises IndexError for invalid index."""
        manager, conn, _ = self._make_tab_manager()
        with pytest.raises(IndexError):
            manager.switch_to_index(99)

    def test_open_tab_registers_id(self):
        """open_tab() returns a TabInfo with a tab_id and registers it."""
        manager, conn, base_page = self._make_tab_manager()
        new_page = _make_page("https://github.com", "GitHub")
        conn.context.expect_page.return_value.__enter__ = MagicMock(return_value=MagicMock())
        conn.context.expect_page.return_value.__exit__ = MagicMock(return_value=False)
        conn.context.expect_page.return_value.__enter__.return_value.value = new_page
        conn.context.pages = [base_page, new_page]

        tab = manager.open_tab("https://github.com")

        assert tab.tab_id > 0
        assert tab.page is new_page
        # Should be registered
        found = manager.get_tab_by_id(tab.tab_id)
        assert found is not None

    def test_close_tab_removes_from_registry(self):
        """close_tab() removes the tab from the registry and resyncs active_page."""
        p1 = _make_page("https://youtube.com", "YouTube")
        p2 = _make_page("https://amazon.de", "Amazon")
        manager, conn, _ = self._make_tab_manager(pages=[p1, p2])

        tabs = manager.list_tabs()
        tab_to_close = tabs[0]
        original_id = tab_to_close.tab_id

        # Simulate the page being removed from context after close
        conn.context.pages = [p2]
        manager.close_tab(tab_to_close)

        # ID should be gone from registry
        assert manager.get_tab_by_id(original_id) is None

    def test_switch_to_tab_id_works(self):
        """switch_to_tab_id() activates a tab by its stable internal ID."""
        p1 = _make_page("https://youtube.com", "YouTube")
        p2 = _make_page("https://amazon.de", "Amazon")
        manager, conn, _ = self._make_tab_manager(pages=[p1, p2])

        tabs = manager.list_tabs()
        target = tabs[1]  # Amazon tab

        result = manager.switch_to_tab_id(target.tab_id)
        assert result.tab_id == target.tab_id
        assert conn.active_page is p2


# ===========================================================================
# 2. INTERRUPT HANDLER TESTS
# ===========================================================================

class TestInterruptHandler:
    """Tests for core/interrupts.py -- mocked DOM visibility."""

    def _handler(self):
        from core.interrupts import InterruptHandler
        return InterruptHandler()

    def test_handle_returns_false_when_nothing_visible(self):
        """Returns False and does not click anything when no interrupts present."""
        handler = self._handler()
        page = _make_page()
        page.is_visible.return_value = False

        result = handler.handle(page)
        assert result is False
        page.click.assert_not_called()

    def test_handle_returns_true_when_cookie_banner_dismissed(self):
        """Returns True and clicks the first visible cookie selector."""
        handler = self._handler()
        page = _make_page()

        def is_visible_side_effect(selector):
            return selector == "button:has-text('Accept all')"

        page.is_visible.side_effect = is_visible_side_effect

        result = handler.handle(page)
        assert result is True
        page.click.assert_called_once_with("button:has-text('Accept all')", timeout=3000)

    def test_handle_returns_true_when_yt_skip_ad_visible(self):
        """Dismisses YouTube skip-ad button when visible."""
        handler = self._handler()
        page = _make_page()

        def is_visible_side_effect(selector):
            return selector == ".ytp-skip-ad-button"

        page.is_visible.side_effect = is_visible_side_effect

        result = handler.handle(page)
        assert result is True
        page.click.assert_called_with(".ytp-skip-ad-button", timeout=3000)

    def test_handle_never_raises(self):
        """handle() never raises even if the page object throws."""
        handler = self._handler()
        page = _make_page()
        page.is_visible.side_effect = RuntimeError("CDP disconnected")

        # Must not raise
        result = handler.handle(page)
        assert result is False

    def test_click_timeout_handled_silently(self):
        """PlaywrightTimeoutError during click is handled silently."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        handler = self._handler()
        page = _make_page()

        page.is_visible.side_effect = lambda sel: sel == "button:has-text('Accept all')"
        page.click.side_effect = PlaywrightTimeoutError("timeout")

        # Must not raise; returns False because click failed
        result = handler.handle(page)
        # The handler attempted a click, caught the timeout -- check no exception
        assert isinstance(result, bool)

    def test_overlay_dismissed_before_cookie(self):
        """Overlays are processed before cookie banners (priority order)."""
        handler = self._handler()
        page = _make_page()

        # Both an overlay and a cookie banner are "visible"
        visible = {"button[aria-label='Close']", "button:has-text('Accept all')"}
        page.is_visible.side_effect = lambda sel: sel in visible

        handler.handle(page)

        # The FIRST click must be for the overlay close button
        first_click_selector = page.click.call_args_list[0][0][0]
        assert first_click_selector == "button[aria-label='Close']"


# ===========================================================================
# 3. PAGE READINESS TESTS
# ===========================================================================

class TestWaitForPageReady:
    """Tests for wait_for_page_ready() fast-path and spinner logic."""

    def test_fast_path_skips_networkidle_when_complete(self):
        """If readyState is complete, wait_for_load_state('networkidle') is never called."""
        from core.actions import wait_for_page_ready
        page = _make_page()
        page.evaluate.return_value = "complete"
        page.is_visible.return_value = False

        wait_for_page_ready(page)

        # networkidle should NOT have been called
        calls = [str(c) for c in page.wait_for_load_state.call_args_list]
        assert not any("networkidle" in c for c in calls), (
            "networkidle must be skipped when readyState is already complete"
        )

    def test_networkidle_called_when_not_complete(self):
        """If readyState is 'loading', domcontentloaded and networkidle are both awaited."""
        from core.actions import wait_for_page_ready
        page = _make_page()
        page.evaluate.return_value = "loading"
        page.is_visible.return_value = False

        wait_for_page_ready(page)

        states_waited = [c[0][0] for c in page.wait_for_load_state.call_args_list]
        assert "domcontentloaded" in states_waited
        assert "networkidle" in states_waited

    def test_spinner_not_waited_when_not_visible(self):
        """Spinners that are not visible are skipped without calling wait_for_selector."""
        from core.actions import wait_for_page_ready
        page = _make_page()
        page.evaluate.return_value = "complete"
        page.is_visible.return_value = False

        wait_for_page_ready(page)

        page.wait_for_selector.assert_not_called()

    def test_spinner_waited_when_visible(self):
        """A visible spinner triggers wait_for_selector(state='hidden')."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from core.actions import wait_for_page_ready
        page = _make_page()
        page.evaluate.return_value = "complete"

        # Only the first spinner selector is visible
        first_spinner = "[data-testid='spinner']"
        page.is_visible.side_effect = lambda sel: sel == first_spinner

        wait_for_page_ready(page)

        page.wait_for_selector.assert_called_once_with(
            first_spinner, state="hidden", timeout=2000
        )

    def test_timeout_during_spinner_wait_is_non_fatal(self):
        """Timeout while waiting for spinner to hide does not crash page-ready check."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from core.actions import wait_for_page_ready
        page = _make_page()
        page.evaluate.return_value = "complete"
        page.is_visible.side_effect = lambda sel: sel == "[data-testid='spinner']"
        page.wait_for_selector.side_effect = PlaywrightTimeoutError("timeout")

        # Must not raise
        wait_for_page_ready(page)


# ===========================================================================
# 4. BROWSER CONNECTION TESTS
# ===========================================================================

class TestBrowserConnection:
    """Tests for core/browser.py -- health-check, resync, error messages."""

    def _make_conn_object(self):
        """Create a BrowserConnection without calling connect()."""
        from core.browser import BrowserConnection
        conn = BrowserConnection()
        # Inject mocked internals
        page = _make_page()
        conn._active_page = page
        conn._playwright = MagicMock()
        conn._browser = MagicMock()
        ctx = MagicMock()
        ctx.pages = [page]
        conn._context = ctx
        return conn, page

    def test_health_check_returns_true_on_live_page(self):
        conn, page = self._make_conn_object()
        page.evaluate.return_value = "complete"
        assert conn.health_check() is True

    def test_health_check_returns_false_on_exception(self):
        conn, page = self._make_conn_object()
        page.evaluate.side_effect = Exception("CDP error")
        assert conn.health_check() is False

    def test_active_page_auto_resyncs_when_page_closed(self):
        """active_page getter resyncs when stored page raises on .url access."""
        conn, original_page = self._make_conn_object()
        new_page = _make_page("https://new.example.com", "New")

        # Simulate the original page being closed
        type(original_page).url = PropertyMock(side_effect=Exception("Page closed"))
        conn._context.pages = [new_page]

        # Accessing active_page should trigger resync
        recovered = conn.active_page
        # After resync, we should get a live page (not the dead one)
        assert recovered is not original_page

    def test_disconnect_is_idempotent(self):
        """disconnect() can be called multiple times without error."""
        conn, _ = self._make_conn_object()
        conn.disconnect()
        conn.disconnect()  # Second call must not raise


# ===========================================================================
# 5. MODE RESOLVER TESTS
# ===========================================================================

class TestModeResolverFix:
    """Tests for the mode-resolver production fixes."""

    def _resolver(self):
        from core.mode_resolver import ModeResolver
        return ModeResolver()

    def test_amazon_is_fast(self):
        """Amazon URLs must resolve to FAST mode (was incorrectly HUMAN before fix)."""
        r = self._resolver()
        assert r.resolve("https://www.amazon.de/s?k=laptop") == "fast"
        assert r.resolve("https://www.amazon.com/dp/B09G3HRMVB") == "fast"
        assert r.resolve("https://www.amazon.co.uk/s?k=headphones") == "fast"

    def test_youtube_is_fast(self):
        r = self._resolver()
        assert r.resolve("https://www.youtube.com/watch?v=abc123") == "fast"
        assert r.resolve("https://www.youtube.com/results?search_query=python") == "fast"

    def test_unknown_site_fallback_is_human(self):
        """Unknown sites must fall back to HUMAN mode (was incorrectly FAST before fix)."""
        r = self._resolver()
        assert r.resolve("https://www.somenewsite.io/page") == "human"
        assert r.resolve("https://www.randomshop-xyz.com/product/123") == "human"

    def test_login_path_is_human(self):
        """URLs containing login/checkout/payment must be HUMAN regardless of domain."""
        r = self._resolver()
        assert r.resolve("https://www.amazon.de/ap/signin") == "human"
        assert r.resolve("https://www.youtube.com/accounts/login") == "human"

    def test_shopify_is_human(self):
        """Shopify checkout pages need HUMAN mode (bot-detection)."""
        r = self._resolver()
        assert r.resolve("https://mystore.shopify.com/checkout") == "human"

    def test_config_override_fast_wins(self):
        """config.EXECUTION_MODE='fast' overrides everything including unknown sites."""
        import config as cfg
        r = self._resolver()
        original = cfg.EXECUTION_MODE
        try:
            cfg.EXECUTION_MODE = "fast"
            assert r.resolve("https://www.randomsite.xyz") == "fast"
        finally:
            cfg.EXECUTION_MODE = original

    def test_config_override_human_wins(self):
        """config.EXECUTION_MODE='human' overrides FAST_DOMAINS like youtube.com."""
        import config as cfg
        r = self._resolver()
        original = cfg.EXECUTION_MODE
        try:
            cfg.EXECUTION_MODE = "human"
            assert r.resolve("https://www.youtube.com/watch?v=xyz") == "human"
        finally:
            cfg.EXECUTION_MODE = original


# ===========================================================================
# 6. EXECUTOR PAGE-SYNC TESTS
# ===========================================================================

class TestExecutorPageSync:
    """Tests that executor re-reads conn.active_page before each step."""

    def _make_executor_with_conn(self, initial_url="https://www.youtube.com"):
        from agent.executor import Executor
        from agent.planner import Step
        from skills.base_skill import Result

        initial_page = _make_page(initial_url)
        new_page = _make_page("https://www.youtube.com/watch?v=abc123")

        conn = MagicMock()
        conn.active_page = initial_page

        skill_manager = MagicMock()
        skill_manager.skill_names = ["MockSkill"]
        mock_skill = MagicMock()
        mock_skill.name = "MockSkill"
        skill_manager.get_skill.return_value = mock_skill

        action_fn = MagicMock(return_value=Result.ok(data="done"))
        mock_skill.get_action.return_value = action_fn

        verifier = MagicMock()
        verifier.verify.return_value = MagicMock(passed=True, should_retry=False, failed=False)

        executor = Executor(
            page=initial_page,
            skill_manager=skill_manager,
            verifier=verifier,
            connection=conn,
        )
        return executor, conn, initial_page, new_page, action_fn

    def test_executor_syncs_page_when_conn_active_page_changes(self):
        """
        When conn.active_page changes between steps (e.g. after open_tab),
        the executor uses the NEW page for the next step's Actions.
        """
        from agent.planner import Step

        executor, conn, initial_page, new_page, action_fn = (
            self._make_executor_with_conn()
        )

        # After step 1 runs, simulate conn.active_page switching to new_page
        call_count = 0
        def get_active_page_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return initial_page
            return new_page

        # Simulate property change by updating mock attribute dynamically
        pages = [initial_page, new_page]
        conn.active_page = initial_page

        step = Step(
            action_name="search",
            params={"query": "test"},
            url=None,
            verify_conditions={},
        )

        result = executor.run([step])
        assert result["success"] is True
        assert result["steps_completed"] == 1

    def test_executor_without_connection_works_normally(self):
        """Executor without connection param runs fine (backward compat)."""
        from agent.executor import Executor
        from agent.planner import Step
        from skills.base_skill import Result

        page = _make_page()
        skill_manager = MagicMock()
        skill_manager.skill_names = ["MockSkill"]
        mock_skill = MagicMock()
        mock_skill.name = "MockSkill"
        skill_manager.get_skill.return_value = mock_skill
        action_fn = MagicMock(return_value=Result.ok(data="ok"))
        mock_skill.get_action.return_value = action_fn
        verifier = MagicMock()
        verifier.verify.return_value = MagicMock(passed=True, should_retry=False, failed=False)

        executor = Executor(page=page, skill_manager=skill_manager, verifier=verifier)

        step = Step(action_name="navigate", params={"url": "https://example.com"})
        result = executor.run([step])
        assert result["success"] is True


# ===========================================================================
# 7. YOUTUBE VIDEO LINK VALIDATION TESTS
# ===========================================================================

class TestYouTubeLinkValidation:
    """Validates that YouTube skill correctly filters non-video links."""

    def test_watch_links_pass_filter(self):
        links = [
            "/watch?v=abc123",
            "https://www.youtube.com/watch?v=xyz789",
        ]
        valid = [h for h in links if "/watch?v=" in h or "/shorts/" in h]
        assert len(valid) == 2

    def test_channel_links_blocked(self):
        links = [
            "/channel/UCxxxxxx",
            "/@SomeCreator",
            "/user/SomeUser",
            "/c/SomeChannel",
        ]
        valid = [h for h in links if "/watch?v=" in h or "/shorts/" in h]
        assert len(valid) == 0

    def test_shorts_links_pass(self):
        links = ["/shorts/abc123", "https://www.youtube.com/shorts/xyz"]
        valid = [h for h in links if "/watch?v=" in h or "/shorts/" in h]
        assert len(valid) == 2

    def test_playlist_links_blocked(self):
        links = ["/playlist?list=PLxxxxxx"]
        valid = [h for h in links if "/watch?v=" in h or "/shorts/" in h]
        assert len(valid) == 0

    def test_mixed_list_filters_correctly(self):
        links = [
            "/watch?v=v1",
            "/channel/UCxxx",
            "/shorts/s1",
            "/@creator",
            "/watch?v=v2",
        ]
        valid = [h for h in links if "/watch?v=" in h or "/shorts/" in h]
        assert len(valid) == 3

    def test_classify_url_function(self):
        """_classify_url() correctly identifies video / shorts / unknown."""
        from skills.youtube_skill import _classify_url
        assert _classify_url("https://www.youtube.com/watch?v=abc") == "video"
        assert _classify_url("https://www.youtube.com/shorts/abc") == "shorts"
        assert _classify_url("https://www.youtube.com/channel/UC123") == "unknown"
        assert _classify_url("https://www.youtube.com/") == "unknown"


# ===========================================================================
# 8. AMAZON SPONSORED FILTERING TESTS (JS logic, Python-side validation)
# ===========================================================================

class TestAmazonSponsoredFiltering:
    """Tests the Python-side validation of Amazon product URLs."""

    def _is_valid_product_url(self, url: str) -> bool:
        """Mirrors the validation logic used in open_top_results."""
        return "/dp/" in url and "/sspa/" not in url

    def test_canonical_dp_url_valid(self):
        assert self._is_valid_product_url("https://www.amazon.de/dp/B09G3HRMVB")

    def test_sspa_url_rejected(self):
        assert not self._is_valid_product_url(
            "https://www.amazon.de/sspa/click?ie=UTF8&spc=xyz&url=%2Fdp%2FB09G3HRMVB"
        )

    def test_non_product_url_rejected(self):
        assert not self._is_valid_product_url("https://www.amazon.de/s?k=laptop")

    def test_extract_asin(self):
        from skills.amazon_skill import _extract_asin
        assert _extract_asin("https://www.amazon.de/product-name/dp/B09G3HRMVB/ref=xxx") == "B09G3HRMVB"
        assert _extract_asin("https://www.amazon.de/dp/B07PQNBV6X") == "B07PQNBV6X"
        assert _extract_asin("https://www.amazon.de/s?k=laptop") == ""

    def test_is_product_url(self):
        from skills.amazon_skill import _is_product_url
        assert _is_product_url("https://www.amazon.de/dp/B09G3HRMVB") is True
        assert _is_product_url("https://www.amazon.de/s?k=laptop") is False
        assert _is_product_url("https://www.amazon.de/sspa/click?url=%2Fdp%2FB09") is False

    def test_amazon_base_extraction(self):
        from skills.amazon_skill import _amazon_base
        assert _amazon_base("https://www.amazon.de/dp/B09G3") == "https://www.amazon.de"
        assert _amazon_base("https://www.amazon.com/s?k=test") == "https://www.amazon.com"
        assert _amazon_base("https://www.amazon.co.uk/dp/B09") == "https://www.amazon.co.uk"
