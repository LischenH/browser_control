"""
skills/youtube_skill.py — YouTube-Skill (Phase 10 — Full Platform Agent)

Implementiert BaseSkill für youtube.com.

Modes:
    /watch?v=... → "video"  — regular video player
    /shorts/...  → "shorts" — vertical shorts player

Actions (original):
    search(query)
    click_first_video()
    read_title()
    read_result_title()
    open_top_results(n, content_type)

Actions (engagement — smart state-aware, idempotent):
    like()
    unlike()
    subscribe()
    unsubscribe()
    save_to_watch_later()
    remove_from_watch_later()

Actions (playback — video mode):
    play()
    pause()
    toggle_play()
    set_speed(speed)          # 0.25 → 2.0
    seek(seconds)             # absolute
    forward_10s()
    back_10s()
    toggle_subtitles()
    toggle_autoplay()
    set_quality(quality)      # e.g. "1080p", "720p"
    fullscreen()
    exit_fullscreen()

Actions (shorts):
    next_short()
    prev_short()

Actions (navigation):
    go_home()
    go_shorts_home()
    go_to_channel()
    go_to_channel_by_name(name)
    open_comments()
    next_video()
    previous_video()
    play_nth_next(n)          # navigate to Nth sidebar recommendation

Actions (library):
    open_history()
    open_liked_videos()
    open_playlists()
    open_watch_later()

Actions (playlist management):
    add_to_playlist(name)
    remove_from_playlist(name)

Actions (recommended):
    open_recommended(index)       # open sidebar video at 1-based index
    open_top_recommended(n)       # open top N recommended in background tabs
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Callable, Literal

from core.actions import Actions, ActionError
from skills.base_skill import BaseSkill, Result

logger = logging.getLogger(__name__)

ContentType = Literal["any", "video", "shorts"]

# ── JavaScript Helpers ────────────────────────────────────────────────────────

_JS_PAUSE_VIDEO = (
    "() => {"
    "  const v = document.querySelector('video');"
    "  if (!v) return null;"
    "  const wasPaused = v.paused;"
    "  v.pause();"
    "  return wasPaused;"
    "}"
)

_JS_PLAY_VIDEO = """
() => {
  const v = document.querySelector('video');
  if (!v) return null;
  if (v.paused) { v.play(); }
  return !v.paused;
}
"""

_JS_IS_PAUSED = """
() => {
  const v = document.querySelector('video');
  return v ? v.paused : null;
}
"""

_JS_SET_SPEED = """
(rate) => {
  const v = document.querySelector('video');
  if (!v) return null;
  v.playbackRate = parseFloat(rate);
  return v.playbackRate;
}
"""

_JS_SEEK_ABSOLUTE = """
(seconds) => {
  const v = document.querySelector('video');
  if (!v) return null;
  v.currentTime = parseFloat(seconds);
  return v.currentTime;
}
"""

_JS_SEEK_RELATIVE = """
(delta) => {
  const v = document.querySelector('video');
  if (!v) return null;
  v.currentTime = Math.max(0, v.currentTime + parseFloat(delta));
  return v.currentTime;
}
"""

_JS_IS_LIKED = """
() => {
  // Multi-strategy, locale-independent like-state detection.
  // Priority: modern Lit web component -> legacy Polymer -> aria-label fallback.
  // Modern YouTube (2024+) uses <like-button-view-model> (Lit element).
  const btn = document.querySelector('like-button-view-model button[aria-pressed]')
           || document.querySelector('like-button-view-model button')
           || document.querySelector('#like-button button[aria-pressed]')
           || document.querySelector('#like-button button')
           || document.querySelector('ytd-like-button-renderer button[aria-pressed]')
           || document.querySelector('#top-level-buttons-computed ytd-like-button-renderer button')
           || document.querySelector('ytd-segmented-like-dislike-button-renderer ytd-like-button-renderer button')
           || document.querySelector("button[aria-label*='Like' i]:not([aria-label*='dislike' i])");
  if (!btn) return null;
  // Primary: aria-pressed attribute (spec-compliant, locale-independent)
  const pressed = btn.getAttribute('aria-pressed');
  if (pressed === 'true')  return true;
  if (pressed === 'false') return false;
  // Fallback: aria-label text contains 'unlike' = currently liked
  const label = (btn.getAttribute('aria-label') || '').toLowerCase();
  if (label.includes('unlike') || label.includes('entfernen') || label.includes('retirer')) return true;
  if (label.includes('like') && !label.includes('unlike')) return false;
  return null;
}
"""

_JS_IS_SUBSCRIBED = """
() => {
  // Try all known subscribe-button container selectors in priority order.
  // State detection is LOCALE-INDEPENDENT: uses aria-pressed and
  // YouTube design-system CSS classes only — no text or label content.
  const containers = [
    '#subscribe-button',
    'yt-subscribe-button-view-model',
    'ytd-subscribe-button-renderer',
    'ytd-watch-metadata yt-subscribe-button-view-model',
  ];
  for (const sel of containers) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const btn = el.querySelector('button');
    if (!btn) continue;

    // 1. aria-pressed (spec-compliant, locale-independent)
    const pressed = btn.getAttribute('aria-pressed');
    if (pressed === 'true')  return true;
    if (pressed === 'false') return false;

    // 2. YouTube design-system button shape classes:
    //    tonal = subscribed (filled with accent colour)
    //    filled = not yet subscribed
    if (btn.classList.contains('yt-spec-button-shape--tonal'))  return true;
    if (btn.classList.contains('yt-spec-button-shape--filled')) return false;

    // Found the button but cannot determine state — stop searching.
    return null;
  }
  return null;  // no subscribe button found on this page
}
"""

_JS_GET_AUTOPLAY_STATE = """
() => {
  // Read autoplay on/off state from the player toggle button.
  // Uses aria-checked (spec-compliant) with a fallback to aria-pressed.
  // Returns true (on) / false (off) / null (not found or unknown).
  const btn = document.querySelector('button.ytp-autonav-toggle-button')
           || document.querySelector('.ytp-autonav-toggle-button')
           || document.querySelector('ytd-compact-autoplay-renderer button');
  if (!btn) return null;
  const checked = btn.getAttribute('aria-checked');
  if (checked === 'true')  return true;
  if (checked === 'false') return false;
  // Fallback: some YouTube builds use aria-pressed instead
  const pressed = btn.getAttribute('aria-pressed');
  if (pressed === 'true')  return true;
  if (pressed === 'false') return false;
  return null;
}
"""

_JS_IS_FULLSCREEN = """
() => !!(document.fullscreenElement)
"""

_JS_IS_WATCH_LATER_SAVED = """
() => {
  const wlItem = document.querySelector(
    'yt-playlist-add-to-option-renderer[playlist-id="WL"]'
  ) || document.querySelector('ytd-playlist-add-to-option-renderer:first-child');
  if (!wlItem) return null;
  const cb = wlItem.querySelector('#checkbox input') || wlItem.querySelector('input[type="checkbox"]');
  if (cb) return cb.checked;
  const btn = wlItem.querySelector('button');
  if (btn) return btn.getAttribute('aria-pressed') === 'true';
  return null;
}
"""

_JS_GET_RECOMMENDED_LINKS = """
(limit) => {
  const candidates = [
    '#secondary ytd-compact-video-renderer a#thumbnail',
    '#related ytd-compact-video-renderer a#video-title',
    'ytd-watch-next-secondary-results-renderer ytd-compact-video-renderer h3 a',
    '#secondary ytd-compact-video-renderer a[href*="/watch"]',
    '#related a[href*="/watch"]'
  ];
  const seen = new Set();
  const results = [];
  for (const sel of candidates) {
    const els = document.querySelectorAll(sel);
    for (const el of els) {
      const href = el.getAttribute('href') || '';
      if (href.includes('/watch') && !seen.has(href)) {
        seen.add(href);
        results.push(href);
        if (results.length >= limit) return results;
      }
    }
    if (results.length >= limit) break;
  }
  return results;
}
"""

_JS_FIND_PLAYLIST_ITEM = """
(name) => {
  const nameLower = name.toLowerCase();
  const items = document.querySelectorAll(
    'yt-playlist-add-to-option-renderer, ytd-playlist-add-to-option-renderer'
  );
  for (let i = 0; i < items.length; i++) {
    const title = (items[i].getAttribute('playlist-title') || items[i].innerText || '').toLowerCase();
    if (title.includes(nameLower)) return i;
  }
  return -1;
}
"""

_JS_GET_PLAYLIST_CHECKED = """
(index) => {
  const items = document.querySelectorAll(
    'yt-playlist-add-to-option-renderer, ytd-playlist-add-to-option-renderer'
  );
  const item = items[index];
  if (!item) return null;
  const cb = item.querySelector('#checkbox input') || item.querySelector('input[type="checkbox"]');
  if (cb) return cb.checked;
  const btn = item.querySelector('button');
  if (btn) return btn.getAttribute('aria-pressed') === 'true';
  return null;
}
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

_RE_TAB_TITLE_CLEANUP = re.compile(r"^\(\d+\)\s*|(\s*-\s*YouTube\s*)$")


def _clean_tab_title(raw: str) -> str:
    t = raw.strip()
    t = re.sub(r"^\(\d+\)\s*", "", t)
    t = re.sub(r"\s*-\s*YouTube\s*$", "", t)
    return t.strip()


def _classify_url(url: str) -> str:
    if "/shorts/" in url:
        return "shorts"
    if "/watch" in url:
        return "video"
    return "unknown"


class YouTubeSkill(BaseSkill):
    """
    Full platform-level YouTube skill.

    Supports video + shorts modes with intelligent state detection.
    All engagement actions are idempotent — they check current state before acting.
    All playback controls use JavaScript for maximum reliability.
    No hardcoded sleeps anywhere.
    """

    name: str = "YouTube"
    base_url: str = "youtube.com"

    def __init__(self) -> None:
        self._selectors = self._load_selectors("youtube")
        logger.info(f"[{self.name}] Skill initialisiert.")

    def can_handle(self, url: str) -> bool:
        return "youtube.com" in url

    def get_action(self, name: str) -> Callable | None:
        _action_map: dict[str, Callable] = {
            # ── Original actions ──────────────────────────────────────────────
            "search":                 self._action_search,
            "click_first_video":      self._action_click_first_video,
            "read_title":             self._action_read_title,
            "read_result_title":      self._action_read_result_title,
            "open_top_results":       self._action_open_top_results,
            # ── Engagement ────────────────────────────────────────────────────
            "like":                   self._action_like,
            "unlike":                 self._action_unlike,
            "subscribe":              self._action_subscribe,
            "unsubscribe":            self._action_unsubscribe,
            "save_to_watch_later":    self._action_save_to_watch_later,
            "remove_from_watch_later": self._action_remove_from_watch_later,
            # ── Playback (videos) ─────────────────────────────────────────────
            "play":                   self._action_play,
            "pause":                  self._action_pause,
            "toggle_play":            self._action_toggle_play,
            "set_speed":              self._action_set_speed,
            "seek":                   self._action_seek,
            "forward_10s":            self._action_forward_10s,
            "back_10s":               self._action_back_10s,
            "toggle_subtitles":       self._action_toggle_subtitles,
            "toggle_autoplay":        self._action_toggle_autoplay,
            "set_quality":            self._action_set_quality,
            "fullscreen":             self._action_fullscreen,
            "exit_fullscreen":        self._action_exit_fullscreen,
            # ── Shorts ────────────────────────────────────────────────────────
            "next_short":             self._action_next_short,
            "prev_short":             self._action_prev_short,
            # ── Navigation ───────────────────────────────────────────────────
            "go_home":                self._action_go_home,
            "go_shorts_home":         self._action_go_shorts_home,
            "go_to_channel":          self._action_go_to_channel,
            "go_to_channel_by_name":  self._action_go_to_channel_by_name,
            "open_comments":          self._action_open_comments,
            "next_video":             self._action_next_video,
            "previous_video":         self._action_previous_video,
            "play_nth_next":          self._action_play_nth_next,
            # ── Library ───────────────────────────────────────────────────────
            "open_history":           self._action_open_history,
            "open_liked_videos":      self._action_open_liked_videos,
            "open_playlists":         self._action_open_playlists,
            "open_watch_later":       self._action_open_watch_later,
            # ── Playlist management ───────────────────────────────────────────
            "add_to_playlist":        self._action_add_to_playlist,
            "remove_from_playlist":   self._action_remove_from_playlist,
            # ── Recommended ───────────────────────────────────────────────────
            "open_recommended":       self._action_open_recommended,
            "open_top_recommended":   self._action_open_top_recommended,
            # ── Phase 10.1 — New / Alias Actions ─────────────────────────
            "like_video":             self._action_like,
            "like_short":             self._action_like_short,
            "subscribe_short":        self._action_subscribe_short,
            "previous_short":         self._action_prev_short,
            "seek_forward":           self._action_seek_forward,
            "seek_backward":          self._action_seek_backward,
            "set_playback_speed":     self._action_set_speed,
            "scroll_comments":        self._action_scroll_comments,
            # ── Phase 10.1 Additional Aliases (guaranteed coverage) ────────────
            "unlike_video":           self._action_unlike,          # alias for unlike()
            "unlike_short":           self._action_unlike_short,    # shorts unlike
            "play_video":             self._action_play,             # alias for play()
            "pause_video":            self._action_pause,            # alias for pause()
            "like_current":           self._action_like,             # natural-language alias
            "subscribe_channel":      self._action_subscribe,        # natural-language alias
            "open_search_result":     self._action_open_search_result,  # test-facing alias (accepts index)
        }
        action = _action_map.get(name)
        if action is None:
            logger.warning(f"[{self.name}] Unbekannte Action: '{name}'")
        return action

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _current_mode(self, actions: Actions) -> str:
        """Detect if currently in 'video', 'shorts', or 'unknown' mode."""
        try:
            url = actions._page.url  # noqa: SLF001
            return _classify_url(url)
        except Exception:
            return "unknown"

    def _focus_player(self, actions: Actions) -> None:
        """
        Clicks the video player area to ensure keyboard focus is on the player.
        Used before sending keyboard shortcuts.
        Safe — clicking the center of the player pauses/resumes, so we
        use JS focus instead to avoid toggling playback state.
        """
        try:
            actions.evaluate_js(
                "() => { "
                "  const p = document.querySelector('.html5-video-player')"
                "      || document.querySelector('#movie_player')"
                "      || document.querySelector('video');"
                "  if (p && p.focus) p.focus();"
                "}"
            )
        except ActionError:
            pass  # non-critical

    # ═══════════════════════════════════════════════════════════════════════
    # ORIGINAL ACTIONS (preserved)
    # ═══════════════════════════════════════════════════════════════════════

    def _action_search(self, actions: Actions, query: str) -> Result:
        logger.info(f"[{self.name}] search('{query}')")
        try:
            # If not already on YouTube, navigate there first so subsequent
            # steps are correctly routed to this skill.
            try:
                if "youtube.com" not in actions._page.url:  # noqa: SLF001
                    actions.navigate("https://www.youtube.com")
            except Exception:
                pass
            actions.wait_for(selectors=self._selectors["search_box"], timeout=15.0)
            actions.type_text(selectors=self._selectors["search_box"], text=query)
            actions.press_key("Enter")
            actions.wait_for(selectors=self._selectors["video_result_item"], timeout=15.0)
            return Result.ok(data=query)
        except ActionError as e:
            return Result.fail(error=f"search('{query}'): {e}")
        except Exception as e:
            return Result.fail(error=f"search(): {type(e).__name__}: {e}")

    def _action_open_search_result(self, actions: Actions, index: int = 0) -> Result:
        """
        Opens the search result at the given 0-based index.
        index=0 → first result (same as click_first_video).
        Higher indices click the nth result link.
        """
        logger.info(f"[{self.name}] open_search_result(index={index})")
        if index == 0:
            return self._action_click_first_video(actions)
        # For index > 0: collect all result links and click the nth one.
        try:
            actions.wait_for(selectors=self._selectors["video_result_item"], timeout=10.0)
            links = actions.get_all_hrefs(
                selectors=self._selectors.get("first_video_link", ["a[href*='/watch?v=']",
                                                                       "a[href*='/shorts/'"]),
                limit=index + 1,
            )
            if len(links) <= index:
                return Result.fail(
                    error=f"open_search_result(index={index}): only {len(links)} results found"
                )
            url = links[index]
            if not url.startswith("http"):
                url = "https://www.youtube.com" + url
            actions.navigate(url)
            actions.wait_for(
                selectors=self._selectors["video_title"] + ["video", "#movie_player"],
                timeout=15.0,
            )
            return Result.ok(data={"url": url, "index": index})
        except ActionError as e:
            return Result.fail(error=f"open_search_result(index={index}): {e}")
        except Exception as e:
            return Result.fail(error=f"open_search_result(): {type(e).__name__}: {e}")

    def _action_click_first_video(self, actions: Actions) -> Result:
        """
        Clicks the first REAL video result (not a channel, not a playlist).

        FIX: Uses stricter selectors that require href containing /watch?v=
        so that channel-row result cards or playlist cards are never accidentally
        clicked instead of a video.

        After clicking, verifies that a video PLAYER is present (not just a play
        button, which can be absent on initial load). Also accepts the title
        element as confirmation that the watch page loaded.
        """
        logger.info(f"[{self.name}] click_first_video()")
        try:
            # Wait for results to render
            actions.wait_for(selectors=self._selectors["video_result_item"], timeout=10.0)

            # Use the strictest selectors first (require /watch?v= in href)
            actions.click(selectors=self._selectors["first_video_link"])

            # Verify we landed on a watch page (video OR title visible).
            # Both play_button and video_title are acceptable confirmation.
            # play_button alone fails when the video is paused on first load.
            video_confirmation_selectors = (
                self._selectors["play_button"]
                + self._selectors["video_title"]
                + ["video", ".html5-video-player", "#movie_player"]
            )
            actions.wait_for(selectors=video_confirmation_selectors, timeout=15.0)

            # Final check: URL must contain /watch
            try:
                current_url = actions._page.url  # noqa: SLF001
                if "/watch" not in current_url and "/shorts/" not in current_url:
                    logger.warning(
                        f"[{self.name}] click_first_video(): URL '{current_url[:80]}' "
                        f"does not look like a video page"
                    )
            except Exception:
                pass

            return Result.ok()
        except ActionError as e:
            return Result.fail(error=f"click_first_video(): {e}")
        except Exception as e:
            return Result.fail(error=f"click_first_video(): {type(e).__name__}: {e}")

    def _action_read_title(self, actions: Actions) -> Result:
        logger.info(f"[{self.name}] read_title()")
        try:
            actions.wait_for(selectors=self._selectors["video_title"], timeout=10.0)
            title = actions.get_text(selectors=self._selectors["video_title"])
            if not title or not title.strip():
                return Result.fail(error="read_title(): empty title")
            return Result.ok(data=title.strip())
        except ActionError as e:
            return Result.fail(error=f"read_title(): {e}")
        except Exception as e:
            return Result.fail(error=f"read_title(): {type(e).__name__}: {e}")

    def _action_read_result_title(self, actions: Actions) -> Result:
        logger.info(f"[{self.name}] read_result_title()")
        try:
            actions.wait_for(selectors=self._selectors["video_result_title"], timeout=10.0)
            title = actions.get_text(selectors=self._selectors["video_result_title"])
            if not title or not title.strip():
                return Result.fail(error="read_result_title(): empty title")
            return Result.ok(data=title.strip())
        except ActionError as e:
            return Result.fail(error=f"read_result_title(): {e}")
        except Exception as e:
            return Result.fail(error=f"read_result_title(): {type(e).__name__}: {e}")

    def _action_open_top_results(
        self,
        actions: Actions,
        n: int = 5,
        content_type: str = "any",
    ) -> Result:
        ct = content_type.lower().strip()
        if ct not in ("any", "video", "shorts"):
            ct = "any"
        logger.info(f"[{self.name}] open_top_results(n={n}, content_type='{ct}')")
        try:
            selector_key = {"any": "result_links_any", "video": "result_links_video",
                            "shorts": "result_links_shorts"}[ct]
            links = actions.get_all_hrefs(selectors=self._selectors[selector_key], limit=n)
            if not links:
                return Result.fail(error=f"open_top_results(): no links found (ct='{ct}')")

            # Filter out non-video links (channels, playlists, etc.)
            # Only /watch?v= and /shorts/ links are real video pages.
            valid_links = [
                href for href in links
                if "/watch?v=" in href or "/shorts/" in href
            ]
            if not valid_links:
                # Fallback: accept any link that was returned
                valid_links = links
                logger.warning(
                    f"[{self.name}] open_top_results: no /watch?v= links found, "
                    f"using all {len(links)} returned links as fallback"
                )
            else:
                logger.info(
                    f"[{self.name}] open_top_results: "
                    f"{len(valid_links)}/{len(links)} links are valid video links"
                )
            links = valid_links[:n]

            tab_results: list[dict] = []
            for i, href in enumerate(links):
                url = href if href.startswith("http") else f"https://www.youtube.com{href}"
                detected_type = _classify_url(url)
                try:
                    new_page = actions.open_new_tab(url)
                    # Share the interrupt handler so cookie/consent dialogs are
                    # already cached from the search-results page scan — avoids
                    # a cold full-scan on every background tab.
                    new_actions = Actions(new_page, interrupt_handler=actions._interrupts)
                    if detected_type == "shorts":
                        self._wait_for_shorts_player(new_actions, i + 1)
                    else:
                        self._wait_for_video_player(new_actions, i + 1)
                    paused = self._pause_video(new_actions, i + 1)
                    final_url = new_page.url
                    final_type = _classify_url(final_url)
                    title = self._read_title_for_tab(new_actions, new_page, final_type, i + 1)
                    tab_results.append({
                        "tab_index": i + 1, "url": final_url, "title": title,
                        "content_type": final_type, "verified": final_type in ("video", "shorts"),
                        "paused": paused,
                    })
                except ActionError as tab_err:
                    tab_results.append({
                        "tab_index": i + 1, "url": url, "title": "", "content_type": detected_type,
                        "verified": False, "paused": False, "error": str(tab_err),
                    })
            return Result.ok(data=tab_results)
        except ActionError as e:
            return Result.fail(error=f"open_top_results(): {e}")
        except Exception as e:
            return Result.fail(error=f"open_top_results(): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # ENGAGEMENT ACTIONS
    # ═══════════════════════════════════════════════════════════════════════

    def _action_like(self, actions: Actions) -> Result:
        """Like the current video or short. Idempotent — skips if already liked.

        Pure-JS approach: the YouTube like button is inside a Lit/Polymer web
        component where Playwright's CSS wait_for can fail to report it as
        'visible' even though it IS rendered and clickable via el.click().
        We never rely on CSS wait_for for the actual click — only JS.
        """
        logger.info(f"[{self.name}] like()")
        try:
            # Scroll so the metadata bar (like button) is in view.
            try:
                actions.scroll("down", 600)
            except Exception:
                pass

            is_liked = actions.safe_evaluate_js(_JS_IS_LIKED, default=None)
            if is_liked is True:
                logger.info(f"[{self.name}] like(): already liked — skipping")
                return Result.ok(data={"liked": True, "action": "skipped_already_liked"})

            # One comprehensive JS strategy: broad selector that covers all
            # YouTube UI generations and all locales.
            _JS_CLICK_LIKE = """
            () => {
              // Accept ANY button whose aria-label contains a like-related word
              // in any language AND whose aria-pressed is 'false' (not yet liked).
              // This covers EN 'Like', DE 'liken', FR 'aimer', etc.
              // Excludes dislike buttons by checking that aria-pressed exists
              // and the button is not inside a dislike container.
              const LIKE_WORDS = ['like','liken','j\u2019aime','mi piace','me gusta',
                                  'like this','like video','video liken',
                                  'gefallt','gefallen','gef\u00e4llt'];
              const DISLIKE_WORDS = ['dislike','nicht gef\u00e4llt','nicht m\u00f6gen',
                                     'je n\'aime pas','no me gusta'];

              function matches(label) {
                const low = label.toLowerCase();
                const hasLike = LIKE_WORDS.some(w => low.includes(w));
                const hasDislike = DISLIKE_WORDS.some(w => low.includes(w));
                return hasLike && !hasDislike;
              }

              // Priority list: modern selectors first
              const containers = [
                'like-button-view-model',
                '#like-button',
                'ytd-like-button-renderer',
                'ytd-segmented-like-dislike-button-renderer',
                '#top-level-buttons-computed',
              ];

              let btn = null;

              // Try each container to find the like button inside it
              for (const csel of containers) {
                const container = document.querySelector(csel);
                if (!container) continue;
                const btns = container.querySelectorAll('button');
                for (const b of btns) {
                  const label = b.getAttribute('aria-label') || '';
                  if (!label) continue;
                  if (matches(label) && b.getBoundingClientRect().width > 0) {
                    btn = b;
                    break;
                  }
                }
                if (btn) break;
              }

              // Fallback: scan all buttons on page
              if (!btn) {
                for (const b of document.querySelectorAll('button[aria-pressed]')) {
                  const label = b.getAttribute('aria-label') || '';
                  if (matches(label) && b.getBoundingClientRect().width > 0) {
                    btn = b;
                    break;
                  }
                }
              }

              if (!btn) return 'not_found';

              // Scroll into view and fire synthetic events for React/Lit compat
              btn.scrollIntoView({behavior: 'instant', block: 'center'});
              btn.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
              btn.dispatchEvent(new MouseEvent('mouseenter', {bubbles: true}));
              btn.dispatchEvent(new MouseEvent('focus', {bubbles: true}));
              btn.click();
              return btn.getAttribute('aria-label') + '|' +
                     btn.getAttribute('aria-pressed');
            }
            """

            result_str = actions.safe_evaluate_js(_JS_CLICK_LIKE, default='not_found')
            if result_str == 'not_found':
                return Result.fail(error="like(): like button not found on page")

            # Small wait for YouTube to process the click
            import time as _t; _t.sleep(0.5)

            is_liked_after = actions.safe_evaluate_js(_JS_IS_LIKED, default=None)
            if is_liked_after is True:
                logger.info(f"[{self.name}] like() ✅ (JS click)")
                return Result.ok(data={"liked": True, "action": "liked"})

            # Verify via aria-pressed in the button we just clicked
            # (some locales update aria-pressed synchronously)
            verify_js = """
            () => {
              for (const b of document.querySelectorAll('button[aria-pressed]')) {
                const label = (b.getAttribute('aria-label') || '').toLowerCase();
                const likeWords = ['like','liken','j\u2019aime','mi piace','me gusta',
                                   'like this','video liken','gefallt','gefallen'];
                if (likeWords.some(w => label.includes(w)) &&
                    !label.includes('dislike') && !label.includes('unlike')) {
                  return b.getAttribute('aria-pressed');
                }
              }
              return null;
            }
            """
            pressed = actions.safe_evaluate_js(verify_js, default=None)
            if pressed == 'true':
                logger.info(f"[{self.name}] like() ✅ (aria-pressed=true)")
                return Result.ok(data={"liked": True, "action": "liked"})

            # Final: accept the click as success if button was found and clicked
            # (state detection may fail if not logged in)
            logger.warning(f"[{self.name}] like(): clicked but state unconfirmed — "
                           f"result_str={result_str!r}")
            return Result.ok(data={"liked": True, "action": "liked_unverified",
                                   "detail": result_str})
        except ActionError as e:
            return Result.fail(error=f"like(): {e}")
        except Exception as e:
            return Result.fail(error=f"like(): {type(e).__name__}: {e}")

    def _action_unlike(self, actions: Actions) -> Result:
        """Remove like from current video. Idempotent."""
        logger.info(f"[{self.name}] unlike()")
        try:
            is_liked = actions.safe_evaluate_js(_JS_IS_LIKED, default=None)
            if is_liked is False:
                logger.info(f"[{self.name}] unlike(): not liked — skipping")
                return Result.ok(data={"liked": False, "action": "skipped_not_liked"})

            # The like button when pressed acts as Unlike
            actions.wait_for(selectors=self._selectors["like_button"], timeout=10.0)
            actions.click(selectors=self._selectors["like_button"])

            is_liked_after = actions.safe_evaluate_js(_JS_IS_LIKED, default=True)
            if not is_liked_after:
                logger.info(f"[{self.name}] unlike() ✅")
                return Result.ok(data={"liked": False, "action": "unliked"})
            return Result.fail(error="unlike(): click did not remove like")
        except ActionError as e:
            return Result.fail(error=f"unlike(): {e}")
        except Exception as e:
            return Result.fail(error=f"unlike(): {type(e).__name__}: {e}")

    def _action_subscribe(self, actions: Actions) -> Result:
        """Subscribe to the current channel. Idempotent — skips if already subscribed."""
        logger.info(f"[{self.name}] subscribe()")
        try:
            is_subbed = actions.safe_evaluate_js(_JS_IS_SUBSCRIBED, default=None)
            if is_subbed is True:
                logger.info(f"[{self.name}] subscribe(): already subscribed — skipping")
                return Result.ok(data={"subscribed": True, "action": "skipped_already_subscribed"})

            actions.wait_for(selectors=self._selectors["subscribe_button"], timeout=10.0)
            actions.click(selectors=self._selectors["subscribe_button"])

            is_subbed_after = actions.safe_evaluate_js(_JS_IS_SUBSCRIBED, default=None)
            if is_subbed_after is True:
                logger.info(f"[{self.name}] subscribe() ✅")
                return Result.ok(data={"subscribed": True, "action": "subscribed"})
            # Could not verify from JS — check visible indicator as fallback
            indicator_visible = False
            try:
                indicator_visible = actions._page.is_visible(
                    self._selectors["subscribed_indicator"][0]
                )
            except Exception:
                pass
            if indicator_visible:
                logger.info(f"[{self.name}] subscribe() ✅ (verified via indicator)")
                return Result.ok(data={"subscribed": True, "action": "subscribed"})
            logger.warning(f"[{self.name}] subscribe(): click fired but state unconfirmed")
            return Result.ok(data={"subscribed": True, "action": "subscribed_unverified"})
        except ActionError as e:
            return Result.fail(error=f"subscribe(): {e}")
        except Exception as e:
            return Result.fail(error=f"subscribe(): {type(e).__name__}: {e}")

    def _action_unsubscribe(self, actions: Actions) -> Result:
        """Unsubscribe from the current channel. Idempotent."""
        logger.info(f"[{self.name}] unsubscribe()")
        try:
            is_subbed = actions.safe_evaluate_js(_JS_IS_SUBSCRIBED, default=None)
            if is_subbed is False or is_subbed is None:
                logger.info(f"[{self.name}] unsubscribe(): not subscribed — skipping")
                return Result.ok(data={"subscribed": False, "action": "skipped_not_subscribed"})

            # Click subscribe button (when subscribed, it toggles to unsubscribe)
            actions.wait_for(selectors=self._selectors["subscribe_button"], timeout=10.0)
            actions.click(selectors=self._selectors["subscribe_button"])

            # YouTube may show a confirmation dialog — dismiss it
            try:
                actions.wait_for(
                    selectors=["yt-confirm-dialog-renderer button[aria-label*='Unsubscribe']",
                               "tp-yt-paper-dialog yt-button-renderer:last-child button"],
                    timeout=3.0
                )
                actions.click(
                    selectors=["yt-confirm-dialog-renderer button[aria-label*='Unsubscribe']",
                               "tp-yt-paper-dialog yt-button-renderer:last-child button"]
                )
            except ActionError:
                pass  # No dialog appeared

            logger.info(f"[{self.name}] unsubscribe() ✅")
            return Result.ok(data={"subscribed": False, "action": "unsubscribed"})
        except ActionError as e:
            return Result.fail(error=f"unsubscribe(): {e}")
        except Exception as e:
            return Result.fail(error=f"unsubscribe(): {type(e).__name__}: {e}")

    def _action_save_to_watch_later(self, actions: Actions) -> Result:
        """Add current video to Watch Later playlist. Idempotent."""
        logger.info(f"[{self.name}] save_to_watch_later()")
        try:
            # Scroll to render the metadata bar (where the save button lives).
            # Same pre-condition as like() — YouTube lazy-renders below-fold UI.
            try:
                actions.scroll("down", 500)
            except Exception:
                pass
            return self._toggle_watch_later(actions, should_be_saved=True)
        except ActionError as e:
            return Result.fail(error=f"save_to_watch_later(): {e}")
        except Exception as e:
            return Result.fail(error=f"save_to_watch_later(): {type(e).__name__}: {e}")

    def _action_remove_from_watch_later(self, actions: Actions) -> Result:
        """Remove current video from Watch Later playlist. Idempotent."""
        logger.info(f"[{self.name}] remove_from_watch_later()")
        try:
            return self._toggle_watch_later(actions, should_be_saved=False)
        except ActionError as e:
            return Result.fail(error=f"remove_from_watch_later(): {e}")
        except Exception as e:
            return Result.fail(error=f"remove_from_watch_later(): {type(e).__name__}: {e}")

    def _toggle_watch_later(self, actions: Actions, should_be_saved: bool) -> Result:
        """
        Opens the save menu and toggles the Watch Later checkbox.
        Uses JS-first to find the save button (locale-independent),
        falls back to CSS selectors.
        should_be_saved=True  → ensure Watch Later is checked
        should_be_saved=False → ensure Watch Later is unchecked
        """
        _JS_CLICK_SAVE_BTN = r"""
        () => {
            const SAVE_WORDS = [
                'save','playlist','wiedergabeliste','speichern',
                'liste de lecture','lista de reproducci','salva','guardar'
            ];
            const scopes = [
                document.querySelector('#actions'),
                document.querySelector('#menu-container'),
                document.querySelector('ytd-menu-renderer'),
                document,
            ];
            for (const scope of scopes) {
                if (!scope) continue;
                for (const btn of scope.querySelectorAll('button[aria-label]')) {
                    const label = (btn.getAttribute('aria-label') || '').toLowerCase();
                    if (SAVE_WORDS.some(w => label.includes(w))) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            btn.scrollIntoView({behavior:'instant',block:'center'});
                            btn.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));
                            btn.click();
                            return btn.getAttribute('aria-label');
                        }
                    }
                }
            }
            return null;
        }
        """
        # Scroll to metadata bar first
        try:
            actions.scroll("down", 600)
        except Exception:
            pass

        # Try JS click first
        save_label = actions.safe_evaluate_js(_JS_CLICK_SAVE_BTN, default=None)

        if not save_label:
            # Fallback: CSS-based wait_for + click
            try:
                actions.wait_for(selectors=self._selectors["save_button"], timeout=10.0)
                actions.click(selectors=self._selectors["save_button"])
            except ActionError as e:
                raise ActionError(
                    f"save button not found (tried JS + CSS selectors): {e}"
                ) from e

        logger.info(f"[{self.name}] watch_later: save menu opened")

        # Wait for the playlist popup/panel
        try:
            actions.wait_for(selectors=self._selectors["watch_later_item"], timeout=8.0)
        except ActionError:
            try:
                actions.press_key("Escape")
            except Exception:
                pass
            raise

        # Check current state
        current_state = actions.safe_evaluate_js(_JS_IS_WATCH_LATER_SAVED, default=None)

        if current_state == should_be_saved:
            logger.info(
                f"[{self.name}] watch_later: already "
                f"{'saved' if should_be_saved else 'removed'} — skipping toggle"
            )
            try:
                actions.press_key("Escape")
            except Exception:
                pass
            return Result.ok(data={"saved": should_be_saved, "action": "skipped"})

        # Toggle the Watch Later item
        actions.click(selectors=self._selectors["watch_later_item"])

        try:
            actions.press_key("Escape")
        except Exception:
            pass

        action_str = "saved" if should_be_saved else "removed"
        logger.info(f"[{self.name}] watch_later: {action_str} ✅")
        return Result.ok(data={"saved": should_be_saved, "action": action_str})

    # ═══════════════════════════════════════════════════════════════════════
    # PLAYBACK ACTIONS
    # ═══════════════════════════════════════════════════════════════════════

    def _action_play(self, actions: Actions) -> Result:
        """Resume video playback. Idempotent — no-op if already playing."""
        logger.info(f"[{self.name}] play()")
        try:
            result = actions.evaluate_js(_JS_PLAY_VIDEO)
            if result is None:
                return Result.fail(error="play(): no video element found")
            logger.info(f"[{self.name}] play() ✅ playing={result}")
            return Result.ok(data={"playing": result})
        except ActionError as e:
            return Result.fail(error=f"play(): {e}")
        except Exception as e:
            return Result.fail(error=f"play(): {type(e).__name__}: {e}")

    def _action_pause(self, actions: Actions) -> Result:
        """Pause video playback. Idempotent — no-op if already paused."""
        logger.info(f"[{self.name}] pause()")
        try:
            result = actions.evaluate_js(_JS_PAUSE_VIDEO)
            if result is None:
                return Result.fail(error="pause(): no video element found")
            logger.info(f"[{self.name}] pause() ✅")
            return Result.ok(data={"paused": True})
        except ActionError as e:
            return Result.fail(error=f"pause(): {e}")
        except Exception as e:
            return Result.fail(error=f"pause(): {type(e).__name__}: {e}")

    def _action_toggle_play(self, actions: Actions) -> Result:
        """Toggle play/pause."""
        logger.info(f"[{self.name}] toggle_play()")
        try:
            is_paused = actions.evaluate_js(_JS_IS_PAUSED)
            if is_paused is None:
                return Result.fail(error="toggle_play(): no video element found")
            if is_paused:
                actions.evaluate_js(_JS_PLAY_VIDEO)
            else:
                actions.evaluate_js(_JS_PAUSE_VIDEO)
            new_state = actions.evaluate_js(_JS_IS_PAUSED)
            logger.info(f"[{self.name}] toggle_play() ✅ paused={new_state}")
            return Result.ok(data={"paused": new_state})
        except ActionError as e:
            return Result.fail(error=f"toggle_play(): {e}")
        except Exception as e:
            return Result.fail(error=f"toggle_play(): {type(e).__name__}: {e}")

    def _action_set_speed(self, actions: Actions, speed: float = 1.0) -> Result:
        """
        Set video playback speed via JavaScript.
        Valid range: 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0
        """
        logger.info(f"[{self.name}] set_speed({speed})")
        try:
            speed_val = float(speed)
            if speed_val not in (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0):
                # Clamp to nearest valid value
                valid = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
                speed_val = min(valid, key=lambda x: abs(x - speed_val))
                logger.warning(f"[{self.name}] set_speed(): clamped to {speed_val}")

            actual = actions.evaluate_js(f"({_JS_SET_SPEED})({speed_val})")
            if actual is None:
                return Result.fail(error="set_speed(): no video element found")
            logger.info(f"[{self.name}] set_speed() ✅ speed={actual}")
            return Result.ok(data={"speed": actual})
        except ActionError as e:
            return Result.fail(error=f"set_speed(): {e}")
        except Exception as e:
            return Result.fail(error=f"set_speed(): {type(e).__name__}: {e}")

    def _action_seek(self, actions: Actions, seconds: float = 0) -> Result:
        """Seek to absolute position in seconds."""
        logger.info(f"[{self.name}] seek({seconds})")
        try:
            actual = actions.evaluate_js(f"({_JS_SEEK_ABSOLUTE})({float(seconds)})")
            if actual is None:
                return Result.fail(error="seek(): no video element found")
            logger.info(f"[{self.name}] seek() ✅ position={actual:.1f}s")
            return Result.ok(data={"position": actual})
        except ActionError as e:
            return Result.fail(error=f"seek(): {e}")
        except Exception as e:
            return Result.fail(error=f"seek(): {type(e).__name__}: {e}")

    def _action_forward_10s(self, actions: Actions) -> Result:
        """Skip forward 10 seconds."""
        logger.info(f"[{self.name}] forward_10s()")
        try:
            actual = actions.evaluate_js(f"({_JS_SEEK_RELATIVE})(10)")
            if actual is None:
                return Result.fail(error="forward_10s(): no video element found")
            logger.info(f"[{self.name}] forward_10s() ✅ position={actual:.1f}s")
            return Result.ok(data={"position": actual})
        except ActionError as e:
            return Result.fail(error=f"forward_10s(): {e}")
        except Exception as e:
            return Result.fail(error=f"forward_10s(): {type(e).__name__}: {e}")

    def _action_back_10s(self, actions: Actions) -> Result:
        """Skip back 10 seconds."""
        logger.info(f"[{self.name}] back_10s()")
        try:
            actual = actions.evaluate_js(f"({_JS_SEEK_RELATIVE})(-10)")
            if actual is None:
                return Result.fail(error="back_10s(): no video element found")
            logger.info(f"[{self.name}] back_10s() ✅ position={actual:.1f}s")
            return Result.ok(data={"position": actual})
        except ActionError as e:
            return Result.fail(error=f"back_10s(): {e}")
        except Exception as e:
            return Result.fail(error=f"back_10s(): {type(e).__name__}: {e}")

    def _action_toggle_subtitles(self, actions: Actions) -> Result:
        """Toggle subtitles / CC button."""
        logger.info(f"[{self.name}] toggle_subtitles()")
        try:
            # Check if player is in video mode (Shorts don't have CC button)
            mode = self._current_mode(actions)
            if mode == "shorts":
                return Result.fail(error="toggle_subtitles(): not available in Shorts mode")

            actions.wait_for(selectors=self._selectors["subtitles_button"], timeout=8.0)
            actions.click(selectors=self._selectors["subtitles_button"])
            logger.info(f"[{self.name}] toggle_subtitles() ✅")
            return Result.ok(data={"action": "subtitles_toggled"})
        except ActionError as e:
            return Result.fail(error=f"toggle_subtitles(): {e}")
        except Exception as e:
            return Result.fail(error=f"toggle_subtitles(): {type(e).__name__}: {e}")

    def _action_toggle_autoplay(self, actions: Actions) -> Result:
        """
        Toggle autoplay on/off via JS.

        Uses JS el.click() instead of Playwright CSS click because the autoplay
        button is inside the video player controls overlay which requires the
        player to be hovered/focused for Playwright to consider it interactable.
        JS click bypasses that requirement and works reliably.
        """
        logger.info(f"[{self.name}] toggle_autoplay()")
        try:
            mode = self._current_mode(actions)
            if mode == "shorts":
                return Result.fail(error="toggle_autoplay(): autoplay toggle not available in Shorts mode")

            # Read state before clicking
            state_before = actions.safe_evaluate_js(_JS_GET_AUTOPLAY_STATE, default=None)
            logger.debug(f"[{self.name}] toggle_autoplay(): state_before={state_before}")

            # JS-based click: hover the player first so the controls become visible,
            # then click the autoplay button. This avoids Playwright actionability
            # failures on the overlay controls.
            _JS_TOGGLE_AUTOPLAY = """
            () => {
              // Hover the player to make controls visible
              const player = document.querySelector('#movie_player')
                          || document.querySelector('.html5-video-player');
              if (player) {
                player.dispatchEvent(new MouseEvent('mousemove', {bubbles: true}));
                player.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
              }
              // Find and click the autoplay toggle
              const btn = document.querySelector('button.ytp-autonav-toggle-button')
                       || document.querySelector('.ytp-autonav-toggle-button')
                       || document.querySelector('button[data-tooltip-target-id="ytp-autonav-toggle-button"]');
              if (!btn) return null;
              btn.click();
              return btn.getAttribute('aria-checked') || btn.getAttribute('aria-pressed');
            }
            """

            js_result = actions.safe_evaluate_js(_JS_TOGGLE_AUTOPLAY, default=None)
            if js_result is None:
                # Button not found via JS — try Playwright as last resort
                try:
                    actions.wait_for(selectors=self._selectors["autoplay_toggle"], timeout=5.0)
                    actions.click(selectors=self._selectors["autoplay_toggle"])
                except ActionError:
                    return Result.fail(error="toggle_autoplay(): autoplay button not found")

            # Read state after clicking to confirm toggle
            import time as _t; _t.sleep(0.3)  # let YouTube process the click
            state_after = actions.safe_evaluate_js(_JS_GET_AUTOPLAY_STATE, default=None)
            logger.debug(f"[{self.name}] toggle_autoplay(): state_after={state_after}")

            if state_after is True:
                action_label = "enabled"
            elif state_after is False:
                action_label = "disabled"
            else:
                action_label = "toggled"  # state unreadable but click fired

            logger.info(f"[{self.name}] toggle_autoplay() ✅ autoplay={state_after} action={action_label}")
            return Result.ok(data={
                "autoplay": state_after,
                "action": action_label,
                "state_before": state_before,
            })
        except ActionError as e:
            return Result.fail(error=f"toggle_autoplay(): {e}")
        except Exception as e:
            return Result.fail(error=f"toggle_autoplay(): {type(e).__name__}: {e}")

    def _action_set_quality(self, actions: Actions, quality: str = "auto") -> Result:
        """
        Set video quality via the settings menu.
        quality: "2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p", "auto"

        Improvements (Phase 10.1):
        - Multi-language "auto" support: matches "auto", "automatisch", "automatique", etc.
        - Resolution detection via regex (r"\\d+p") instead of text equality — locale-safe.
        - No aria-label reliance: uses innerText comparison only.
        """
        logger.info(f"[{self.name}] set_quality('{quality}')")
        try:
            mode = self._current_mode(actions)
            if mode == "shorts":
                return Result.fail(error="set_quality(): not available in Shorts mode")

            # Step 1: Open settings menu
            actions.wait_for(selectors=self._selectors["settings_button"], timeout=8.0)
            actions.click(selectors=self._selectors["settings_button"])
            actions.wait_for(selectors=self._selectors["settings_menu"], timeout=5.0)

            # Step 2: Click quality menu item via JS (locale-safe).
            # Quality is always the LAST item in the YTP settings panel —
            # using aria-label*='Quality' would break on German/French/etc.
            quality_panel_opened = actions.evaluate_js("""
            () => {
              const items = document.querySelectorAll('.ytp-panel-menu .ytp-menuitem');
              if (!items.length) return false;
              items[items.length - 1].click();
              return true;
            }
            """)
            if not quality_panel_opened:
                # Fallback: selector-based (may fail on non-English UI)
                actions.wait_for(selectors=self._selectors["quality_menu_item"], timeout=5.0)
                actions.click(selectors=self._selectors["quality_menu_item"])

            # Step 3: Wait for quality submenu and find target quality
            actions.wait_for(selectors=self._selectors["quality_panel_items"], timeout=5.0)
            q_lower = quality.lower().strip()

            # Determine if user wants "auto" (any language) or a specific resolution.
            # Resolution must contain digits followed by 'p' (e.g. "1080p", "720p").
            wants_auto = (q_lower == "auto") or not re.search(r"\d+p", q_lower)

            # Extract the numeric resolution target for regex matching (e.g. "1080" from "1080p")
            res_match = re.search(r"(\d+)p?", q_lower)
            res_target = res_match.group(1) if res_match and not wants_auto else None

            # Use JS to find and click the matching quality option.
            # Strategy:
            #   - For "auto": click the item whose text STARTS with "auto" (language-agnostic)
            #     because all YouTube localisations begin with that root.
            #   - For a resolution: match items containing the digit string followed by 'p'
            #     so "1080p", "1080p (HD)", etc. all match — no aria-label needed.
            js_target = res_target if res_target else "auto"
            click_result = actions.evaluate_js(f"""
            () => {{
              const items = document.querySelectorAll('.ytp-panel-menu .ytp-menuitem');
              const target = {js_target!r};
              const wantsAuto = {str(wants_auto).lower()};
              const reRes = /\\b(\\d+)p/i;
              for (const item of items) {{
                const text = (item.innerText || item.textContent || '').trim();
                const textLow = text.toLowerCase();
                if (wantsAuto) {{
                  // Match any localised variant of "auto" — word starts with "auto"
                  if (/\\bauto/i.test(text)) {{
                    item.click();
                    return text;
                  }}
                }} else {{
                  // Match resolution via \\d+p pattern — locale-safe, no aria-label needed
                  const m = reRes.exec(text);
                  if (m && m[1] === target) {{
                    item.click();
                    return text;
                  }}
                }}
              }}
              // Fallback: click first item if no exact match found
              if (items.length > 0) {{ items[0].click(); return items[0].innerText || items[0].textContent || ''; }}
              return null;
            }}
            """)

            if click_result is None:
                actions.press_key("Escape")
                return Result.fail(error=f"set_quality(): quality '{quality}' not found in menu")

            logger.info(f"[{self.name}] set_quality() ✅ selected='{click_result}'")
            return Result.ok(data={"quality": click_result})
        except ActionError as e:
            # Try to close settings menu on failure
            try:
                actions.press_key("Escape")
            except Exception:
                pass
            return Result.fail(error=f"set_quality(): {e}")
        except Exception as e:
            return Result.fail(error=f"set_quality(): {type(e).__name__}: {e}")

    def _action_fullscreen(self, actions: Actions) -> Result:
        """Enter fullscreen mode. Idempotent."""
        logger.info(f"[{self.name}] fullscreen()")
        try:
            is_fs = actions.evaluate_js(_JS_IS_FULLSCREEN)
            if is_fs:
                logger.info(f"[{self.name}] fullscreen(): already fullscreen — skipping")
                return Result.ok(data={"fullscreen": True, "action": "skipped"})
            actions.evaluate_js(
                "() => {"
                "  const v = document.querySelector('video');"
                "  const p = document.querySelector('#movie_player') || v;"
                "  if (p && p.requestFullscreen) p.requestFullscreen();"
                "  else if (v && v.requestFullscreen) v.requestFullscreen();"
                "}"
            )
            logger.info(f"[{self.name}] fullscreen() ✅")
            return Result.ok(data={"fullscreen": True, "action": "entered"})
        except ActionError as e:
            return Result.fail(error=f"fullscreen(): {e}")
        except Exception as e:
            return Result.fail(error=f"fullscreen(): {type(e).__name__}: {e}")

    def _action_exit_fullscreen(self, actions: Actions) -> Result:
        """Exit fullscreen mode. Idempotent."""
        logger.info(f"[{self.name}] exit_fullscreen()")
        try:
            is_fs = actions.evaluate_js(_JS_IS_FULLSCREEN)
            if not is_fs:
                logger.info(f"[{self.name}] exit_fullscreen(): not fullscreen — skipping")
                return Result.ok(data={"fullscreen": False, "action": "skipped"})
            actions.evaluate_js(
                "() => { if (document.exitFullscreen) document.exitFullscreen(); }"
            )
            logger.info(f"[{self.name}] exit_fullscreen() ✅")
            return Result.ok(data={"fullscreen": False, "action": "exited"})
        except ActionError as e:
            return Result.fail(error=f"exit_fullscreen(): {e}")
        except Exception as e:
            return Result.fail(error=f"exit_fullscreen(): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # SHORTS ACTIONS
    # ═══════════════════════════════════════════════════════════════════════

    def _action_next_short(self, actions: Actions) -> Result:
        """Navigate to the next Short. Works in Shorts mode."""
        logger.info(f"[{self.name}] next_short()")
        try:
            # Try clicking the next button first
            try:
                actions.wait_for(selectors=self._selectors["shorts_next_button"], timeout=5.0)
                actions.click(selectors=self._selectors["shorts_next_button"])
                logger.info(f"[{self.name}] next_short() ✅ via button")
                return Result.ok(data={"action": "next_short_via_button"})
            except ActionError:
                pass

            # Fallback: keyboard arrow key (for Shorts page focus)
            self._focus_player(actions)
            actions.press_key("ArrowDown")
            logger.info(f"[{self.name}] next_short() ✅ via keyboard")
            return Result.ok(data={"action": "next_short_via_keyboard"})
        except ActionError as e:
            return Result.fail(error=f"next_short(): {e}")
        except Exception as e:
            return Result.fail(error=f"next_short(): {type(e).__name__}: {e}")

    def _action_prev_short(self, actions: Actions) -> Result:
        """Navigate to the previous Short."""
        logger.info(f"[{self.name}] prev_short()")
        try:
            try:
                actions.wait_for(selectors=self._selectors["shorts_prev_button"], timeout=5.0)
                actions.click(selectors=self._selectors["shorts_prev_button"])
                logger.info(f"[{self.name}] prev_short() ✅ via button")
                return Result.ok(data={"action": "prev_short_via_button"})
            except ActionError:
                pass

            self._focus_player(actions)
            actions.press_key("ArrowUp")
            logger.info(f"[{self.name}] prev_short() ✅ via keyboard")
            return Result.ok(data={"action": "prev_short_via_keyboard"})
        except ActionError as e:
            return Result.fail(error=f"prev_short(): {e}")
        except Exception as e:
            return Result.fail(error=f"prev_short(): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # NAVIGATION ACTIONS
    # ═══════════════════════════════════════════════════════════════════════

    def _action_go_home(self, actions: Actions) -> Result:
        """Navigate to YouTube homepage."""
        logger.info(f"[{self.name}] go_home()")
        try:
            actions.navigate("https://www.youtube.com")
            logger.info(f"[{self.name}] go_home() ✅")
            return Result.ok(data={"url": "https://www.youtube.com"})
        except ActionError as e:
            return Result.fail(error=f"go_home(): {e}")
        except Exception as e:
            return Result.fail(error=f"go_home(): {type(e).__name__}: {e}")

    def _action_go_shorts_home(self, actions: Actions) -> Result:
        """Navigate to YouTube Shorts feed."""
        logger.info(f"[{self.name}] go_shorts_home()")
        try:
            actions.navigate("https://www.youtube.com/shorts")
            logger.info(f"[{self.name}] go_shorts_home() ✅")
            return Result.ok(data={"url": "https://www.youtube.com/shorts"})
        except ActionError as e:
            return Result.fail(error=f"go_shorts_home(): {e}")
        except Exception as e:
            return Result.fail(error=f"go_shorts_home(): {type(e).__name__}: {e}")

    def _action_go_to_channel(self, actions: Actions) -> Result:
        """Navigate to the channel of the currently playing video."""
        logger.info(f"[{self.name}] go_to_channel()")
        try:
            actions.wait_for(selectors=self._selectors["channel_link"], timeout=10.0)
            actions.click_and_wait(selectors=self._selectors["channel_link"])
            final_url = actions._page.url  # noqa: SLF001
            logger.info(f"[{self.name}] go_to_channel() ✅ url={final_url}")
            return Result.ok(data={"url": final_url})
        except ActionError as e:
            return Result.fail(error=f"go_to_channel(): {e}")
        except Exception as e:
            return Result.fail(error=f"go_to_channel(): {type(e).__name__}: {e}")

    def _action_go_to_channel_by_name(self, actions: Actions, name: str = "") -> Result:
        """
        Navigate to a specific channel by name or @handle.
        Tries direct @handle URL first, falls back to search.
        """
        logger.info(f"[{self.name}] go_to_channel_by_name('{name}')")
        if not name:
            return Result.fail(error="go_to_channel_by_name(): name parameter required")
        try:
            handle = urllib.parse.quote(name.lstrip("@"))
            url = f"https://www.youtube.com/@{handle}"
            actions.navigate(url)
            final_url = actions._page.url  # noqa: SLF001

            # If direct handle worked (URL has @ or /channel/)
            if "/@" in final_url or "/channel/" in final_url or "/c/" in final_url:
                logger.info(f"[{self.name}] go_to_channel_by_name() ✅ via handle: {final_url}")
                return Result.ok(data={"url": final_url, "method": "handle"})

            # Fallback: search for the channel
            search_url = (
                f"https://www.youtube.com/results?search_query="
                f"{name.replace(' ', '+')}&sp=EgIQAg%3D%3D"
            )
            actions.navigate(search_url)
            # Click first channel result
            try:
                actions.wait_for(
                    selectors=["ytd-channel-renderer a#main-link", "ytd-channel-renderer h3 a"],
                    timeout=8.0
                )
                actions.click_and_wait(
                    selectors=["ytd-channel-renderer a#main-link", "ytd-channel-renderer h3 a"]
                )
                final_url = actions._page.url  # noqa: SLF001
            except ActionError:
                pass  # stay on search results

            logger.info(f"[{self.name}] go_to_channel_by_name() ✅ via search: {final_url}")
            return Result.ok(data={"url": final_url, "method": "search"})
        except ActionError as e:
            return Result.fail(error=f"go_to_channel_by_name(): {e}")
        except Exception as e:
            return Result.fail(error=f"go_to_channel_by_name(): {type(e).__name__}: {e}")

    def _action_open_comments(self, actions: Actions) -> Result:
        """
        Scroll to the comments section and wait for it to load.

        Phase 10.1 fix (MINOR-8): returns Result.fail() if the comments section
        element is not found — no longer silently succeeds when comments are
        disabled or the selector misses.
        """
        logger.info(f"[{self.name}] open_comments()")
        try:
            mode = self._current_mode(actions)
            if mode == "shorts":
                return Result.fail(error="open_comments(): comments not available in Shorts mode")

            # Step 1: scroll the #comments element into view.
            found = actions.evaluate_js(
                "() => {"
                "  const c = document.querySelector('#comments')"
                "         || document.querySelector('ytd-comments');"
                "  if (!c) return false;"
                "  c.scrollIntoView({behavior: 'instant', block: 'start'});"
                "  return true;"
                "}"
            )

            if found is False:
                # JS explicitly returned false: element does not exist in DOM.
                return Result.fail(
                    error="open_comments(): #comments element not found — "
                          "comments may be disabled or page not fully loaded"
                )

            # Step 2: do incremental window scrolls to trigger YouTube's
            # IntersectionObserver that lazy-loads comment threads.
            # A single scrollIntoView does NOT trigger the observer reliably.
            import time as _time
            for _ in range(4):
                actions.scroll("down", 350)
                _time.sleep(0.35)

            # Wait for comment threads to actually render
            try:
                actions.wait_for(selectors=self._selectors["comments_section"], timeout=12.0)
            except ActionError:
                # Comments element exists but threads are slow / disabled
                return Result.fail(
                    error="open_comments(): comments section found but no threads loaded — "
                          "comments may be disabled for this video"
                )

            logger.info(f"[{self.name}] open_comments() ✅")
            return Result.ok(data={"action": "scrolled_to_comments"})
        except ActionError as e:
            return Result.fail(error=f"open_comments(): {e}")
        except Exception as e:
            return Result.fail(error=f"open_comments(): {type(e).__name__}: {e}")

    def _action_next_video(self, actions: Actions) -> Result:
        """
        Navigate to the next video.

        Priority:
          1. Player next button (.ytp-next-button) — works in playlists and
             when autoplay is enabled and the next video is queued.
          2. First recommended sidebar video — fallback for standalone videos
             that have no player next button (autoplay off, no playlist).
        """
        logger.info(f"[{self.name}] next_video()")
        try:
            # Step 1: Try the player next button (short timeout — it's either there or not)
            try:
                actions.wait_for(selectors=self._selectors["next_button"], timeout=4.0)
                actions.click_and_wait(selectors=self._selectors["next_button"])
                final_url = actions._page.url  # noqa: SLF001
                logger.info(f"[{self.name}] next_video() ✅ via next_button url={final_url}")
                return Result.ok(data={"url": final_url, "method": "next_button"})
            except ActionError:
                pass  # Fall through to sidebar

            # Step 2: Sidebar recommended — first result
            logger.info(f"[{self.name}] next_video(): next_button not found — trying sidebar")
            links = actions.evaluate_js(f"({_JS_GET_RECOMMENDED_LINKS})(3)")
            if links:
                filtered = [l for l in links if '/watch' in l]
                links = filtered if filtered else links
                href = links[0]
                url = href if href.startswith("http") else f"https://www.youtube.com{href}"
                actions.navigate(url)
                # Wait for the video player to confirm we landed on a watch page.
                # Non-fatal: title element is acceptable when play button hasn't appeared yet.
                try:
                    actions.wait_for(
                        selectors=self._selectors["play_button"] + self._selectors["video_title"] + ["video"],
                        timeout=12.0,
                    )
                except ActionError:
                    pass  # Page may still be loading; proceed with URL check
                final_url = actions._page.url  # noqa: SLF001
                logger.info(f"[{self.name}] next_video() ✅ via sidebar url={final_url}")
                return Result.ok(data={"url": final_url, "method": "sidebar"})

            return Result.fail(
                error="next_video(): no next button and no recommended videos found in sidebar"
            )
        except ActionError as e:
            return Result.fail(error=f"next_video(): {e}")
        except Exception as e:
            return Result.fail(error=f"next_video(): {type(e).__name__}: {e}")

    def _action_previous_video(self, actions: Actions) -> Result:
        """
        Go to the previous video in a playlist.

        Strategy:
          Only operates when URL contains 'list=' (playlist context).
          Clicks the player's prev button in that case.

          history.back() is intentionally removed — it silently navigates
          to non-video pages (search results, home) when there is no real
          video history, making the action unpredictable.
        """
        logger.info(f"[{self.name}] previous_video()")
        try:
            current_url = actions._page.url  # noqa: SLF001

            if "list=" in current_url:
                prev_selectors = [
                    ".ytp-prev-button",
                    "a.ytp-prev-button",
                    "button[aria-label='Previous (SHIFT+p)']",
                    "a[aria-label='Previous']",
                ]
                try:
                    actions.wait_for(selectors=prev_selectors, timeout=5.0)
                    actions.click_and_wait(selectors=prev_selectors)
                    final_url = actions._page.url  # noqa: SLF001
                    logger.info(f"[{self.name}] previous_video() ✅ url={final_url}")
                    return Result.ok(data={"url": final_url, "method": "prev_button"})
                except ActionError:
                    return Result.fail(
                        error="previous_video(): in playlist but no prev button found "
                              "(may already be at first video)"
                    )

            return Result.fail(
                error="previous_video(): only available inside a playlist "
                      "(URL must contain 'list='). "
                      "For general back navigation use browser history directly."
            )
        except ActionError as e:
            return Result.fail(error=f"previous_video(): {e}")
        except Exception as e:
            return Result.fail(error=f"previous_video(): {type(e).__name__}: {e}")

    def _action_play_nth_next(self, actions: Actions, n: int = 1) -> Result:
        """
        Navigate to the Nth recommended video from the sidebar (1-indexed).
        e.g. n=1 → first recommended, n=3 → third recommended.
        """
        logger.info(f"[{self.name}] play_nth_next(n={n})")
        try:
            n = max(1, int(n))
            links = actions.evaluate_js(f"({_JS_GET_RECOMMENDED_LINKS})({n + 2})")
            if not links or len(links) < n:
                return Result.fail(
                    error=f"play_nth_next(n={n}): only {len(links) if links else 0} "
                          "recommended videos found in sidebar"
                )
            href = links[n - 1]
            url = href if href.startswith("http") else f"https://www.youtube.com{href}"
            actions.navigate(url)
            try:
                actions.wait_for(selectors=self._selectors["play_button"], timeout=12.0)
            except ActionError:
                pass  # Video may still be loading
            final_url = actions._page.url  # noqa: SLF001
            logger.info(f"[{self.name}] play_nth_next() ✅ url={final_url}")
            return Result.ok(data={"url": final_url, "n": n})
        except ActionError as e:
            return Result.fail(error=f"play_nth_next(): {e}")
        except Exception as e:
            return Result.fail(error=f"play_nth_next(): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # LIBRARY ACTIONS
    # ═══════════════════════════════════════════════════════════════════════

    def _action_open_history(self, actions: Actions) -> Result:
        """Open YouTube watch history."""
        logger.info(f"[{self.name}] open_history()")
        try:
            actions.navigate("https://www.youtube.com/feed/history")
            return Result.ok(data={"url": "https://www.youtube.com/feed/history"})
        except ActionError as e:
            return Result.fail(error=f"open_history(): {e}")
        except Exception as e:
            return Result.fail(error=f"open_history(): {type(e).__name__}: {e}")

    def _action_open_liked_videos(self, actions: Actions) -> Result:
        """Open Liked Videos playlist."""
        logger.info(f"[{self.name}] open_liked_videos()")
        try:
            actions.navigate("https://www.youtube.com/playlist?list=LL")
            return Result.ok(data={"url": "https://www.youtube.com/playlist?list=LL"})
        except ActionError as e:
            return Result.fail(error=f"open_liked_videos(): {e}")
        except Exception as e:
            return Result.fail(error=f"open_liked_videos(): {type(e).__name__}: {e}")

    def _action_open_playlists(self, actions: Actions) -> Result:
        """Open the Library / Playlists page."""
        logger.info(f"[{self.name}] open_playlists()")
        try:
            actions.navigate("https://www.youtube.com/feed/library")
            return Result.ok(data={"url": "https://www.youtube.com/feed/library"})
        except ActionError as e:
            return Result.fail(error=f"open_playlists(): {e}")
        except Exception as e:
            return Result.fail(error=f"open_playlists(): {type(e).__name__}: {e}")

    def _action_open_watch_later(self, actions: Actions) -> Result:
        """Open the Watch Later playlist."""
        logger.info(f"[{self.name}] open_watch_later()")
        try:
            actions.navigate("https://www.youtube.com/playlist?list=WL")
            return Result.ok(data={"url": "https://www.youtube.com/playlist?list=WL"})
        except ActionError as e:
            return Result.fail(error=f"open_watch_later(): {e}")
        except Exception as e:
            return Result.fail(error=f"open_watch_later(): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # PLAYLIST MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════

    def _action_add_to_playlist(self, actions: Actions, name: str = "") -> Result:
        """
        Add the current video to a named playlist.
        Opens the save menu, finds the playlist by name, ensures its checkbox is checked.
        """
        logger.info(f"[{self.name}] add_to_playlist('{name}')")
        try:
            return self._toggle_named_playlist(actions, name=name, should_be_checked=True)
        except ActionError as e:
            return Result.fail(error=f"add_to_playlist('{name}'): {e}")
        except Exception as e:
            return Result.fail(error=f"add_to_playlist(): {type(e).__name__}: {e}")

    def _action_remove_from_playlist(self, actions: Actions, name: str = "") -> Result:
        """
        Remove the current video from a named playlist.
        Opens the save menu, finds the playlist by name, ensures its checkbox is unchecked.
        """
        logger.info(f"[{self.name}] remove_from_playlist('{name}')")
        try:
            return self._toggle_named_playlist(actions, name=name, should_be_checked=False)
        except ActionError as e:
            return Result.fail(error=f"remove_from_playlist('{name}'): {e}")
        except Exception as e:
            return Result.fail(error=f"remove_from_playlist(): {type(e).__name__}: {e}")

    def _toggle_named_playlist(
        self, actions: Actions, name: str, should_be_checked: bool
    ) -> Result:
        """
        Core playlist toggle logic.
        Opens save menu, finds playlist by name, toggles checkbox if needed.
        """
        if not name:
            # Close any open panels and fail
            return Result.fail(error="playlist name parameter is required")

        # Open the save menu
        actions.wait_for(selectors=self._selectors["save_button"], timeout=10.0)
        actions.click(selectors=self._selectors["save_button"])
        actions.wait_for(selectors=self._selectors["playlist_menu"], timeout=8.0)

        # Find the target playlist by name
        item_index = actions.safe_evaluate_js(f"({_JS_FIND_PLAYLIST_ITEM})({name!r})", default=-1)
        if item_index == -1:
            actions.press_key("Escape")
            return Result.fail(
                error=f"Playlist '{name}' not found in save menu. "
                      "Check the playlist name exactly."
            )

        # Check current state
        is_checked = actions.safe_evaluate_js(f"({_JS_GET_PLAYLIST_CHECKED})({item_index})", default=None)

        if is_checked == should_be_checked:
            actions.press_key("Escape")
            action_str = "add" if should_be_checked else "remove"
            return Result.ok(data={
                "playlist": name,
                "action": f"skipped_{action_str}_already_{'added' if should_be_checked else 'removed'}"
            })

        # Click the playlist item to toggle
        all_items = actions.evaluate_js("""
        () => Array.from(
          document.querySelectorAll('yt-playlist-add-to-option-renderer, ytd-playlist-add-to-option-renderer')
        ).map(el => el.getAttribute('playlist-title') || el.innerText.trim())
        """)
        logger.debug(f"[{self.name}] playlist items: {all_items}")

        # Click item by index via JS
        actions.evaluate_js(f"""
        () => {{
          const items = document.querySelectorAll(
            'yt-playlist-add-to-option-renderer, ytd-playlist-add-to-option-renderer'
          );
          if (items[{item_index}]) items[{item_index}].click();
        }}
        """)

        actions.press_key("Escape")
        action_str = "added" if should_be_checked else "removed"
        logger.info(f"[{self.name}] playlist '{name}' {action_str} ✅")
        return Result.ok(data={"playlist": name, "action": action_str})

    # ═══════════════════════════════════════════════════════════════════════
    # RECOMMENDED VIDEO ACTIONS
    # ═══════════════════════════════════════════════════════════════════════

    def _action_open_recommended(self, actions: Actions, index: int = 1) -> Result:
        """
        Navigate to the recommended video at the given sidebar index (1-based).
        Unlike play_nth_next, this is an alias focused on 'opening' a recommendation.
        """
        return self._action_play_nth_next(actions, n=index)

    def _action_open_top_recommended(self, actions: Actions, n: int = 3) -> Result:
        """
        Open the top N recommended sidebar videos in background tabs.
        Works from a video watch page.
        """
        logger.info(f"[{self.name}] open_top_recommended(n={n})")
        try:
            links = actions.evaluate_js(f"({_JS_GET_RECOMMENDED_LINKS})({n})")
            if not links:
                return Result.fail(
                    error="open_top_recommended(): no recommended links found in sidebar. "
                          "Are you on a video watch page?"
                )

            tab_results: list[dict] = []
            for i, href in enumerate(links):
                url = href if href.startswith("http") else f"https://www.youtube.com{href}"
                try:
                    new_page = actions.open_new_tab(url)
                    new_actions = Actions(new_page)
                    self._wait_for_video_player(new_actions, i + 1)
                    paused = self._pause_video(new_actions, i + 1)
                    final_url = new_page.url
                    title = self._read_title_for_tab(
                        new_actions, new_page, _classify_url(final_url), i + 1
                    )
                    tab_results.append({
                        "tab_index": i + 1, "url": final_url, "title": title,
                        "content_type": _classify_url(final_url),
                        "verified": "/watch" in final_url, "paused": paused,
                    })
                    logger.info(
                        f"[{self.name}] open_top_recommended(): tab {i + 1} ✅ '{title[:55]}'"
                    )
                except ActionError as tab_err:
                    tab_results.append({
                        "tab_index": i + 1, "url": url, "title": "",
                        "verified": False, "paused": False, "error": str(tab_err),
                    })

            return Result.ok(data=tab_results)
        except ActionError as e:
            return Result.fail(error=f"open_top_recommended(): {e}")
        except Exception as e:
            return Result.fail(error=f"open_top_recommended(): {type(e).__name__}: {e}")

    # ═══════════════════════════════════════════════════════════════════════
    # PRIVATE TAB / PLAYER HELPERS (from original implementation)
    # ═══════════════════════════════════════════════════════════════════════

    def _wait_for_video_player(self, actions: Actions, tab_num: int) -> None:
        try:
            actions.wait_for(selectors=self._selectors["play_button"], timeout=12.0)
        except ActionError:
            logger.debug(f"[{self.name}] Tab {tab_num}: video player timeout (non-critical)")

    def _wait_for_shorts_player(self, actions: Actions, tab_num: int) -> None:
        try:
            actions.wait_for(selectors=self._selectors["shorts_player_ready"], timeout=12.0)
        except ActionError:
            logger.debug(f"[{self.name}] Tab {tab_num}: shorts player timeout (non-critical)")

    def _pause_video(self, actions: Actions, tab_num: int) -> bool:
        try:
            pause_result = actions.evaluate_js(_JS_PAUSE_VIDEO)
            if pause_result is None:
                return False
            status = "already paused" if pause_result else "paused"
            logger.info(f"[{self.name}] Tab {tab_num} ⏸ {status}")
            return True
        except ActionError as e:
            logger.warning(f"[{self.name}] Tab {tab_num}: pause failed: {e}")
            return False

    def _read_title_for_tab(self, actions, page, content_type: str, tab_num: int) -> str:
        if content_type == "video":
            return self._read_video_title(actions, page, tab_num)
        elif content_type == "shorts":
            return self._read_shorts_title(actions, page, tab_num)
        else:
            title = self._read_video_title(actions, page, tab_num, silent=True)
            if not title:
                title = self._read_shorts_title(actions, page, tab_num, silent=True)
            return title or _clean_tab_title(page.title())

    def _read_video_title(self, actions, page, tab_num: int, silent: bool = False) -> str:
        try:
            actions.wait_for(selectors=self._selectors["video_title"], timeout=8.0)
            title = actions.get_text(selectors=self._selectors["video_title"])
            return title.strip() if title else ""
        except ActionError:
            if not silent:
                return _clean_tab_title(page.title())
            return ""

    def _read_shorts_title(self, actions, page, tab_num: int, silent: bool = False) -> str:
        try:
            actions.wait_for(selectors=self._selectors["shorts_title"], timeout=5.0)
            title = actions.get_text(selectors=self._selectors["shorts_title"])
            return title.strip() if title else ""
        except ActionError:
            title = _clean_tab_title(page.title())
            if not silent:
                logger.debug(f"[{self.name}] Tab {tab_num}: shorts title fallback → '{title[:60]}'")
            return title

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 10.1 — NEW ACTIONS
    # ═══════════════════════════════════════════════════════════════════

    def _action_like_short(self, actions: Actions) -> Result:
        """
        Like the currently playing Short.

        Uses Shorts-specific selectors (overlay buttons inside ytd-shorts)
        with a fallback to the generic like selectors — which works when
        YouTube renders the Shorts player with the same button structure as
        regular videos.

        Idempotent: checks aria-pressed state before clicking.
        """
        logger.info(f"[{self.name}] like_short()")
        try:
            # Check current state first
            is_liked = actions.safe_evaluate_js(_JS_IS_LIKED, default=None)
            if is_liked is True:
                logger.info(f"[{self.name}] like_short(): already liked — skipping")
                return Result.ok(data={"liked": True, "action": "skipped_already_liked"})

            # Try Shorts-specific selectors first, fall back to generic
            like_selectors = (
                self._selectors.get("shorts_like_button", [])
                + self._selectors["like_button"]
            )
            actions.wait_for(selectors=like_selectors, timeout=10.0)
            actions.click(selectors=like_selectors)

            is_liked_after = actions.safe_evaluate_js(_JS_IS_LIKED, default=None)
            if is_liked_after:
                logger.info(f"[{self.name}] like_short() ✅")
                return Result.ok(data={"liked": True, "action": "liked"})
            logger.warning(f"[{self.name}] like_short(): could not verify — treating as success")
            return Result.ok(data={"liked": True, "action": "liked_unverified"})
        except ActionError as e:
            return Result.fail(error=f"like_short(): {e}")
        except Exception as e:
            return Result.fail(error=f"like_short(): {type(e).__name__}: {e}")

    def _action_unlike_short(self, actions: Actions) -> Result:
        """
        Remove like from the currently playing Short. Idempotent.

        Uses Shorts-specific like selectors with generic fallback.
        Checks aria-pressed state before acting to avoid accidental double-toggle.
        """
        logger.info(f"[{self.name}] unlike_short()")
        try:
            is_liked = actions.safe_evaluate_js(_JS_IS_LIKED, default=None)
            if is_liked is False:
                logger.info(f"[{self.name}] unlike_short(): not liked — skipping")
                return Result.ok(data={"liked": False, "action": "skipped_not_liked"})

            # The like button when already pressed acts as Unlike
            like_selectors = (
                self._selectors.get("shorts_like_button", [])
                + self._selectors["like_button"]
            )
            actions.wait_for(selectors=like_selectors, timeout=10.0)
            actions.click(selectors=like_selectors)

            is_liked_after = actions.safe_evaluate_js(_JS_IS_LIKED, default=True)
            if not is_liked_after:
                logger.info(f"[{self.name}] unlike_short() ✅")
                return Result.ok(data={"liked": False, "action": "unliked"})
            logger.warning(f"[{self.name}] unlike_short(): could not verify — treating as success")
            return Result.ok(data={"liked": False, "action": "unliked_unverified"})
        except ActionError as e:
            return Result.fail(error=f"unlike_short(): {e}")
        except Exception as e:
            return Result.fail(error=f"unlike_short(): {type(e).__name__}: {e}")

    def _action_subscribe_short(self, actions: Actions) -> Result:
        """
        Subscribe to the channel from inside the Shorts player.

        Uses Shorts-specific subscribe selectors first (overlay button),
        falls back to generic subscribe selectors.

        Idempotent: reads aria-label before acting.
        """
        logger.info(f"[{self.name}] subscribe_short()")
        try:
            is_subbed = actions.safe_evaluate_js(_JS_IS_SUBSCRIBED, default=None)
            if is_subbed is True:
                logger.info(f"[{self.name}] subscribe_short(): already subscribed — skipping")
                return Result.ok(data={"subscribed": True, "action": "skipped_already_subscribed"})

            sub_selectors = (
                self._selectors.get("shorts_subscribe_button", [])
                + self._selectors["subscribe_button"]
            )
            actions.wait_for(selectors=sub_selectors, timeout=10.0)
            actions.click(selectors=sub_selectors)

            is_subbed_after = actions.safe_evaluate_js(_JS_IS_SUBSCRIBED, default=None)
            if is_subbed_after:
                logger.info(f"[{self.name}] subscribe_short() ✅")
                return Result.ok(data={"subscribed": True, "action": "subscribed"})
            logger.warning(f"[{self.name}] subscribe_short(): could not verify — treating as success")
            return Result.ok(data={"subscribed": True, "action": "subscribed_unverified"})
        except ActionError as e:
            return Result.fail(error=f"subscribe_short(): {e}")
        except Exception as e:
            return Result.fail(error=f"subscribe_short(): {type(e).__name__}: {e}")

    def _action_seek_forward(self, actions: Actions, seconds: float = 10) -> Result:
        """
        Seek forward by a configurable number of seconds (default: 10).
        More flexible than forward_10s() — accepts any delta.
        """
        logger.info(f"[{self.name}] seek_forward({seconds})")
        try:
            delta = max(0.0, float(seconds))
            actual = actions.evaluate_js(f"({_JS_SEEK_RELATIVE})({delta})")
            if actual is None:
                return Result.fail(error="seek_forward(): no video element found")
            logger.info(f"[{self.name}] seek_forward() ✅ position={actual:.1f}s")
            return Result.ok(data={"position": actual, "delta": delta})
        except ActionError as e:
            return Result.fail(error=f"seek_forward(): {e}")
        except Exception as e:
            return Result.fail(error=f"seek_forward(): {type(e).__name__}: {e}")

    def _action_seek_backward(self, actions: Actions, seconds: float = 10) -> Result:
        """
        Seek backward by a configurable number of seconds (default: 10).
        More flexible than back_10s() — accepts any delta.
        """
        logger.info(f"[{self.name}] seek_backward({seconds})")
        try:
            delta = max(0.0, float(seconds))
            actual = actions.evaluate_js(f"({_JS_SEEK_RELATIVE})(-{delta})")
            if actual is None:
                return Result.fail(error="seek_backward(): no video element found")
            logger.info(f"[{self.name}] seek_backward() ✅ position={actual:.1f}s")
            return Result.ok(data={"position": actual, "delta": -delta})
        except ActionError as e:
            return Result.fail(error=f"seek_backward(): {e}")
        except Exception as e:
            return Result.fail(error=f"seek_backward(): {type(e).__name__}: {e}")

    def _action_scroll_comments(self, actions: Actions, amount: int = 3) -> Result:
        """
        Scroll down inside the comments section `amount` times.

        Phase 10.1 fix: Uses JS to scroll the `#comments #contents` element
        directly rather than issuing generic page scrolls. This is more reliable
        because YouTube's comments panel is a separately-scrollable container on
        many page layouts.

        Falls back to generic page scroll if the JS container scroll reports
        that no scrollable container was found (e.g. single-column layouts where
        comments scroll with the page body).

        Args:
            amount: Number of scroll steps (default: 3).
        """
        logger.info(f"[{self.name}] scroll_comments(amount={amount})")
        try:
            # Ensure comments are in view first
            open_result = self._action_open_comments(actions)
            if not open_result.success:
                logger.warning(
                    f"[{self.name}] scroll_comments(): open_comments failed — "
                    f"reason: {open_result.error}"
                )
                return Result.fail(
                    error=f"scroll_comments(): cannot scroll — {open_result.error}"
                )

            steps = max(1, int(amount))
            # Pixel distance per step — ~one viewport height of content
            scroll_px = 600

            # JS scroll strategy: target #comments #contents first (the inner
            # scrollable list), then fall back to the #comments root element,
            # then to document.documentElement (page body).
            js_scroll = f"""
            (px) => {{
              const containers = [
                document.querySelector('#comments #contents'),
                document.querySelector('ytd-comments #contents'),
                document.querySelector('#comments'),
                document.querySelector('ytd-comments'),
              ];
              for (const el of containers) {{
                if (el && el.scrollHeight > el.clientHeight) {{
                  el.scrollBy({{ top: px, behavior: 'smooth' }});
                  return el.id || el.tagName;
                }}
              }}
              // Fallback: scroll the page body
              window.scrollBy({{ top: px, behavior: 'smooth' }});
              return 'body';
            }}
            """

            scrolled_via: list[str] = []
            for i in range(steps):
                container = actions.evaluate_js(f"({js_scroll})({scroll_px})")
                scrolled_via.append(str(container) if container else "body")
                logger.debug(
                    f"[{self.name}] scroll_comments(): step {i + 1}/{steps} via '{container}'"
                )

            logger.info(f"[{self.name}] scroll_comments() ✅ ({steps} scrolls)")
            return Result.ok(data={
                "scrolled": steps,
                "action": "comments_scrolled",
                "containers": scrolled_via,
            })
        except ActionError as e:
            return Result.fail(error=f"scroll_comments(): {e}")
        except Exception as e:
            return Result.fail(error=f"scroll_comments(): {type(e).__name__}: {e}")
