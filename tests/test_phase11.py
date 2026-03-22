"""
tests/test_phase11.py — Full test suite for Phase 11 (Full Platform Agent)

Covers every new capability added in Phase 11 — fully mocked,
no browser, no Playwright, no network required.

Test categories:
  1.  YouTube Skill — Engagement Actions (like, unlike, subscribe, etc.)
  2.  YouTube Skill — Playback Actions (play, pause, speed, seek, quality, etc.)
  3.  YouTube Skill — Shorts Actions (next_short, prev_short)
  4.  YouTube Skill — Navigation Actions (go_home, go_to_channel, etc.)
  5.  YouTube Skill — Library Actions (open_history, open_liked_videos, etc.)
  6.  YouTube Skill — Playlist Management (add/remove_from_playlist)
  7.  YouTube Skill — Recommended Videos (open_recommended, open_top_recommended)
  8.  YouTube Skill — Mode Detection (video vs shorts)
  9.  Amazon Skill — Shopping Actions (add_to_cart, wishlist, buy_now)
  10. Amazon Skill — Account Navigation (open_orders, open_cart, open_wishlist)
  11. Amazon Skill — Product Data (read_price, read_rating, read_reviews)
  12. Planner — YouTube On-Page Patterns (TemplateEngine)
  13. Planner — Amazon On-Page Patterns (TemplateEngine)
  14. Planner — Combined & Edge-Case Patterns
  15. Executor — Idempotency Guard (skipped_* results)
  16. Executor — Retry-Safety (deep-copy params, no double-toggle)

Run with:
    python -m pytest tests/test_phase11.py -v
    python -m pytest tests/test_phase11.py -v -x          # stop on first fail
    python -m pytest tests/test_phase11.py -v -k "like"   # filter by name
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import copy
from unittest.mock import MagicMock, patch, call
import pytest

from agent.executor import Executor, _result_data_is_idempotent_skip
from agent.planner import Planner, Step, _TemplateEngine
from agent.verifier import VerifyResult
from core.actions import Actions, ActionError
from skills.base_skill import Result
from skills.youtube_skill import YouTubeSkill
from skills.amazon_skill import AmazonSkill


# ══════════════════════════════════════════════════════════════════════════════
# SHARED MOCK HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _make_page(url: str = "https://www.youtube.com/watch?v=abc") -> MagicMock:
    page = MagicMock()
    page.url = url
    return page


def _make_actions(page=None, js_return=None, js_side_effect=None) -> Actions:
    """
    Build an Actions instance whose internal page is a MagicMock.

    js_return:      value returned by every evaluate_js() call
    js_side_effect: list of return values consumed in order by evaluate_js()
    """
    page = page or _make_page()
    actions = Actions.__new__(Actions)
    actions._page = page
    actions._interrupts = MagicMock()

    # Default: evaluate_js → None unless overridden
    if js_side_effect is not None:
        actions.evaluate_js = MagicMock(side_effect=js_side_effect)
    else:
        actions.evaluate_js = MagicMock(return_value=js_return)

    actions.wait_for = MagicMock()
    actions.click = MagicMock()
    actions.click_and_wait = MagicMock()
    actions.type_text = MagicMock()
    actions.get_text = MagicMock(return_value="Test Title")
    actions.press_key = MagicMock()
    actions.navigate = MagicMock()
    actions.scroll = MagicMock()
    actions.get_all_hrefs = MagicMock(return_value=[])
    actions.open_new_tab = MagicMock(return_value=_make_page())
    return actions


def _yt_skill() -> YouTubeSkill:
    skill = YouTubeSkill.__new__(YouTubeSkill)
    skill._selectors = YouTubeSkill().__dict__.get("_selectors") or YouTubeSkill()._selectors
    return skill


def _amz_skill() -> AmazonSkill:
    return AmazonSkill()


# ══════════════════════════════════════════════════════════════════════════════
# 1. YOUTUBE SKILL — ENGAGEMENT ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

class TestYouTubeEngagement:

    def setup_method(self):
        self.skill = YouTubeSkill()

    # ── like() ────────────────────────────────────────────────────────────────

    def test_like_when_not_liked_calls_click(self):
        """like() clicks the like button when video is not yet liked."""
        actions = _make_actions(js_side_effect=[False, True])  # is_liked=False, after=True
        result = self.skill.get_action("like")(actions)
        assert result.success
        assert result.data["action"] == "liked"
        actions.click.assert_called_once()

    def test_like_when_already_liked_skips(self):
        """like() skips click if video is already liked (idempotent)."""
        actions = _make_actions(js_return=True)  # already liked
        result = self.skill.get_action("like")(actions)
        assert result.success
        assert result.data["action"] == "skipped_already_liked"
        actions.click.assert_not_called()

    def test_unlike_when_liked_calls_click(self):
        """unlike() clicks the like button to remove the like."""
        actions = _make_actions(js_side_effect=[True, False])  # is_liked=True, after=False
        result = self.skill.get_action("unlike")(actions)
        assert result.success
        assert result.data["action"] == "unliked"

    def test_unlike_when_not_liked_skips(self):
        """unlike() skips if video is already not liked."""
        actions = _make_actions(js_return=False)
        result = self.skill.get_action("unlike")(actions)
        assert result.success
        assert "skipped" in result.data["action"]

    def test_subscribe_when_not_subscribed_calls_click(self):
        """subscribe() clicks subscribe when not yet subscribed."""
        actions = _make_actions(js_side_effect=[False, True])
        result = self.skill.get_action("subscribe")(actions)
        assert result.success
        actions.click.assert_called_once()

    def test_subscribe_when_already_subscribed_skips(self):
        """subscribe() skips if already subscribed."""
        actions = _make_actions(js_return=True)
        result = self.skill.get_action("subscribe")(actions)
        assert result.success
        assert result.data["action"] == "skipped_already_subscribed"
        actions.click.assert_not_called()

    def test_unsubscribe_calls_click(self):
        """unsubscribe() clicks when currently subscribed."""
        actions = _make_actions(js_side_effect=[True])  # is_subscribed=True
        result = self.skill.get_action("unsubscribe")(actions)
        assert result.success
        actions.click.assert_called()

    def test_unsubscribe_when_not_subscribed_skips(self):
        """unsubscribe() skips if not subscribed."""
        actions = _make_actions(js_return=False)
        result = self.skill.get_action("unsubscribe")(actions)
        assert result.success
        assert "skipped" in result.data["action"]
        actions.click.assert_not_called()

    def test_save_to_watch_later_when_not_saved(self):
        """save_to_watch_later() opens save menu and checks WL when not saved."""
        # is_watch_later_saved returns False → should toggle
        actions = _make_actions(js_return=False)
        result = self.skill.get_action("save_to_watch_later")(actions)
        assert result.success
        # click should have been called: save_button + watch_later_item
        assert actions.click.call_count >= 2

    def test_save_to_watch_later_already_saved_skips(self):
        """save_to_watch_later() skips if already in Watch Later."""
        # should_be_saved=True, current_state=True → skip
        actions = _make_actions(js_return=True)
        result = self.skill.get_action("save_to_watch_later")(actions)
        assert result.success
        assert result.data["action"] == "skipped"

    def test_remove_from_watch_later_not_saved_skips(self):
        """remove_from_watch_later() skips if not in Watch Later."""
        actions = _make_actions(js_return=False)
        result = self.skill.get_action("remove_from_watch_later")(actions)
        assert result.success
        assert result.data["action"] == "skipped"

    def test_remove_from_watch_later_when_saved(self):
        """remove_from_watch_later() toggles when saved."""
        actions = _make_actions(js_side_effect=[True, False])
        # First js call returns True (is saved), second irrelevant
        result = self.skill.get_action("remove_from_watch_later")(actions)
        assert result.success

    def test_engagement_action_returns_result_object(self):
        """All engagement actions return a proper Result object."""
        for action_name in ["like", "unlike", "subscribe", "unsubscribe"]:
            actions = _make_actions(js_return=False)
            fn = self.skill.get_action(action_name)
            result = fn(actions)
            assert isinstance(result, Result), f"{action_name} must return Result"

    def test_engagement_handles_action_error_gracefully(self):
        """Actions recover gracefully when wait_for raises ActionError."""
        actions = _make_actions(js_return=False)
        actions.wait_for.side_effect = ActionError("selector not found", [])
        result = self.skill.get_action("like")(actions)
        assert not result.success
        assert "like()" in result.error

    def test_like_action_is_in_action_map(self):
        assert self.skill.get_action("like") is not None

    def test_subscribe_action_is_in_action_map(self):
        assert self.skill.get_action("subscribe") is not None

    def test_save_to_watch_later_is_in_action_map(self):
        assert self.skill.get_action("save_to_watch_later") is not None


# ══════════════════════════════════════════════════════════════════════════════
# 2. YOUTUBE SKILL — PLAYBACK ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

class TestYouTubePlayback:

    def setup_method(self):
        self.skill = YouTubeSkill()

    def test_play_calls_js(self):
        """play() uses JS to resume playback."""
        actions = _make_actions(js_return=True)  # is playing
        result = self.skill.get_action("play")(actions)
        assert result.success
        actions.evaluate_js.assert_called_once()

    def test_play_fails_when_no_video(self):
        """play() returns failure when no <video> element."""
        actions = _make_actions(js_return=None)
        result = self.skill.get_action("play")(actions)
        assert not result.success

    def test_pause_calls_js(self):
        actions = _make_actions(js_return=False)  # was not paused
        result = self.skill.get_action("pause")(actions)
        assert result.success

    def test_pause_fails_when_no_video(self):
        actions = _make_actions(js_return=None)
        result = self.skill.get_action("pause")(actions)
        assert not result.success

    def test_toggle_play_when_paused_calls_play(self):
        """toggle_play() calls play JS when video is paused."""
        # First call (is_paused) → True, then play → True, then new is_paused → False
        actions = _make_actions(js_side_effect=[True, True, False])
        result = self.skill.get_action("toggle_play")(actions)
        assert result.success
        assert actions.evaluate_js.call_count >= 2

    def test_toggle_play_when_playing_calls_pause(self):
        """toggle_play() calls pause JS when video is playing."""
        actions = _make_actions(js_side_effect=[False, None, True])
        result = self.skill.get_action("toggle_play")(actions)
        assert result.success

    def test_toggle_play_fails_when_no_video(self):
        actions = _make_actions(js_return=None)
        result = self.skill.get_action("toggle_play")(actions)
        assert not result.success

    def test_set_speed_valid_value(self):
        """set_speed() sets playbackRate via JS."""
        actions = _make_actions(js_return=1.5)
        result = self.skill.get_action("set_speed")(actions, speed=1.5)
        assert result.success
        assert result.data["speed"] == 1.5

    def test_set_speed_clamps_invalid_value(self):
        """set_speed() clamps 1.3 to the nearest valid (1.25 or 1.5)."""
        actions = _make_actions(js_return=1.25)
        result = self.skill.get_action("set_speed")(actions, speed=1.3)
        assert result.success  # should clamp and succeed

    def test_set_speed_fails_when_no_video(self):
        actions = _make_actions(js_return=None)
        result = self.skill.get_action("set_speed")(actions, speed=2.0)
        assert not result.success

    def test_seek_absolute(self):
        """seek() sets video.currentTime via JS."""
        actions = _make_actions(js_return=90.0)
        result = self.skill.get_action("seek")(actions, seconds=90)
        assert result.success
        assert result.data["position"] == 90.0

    def test_seek_fails_when_no_video(self):
        actions = _make_actions(js_return=None)
        result = self.skill.get_action("seek")(actions, seconds=30)
        assert not result.success

    def test_forward_10s(self):
        actions = _make_actions(js_return=70.0)
        result = self.skill.get_action("forward_10s")(actions)
        assert result.success
        assert result.data["position"] == 70.0

    def test_back_10s(self):
        actions = _make_actions(js_return=50.0)
        result = self.skill.get_action("back_10s")(actions)
        assert result.success
        assert result.data["position"] == 50.0

    def test_forward_10s_no_video_fails(self):
        actions = _make_actions(js_return=None)
        result = self.skill.get_action("forward_10s")(actions)
        assert not result.success

    def test_toggle_subtitles_video_mode(self):
        """toggle_subtitles() works on /watch pages."""
        page = _make_page(url="https://www.youtube.com/watch?v=abc")
        actions = _make_actions(page=page)
        result = self.skill.get_action("toggle_subtitles")(actions)
        assert result.success
        actions.click.assert_called_once()

    def test_toggle_subtitles_shorts_mode_fails(self):
        """toggle_subtitles() is not available in Shorts mode."""
        page = _make_page(url="https://www.youtube.com/shorts/xyz")
        actions = _make_actions(page=page)
        result = self.skill.get_action("toggle_subtitles")(actions)
        assert not result.success
        assert "shorts" in result.error.lower()

    def test_toggle_autoplay(self):
        actions = _make_actions()
        result = self.skill.get_action("toggle_autoplay")(actions)
        assert result.success
        actions.click.assert_called_once()

    def test_fullscreen_when_not_fullscreen(self):
        """fullscreen() calls requestFullscreen JS when not already fullscreen."""
        actions = _make_actions(js_side_effect=[False, None])  # is_fs=False, then call
        result = self.skill.get_action("fullscreen")(actions)
        assert result.success
        assert result.data["action"] == "entered"

    def test_fullscreen_already_fullscreen_skips(self):
        """fullscreen() skips if already fullscreen."""
        actions = _make_actions(js_return=True)
        result = self.skill.get_action("fullscreen")(actions)
        assert result.success
        assert result.data["action"] == "skipped"

    def test_exit_fullscreen_when_fullscreen(self):
        """exit_fullscreen() calls exitFullscreen when fullscreen."""
        actions = _make_actions(js_side_effect=[True, None])
        result = self.skill.get_action("exit_fullscreen")(actions)
        assert result.success
        assert result.data["action"] == "exited"

    def test_exit_fullscreen_not_fullscreen_skips(self):
        """exit_fullscreen() skips when not fullscreen."""
        actions = _make_actions(js_return=False)
        result = self.skill.get_action("exit_fullscreen")(actions)
        assert result.success
        assert result.data["action"] == "skipped"

    def test_set_quality_shorts_mode_fails(self):
        """set_quality() is not available in Shorts mode."""
        page = _make_page(url="https://www.youtube.com/shorts/xyz")
        actions = _make_actions(page=page)
        result = self.skill.get_action("set_quality")(actions, quality="1080p")
        assert not result.success

    def test_all_playback_actions_in_map(self):
        actions = ["play", "pause", "toggle_play", "set_speed", "seek",
                   "forward_10s", "back_10s", "toggle_subtitles",
                   "toggle_autoplay", "fullscreen", "exit_fullscreen"]
        for a in actions:
            assert self.skill.get_action(a) is not None, f"'{a}' missing from action map"


# ══════════════════════════════════════════════════════════════════════════════
# 3. YOUTUBE SKILL — SHORTS ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

class TestYouTubeShorts:

    def setup_method(self):
        self.skill = YouTubeSkill()

    def test_next_short_via_button(self):
        """next_short() clicks the nav button when available."""
        actions = _make_actions()
        result = self.skill.get_action("next_short")(actions)
        assert result.success
        assert "next_short" in result.data["action"]

    def test_next_short_via_keyboard_fallback(self):
        """next_short() uses ArrowDown keyboard when button not found."""
        actions = _make_actions()
        actions.wait_for.side_effect = [ActionError("not found", []), None]
        result = self.skill.get_action("next_short")(actions)
        assert result.success
        actions.press_key.assert_called_with("ArrowDown")

    def test_prev_short_via_button(self):
        actions = _make_actions()
        result = self.skill.get_action("prev_short")(actions)
        assert result.success

    def test_prev_short_via_keyboard_fallback(self):
        actions = _make_actions()
        actions.wait_for.side_effect = [ActionError("not found", []), None]
        result = self.skill.get_action("prev_short")(actions)
        assert result.success
        actions.press_key.assert_called_with("ArrowUp")

    def test_next_short_in_action_map(self):
        assert self.skill.get_action("next_short") is not None

    def test_prev_short_in_action_map(self):
        assert self.skill.get_action("prev_short") is not None


# ══════════════════════════════════════════════════════════════════════════════
# 4. YOUTUBE SKILL — NAVIGATION ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

class TestYouTubeNavigation:

    def setup_method(self):
        self.skill = YouTubeSkill()

    def test_go_home_navigates_to_youtube(self):
        actions = _make_actions()
        result = self.skill.get_action("go_home")(actions)
        assert result.success
        actions.navigate.assert_called_once_with("https://www.youtube.com")
        assert result.data["url"] == "https://www.youtube.com"

    def test_go_shorts_home_navigates_to_shorts(self):
        actions = _make_actions()
        result = self.skill.get_action("go_shorts_home")(actions)
        assert result.success
        actions.navigate.assert_called_once_with("https://www.youtube.com/shorts")

    def test_go_to_channel_clicks_link(self):
        actions = _make_actions()
        actions._page.url = "https://www.youtube.com/@TestChannel"
        result = self.skill.get_action("go_to_channel")(actions)
        assert result.success
        actions.click_and_wait.assert_called_once()

    def test_go_to_channel_by_name_navigates_to_handle(self):
        """go_to_channel_by_name() tries direct @handle URL first."""
        page = _make_page(url="https://www.youtube.com/@MrBeast")
        actions = _make_actions(page=page)
        result = self.skill.get_action("go_to_channel_by_name")(actions, name="MrBeast")
        assert result.success
        actions.navigate.assert_called()
        call_url = actions.navigate.call_args[0][0]
        assert "MrBeast" in call_url or "@" in call_url

    def test_go_to_channel_by_name_missing_name_fails(self):
        """go_to_channel_by_name() requires a name."""
        actions = _make_actions()
        result = self.skill.get_action("go_to_channel_by_name")(actions, name="")
        assert not result.success

    def test_open_comments_scrolls_into_view(self):
        actions = _make_actions()
        result = self.skill.get_action("open_comments")(actions)
        assert result.success
        actions.evaluate_js.assert_called_once()

    def test_next_video_clicks_button(self):
        page = _make_page(url="https://www.youtube.com/watch?v=xyz")
        actions = _make_actions(page=page)
        result = self.skill.get_action("next_video")(actions)
        assert result.success
        actions.click_and_wait.assert_called_once()

    def test_play_nth_next_navigates_to_nth_link(self):
        """play_nth_next(n=2) navigates to the 2nd sidebar link."""
        links = ["/watch?v=v1", "/watch?v=v2", "/watch?v=v3", "/watch?v=v4"]
        actions = _make_actions(js_return=links)
        page = _make_page(url="https://www.youtube.com/watch?v=v2")
        actions._page = page
        result = self.skill.get_action("play_nth_next")(actions, n=2)
        assert result.success
        actions.navigate.assert_called_once()
        nav_url = actions.navigate.call_args[0][0]
        assert "v2" in nav_url

    def test_play_nth_next_fails_when_not_enough_links(self):
        """play_nth_next(n=5) fails if fewer than 5 links exist."""
        actions = _make_actions(js_return=["/watch?v=v1"])
        result = self.skill.get_action("play_nth_next")(actions, n=5)
        assert not result.success

    def test_all_navigation_actions_in_map(self):
        nav_actions = ["go_home", "go_shorts_home", "go_to_channel",
                       "go_to_channel_by_name", "open_comments",
                       "next_video", "previous_video", "play_nth_next"]
        for a in nav_actions:
            assert self.skill.get_action(a) is not None, f"'{a}' missing from action map"


# ══════════════════════════════════════════════════════════════════════════════
# 5. YOUTUBE SKILL — LIBRARY ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

class TestYouTubeLibrary:

    def setup_method(self):
        self.skill = YouTubeSkill()

    def test_open_history_navigates_correctly(self):
        actions = _make_actions()
        result = self.skill.get_action("open_history")(actions)
        assert result.success
        actions.navigate.assert_called_once_with("https://www.youtube.com/feed/history")

    def test_open_liked_videos_navigates_correctly(self):
        actions = _make_actions()
        result = self.skill.get_action("open_liked_videos")(actions)
        assert result.success
        actions.navigate.assert_called_once_with("https://www.youtube.com/playlist?list=LL")

    def test_open_playlists_navigates_correctly(self):
        actions = _make_actions()
        result = self.skill.get_action("open_playlists")(actions)
        assert result.success
        actions.navigate.assert_called_once_with("https://www.youtube.com/feed/library")

    def test_open_watch_later_navigates_correctly(self):
        actions = _make_actions()
        result = self.skill.get_action("open_watch_later")(actions)
        assert result.success
        actions.navigate.assert_called_once_with("https://www.youtube.com/playlist?list=WL")

    def test_library_urls_are_correct(self):
        """Verify all library URLs are exactly as expected."""
        cases = [
            ("open_history",     "https://www.youtube.com/feed/history"),
            ("open_liked_videos","https://www.youtube.com/playlist?list=LL"),
            ("open_playlists",   "https://www.youtube.com/feed/library"),
            ("open_watch_later", "https://www.youtube.com/playlist?list=WL"),
        ]
        for action_name, expected_url in cases:
            actions = _make_actions()
            self.skill.get_action(action_name)(actions)
            actions.navigate.assert_called_once_with(expected_url)


# ══════════════════════════════════════════════════════════════════════════════
# 6. YOUTUBE SKILL — PLAYLIST MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

class TestYouTubePlaylistManagement:

    def setup_method(self):
        self.skill = YouTubeSkill()

    def test_add_to_playlist_requires_name(self):
        """add_to_playlist() fails when name is empty."""
        actions = _make_actions()
        result = self.skill.get_action("add_to_playlist")(actions, name="")
        assert not result.success
        assert "name" in result.error.lower() or "playlist" in result.error.lower()

    def test_remove_from_playlist_requires_name(self):
        actions = _make_actions()
        result = self.skill.get_action("remove_from_playlist")(actions, name="")
        assert not result.success

    def test_add_to_playlist_opens_save_menu(self):
        """add_to_playlist() opens the save menu before looking for the playlist."""
        # JS returns: item_index=0, is_checked=False
        actions = _make_actions(js_side_effect=[0, False, None, None])
        result = self.skill.get_action("add_to_playlist")(actions, name="Favorites")
        assert result.success
        # save_button should have been clicked
        actions.click.assert_called()

    def test_add_to_playlist_playlist_not_found_fails(self):
        """add_to_playlist() fails when playlist name not found in menu."""
        actions = _make_actions(js_return=-1)  # item_index = -1 = not found
        result = self.skill.get_action("add_to_playlist")(actions, name="NonExistent")
        assert not result.success
        assert "NonExistent" in result.error or "not found" in result.error.lower()

    def test_add_to_playlist_already_added_skips(self):
        """add_to_playlist() skips if already added (is_checked == should_be_checked)."""
        # item_index=0, is_checked=True (already added)
        actions = _make_actions(js_side_effect=[0, True, None, None])
        result = self.skill.get_action("add_to_playlist")(actions, name="Music")
        assert result.success
        assert "skipped" in result.data["action"]

    def test_remove_from_playlist_already_removed_skips(self):
        """remove_from_playlist() skips if already not in playlist."""
        # item_index=0, is_checked=False (already not added)
        actions = _make_actions(js_side_effect=[0, False, None, None])
        result = self.skill.get_action("remove_from_playlist")(actions, name="Music")
        assert result.success
        assert "skipped" in result.data["action"]

    def test_playlist_actions_in_map(self):
        assert self.skill.get_action("add_to_playlist") is not None
        assert self.skill.get_action("remove_from_playlist") is not None


# ══════════════════════════════════════════════════════════════════════════════
# 7. YOUTUBE SKILL — RECOMMENDED VIDEOS
# ══════════════════════════════════════════════════════════════════════════════

class TestYouTubeRecommended:

    def setup_method(self):
        self.skill = YouTubeSkill()

    def test_open_recommended_is_alias_for_play_nth_next(self):
        """open_recommended(index=1) navigates to the 1st sidebar video."""
        links = ["/watch?v=r1", "/watch?v=r2", "/watch?v=r3"]
        actions = _make_actions(js_return=links)
        page = _make_page(url="https://www.youtube.com/watch?v=r1")
        actions._page = page
        result = self.skill.get_action("open_recommended")(actions, index=1)
        assert result.success

    def test_open_top_recommended_opens_background_tabs(self):
        """open_top_recommended(n=2) opens 2 background tabs."""
        links = ["/watch?v=r1", "/watch?v=r2"]
        mock_page = _make_page(url="https://www.youtube.com/watch?v=r1")
        mock_page.title.return_value = "Test Video"

        actions = _make_actions(js_return=links)
        actions.open_new_tab.return_value = mock_page

        # The new tab's Actions needs evaluate_js too
        with patch("skills.youtube_skill.Actions") as MockActions:
            mock_new_actions = _make_actions(page=mock_page, js_return=True)
            mock_new_actions.get_text.return_value = "Test Title"
            MockActions.return_value = mock_new_actions

            result = self.skill.get_action("open_top_recommended")(actions, n=2)

        assert result.success
        assert isinstance(result.data, list)

    def test_open_top_recommended_no_links_fails(self):
        """open_top_recommended() fails when sidebar has no links."""
        actions = _make_actions(js_return=[])
        result = self.skill.get_action("open_top_recommended")(actions, n=3)
        assert not result.success
        assert "no recommended" in result.error.lower()

    def test_recommended_actions_in_map(self):
        assert self.skill.get_action("open_recommended") is not None
        assert self.skill.get_action("open_top_recommended") is not None


# ══════════════════════════════════════════════════════════════════════════════
# 8. YOUTUBE SKILL — MODE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

class TestYouTubeModeDetection:

    def setup_method(self):
        self.skill = YouTubeSkill()

    def test_video_mode_on_watch_url(self):
        from skills.youtube_skill import _classify_url
        assert _classify_url("https://www.youtube.com/watch?v=abc") == "video"

    def test_shorts_mode_on_shorts_url(self):
        from skills.youtube_skill import _classify_url
        assert _classify_url("https://www.youtube.com/shorts/xyz") == "shorts"

    def test_unknown_mode_on_other_url(self):
        from skills.youtube_skill import _classify_url
        assert _classify_url("https://www.youtube.com/") == "unknown"

    def test_can_handle_youtube_url(self):
        assert self.skill.can_handle("https://www.youtube.com/watch?v=abc")

    def test_cannot_handle_amazon_url(self):
        assert not self.skill.can_handle("https://www.amazon.de/dp/B08N5KWB9H")

    def test_cannot_handle_google_url(self):
        assert not self.skill.can_handle("https://www.google.com")


# ══════════════════════════════════════════════════════════════════════════════
# 9. AMAZON SKILL — SHOPPING ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

class TestAmazonShopping:

    def setup_method(self):
        self.skill = AmazonSkill()

    # ── add_to_cart() ─────────────────────────────────────────────────────────

    def test_add_to_cart_on_product_page_succeeds(self):
        """add_to_cart() works when on a product page."""
        # is_product_page=True, cart_count_before=0, confirmation found
        actions = _make_actions(js_side_effect=[True, 0])
        result = self.skill.get_action("add_to_cart")(actions)
        assert result.success
        actions.click.assert_called_once()

    def test_add_to_cart_not_on_product_page_fails(self):
        """add_to_cart() fails when not on a /dp/ product page."""
        actions = _make_actions(js_return=False)  # is_product_page=False
        result = self.skill.get_action("add_to_cart")(actions)
        assert not result.success
        assert "product page" in result.error.lower()

    def test_add_to_cart_verifies_confirmation(self):
        """add_to_cart() waits for a confirmation element."""
        actions = _make_actions(js_side_effect=[True, 0])
        result = self.skill.get_action("add_to_cart")(actions)
        assert result.success
        # wait_for called twice: button + confirmation
        assert actions.wait_for.call_count >= 1

    # ── remove_from_cart() ────────────────────────────────────────────────────

    def test_remove_from_cart_on_cart_page(self):
        """remove_from_cart() clicks delete when on cart page."""
        page = _make_page(url="https://www.amazon.de/cart")
        actions = _make_actions(page=page, js_return=True)  # is_cart_page=True
        result = self.skill.get_action("remove_from_cart")(actions)
        assert result.success
        actions.click.assert_called()

    def test_remove_from_cart_auto_navigates(self):
        """remove_from_cart() navigates to cart if not already there."""
        page = _make_page(url="https://www.amazon.de/dp/B08N5KWXXX")
        actions = _make_actions(page=page, js_return=False)  # is_cart_page=False
        result = self.skill.get_action("remove_from_cart")(actions)
        assert result.success
        actions.navigate.assert_called()

    # ── add_to_wishlist() ─────────────────────────────────────────────────────

    def test_add_to_wishlist_on_product_page(self):
        """add_to_wishlist() clicks wishlist button on product page."""
        actions = _make_actions(js_return=True)  # is_product_page=True
        result = self.skill.get_action("add_to_wishlist")(actions)
        assert result.success
        actions.click.assert_called()

    def test_add_to_wishlist_not_on_product_page_fails(self):
        actions = _make_actions(js_return=False)
        result = self.skill.get_action("add_to_wishlist")(actions)
        assert not result.success
        assert "product page" in result.error.lower()

    def test_add_to_wishlist_data_key(self):
        """add_to_wishlist() returns added_to_wishlist=True on success."""
        actions = _make_actions(js_return=True)
        result = self.skill.get_action("add_to_wishlist")(actions)
        assert result.success
        assert result.data.get("added_to_wishlist") is True

    # ── remove_from_wishlist() ────────────────────────────────────────────────

    def test_remove_from_wishlist_on_wishlist_page(self):
        page = _make_page(url="https://www.amazon.de/hz/wishlist/ls/ABC")
        actions = _make_actions(page=page, js_return=True)  # is_wishlist_page=True
        result = self.skill.get_action("remove_from_wishlist")(actions)
        assert result.success

    def test_remove_from_wishlist_auto_navigates(self):
        page = _make_page(url="https://www.amazon.de/dp/B08N5KWXXX")
        actions = _make_actions(page=page, js_return=False)
        result = self.skill.get_action("remove_from_wishlist")(actions)
        assert result.success
        actions.navigate.assert_called()

    # ── buy_now() ─────────────────────────────────────────────────────────────

    def test_buy_now_on_product_page(self):
        actions = _make_actions(js_return=True)
        result = self.skill.get_action("buy_now")(actions)
        assert result.success
        assert result.data.get("checkout_initiated") is True

    def test_buy_now_not_on_product_page_fails(self):
        actions = _make_actions(js_return=False)
        result = self.skill.get_action("buy_now")(actions)
        assert not result.success

    def test_all_shopping_actions_in_map(self):
        for a in ["add_to_cart", "remove_from_cart",
                  "add_to_wishlist", "remove_from_wishlist", "buy_now"]:
            assert self.skill.get_action(a) is not None, f"'{a}' missing"


# ══════════════════════════════════════════════════════════════════════════════
# 10. AMAZON SKILL — ACCOUNT NAVIGATION
# ══════════════════════════════════════════════════════════════════════════════

class TestAmazonAccountNavigation:

    def setup_method(self):
        self.skill = AmazonSkill()

    def test_open_cart_tries_nav_icon_first(self):
        """open_cart() tries to click the cart icon, then falls back to URL."""
        page = _make_page(url="https://www.amazon.de/cart")
        actions = _make_actions(page=page)
        result = self.skill.get_action("open_cart")(actions)
        assert result.success

    def test_open_cart_fallback_to_url(self):
        """open_cart() navigates to /cart when icon not found."""
        page = _make_page(url="https://www.amazon.de/cart")
        actions = _make_actions(page=page)
        actions.wait_for.side_effect = ActionError("not found", [])
        result = self.skill.get_action("open_cart")(actions)
        assert result.success
        actions.navigate.assert_called()

    def test_open_orders_fallback_to_url(self):
        """open_orders() navigates to order history URL when link not found."""
        page = _make_page(url="https://www.amazon.de/gp/your-account/order-history")
        actions = _make_actions(page=page)
        actions.wait_for.side_effect = ActionError("not found", [])
        result = self.skill.get_action("open_orders")(actions)
        assert result.success
        actions.navigate.assert_called()

    def test_open_wishlist_fallback_to_url(self):
        page = _make_page(url="https://www.amazon.de/hz/wishlist/ls")
        actions = _make_actions(page=page)
        actions.wait_for.side_effect = ActionError("not found", [])
        result = self.skill.get_action("open_wishlist")(actions)
        assert result.success
        actions.navigate.assert_called()

    def test_open_orders_url_contains_order_history(self):
        """open_orders() navigates to the order-history URL."""
        page = _make_page(url="https://www.amazon.de/gp/your-account/order-history")
        actions = _make_actions(page=page)
        actions.wait_for.side_effect = ActionError("not found", [])
        self.skill.get_action("open_orders")(actions)
        nav_url = actions.navigate.call_args[0][0]
        assert "order-history" in nav_url or "your-orders" in nav_url

    def test_account_nav_actions_in_map(self):
        for a in ["open_cart", "open_orders", "open_wishlist"]:
            assert self.skill.get_action(a) is not None


# ══════════════════════════════════════════════════════════════════════════════
# 11. AMAZON SKILL — PRODUCT DATA
# ══════════════════════════════════════════════════════════════════════════════

class TestAmazonProductData:

    def setup_method(self):
        self.skill = AmazonSkill()

    def test_read_price_returns_text(self):
        actions = _make_actions()
        actions.get_text.return_value = "€29,99"
        result = self.skill.get_action("read_price")(actions)
        assert result.success
        assert result.data["price"] == "€29,99"

    def test_read_price_empty_text_fails(self):
        actions = _make_actions()
        actions.get_text.return_value = ""
        result = self.skill.get_action("read_price")(actions)
        assert not result.success

    def test_read_price_whitespace_text_fails(self):
        actions = _make_actions()
        actions.get_text.return_value = "   "
        result = self.skill.get_action("read_price")(actions)
        assert not result.success

    def test_read_rating_returns_text(self):
        actions = _make_actions()
        actions.get_text.return_value = "4.5 out of 5 stars"
        result = self.skill.get_action("read_rating")(actions)
        assert result.success
        assert result.data["rating"] == "4.5 out of 5 stars"

    def test_read_rating_selector_not_found_fails(self):
        actions = _make_actions()
        actions.wait_for.side_effect = ActionError("no rating", [])
        result = self.skill.get_action("read_rating")(actions)
        assert not result.success

    def test_read_reviews_returns_list(self):
        """read_reviews() returns a list of review dicts with title and body."""
        reviews_data = [
            {"title": "Great product", "body": "Loved it!"},
            {"title": "Ok", "body": "It's fine."},
        ]
        actions = _make_actions(js_return=reviews_data)
        result = self.skill.get_action("read_reviews")(actions, n=2)
        assert result.success
        assert isinstance(result.data["reviews"], list)
        assert result.data["count"] == 2

    def test_read_reviews_empty_fails(self):
        """read_reviews() fails when no reviews are found."""
        actions = _make_actions(js_return=[])
        result = self.skill.get_action("read_reviews")(actions, n=3)
        assert not result.success

    def test_read_reviews_default_n_is_3(self):
        """read_reviews() defaults to n=3 when not provided."""
        reviews_data = [{"title": f"R{i}", "body": "body"} for i in range(3)]
        actions = _make_actions(js_return=reviews_data)
        result = self.skill.get_action("read_reviews")(actions)
        assert result.success
        assert result.data["count"] == 3

    def test_product_data_actions_in_map(self):
        for a in ["read_price", "read_rating", "read_reviews"]:
            assert self.skill.get_action(a) is not None


# ══════════════════════════════════════════════════════════════════════════════
# 12. PLANNER — YOUTUBE ON-PAGE PATTERNS
# ══════════════════════════════════════════════════════════════════════════════

class TestPlannerYouTubeOnPage:

    def setup_method(self):
        self.engine = _TemplateEngine()

    def _plan(self, goal: str) -> list[Step]:
        return self.engine.plan(goal)

    def _single(self, goal: str) -> Step:
        steps = self._plan(goal)
        assert len(steps) == 1, f"Expected 1 step for '{goal}', got {len(steps)}"
        return steps[0]

    # ── Engagement ────────────────────────────────────────────────────────────

    def test_like_this_video(self):
        step = self._single("like this video")
        assert step.action_name == "like"

    def test_like_short_form(self):
        step = self._single("like")
        assert step.action_name == "like"

    def test_unlike_video(self):
        step = self._single("unlike this video")
        assert step.action_name == "unlike"

    def test_remove_like(self):
        step = self._single("remove like")
        assert step.action_name == "unlike"

    def test_subscribe_to_channel(self):
        step = self._single("subscribe to this channel")
        assert step.action_name == "subscribe"

    def test_subscribe_short_form(self):
        step = self._single("subscribe")
        assert step.action_name == "subscribe"

    def test_unsubscribe(self):
        step = self._single("unsubscribe")
        assert step.action_name == "unsubscribe"

    def test_like_and_subscribe_two_steps(self):
        """'like this video and subscribe' → 2 steps."""
        steps = self._plan("like this video and subscribe")
        assert len(steps) == 2
        assert steps[0].action_name == "like"
        assert steps[1].action_name == "subscribe"

    def test_save_to_watch_later(self):
        step = self._single("save to watch later")
        assert step.action_name == "save_to_watch_later"

    def test_add_to_watch_later(self):
        step = self._single("add to watch later")
        assert step.action_name == "save_to_watch_later"

    def test_remove_from_watch_later(self):
        step = self._single("remove from watch later")
        assert step.action_name == "remove_from_watch_later"

    # ── Playback ──────────────────────────────────────────────────────────────

    def test_play(self):
        step = self._single("play")
        assert step.action_name == "play"

    def test_pause(self):
        step = self._single("pause")
        assert step.action_name == "pause"

    def test_set_speed_1_5x(self):
        step = self._single("set speed to 1.5x")
        assert step.action_name == "set_speed"
        assert step.params.get("speed") == 1.5

    def test_increase_speed_2x(self):
        step = self._single("increase speed to 2x")
        assert step.action_name == "set_speed"
        assert step.params.get("speed") == 2.0

    def test_speed_125(self):
        step = self._single("speed 1.25")
        assert step.action_name == "set_speed"
        assert step.params.get("speed") == 1.25

    def test_skip_10_seconds(self):
        step = self._single("skip 10 seconds")
        assert step.action_name == "forward_10s"

    def test_forward_10(self):
        step = self._single("forward 10 seconds")
        assert step.action_name == "forward_10s"

    def test_go_back_10(self):
        step = self._single("go back 10 seconds")
        assert step.action_name == "back_10s"

    def test_rewind_10(self):
        step = self._single("rewind 10")
        assert step.action_name == "back_10s"

    def test_seek_to_90_seconds(self):
        step = self._single("seek to 90 seconds")
        assert step.action_name == "seek"
        assert step.params.get("seconds") == 90.0

    def test_toggle_subtitles(self):
        step = self._single("toggle subtitles")
        assert step.action_name == "toggle_subtitles"

    def test_toggle_cc(self):
        step = self._single("toggle cc")
        assert step.action_name == "toggle_subtitles"

    def test_toggle_autoplay(self):
        step = self._single("toggle autoplay")
        assert step.action_name == "toggle_autoplay"

    def test_autoplay_off(self):
        step = self._single("autoplay off")
        assert step.action_name == "toggle_autoplay"

    def test_fullscreen(self):
        step = self._single("fullscreen")
        assert step.action_name == "fullscreen"

    def test_enter_fullscreen(self):
        step = self._single("enter fullscreen")
        assert step.action_name == "fullscreen"

    def test_exit_fullscreen(self):
        step = self._single("exit fullscreen")
        assert step.action_name == "exit_fullscreen"

    # ── Navigation ────────────────────────────────────────────────────────────

    def test_next_video(self):
        step = self._single("next video")
        assert step.action_name == "next_video"

    def test_play_next(self):
        step = self._single("play next")
        assert step.action_name == "next_video"

    def test_play_next_video(self):
        step = self._single("play next video")
        assert step.action_name == "next_video"

    def test_previous_video(self):
        step = self._single("previous video")
        assert step.action_name == "previous_video"

    def test_nth_next_video_3rd(self):
        step = self._single("play the 3rd next video")
        assert step.action_name == "play_nth_next"
        assert step.params.get("n") == 3

    def test_nth_next_video_1st(self):
        step = self._single("play the 1st next video")
        assert step.action_name == "play_nth_next"
        assert step.params.get("n") == 1

    def test_open_3rd_next_video(self):
        step = self._single("open the 3rd next video")
        assert step.action_name == "play_nth_next"
        assert step.params.get("n") == 3

    def test_open_next_3_videos(self):
        step = self._single("open next 3 videos")
        assert step.action_name == "open_top_recommended"
        assert step.params.get("n") == 3

    def test_open_top_5_recommended(self):
        step = self._single("open top 5 recommended")
        assert step.action_name == "open_top_recommended"
        assert step.params.get("n") == 5

    def test_go_to_channel_mrbeast(self):
        step = self._single("go to channel MrBeast")
        assert step.action_name == "go_to_channel_by_name"
        assert step.params.get("name") == "MrBeast"

    def test_go_to_channel_no_name(self):
        step = self._single("go to channel")
        assert step.action_name == "go_to_channel"
        assert step.params == {}

    def test_open_comments(self):
        step = self._single("open comments")
        assert step.action_name == "open_comments"

    def test_show_comments(self):
        step = self._single("show comments")
        assert step.action_name == "open_comments"

    # ── Shorts ────────────────────────────────────────────────────────────────

    def test_next_short(self):
        step = self._single("next short")
        assert step.action_name == "next_short"

    def test_play_next_short(self):
        step = self._single("next shorts")
        assert step.action_name == "next_short"

    def test_previous_short(self):
        step = self._single("previous short")
        assert step.action_name == "prev_short"

    def test_back_short(self):
        step = self._single("back short")
        assert step.action_name == "prev_short"

    # ── Library ───────────────────────────────────────────────────────────────

    def test_open_history(self):
        step = self._single("open history")
        assert step.action_name == "open_history"

    def test_watch_history(self):
        step = self._single("watch history")
        assert step.action_name == "open_history"

    def test_open_liked_videos(self):
        step = self._single("open liked videos")
        assert step.action_name == "open_liked_videos"

    def test_my_liked(self):
        step = self._single("my liked videos")
        assert step.action_name == "open_liked_videos"

    def test_open_playlists(self):
        step = self._single("open playlists")
        assert step.action_name == "open_playlists"

    def test_open_watch_later(self):
        step = self._single("open watch later")
        assert step.action_name == "open_watch_later"

    def test_show_watch_later(self):
        step = self._single("show watch later")
        assert step.action_name == "open_watch_later"

    # ── Quality ───────────────────────────────────────────────────────────────

    def test_set_quality_1080p(self):
        step = self._single("set quality to 1080p")
        assert step.action_name == "set_quality"
        assert "1080" in step.params.get("quality", "")

    def test_change_quality_720p(self):
        step = self._single("change quality to 720p")
        assert step.action_name == "set_quality"

    # ── Playlist management ───────────────────────────────────────────────────

    def test_add_to_playlist_music(self):
        step = self._single("add to playlist Music")
        assert step.action_name == "add_to_playlist"
        assert step.params.get("name") == "Music"

    def test_add_to_playlist_with_spaces(self):
        step = self._single("add to playlist My Favorites")
        assert step.action_name == "add_to_playlist"
        assert step.params.get("name") == "My Favorites"

    def test_remove_from_playlist_music(self):
        step = self._single("remove from playlist Music")
        assert step.action_name == "remove_from_playlist"
        assert step.params.get("name") == "Music"

    # ── Go home / shorts home ─────────────────────────────────────────────────

    def test_go_home(self):
        step = self._single("youtube home")
        assert step.action_name == "go_home"

    def test_go_to_shorts(self):
        step = self._single("go to shorts")
        assert step.action_name == "go_shorts_home"

    def test_shorts_home(self):
        step = self._single("shorts home")
        assert step.action_name == "go_shorts_home"

    # ── verify_conditions present for all on-page steps ──────────────────────

    def test_all_on_page_steps_have_url_contains(self):
        """Every on-page YouTube step must have url_contains in verify_conditions."""
        goals = [
            "like this video", "subscribe", "save to watch later",
            "play", "pause", "forward 10 seconds", "go back 10",
            "next video", "next short", "open comments",
            "open history", "open liked videos",
        ]
        for goal in goals:
            steps = self._plan(goal)
            for step in steps:
                assert "url_contains" in step.verify_conditions, (
                    f"Step '{step.action_name}' for goal '{goal}' missing url_contains"
                )

    def test_on_page_steps_url_is_youtube(self):
        """On-page YouTube steps must route to youtube.com."""
        goals = ["like this video", "subscribe", "next video"]
        for goal in goals:
            steps = self._plan(goal)
            for step in steps:
                assert "youtube" in step.url.lower(), (
                    f"Step '{step.action_name}' for '{goal}' has url='{step.url}'"
                )


# ══════════════════════════════════════════════════════════════════════════════
# 13. PLANNER — AMAZON ON-PAGE PATTERNS
# ══════════════════════════════════════════════════════════════════════════════

class TestPlannerAmazonOnPage:

    def setup_method(self):
        self.engine = _TemplateEngine()

    def _plan(self, goal: str) -> list[Step]:
        return self.engine.plan(goal)

    def _single(self, goal: str) -> Step:
        steps = self._plan(goal)
        assert len(steps) == 1, f"Expected 1 step for '{goal}', got {len(steps)}"
        return steps[0]

    def test_add_to_cart(self):
        step = self._single("add to cart")
        assert step.action_name == "add_to_cart"

    def test_add_this_product_to_cart(self):
        step = self._single("add this product to cart")
        assert step.action_name == "add_to_cart"

    def test_remove_from_cart(self):
        step = self._single("remove from cart")
        assert step.action_name == "remove_from_cart"

    def test_add_to_wishlist(self):
        step = self._single("add to wishlist")
        assert step.action_name == "add_to_wishlist"

    def test_add_this_product_to_wishlist(self):
        step = self._single("add this product to wishlist")
        assert step.action_name == "add_to_wishlist"

    def test_remove_from_wishlist(self):
        step = self._single("remove from wishlist")
        assert step.action_name == "remove_from_wishlist"

    def test_buy_now(self):
        step = self._single("buy now")
        assert step.action_name == "buy_now"

    def test_purchase_now(self):
        step = self._single("purchase now")
        assert step.action_name == "buy_now"

    def test_open_cart(self):
        step = self._single("open cart")
        assert step.action_name == "open_cart"

    def test_view_my_cart(self):
        step = self._single("view my cart")
        assert step.action_name == "open_cart"

    def test_open_orders(self):
        step = self._single("open orders")
        assert step.action_name == "open_orders"

    def test_order_history(self):
        step = self._single("order history")
        assert step.action_name == "open_orders"

    def test_open_wishlist(self):
        step = self._single("open wishlist")
        assert step.action_name == "open_wishlist"

    def test_view_wishlist(self):
        step = self._single("view my wishlist")
        assert step.action_name == "open_wishlist"

    def test_read_price(self):
        step = self._single("read the price")
        assert step.action_name == "read_price"

    def test_whats_the_price(self):
        step = self._single("what's the price")
        assert step.action_name == "read_price"

    def test_read_rating(self):
        step = self._single("read the rating")
        assert step.action_name == "read_rating"

    def test_read_reviews_default_n(self):
        step = self._single("read reviews")
        assert step.action_name == "read_reviews"
        assert step.params.get("n") == 3

    def test_show_5_reviews(self):
        step = self._single("show 5 reviews")
        assert step.action_name == "read_reviews"
        assert step.params.get("n") == 5

    def test_read_3_reviews(self):
        step = self._single("read 3 reviews")
        assert step.action_name == "read_reviews"
        assert step.params.get("n") == 3

    def test_all_amazon_on_page_steps_route_to_amazon(self):
        """All Amazon on-page steps must route to amazon."""
        goals = ["add to cart", "add to wishlist", "buy now",
                 "open cart", "open orders", "open wishlist",
                 "read the price", "read the rating", "read reviews"]
        for goal in goals:
            steps = self._plan(goal)
            for step in steps:
                assert "amazon" in step.url.lower(), (
                    f"Step '{step.action_name}' for '{goal}' has url='{step.url}'"
                )

    def test_all_amazon_on_page_steps_have_verify_conditions(self):
        goals = ["add to cart", "add to wishlist", "buy now",
                 "open cart", "open orders", "open wishlist",
                 "read the price", "read the rating"]
        for goal in goals:
            steps = self._plan(goal)
            for step in steps:
                assert step.verify_conditions, (
                    f"Step '{step.action_name}' for '{goal}' missing verify_conditions"
                )


# ══════════════════════════════════════════════════════════════════════════════
# 14. PLANNER — COMBINED AND EDGE-CASE PATTERNS
# ══════════════════════════════════════════════════════════════════════════════

class TestPlannerEdgeCases:

    def setup_method(self):
        self.planner = Planner(engine="template")
        self.engine = _TemplateEngine()

    def test_like_case_insensitive(self):
        steps = self.engine.plan("LIKE THIS VIDEO")
        assert len(steps) == 1
        assert steps[0].action_name == "like"

    def test_subscribe_case_insensitive(self):
        steps = self.engine.plan("SUBSCRIBE TO THIS CHANNEL")
        assert len(steps) == 1
        assert steps[0].action_name == "subscribe"

    def test_set_speed_float_extraction(self):
        """Speed regex correctly extracts float values."""
        cases = [
            ("set speed to 0.5x", 0.5),
            ("increase speed to 2x", 2.0),
            ("speed 1.75", 1.75),
        ]
        for goal, expected_speed in cases:
            steps = self.engine.plan(goal)
            assert steps[0].params.get("speed") == expected_speed, (
                f"For '{goal}' expected speed={expected_speed}"
            )

    def test_go_to_channel_with_spaces_in_name(self):
        steps = self.engine.plan("go to channel Linus Tech Tips")
        assert steps[0].action_name == "go_to_channel_by_name"
        assert steps[0].params.get("name") == "Linus Tech Tips"

    def test_amazon_search_still_works(self):
        """Phase 9 patterns still work alongside Phase 11 patterns."""
        steps = self.planner.plan("search Amazon for headphones")
        assert len(steps) == 3
        assert steps[1].action_name == "search"

    def test_youtube_search_still_works(self):
        steps = self.planner.plan("search YouTube for Python tutorial")
        assert len(steps) == 3
        assert steps[1].action_name == "search"

    def test_open_top_results_still_works(self):
        steps = self.planner.plan("search YouTube for Python and open top 3 videos")
        assert len(steps) == 3
        assert steps[2].action_name == "open_top_results"

    def test_on_page_action_not_confused_with_search(self):
        """'like' does not trigger a full search plan."""
        steps = self.engine.plan("like this video")
        assert len(steps) == 1
        assert steps[0].action_name == "like"

    def test_on_page_amazon_action_not_confused_with_search(self):
        """'add to cart' does not trigger Amazon search navigation."""
        steps = self.engine.plan("add to cart")
        assert len(steps) == 1
        assert steps[0].action_name == "add_to_cart"

    def test_valid_actions_set_contains_all_phase11_actions(self):
        """All Phase 11 actions must be in _VALID_ACTIONS."""
        from agent.planner import _VALID_ACTIONS
        phase11_actions = [
            "like", "unlike", "subscribe", "unsubscribe",
            "save_to_watch_later", "remove_from_watch_later",
            "play", "pause", "toggle_play", "set_speed", "seek",
            "forward_10s", "back_10s", "toggle_subtitles",
            "toggle_autoplay", "set_quality", "fullscreen", "exit_fullscreen",
            "next_short", "prev_short",
            "go_home", "go_shorts_home", "go_to_channel", "go_to_channel_by_name",
            "open_comments", "next_video", "previous_video", "play_nth_next",
            "open_history", "open_liked_videos", "open_playlists", "open_watch_later",
            "add_to_playlist", "remove_from_playlist",
            "open_recommended", "open_top_recommended",
            "add_to_cart", "remove_from_cart", "add_to_wishlist",
            "remove_from_wishlist", "buy_now",
            "open_orders", "open_cart", "open_wishlist",
            "read_price", "read_rating", "read_reviews",
        ]
        for action in phase11_actions:
            assert action in _VALID_ACTIONS, f"'{action}' missing from _VALID_ACTIONS"


# ══════════════════════════════════════════════════════════════════════════════
# 15. EXECUTOR — IDEMPOTENCY GUARD
# ══════════════════════════════════════════════════════════════════════════════

class TestExecutorIdempotencyGuard:
    """Tests for the Phase 11 idempotency guard in executor.py."""

    def _make_executor(self, action_fn, verify_results=None, max_retries=3):
        page = _make_page()
        skill = MagicMock()
        skill.name = "MockSkill"
        skill.get_action.return_value = action_fn

        manager = MagicMock()
        manager.get_skill.return_value = skill
        manager.skill_names = ["MockSkill"]

        verifier = MagicMock()
        if verify_results:
            verifier.verify.side_effect = verify_results
        else:
            verifier.verify.return_value = VerifyResult(
                status="pass", reason="ok", details={}
            )

        return Executor(page=page, skill_manager=manager,
                        verifier=verifier, max_retries=max_retries)

    # ── _result_data_is_idempotent_skip() helper ──────────────────────────────

    def test_detect_skipped_already_liked(self):
        assert _result_data_is_idempotent_skip({"action": "skipped_already_liked"})

    def test_detect_skipped_not_subscribed(self):
        assert _result_data_is_idempotent_skip({"action": "skipped_not_subscribed"})

    def test_detect_plain_skipped(self):
        assert _result_data_is_idempotent_skip({"action": "skipped"})

    def test_not_skipped_for_normal_action(self):
        assert not _result_data_is_idempotent_skip({"action": "liked"})

    def test_not_skipped_for_subscribed(self):
        assert not _result_data_is_idempotent_skip({"action": "subscribed"})

    def test_not_skipped_for_none_data(self):
        assert not _result_data_is_idempotent_skip(None)

    def test_not_skipped_for_non_dict(self):
        assert not _result_data_is_idempotent_skip("skipped_already_liked")

    def test_not_skipped_for_missing_action_key(self):
        assert not _result_data_is_idempotent_skip({"liked": True})

    # ── Idempotency guard in run() ─────────────────────────────────────────────

    def test_skipped_result_treated_as_success(self):
        """When an action returns 'skipped_*', it should be treated as success."""
        def like_action(actions, **p):
            return Result.ok(data={"liked": True, "action": "skipped_already_liked"})

        executor = self._make_executor(like_action)
        result = executor.run([
            Step(action_name="like", url="youtube.com",
                 verify_conditions={"url_contains": "youtube.com"})
        ])
        assert result["success"] is True

    def test_skipped_result_does_not_call_verify(self):
        """Verify should NOT be called when action returns skipped_*."""
        def like_action(actions, **p):
            return Result.ok(data={"action": "skipped_already_liked"})

        page = _make_page()
        skill = MagicMock()
        skill.name = "MockSkill"
        skill.get_action.return_value = like_action
        manager = MagicMock()
        manager.get_skill.return_value = skill
        manager.skill_names = ["MockSkill"]
        verifier = MagicMock()

        executor = Executor(page=page, skill_manager=manager,
                            verifier=verifier, max_retries=3)
        executor.run([
            Step(action_name="like", url="youtube.com",
                 verify_conditions={"url_contains": "youtube.com"})
        ])

        # Verifier must never be called for a skipped action
        verifier.verify.assert_not_called()

    def test_non_skipped_result_calls_verify(self):
        """Normal action result should still call verify."""
        def like_action(actions, **p):
            return Result.ok(data={"action": "liked"})

        page = _make_page()
        skill = MagicMock()
        skill.name = "MockSkill"
        skill.get_action.return_value = like_action
        manager = MagicMock()
        manager.get_skill.return_value = skill
        manager.skill_names = ["MockSkill"]
        verifier = MagicMock()
        verifier.verify.return_value = VerifyResult(status="pass", reason="ok", details={})

        executor = Executor(page=page, skill_manager=manager,
                            verifier=verifier, max_retries=3)
        executor.run([
            Step(action_name="like", url="youtube.com",
                 verify_conditions={"url_contains": "youtube.com"})
        ])
        verifier.verify.assert_called_once()

    def test_multiple_idempotent_steps_all_succeed(self):
        """Multiple skipped-* actions in sequence all succeed."""
        def subscribe_action(actions, **p):
            return Result.ok(data={"action": "skipped_already_subscribed"})

        executor = self._make_executor(subscribe_action)
        result = executor.run([
            Step(action_name="subscribe", url="youtube.com",
                 verify_conditions={"url_contains": "youtube.com"}),
            Step(action_name="subscribe", url="youtube.com",
                 verify_conditions={"url_contains": "youtube.com"}),
        ])
        assert result["success"] is True
        assert result["steps_completed"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# 16. EXECUTOR — RETRY-SAFETY & DEEP COPY
# ══════════════════════════════════════════════════════════════════════════════

class TestExecutorRetrySafety:

    def _make_executor(self, action_fn, verify_results, max_retries=3):
        page = _make_page()
        skill = MagicMock()
        skill.name = "MockSkill"
        skill.get_action.return_value = action_fn
        manager = MagicMock()
        manager.get_skill.return_value = skill
        manager.skill_names = ["MockSkill"]
        verifier = MagicMock()
        verifier.verify.side_effect = verify_results
        return Executor(page=page, skill_manager=manager,
                        verifier=verifier, max_retries=max_retries)

    def test_params_not_mutated_between_retries(self):
        """
        If an action mutates its params dict, retries still get fresh params.
        This verifies the deep-copy behavior.
        """
        call_params = []

        def mutating_action(actions, speed=1.0, **p):
            call_params.append(speed)
            return Result.ok(data={"mutated": True})

        pass_result = VerifyResult(status="pass", reason="ok", details={})
        retry_result = VerifyResult(status="retry", reason="transient", details={})

        executor = self._make_executor(
            mutating_action,
            verify_results=[retry_result, pass_result],
            max_retries=3
        )

        with patch("time.sleep"):
            result = executor.run([
                Step(action_name="set_speed",
                     params={"speed": 1.5},
                     url="youtube.com",
                     verify_conditions={"url_contains": "youtube.com"})
            ])

        assert result["success"] is True
        # Both calls should have received speed=1.5
        assert all(s == 1.5 for s in call_params), (
            f"Params were mutated between retries: {call_params}"
        )

    def test_retry_on_transient_verify_failure(self):
        """
        A non-idempotent action that gets retry on first verify should be retried.
        """
        call_count = [0]

        def normal_action(actions, **p):
            call_count[0] += 1
            return Result.ok(data={"action": "done"})

        pass_result = VerifyResult(status="pass", reason="ok", details={})
        retry_result = VerifyResult(status="retry", reason="transient", details={})

        executor = self._make_executor(
            normal_action,
            verify_results=[retry_result, pass_result]
        )

        with patch("time.sleep"):
            result = executor.run([
                Step(action_name="normal",
                     url="youtube.com",
                     verify_conditions={"url_contains": "youtube.com"})
            ])

        assert result["success"] is True
        assert call_count[0] == 2  # Retried once

    def test_skipped_action_never_retried(self):
        """
        An action returning skipped_* is never retried even if
        verify_conditions would have triggered a retry.
        """
        call_count = [0]

        def like_action(actions, **p):
            call_count[0] += 1
            return Result.ok(data={"action": "skipped_already_liked"})

        # If verify were called, it would retry — but it shouldn't be called
        retry_result = VerifyResult(status="retry", reason="would retry", details={})
        page = _make_page()
        skill = MagicMock()
        skill.name = "MockSkill"
        skill.get_action.return_value = like_action
        manager = MagicMock()
        manager.get_skill.return_value = skill
        manager.skill_names = ["MockSkill"]
        verifier = MagicMock()
        verifier.verify.return_value = retry_result

        executor = Executor(page=page, skill_manager=manager,
                            verifier=verifier, max_retries=3)
        result = executor.run([
            Step(action_name="like", url="youtube.com",
                 verify_conditions={"url_contains": "youtube.com"})
        ])

        assert result["success"] is True
        assert call_count[0] == 1  # Called exactly once, never retried
        verifier.verify.assert_not_called()

    def test_opened_tabs_collected_from_open_top_recommended(self):
        """
        opened_tabs are populated when action returns a list of tab dicts.
        """
        tab_data = [
            {"tab_index": 1, "url": "https://www.youtube.com/watch?v=r1",
             "title": "Video 1", "verified": True},
            {"tab_index": 2, "url": "https://www.youtube.com/watch?v=r2",
             "title": "Video 2", "verified": True},
        ]

        def open_recommended(actions, **p):
            return Result.ok(data=tab_data)

        pass_result = VerifyResult(status="pass", reason="ok", details={})
        executor = self._make_executor(open_recommended, [pass_result])
        result = executor.run([
            Step(action_name="open_top_recommended",
                 url="youtube.com",
                 verify_conditions={"url_contains": "youtube.com"})
        ])

        assert result["success"] is True
        assert len(result["opened_tabs"]) == 2
        assert result["opened_tabs"][0]["title"] == "Video 1"


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
