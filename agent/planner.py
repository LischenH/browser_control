"""
agent/planner.py — Step-Datentyp + Planner-Interface (Phase 4/5/7/8/9/10)

Design:
  - Step: Dataclass with url, action_name, params, verify_conditions, description
  - Planner: public interface plan(goal: str) → list[Step]  ← stable, never changes
  - TemplateEngine: keyword matching (Phase 4/5/7/9/10, offline, fast)
  - LLMEngine: Local Ollama LLM planner (Phase 8)
  - Engine selection via config.PLANNER_ENGINE

Phase 10 — Full Platform Agent:
  New TemplateEngine patterns:
    YouTube on-page:
      "like this video"
      "subscribe to this channel"
      "play next video"
      "play the 3rd next video"
      "increase speed to 1.5x"
      "skip 10 seconds"
      "go back 10 seconds"
      "open comments"
      "fullscreen"
      "go to channel"
      "go to channel [name]"
      "open history"
      "open liked videos"
      "open watch later"
      "save to watch later" / "add to watch later"
    Shorts:
      "next short"
      "previous short"
      "like this short"
    Amazon on-page:
      "add to cart"
      "add to wishlist"
      "buy now"
      "open cart"
      "open orders"
      "open wishlist"
      "read price"
      "read rating"

Stable Contract:
  planner.plan(goal: str) → list[Step]

Step fields:
  url               – URL fragment for skill selection (e.g. "youtube.com")
                      Empty string → uses currently loaded page
  action_name       – Name of action to call (e.g. "search")
  params            – kwargs for the action (e.g. {"query": "lo-fi music"})
  verify_conditions – Conditions dict for Verifier
  description       – Human-readable description for logging
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import config

logger = logging.getLogger(__name__)


# ── Step-Dataclass ────────────────────────────────────────────────────────────

@dataclass
class Step:
    """
    A single execution step for the Executor.

    Fields:
        url               : URL fragment for skill routing (empty = current page)
        action_name       : Action name on the corresponding skill
        params            : Parameter dict for the action (passed as **kwargs)
        verify_conditions : Conditions dict for Verifier after action
        description       : Human-readable description (for logs + debugging)
    """
    action_name: str
    url: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    verify_conditions: dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def __repr__(self) -> str:
        desc = f" ({self.description!r})" if self.description else ""
        return (
            f"Step(action={self.action_name!r}, url={self.url!r}, "
            f"params={self.params}, conditions={list(self.verify_conditions.keys())}){desc}"
        )


# ── Engine interface ──────────────────────────────────────────────────────────

class _PlannerEngine:
    def plan(self, goal: str) -> list[Step]:
        raise NotImplementedError


# ── Shared verify condition presets ──────────────────────────────────────────

_YT_ON_PAGE_VERIFY = {
    "url_contains": "youtube.com",
    "element_exists": ["video", "#movie_player", "ytd-shorts"],
}

_YT_WATCH_VERIFY = {
    "url_contains": "watch",
    "element_exists": [".ytp-play-button", "button[aria-label='Play (k)']",
                       "button[aria-label='Pause (k)']"],
}

_YT_HOME_VERIFY = {
    "url_contains": "youtube.com",
    "element_exists": ["#search-input", "input[name='search_query']"],
}

_AMZ_ON_PAGE_VERIFY = {
    "url_contains": "amazon",
    "element_exists": ["#productTitle", "span#productTitle"],
}

_AMZ_CART_VERIFY = {
    "url_contains": "amazon",
    "element_exists": ["#nav-cart", "#activeCartViewForm", ".sc-list-item"],
}

_AMZ_ORDERS_VERIFY = {
    "url_contains": "amazon",
    "element_exists": [".order-info", "#yourOrders", ".a-box-group"],
}


# ── TemplateEngine ────────────────────────────────────────────────────────────

class _TemplateEngine(_PlannerEngine):
    """
    Keyword-based planning strategy.

    Supports two categories of goals:
      1. SEARCH goals: navigate → search → (click/open)
      2. ON-PAGE goals: single step on currently open page (no navigate needed)

    All on-page goals produce a single Step with url="" (uses current page).
    Combined on-page goals ("like and subscribe") produce multiple steps.
    """

    # ── YouTube search patterns ───────────────────────────────────────────────
    _RE_YT_TOP_N = re.compile(
        r"search\s+youtube\s+for\s+(.+?)\s+and\s+open\s+(?:top|first)\s+(\d+)"
        r"(?:\s+(shorts?|videos?|both|any))?",
        re.IGNORECASE,
    )
    _RE_YT_SEARCH = re.compile(
        r"search\s+youtube\s+for\s+(.+?)(?:\s+and\s+click.*)?$",
        re.IGNORECASE,
    )

    # ── Amazon search patterns ────────────────────────────────────────────────
    _RE_AMZ_TOP_N = re.compile(
        r"search\s+amazon\s+for\s+(.+?)\s+and\s+open\s+(?:top|first)\s+(\d+)",
        re.IGNORECASE,
    )
    _RE_AMZ_SEARCH = re.compile(
        r"search\s+amazon\s+for\s+(.+?)(?:\s+and\s+click.*)?$",
        re.IGNORECASE,
    )
    _RE_CLICK = re.compile(r"and\s+click", re.IGNORECASE)

    # ── YouTube on-page patterns ──────────────────────────────────────────────
    # Speed: "set speed to 1.5x", "increase speed to 2x", "speed 1.25"
    _RE_SET_SPEED = re.compile(
        r"(?:set|increase|decrease|change|speed)[\s\w]*?(\d+\.?\d*)\s*x?",
        re.IGNORECASE,
    )
    # FIX 2: "seek to 90 seconds" — added optional (?:\s+to)? after "seek"
    # so "seek to 90 seconds", "seek 90s", and "go to 120 seconds" all match.
    _RE_SEEK = re.compile(
        r"(?:seek(?:\s+to)?|go\s+to)\s+(\d+\.?\d*)\s*(?:seconds?|secs?|s\b)",
        re.IGNORECASE,
    )
    # Nth next: "play the 3rd next video", "open 3rd next video"
    _RE_NTH_NEXT = re.compile(
        r"(?:play|open)\s+(?:the\s+)?(\d+)(?:st|nd|rd|th)?\s+next\s+video",
        re.IGNORECASE,
    )
    # N recommended: "open next 3 videos", "open top 3 recommended"
    _RE_TOP_RECOMMENDED = re.compile(
        r"open\s+(?:next|top)\s+(\d+)\s+(?:videos?|recommended)",
        re.IGNORECASE,
    )
    # Go to named channel: "go to channel MrBeast", "open channel veritasium"
    _RE_GO_TO_CHANNEL_NAMED = re.compile(
        r"(?:go\s+to|open|navigate\s+to)\s+channel\s+(.+)",
        re.IGNORECASE,
    )
    # Set quality: "set quality to 1080p", "change quality 720p"
    _RE_SET_QUALITY = re.compile(
        r"(?:set|change|switch)\s+quality\s+(?:to\s+)?(\w+p?)",
        re.IGNORECASE,
    )
    # Add to named playlist: "add to playlist Music", "save to playlist Favorites"
    _RE_ADD_PLAYLIST = re.compile(
        r"add\s+to\s+playlist\s+(.+)",
        re.IGNORECASE,
    )
    _RE_REMOVE_PLAYLIST = re.compile(
        r"remove\s+from\s+playlist\s+(.+)",
        re.IGNORECASE,
    )
    # Phase 10.1 — Shorts batch scroll: "watch next 5 shorts"
    _RE_SHORTS_SCROLL_N = re.compile(
        r"(?:watch|scroll|view)\s+(?:next\s+)?(\d+)\s+shorts?",
        re.IGNORECASE,
    )
    # seek forward/backward with custom delta: "seek forward 30 seconds"
    _RE_SEEK_FORWARD = re.compile(
        r"(?:seek|skip)\s+forward\s+(\d+\.?\d*)\s*(?:seconds?|secs?|s\b)?|forward\s+(\d+\.?\d*)\s*(?:seconds?|secs?|s\b)",
        re.IGNORECASE,
    )
    _RE_SEEK_BACKWARD = re.compile(
        r"(?:seek|skip)\s+(?:back(?:ward)?|rewind)\s+(\d+\.?\d*)\s*(?:seconds?|secs?|s\b)?|(?:rewind|back)\s+(\d+\.?\d*)\s*(?:seconds?|secs?|s\b)",
        re.IGNORECASE,
    )
    # Amazon read reviews: "read 5 reviews", "show 3 reviews"
    _RE_READ_REVIEWS = re.compile(
        r"(?:read|show|get)\s+(\d+)\s+reviews?",
        re.IGNORECASE,
    )

    def plan(self, goal: str) -> list[Step]:
        g = goal.strip()

        # ── Phase 9/10: YouTube search + open top N ───────────────────────────
        yt_top = self._RE_YT_TOP_N.search(g)
        if yt_top:
            query = yt_top.group(1).strip()
            n = int(yt_top.group(2))
            ct_raw = (yt_top.group(3) or "").lower()
            ct = "shorts" if ct_raw.startswith("short") else (
                "video" if ct_raw.startswith("video") else "any"
            )
            return self._plan_yt_open_top(query, n, ct)

        # ── YouTube search ────────────────────────────────────────────────────
        yt_match = self._RE_YT_SEARCH.match(g)
        if yt_match:
            query = yt_match.group(1).strip()
            wants_click = bool(self._RE_CLICK.search(g))
            return (self._plan_yt_search_and_click(query) if wants_click
                    else self._plan_yt_search(query))

        # ── YouTube navigation ────────────────────────────────────────────────
        if re.match(r"open\s+youtube", g, re.IGNORECASE):
            return self._plan_yt_navigate()

        # ── Phase 9/10: Amazon search + open top N ────────────────────────────
        amz_top = self._RE_AMZ_TOP_N.search(g)
        if amz_top:
            return self._plan_amz_open_top(amz_top.group(1).strip(), int(amz_top.group(2)))

        # ── Amazon search ─────────────────────────────────────────────────────
        amz_match = self._RE_AMZ_SEARCH.match(g)
        if amz_match:
            query = amz_match.group(1).strip()
            wants_click = bool(self._RE_CLICK.search(g))
            return (self._plan_amz_search_and_click(query) if wants_click
                    else self._plan_amz_search(query))

        # ── Amazon navigation ─────────────────────────────────────────────────
        if re.match(r"open\s+amazon", g, re.IGNORECASE):
            return self._plan_amz_navigate()

        # ── Phase 10: YouTube ON-PAGE actions ─────────────────────────────────
        yt_steps = self._try_yt_on_page(g)
        if yt_steps:
            return yt_steps

        # ── Phase 10: Amazon ON-PAGE actions ──────────────────────────────────
        amz_steps = self._try_amz_on_page(g)
        if amz_steps:
            return amz_steps

        logger.warning(
            f"[TemplateEngine] Unknown goal: '{g}'. "
            "No steps generated. (Use LLMEngine for complex goals.)"
        )
        return []

    # ── YouTube ON-PAGE action resolver ──────────────────────────────────────

    def _try_yt_on_page(self, g: str) -> list[Step]:
        """
        Attempts to match YouTube on-page commands.
        Returns a list of Steps if matched, empty list otherwise.

        On-page actions operate on the currently visible page.
        They use url="youtube.com" so the SkillManager routes to YouTubeSkill.
        verify_conditions only check we're still on YouTube.
        """
        steps: list[Step] = []

        # ── Combined patterns first ───────────────────────────────────────────
        # "like this video and subscribe"
        if re.search(r"\blike\b", g, re.IGNORECASE) and re.search(r"\bsubscribe\b", g, re.IGNORECASE):
            steps.append(self._yt_step("like", "Like this video"))
            steps.append(self._yt_step("subscribe", "Subscribe to this channel"))
            return steps
        
        # Phase 10.1: "watch next N shorts"
        shorts_n_m = self._RE_SHORTS_SCROLL_N.search(g)
        if shorts_n_m:
            n = int(shorts_n_m.group(1))
            return [self._yt_step("next_short", f"Watch short {i+1}/{n}") for i in range(n)]

        # Phase 10.1: "seek forward 30s"
        sf_m = self._RE_SEEK_FORWARD.search(g)
        if sf_m:
            secs = float(sf_m.group(1) or sf_m.group(2) or 10)
            return [self._yt_step("seek_forward", f"Seek forward {secs}s", seconds=secs)]

        # Phase 10.1: "rewind 30s"  
        sb_m = self._RE_SEEK_BACKWARD.search(g)
        if sb_m:
            secs = float(sb_m.group(1) or sb_m.group(2) or 10)
            return [self._yt_step("seek_backward", f"Seek backward {secs}s", seconds=secs)]

        # Phase 10.1: scroll comments
        if re.search(r"scroll\s+comments?", g, re.IGNORECASE):
            amt_m = re.search(r"(\d+)\s+times?", g, re.IGNORECASE)
            return [self._yt_step("scroll_comments", "Scroll comments", amount=int(amt_m.group(1)) if amt_m else 3)]

        # Phase 10.1: like this short
        if re.search(r"like\s+(?:this\s+)?short", g, re.IGNORECASE):
            return [self._yt_step("like_short", "Like this Short")]

        # ── Speed ─────────────────────────────────────────────────────────────
        speed_m = self._RE_SET_SPEED.search(g)
        if speed_m and re.search(r"speed|rate|playback", g, re.IGNORECASE):
            speed = float(speed_m.group(1))
            return [self._yt_step("set_speed", f"Set speed to {speed}x", speed=speed)]

        # ── Seek absolute ─────────────────────────────────────────────────────
        seek_m = self._RE_SEEK.search(g)
        if seek_m:
            seconds = float(seek_m.group(1))
            return [self._yt_step("seek", f"Seek to {seconds}s", seconds=seconds)]

        # ── Skip / forward 10s ────────────────────────────────────────────────
        if re.search(r"skip\s+(?:10|forward|ahead)", g, re.IGNORECASE) or \
           re.search(r"forward\s+10", g, re.IGNORECASE) or \
           re.search(r"skip\s+10\s*s", g, re.IGNORECASE):
            return [self._yt_step("forward_10s", "Skip forward 10 seconds")]

        # ── Back 10s ──────────────────────────────────────────────────────────
        if re.search(r"back\s+10|rewind\s+10|go\s+back\s+10", g, re.IGNORECASE):
            return [self._yt_step("back_10s", "Go back 10 seconds")]

        # ── Nth next video ────────────────────────────────────────────────────
        nth_m = self._RE_NTH_NEXT.search(g)
        if nth_m:
            n = int(nth_m.group(1))
            return [self._yt_step("play_nth_next", f"Play {n}th next recommended video", n=n)]

        # ── Open next N recommended ───────────────────────────────────────────
        top_rec_m = self._RE_TOP_RECOMMENDED.search(g)
        if top_rec_m:
            n = int(top_rec_m.group(1))
            return [self._yt_step(
                "open_top_recommended", f"Open top {n} recommended videos in background tabs", n=n
            )]

        # ── Named channel navigation ──────────────────────────────────────────
        named_ch_m = self._RE_GO_TO_CHANNEL_NAMED.search(g)
        if named_ch_m:
            name = named_ch_m.group(1).strip()
            return [self._yt_step("go_to_channel_by_name", f"Go to channel '{name}'", name=name)]

        # ── Quality setting ───────────────────────────────────────────────────
        quality_m = self._RE_SET_QUALITY.search(g)
        if quality_m:
            quality = quality_m.group(1)
            return [self._yt_step("set_quality", f"Set quality to {quality}", quality=quality)]

        # ── Add to named playlist ─────────────────────────────────────────────
        add_pl_m = self._RE_ADD_PLAYLIST.search(g)
        if add_pl_m:
            name = add_pl_m.group(1).strip()
            return [self._yt_step("add_to_playlist", f"Add to playlist '{name}'", name=name)]

        remove_pl_m = self._RE_REMOVE_PLAYLIST.search(g)
        if remove_pl_m:
            name = remove_pl_m.group(1).strip()
            return [self._yt_step("remove_from_playlist", f"Remove from playlist '{name}'", name=name)]

        # ── Simple keyword mappings ────────────────────────────────────────────
        # IMPORTANT: order matters — more-specific patterns must come before
        # broader ones that share keywords (unlike before like, exit_fullscreen
        # before fullscreen).
        _kw: list[tuple[str, str, str, dict]] = [
            # (regex_pattern, action_name, description, params)
            # FIX 1: unlike/remove like BEFORE like — "remove like" contains the
            #         word "like", so the unlike entry must be tested first.
            (r"\bunlike\b|\bremove\s+like\b", "unlike", "Remove like", {}),
            (r"\blike\b(?!\s+this\s+product)", "like", "Like this video/short", {}),
            (r"\bsubscribe\b", "subscribe", "Subscribe to this channel", {}),
            (r"\bunsubscribe\b", "unsubscribe", "Unsubscribe from this channel", {}),
            (r"save\s+to\s+watch\s+later|add\s+to\s+watch\s+later", "save_to_watch_later",
             "Save to Watch Later", {}),
            (r"remove\s+from\s+watch\s+later", "remove_from_watch_later",
             "Remove from Watch Later", {}),
            (r"\bplay\b(?!\s+next|\s+the|\s+nth)", "play", "Play video", {}),
            (r"\bpause\b", "pause", "Pause video", {}),
            (r"toggle\s+play|play.*?toggle", "toggle_play", "Toggle play/pause", {}),
            (r"toggle\s+sub(?:title)?|toggle\s+cc\b|captions?\s+on|captions?\s+off",
             "toggle_subtitles", "Toggle subtitles/CC", {}),
            (r"toggle\s+autoplay|autoplay\s+(?:on|off)", "toggle_autoplay",
             "Toggle autoplay", {}),
            # FIX 3: exit_fullscreen BEFORE fullscreen — "exit fullscreen" contains
            #         the word "fullscreen", so the exit entry must be tested first.
            (r"exit\s+fullscreen|leave\s+fullscreen", "exit_fullscreen",
             "Exit fullscreen", {}),
            (r"\bfullscreen\b(?!\s+exit|\s+off)|enter\s+fullscreen", "fullscreen",
             "Enter fullscreen", {}),
            (r"(?:next\s+video|play\s+next(?:\s+video)?)\b(?!\s+\d)", "next_video",
             "Play next video", {}),
            (r"previous\s+video|go\s+back(?:\s+to\s+video)?", "previous_video",
             "Go to previous video", {}),
            (r"go\s+to\s+channel\b(?!\s+\w)|open\s+channel\b(?!\s+\w)", "go_to_channel",
             "Go to current video's channel", {}),
            (r"(?:open|show)\s+comments?", "open_comments", "Open comments section", {}),
            (r"next\s+short|next\s+shorts", "next_short", "Next Short", {}),
            (r"prev(?:ious)?\s+short|back\s+short", "prev_short", "Previous Short", {}),
            (r"go\s+home|youtube\s+home", "go_home", "Go to YouTube homepage", {}),
            (r"shorts?\s+home|go\s+to\s+shorts", "go_shorts_home",
             "Go to YouTube Shorts home", {}),
            (r"open\s+history|watch\s+history", "open_history",
             "Open watch history", {}),
            (r"(?:open|show)\s+liked\s+videos?|my\s+liked", "open_liked_videos",
             "Open liked videos", {}),
            (r"(?:open|show)\s+playlists?|my\s+playlists?", "open_playlists",
             "Open playlists", {}),
            (r"(?:open|show)\s+watch\s+later", "open_watch_later",
             "Open Watch Later playlist", {}),
        ]

        for pattern, action_name, description, params in _kw:
            if re.search(pattern, g, re.IGNORECASE):
                return [self._yt_step(action_name, description, **params)]

        return []

    def _yt_step(self, action_name: str, description: str, **params) -> Step:
        """Build a YouTube on-page Step with standard verify conditions."""
        return Step(
            url="youtube.com",
            action_name=action_name,
            params=params,
            verify_conditions=_YT_ON_PAGE_VERIFY,
            description=description,
        )

    # ── Amazon ON-PAGE action resolver ────────────────────────────────────────

    def _try_amz_on_page(self, g: str) -> list[Step]:
        """
        Attempts to match Amazon on-page commands.
        Returns a list of Steps if matched, empty list otherwise.
        """
        # Read N reviews
        rev_m = self._RE_READ_REVIEWS.search(g)
        if rev_m:
            n = int(rev_m.group(1))
            return [self._amz_step("read_reviews", f"Read {n} customer reviews", n=n)]

        _kw: list[tuple[str, str, str, dict]] = [
            (r"add\s+(?:this\s+)?(?:product\s+)?to\s+cart|add\s+to\s+cart",
             "add_to_cart", "Add product to cart", {}),
            (r"remove\s+from\s+cart", "remove_from_cart", "Remove from cart", {}),
            (r"add\s+(?:this\s+)?(?:product\s+)?to\s+wishlist|add\s+to\s+wishlist",
             "add_to_wishlist", "Add product to wishlist", {}),
            (r"remove\s+from\s+wishlist", "remove_from_wishlist",
             "Remove from wishlist", {}),
            (r"buy\s+now|purchase\s+now", "buy_now", "Buy this product now", {}),
            (r"(?:open|view|go\s+to)\s+(?:my\s+)?(?:shopping\s+)?cart",
             "open_cart", "Open shopping cart", {}),
            (r"(?:open|view|show)\s+(?:my\s+)?orders?|order\s+history",
             "open_orders", "Open order history", {}),
            (r"(?:open|view|show)\s+(?:my\s+)?wishlist",
             "open_wishlist", "Open wishlist", {}),
            (r"(?:read|show|get)\s+(?:the\s+)?price|what(?:'s|\s+is)\s+the\s+price",
             "read_price", "Read product price", {}),
            (r"(?:read|show|get)\s+(?:the\s+)?rating|what(?:'s|\s+is)\s+the\s+rating",
             "read_rating", "Read product rating", {}),
            (r"(?:read|show|get)\s+reviews?",
             "read_reviews", "Read product reviews", {"n": 3}),
        ]

        for pattern, action_name, description, params in _kw:
            if re.search(pattern, g, re.IGNORECASE):
                return [self._amz_step(action_name, description, **params)]

        return []

    def _amz_step(self, action_name: str, description: str, **params) -> Step:
        """Build an Amazon on-page Step with standard verify conditions."""
        return Step(
            url="amazon",
            action_name=action_name,
            params=params,
            verify_conditions={"url_contains": "amazon",
                               "element_exists": ["body"]},
            description=description,
        )

    # ── YouTube plan templates (unchanged + phase 10 additions) ──────────────

    @staticmethod
    def _plan_yt_navigate() -> list[Step]:
        return [Step(
            url="", action_name="navigate",
            params={"url": "https://www.youtube.com"},
            verify_conditions={
                "url_contains": "youtube.com",
                "element_exists": ["#search-input", "input[name='search_query']"],
            },
            description="Navigate to YouTube",
        )]

    @staticmethod
    def _plan_yt_search(query: str) -> list[Step]:
        return [
            Step(url="", action_name="navigate",
                 params={"url": "https://www.youtube.com"},
                 verify_conditions={"url_contains": "youtube.com"},
                 description="Navigate to YouTube"),
            Step(url="youtube.com", action_name="search",
                 params={"query": query},
                 verify_conditions={
                     "url_contains": "results",
                     "element_exists": ["ytd-video-renderer",
                                        "#contents ytd-item-section-renderer ytd-video-renderer"],
                 },
                 description=f"Search for '{query}'"),
        ]

    @staticmethod
    def _plan_yt_search_and_click(query: str) -> list[Step]:
        return [
            Step(url="", action_name="navigate",
                 params={"url": "https://www.youtube.com"},
                 verify_conditions={"url_contains": "youtube.com"},
                 description="Navigate to YouTube"),
            Step(url="youtube.com", action_name="search",
                 params={"query": query},
                 verify_conditions={
                     "url_contains": "results",
                     "element_exists": ["ytd-video-renderer"],
                 },
                 description=f"Search for '{query}'"),
            Step(url="youtube.com", action_name="click_first_video",
                 params={},
                 verify_conditions={
                     "url_contains": "watch",
                     "element_exists": [".ytp-play-button",
                                        "button[aria-label='Play (k)']",
                                        "button[aria-label='Pause (k)']"],
                 },
                 description="Click first video"),
            Step(url="youtube.com", action_name="read_title",
                 params={},
                 verify_conditions={
                     "url_contains": "watch",
                     "element_exists": ["h1.ytd-watch-metadata yt-formatted-string", "#title h1"],
                 },
                 description="Read video title"),
        ]

    @staticmethod
    def _plan_yt_open_top(query: str, n: int, content_type: str = "any") -> list[Step]:
        type_labels = {"any": "Videos/Shorts", "video": "Videos", "shorts": "Shorts"}
        label = type_labels.get(content_type, "results")
        return [
            Step(url="", action_name="navigate",
                 params={"url": "https://www.youtube.com"},
                 verify_conditions={"url_contains": "youtube.com"},
                 description="Navigate to YouTube"),
            Step(url="youtube.com", action_name="search",
                 params={"query": query},
                 verify_conditions={
                     "url_contains": "results",
                     "element_exists": ["ytd-video-renderer"],
                 },
                 description=f"Search for '{query}'"),
            Step(url="youtube.com", action_name="open_top_results",
                 params={"n": n, "content_type": content_type},
                 verify_conditions={
                     "url_contains": "results",
                     "element_exists": ["ytd-video-renderer"],
                 },
                 description=f"Open top {n} {label} in background tabs"),
        ]

    # ── Amazon plan templates ─────────────────────────────────────────────────

    @staticmethod
    def _plan_amz_navigate() -> list[Step]:
        return [Step(
            url="", action_name="navigate",
            params={"url": "https://www.amazon.de"},
            verify_conditions={
                "url_contains": "amazon",
                "element_exists": ["#twotabsearchtextbox", "input[name='field-keywords']"],
            },
            description="Navigate to Amazon",
        )]

    @staticmethod
    def _plan_amz_search(query: str) -> list[Step]:
        return [
            Step(url="", action_name="navigate",
                 params={"url": "https://www.amazon.de"},
                 verify_conditions={
                     "url_contains": "amazon",
                     "element_exists": ["#twotabsearchtextbox"],
                 },
                 description="Navigate to Amazon"),
            Step(url="amazon", action_name="search",
                 params={"query": query},
                 verify_conditions={
                     "url_contains": "s?k=",
                     "element_exists": ["div[data-component-type='s-search-result']"],
                 },
                 description=f"Search for '{query}'"),
        ]

    @staticmethod
    def _plan_amz_search_and_click(query: str) -> list[Step]:
        return [
            Step(url="", action_name="navigate",
                 params={"url": "https://www.amazon.de"},
                 verify_conditions={
                     "url_contains": "amazon",
                     "element_exists": ["#twotabsearchtextbox"],
                 },
                 description="Navigate to Amazon"),
            Step(url="amazon", action_name="search",
                 params={"query": query},
                 verify_conditions={
                     "url_contains": "s?k=",
                     "element_exists": ["div[data-component-type='s-search-result']"],
                 },
                 description=f"Search for '{query}'"),
            Step(url="amazon", action_name="click_first_result",
                 params={},
                 verify_conditions={
                     "url_contains": "/dp/",
                     "element_exists": ["#productTitle", "span#productTitle"],
                 },
                 description="Click first result"),
            Step(url="amazon", action_name="read_product_title",
                 params={},
                 verify_conditions={
                     "url_contains": "/dp/",
                     "element_exists": ["#productTitle"],
                 },
                 description="Read product title"),
        ]

    @staticmethod
    def _plan_amz_open_top(query: str, n: int) -> list[Step]:
        return [
            Step(url="", action_name="navigate",
                 params={"url": "https://www.amazon.de"},
                 verify_conditions={
                     "url_contains": "amazon",
                     "element_exists": ["#twotabsearchtextbox"],
                 },
                 description="Navigate to Amazon"),
            Step(url="amazon", action_name="search",
                 params={"query": query},
                 verify_conditions={
                     "url_contains": "s?k=",
                     "element_exists": ["div[data-component-type='s-search-result']"],
                 },
                 description=f"Search for '{query}'"),
            Step(url="amazon", action_name="open_top_results",
                 params={"n": n},
                 verify_conditions={
                     "url_contains": "s?k=",
                     "element_exists": ["div[data-component-type='s-search-result']"],
                 },
                 description=f"Open top {n} products in background tabs"),
        ]


# ── Validation Layer ──────────────────────────────────────────────────────────

#: All allowed action names (must match skill implementations).
#: Phase 10.1 (MAJOR-2 + MINOR-11): all Phase 10.1 aliases and new actions added.
_VALID_ACTIONS: frozenset[str] = frozenset({
    # Core / navigation
    "navigate",
    # YouTube — search & read
    "search",
    "click_first_video",
    "read_title",
    "read_result_title",
    "open_top_results",
    # YouTube — engagement (canonical)
    "like",
    "unlike",
    "subscribe",
    "unsubscribe",
    "save_to_watch_later",
    "remove_from_watch_later",
    # YouTube — engagement aliases (Phase 10.1)
    "like_video",           # alias: like
    "unlike_video",         # alias: unlike
    "like_current",         # natural-language alias: like
    "subscribe_channel",    # natural-language alias: subscribe
    # YouTube — Shorts engagement (Phase 10.1)
    "like_short",
    "unlike_short",
    "subscribe_short",
    # YouTube — playback (canonical)
    "play",
    "pause",
    "toggle_play",
    "set_speed",
    "seek",
    "forward_10s",
    "back_10s",
    "toggle_subtitles",
    "toggle_autoplay",
    "set_quality",
    "fullscreen",
    "exit_fullscreen",
    # YouTube — playback aliases (Phase 10.1)
    "play_video",           # alias: play
    "pause_video",          # alias: pause
    "set_playback_speed",   # alias: set_speed
    "seek_forward",         # configurable-delta forward seek
    "seek_backward",        # configurable-delta backward seek
    # YouTube — Shorts navigation (canonical + alias)
    "next_short",
    "prev_short",
    "previous_short",       # alias: prev_short
    # YouTube — navigation
    "go_home",
    "go_shorts_home",
    "go_to_channel",
    "go_to_channel_by_name",
    "open_comments",
    "next_video",
    "previous_video",
    "play_nth_next",
    # YouTube — library
    "open_history",
    "open_liked_videos",
    "open_playlists",
    "open_watch_later",
    # YouTube — playlists
    "add_to_playlist",
    "remove_from_playlist",
    # YouTube — recommended
    "open_recommended",
    "open_top_recommended",
    # YouTube — comments (Phase 10.1)
    "scroll_comments",
    # Amazon — original
    "click_first_result",
    "read_product_title",
    # Amazon — shopping
    "add_to_cart",
    "remove_from_cart",
    "add_to_wishlist",
    "remove_from_wishlist",
    "buy_now",
    # Amazon — navigation
    "open_orders",
    "open_cart",
    "open_wishlist",
    # Amazon — data
    "read_price",
    "read_rating",
    "read_reviews",
    # Generic — data (Phase E: scrape any page)
    "scrape_page",
    # MakerWorld — real action names (routed via url="makerworld.com")
    "get_model_info",
    "get_search_results",
    "collect",
    "uncollect",
    "toggle_like",
    "download",
    "download_3mf",
    "download_stl",
    "navigate_to_model",
    # MakerWorld — LLM-facing aliases (Phase E)
    "mw_search",            # alias: search (routed to MakerWorldSkill by URL)
    "mw_open_top",          # alias: open_top_results
    "mw_get_info",          # alias: get_model_info
    "mw_get_results",       # alias: get_search_results
    # MakerWorld — engagement
    "mw_like",              # alias: like
    "mw_unlike",            # alias: unlike
    "mw_toggle_like",       # alias: toggle_like
    "mw_collect",           # alias: collect
    "mw_uncollect",         # alias: uncollect
    # MakerWorld — download
    "mw_download",          # alias: download
    "mw_download_3mf",      # alias: download_3mf
    "mw_download_stl",      # alias: download_stl
    # MakerWorld — navigation
    "mw_navigate_to_model", # alias: navigate_to_model
})

_REQUIRED_STEP_KEYS: frozenset[str] = frozenset({
    "action_name", "url", "params", "verify_conditions", "description",
})


def validate_steps(data: Any) -> list[Step] | None:
    """
    Validates a raw list of Step dicts from LLM output.
    Returns list[Step] if valid, None otherwise.
    """
    if not isinstance(data, list) or len(data) == 0:
        logger.warning(f"[validate_steps] Expected non-empty list, got: {type(data).__name__}")
        return None

    steps: list[Step] = []
    for i, item in enumerate(data):
        prefix = f"[validate_steps] Step {i}"
        if not isinstance(item, dict):
            logger.warning(f"{prefix}: not a dict")
            return None
        missing = _REQUIRED_STEP_KEYS - set(item.keys())
        if missing:
            logger.warning(f"{prefix}: missing keys: {sorted(missing)}")
            return None
        an = item["action_name"]
        if not isinstance(an, str) or an not in _VALID_ACTIONS:
            logger.warning(f"{prefix}: invalid action '{an}'")
            return None
        if not item.get("verify_conditions"):
            logger.warning(f"{prefix}: verify_conditions is empty")
            return None
        steps.append(Step(
            action_name=an, url=item["url"], params=item["params"],
            verify_conditions=item["verify_conditions"], description=item["description"],
        ))

    logger.info(f"[validate_steps] ✅ {len(steps)} steps validated")
    return steps


# ── LLMEngine (Phase 8) ───────────────────────────────────────────────────────

class _LLMEngine(_PlannerEngine):
    """
    LLM-based planning via local Ollama (Phase 8).
    Falls back to TemplateEngine on failure.
    """
    OLLAMA_URL:      str = "http://localhost:11434/api/generate"
    PRIMARY_MODEL:   str = "phi4:14b"
    FALLBACK_MODEL:  str = "llama3.3:8b"
    REQUEST_TIMEOUT: int = 90

    _SYSTEM_PROMPT: str = (
        "You are a browser automation planner.\n"
        "Convert a user goal into a STRICT JSON list of steps.\n"
        "Rules:\n"
        "* Only allowed actions (exact names — no others):\n"
        "\n"
        "  CORE:\n"
        "    navigate\n"
        "\n"
        "  YOUTUBE — search & read:\n"
        "    search, click_first_video, read_title, read_result_title, open_top_results\n"
        "\n"
        "  YOUTUBE — engagement (canonical + aliases):\n"
        "    like, unlike, subscribe, unsubscribe,\n"
        "    save_to_watch_later, remove_from_watch_later,\n"
        "    like_video, unlike_video, like_current, subscribe_channel,\n"
        "    like_short, unlike_short, subscribe_short\n"
        "\n"
        "  YOUTUBE — playback (canonical + aliases):\n"
        "    play, pause, toggle_play,\n"
        "    set_speed, set_playback_speed,\n"
        "    seek, seek_forward, seek_backward,\n"
        "    forward_10s, back_10s,\n"
        "    toggle_subtitles, toggle_autoplay, set_quality,\n"
        "    fullscreen, exit_fullscreen,\n"
        "    play_video, pause_video\n"
        "\n"
        "  YOUTUBE — Shorts:\n"
        "    next_short, prev_short, previous_short\n"
        "\n"
        "  YOUTUBE — navigation:\n"
        "    go_home, go_shorts_home, go_to_channel, go_to_channel_by_name,\n"
        "    open_comments, scroll_comments,\n"
        "    next_video, previous_video, play_nth_next\n"
        "\n"
        "  YOUTUBE — library:\n"
        "    open_history, open_liked_videos, open_playlists, open_watch_later\n"
        "\n"
        "  YOUTUBE — playlists:\n"
        "    add_to_playlist, remove_from_playlist\n"
        "\n"
        "  YOUTUBE — recommended:\n"
        "    open_recommended, open_top_recommended\n"
        "\n"
        "  AMAZON — search & read:\n"
        "    search, click_first_result, read_product_title, open_top_results\n"
        "\n"
        "  AMAZON — shopping:\n"
        "    add_to_cart, remove_from_cart, add_to_wishlist, remove_from_wishlist, buy_now\n"
        "\n"
        "  AMAZON — navigation:\n"
        "    open_orders, open_cart, open_wishlist\n"
        "\n"
        "  AMAZON — data:\n"
        "    read_price, read_rating, read_reviews\n"
        "\n"
        "* Always include: action_name, url, params, verify_conditions, description\n"
        "* verify_conditions must have url_contains and element_exists\n"
        "* NEVER output text outside JSON\n"
        "* NEVER hallucinate actions not listed above\n"
        "* On-page actions (like, subscribe, add_to_cart, scroll_comments etc.) need NO navigate step\n"
        "* For \"like this video\": use \"like\" or \"like_video\" — both are valid\n"
        "* For \"like a short\": use \"like_short\"\n"
        "* For \"seek forward 30s\": use \"seek_forward\" with params {\"seconds\": 30}\n"
        "* For \"scroll comments\": use \"scroll_comments\" with params {\"amount\": 3}"
    )

    def plan(self, goal: str) -> list[Step]:
        for model in (self.PRIMARY_MODEL, self.FALLBACK_MODEL):
            logger.info(f"[LLMEngine] Trying model '{model}' for: '{goal}'")
            try:
                raw = self._call_ollama(model, goal)
                steps = self._parse_and_validate(raw)
                if steps is not None:
                    logger.info(f"[LLMEngine] ✅ {len(steps)} steps via '{model}'")
                    return steps
                logger.warning(f"[LLMEngine] Validation failed for '{model}'")
            except Exception as exc:
                logger.warning(f"[LLMEngine] '{model}' failed: {type(exc).__name__}: {exc}")

        logger.warning("[LLMEngine] All LLM models failed. Falling back to TemplateEngine.")
        return _TemplateEngine().plan(goal)

    def _call_ollama(self, model: str, goal: str) -> str:
        user_prompt = (
            f"User goal: {goal}\n\n"
            "Return ONLY a valid JSON array of steps. "
            "No markdown, no code blocks, no explanation — pure JSON only."
        )
        payload = json.dumps({
            "model": model,
            "prompt": f"{self._SYSTEM_PROMPT}\n\n{user_prompt}",
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            url=self.OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)["response"]

    def _parse_and_validate(self, raw: str) -> list[Step] | None:
        text = re.sub(r"```(?:json)?\s*", "", raw.strip())
        text = re.sub(r"```\s*$", "", text, flags=re.MULTILINE).strip()
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            logger.warning(f"[LLMEngine] No JSON array found. Raw: {text[:150]!r}")
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            logger.warning(f"[LLMEngine] JSON parse failed: {exc}")
            return None
        return validate_steps(data)


# ── Planner (public interface) ────────────────────────────────────────────────

class Planner:
    """
    Public planning interface.

    Converts a free-text goal into an executable list of Steps.

    Usage:
        planner = Planner()
        steps = planner.plan("like this video and subscribe")
        # → [Step(like), Step(subscribe)]

        steps = planner.plan("search YouTube for Python tutorial and open top 3 videos")
        # → [Step(navigate), Step(search), Step(open_top_results, n=3)]

        steps = planner.plan("add this product to cart")
        # → [Step(add_to_cart)]

        steps = planner.plan("go to channel MrBeast")
        # → [Step(go_to_channel_by_name, name='MrBeast')]

    Engine selection via config.PLANNER_ENGINE: "template" | "llm"

    Stable Contract:
        plan(goal: str) → list[Step]
    """

    def __init__(self, engine: str | None = None) -> None:
        engine_name = engine or config.PLANNER_ENGINE
        if engine_name == "template":
            self._engine: _PlannerEngine = _TemplateEngine()
        elif engine_name == "llm":
            self._engine = _LLMEngine()
        else:
            logger.warning(f"[Planner] Unknown engine: '{engine_name}'. Using TemplateEngine.")
            self._engine = _TemplateEngine()
        logger.info(f"[Planner] Engine: {type(self._engine).__name__}")

    def plan(self, goal: str) -> list[Step]:
        logger.info(f"[Planner] Goal: '{goal}'")
        steps = self._engine.plan(goal)
        logger.info(f"[Planner] → {len(steps)} steps generated")
        for i, step in enumerate(steps):
            logger.debug(f"[Planner]   Step {i + 1}: {step}")
        return steps
