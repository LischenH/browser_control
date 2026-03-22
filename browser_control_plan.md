# 🧠 Browser Control System — Design & Progress Tracker

---

## 📦 Phase 12 — Full System Stability Audit & Production Fixes

> Applied after a full audit of all reported production failures.
> All changes are backward-compatible. Stable contracts preserved.

### ✅ Fixes Applied

| # | Component | Issue (RESOLVED) | Fix |
|---|-----------|-------------------|-----|
| 1 | `core/browser.py` | Chrome attach unreliable; no error if port not open | Added `_pick_best_page()`, `health_check()`, `reconnect()`, `resync_active_page()`. Explicit `ConnectionError` with start-Chrome instructions. Auto-resync on `active_page` access. |
| 2 | `core/tab_manager.py` | Multi-tab inconsistent; no stable tab identity | Added `tab_id` field to `TabInfo`. `_registry: dict[int,Page]`. `get_tab_by_id()`, `switch_to_tab_id()`. Focus guarantee: both `bring_to_front()` + `conn.active_page` set on every switch. `open_tab()` returns registered `TabInfo`. |
| 3 | `core/mode_resolver.py` | Amazon was HUMAN (slow); unknown sites were FAST (fragile) | Amazon moved to `FAST_DOMAINS`. Unknown-site fallback changed `"fast"` → `"human"`. `HUMAN_PATH_PATTERNS` now checked **before** `FAST_DOMAINS` so login/checkout on any domain gets HUMAN mode. |
| 4 | `core/actions.py` | No JS click fallback; no scroll-into-view in FAST mode; no bounding-box check | FAST mode: `_scroll_element_into_view()` called before click. Bounding-box validation (zero-size = skip). JS `.click()` fallback when Playwright click fails (pointer-events:none, overlays). HUMAN mode: same JS fallback added. |
| 5 | `agent/executor.py` | Multi-tab steps ran on wrong page (no page-sync) | Added optional `connection` param. Before each step: reads `conn.active_page` and updates `self._page` + rebuilds `Verifier` if changed. Fixes open-tab-then-act flows. |
| 6 | `skills/youtube_skill.py` | Channel/playlist links clicked instead of videos; no player verification | `click_first_video()` now waits for `video`/`#movie_player`/title as confirmation (not just play button). Added URL check: warns if `/watch` absent. `open_top_results()` filters links: only `/watch?v=` or `/shorts/` kept; fallback logs warning. |
| 7 | `skills/selectors/youtube.json` | Broad selectors matched channels | `first_video_link`, `result_links`, `result_links_video` all prioritize `[href*='/watch?v=']` selectors. |
| 8 | `skills/amazon_skill.py` | Sponsored `/sspa/` links returned as products | JS extractor rewritten: skips `.puis-sponsored-label-text` / `[aria-label*=Sponsored]`. Prefers `/dp/` links. Falls back to ASIN-constructed `/dp/<ASIN>` canonical URL. `open_top_results()` requests `n*3` candidates to absorb sponsored filtering. |
| 9 | `tests/test_stability.py` | No tests for stability fixes | **New test file**: 8 test classes, 35+ tests covering tab IDs, interrupt handler, page-ready fast-path, health-check, mode resolver, executor page-sync, YT link filter, Amazon ASIN extraction. |

### 🔒 Contracts Preserved (Unchanged)

| Contract | Status |
|----------|--------|
| `actions.py` fn(selectors: list[str], ...) | ✅ UNCHANGED |
| `planner.plan(goal: str) → list[Step]` | ✅ UNCHANGED |
| `skill.get_action(name) → callable` | ✅ UNCHANGED |
| `verifier.verify(dict) → VerifyResult` | ✅ UNCHANGED |
| `skill_manager.get_skill(url) → Skill` | ✅ UNCHANGED |
| `executor.run(steps: list[Step]) → dict` | ✅ UNCHANGED (new optional `connection` param) |
| `TabManager.list_tabs()`, `open_tab()`, `close_tab()` | ✅ UNCHANGED (TabInfo gained `tab_id` field) |

### 📈 Performance Impact

| Area | Before | After |
|------|--------|-------|
| Pre-action page-ready on loaded page | ~1500ms (networkidle) | ~50ms (fast-path) |
| Spinner check (none present) | ~20s worst case | ~10ms (is_visible pre-check) |
| FAST mode click (element offscreen) | Error / wrong element | scroll-into-view → click |
| Click on covered element | ActionError | JS fallback .click() |
| Chrome attach failure | Generic exception | Clear error + start instructions |
| Tab switch (multi-tab flows) | Wrong page (no sync) | Correct page (conn.active_page sync) |
| Amazon sponsored results | Included (ad redirects) | Filtered (canonical /dp/ only) |
| YouTube channel misclick | Possible | Prevented (href filter + URL check) |

---

## 🗂️ Folder Structure

```
browser_control/
├── agent/
│   ├── __init__.py
│   ├── executor.py         # Step orchestration — retry, idempotency, tab-tracking
│   ├── planner.py          # goal (str) → Step list  [LLM-ready interface]
│   └── verifier.py         # Multi-condition verification
│
├── core/
│   ├── __init__.py         # Package-Export: BrowserConnection, Actions, TabManager,
│   │                       #   ModeResolver, InterruptHandler
│   ├── actions.py          # Primitives — selector lists, adaptive mode, JS eval,
│   │                       #   interrupt hooks, wait_for_page_ready
│   ├── browser.py          # Chrome attachment via CDP
│   ├── interrupts.py       # Cookie banners, ad skippers, modal dismissers
│   ├── mode_resolver.py    # FAST/HUMAN/AUTO mode resolution
│   └── tab_manager.py      # Tab lifecycle & live state
│
├── skill_manager/
│   ├── __init__.py
│   └── manager.py          # URL → skill routing
│
├── skills/
│   ├── __init__.py
│   ├── base_skill.py       # Abstract interface + Result type + selector loader
│   ├── generic_skill.py    # Fallback skill (navigate, noop)
│   ├── youtube_skill.py    # Full YouTube platform agent (video + shorts)
│   ├── amazon_skill.py     # Full Amazon platform agent (shopping + account)
│   └── selectors/
│       ├── youtube.json    # All YouTube CSS selector groups with fallbacks
│       └── amazon.json     # All Amazon CSS selector groups with fallbacks
│
├── tests/
│   ├── test_executor.py    # 24 Unit-Tests (Phase 4)
│   ├── test_planner.py     # 32 Unit-Tests (Phase 5/6)
│   └── test_verifier.py    # 22 Unit-Tests (Phase 3)
│
├── config.py
├── main.py
├── requirements.txt
├── README.md
└── browser_control_plan.md
```

---

## ⚠️ Critical Design Decisions (Updated)

### ❗ Fix 1 — `actions.py` accepts selector lists, not single selectors

**Problem with naive approach:**
If `click(selector: str)` lives in core, skills must resolve selectors themselves.
This duplicates fallback logic in every skill and defeats the purpose of centralizing core.

**Solution:** Every primitive in `actions.py` accepts `selectors: list[str]`.
Core is responsible for:
- Iterating the list until one resolves
- Retrying on transient failures (configurable N times)
- Logging which selector succeeded or why all failed

```
actions.click(["#search-input", "input[name='search']", "input[type='text']"])
              └─ core tries each in order, retries on flake, logs result
```

Skills pass their full selector list. Core handles everything else.

---

### ❗ Fix 2 — `verifier.py` uses multi-condition checks

**Problem with naive approach:**
Checking only "did a selector appear" will miss wrong-page navigation,
partial loads, and stale DOM matches.

**Solution:** Verifier accepts a condition dict. All specified conditions must pass.

```
verify({
  "url_contains":    "youtube.com/results",   # correct page?
  "element_exists":  "#video-results",         # content loaded?
  "text_contains":   "Python"                  # query reflected?
})
```

Supported condition keys:
| Key | Checks |
|---|---|
| `url_contains` | Current tab URL includes substring |
| `url_equals` | Exact URL match |
| `element_exists` | At least one selector in list resolves |
| `element_absent` | None of the selectors resolve (e.g. spinner gone) |
| `text_contains` | Any visible text on page contains string |

Each step in a plan declares its expected verify dict.
Verifier returns: `pass` / `retry` / `fail` with reason string.

---

### ❗ Fix 3 — `planner.py` interface is LLM-ready from day one

**Problem with template-only approach:**
Templates break on natural-language goals like:
- *"find the best Python tutorial and check the comment sentiment"*
- *"search for headphones under €50 and open the top 3"*

These require intent parsing that templates cannot cover.

**Solution:** The public interface is fixed now. The internal engine is swappable.

```
# Public interface — never changes
plan = planner.plan(goal: str) → list[Step]

# Internal engine — swappable without touching anything else
Phase 1:  TemplateEngine  (regex/keyword matching, fast, offline)
Phase 2+: LLMEngine       (calls Ollama, handles complex goals)
```

The engine is selected via config. Everything upstream (executor, main) is unaware of which engine is active.

---

## 📊 Data Flow

```
User goal (str)
      │
      ▼
agent/planner.plan(goal)
      │  Interface: always plan(str) → list[Step]
      │  Engine:    Template (now) or LLM (later)
      │
      ▼  list[Step]  e.g. [navigate, search, verify_results]
agent/executor.py
      │
      ├─ tab_manager: what URL is active right now?
      ├─ skill_manager: which skill handles this URL?
      ├─ skill.get_action("search")(actions, params)
      │        │
      │        └─ skill calls core/actions.py only
      │                 │  actions accept list[str] selectors
      │                 │  core handles fallback + retry + logging
      │                 │  interrupt handler runs on every action
      │                 └─ Playwright CDP → Real Chrome
      │
      └─ verifier.verify({ url_contains, element_exists, ... })
               │
               ├─ pass  → next step
               ├─ retry → re-run step (up to config.MAX_RETRIES)
               └─ fail  → stop, structured error returned to user
      │
      ▼
Result { success, data, steps_completed, error, opened_tabs }
```

---

## 🧩 Skill Interface Contract

```
Skill
├── name: str                        → "YouTube"
├── base_url: str                    → "youtube.com"
│
├── can_handle(url: str) → bool      → URL ownership check
│
├── get_action(name: str) → callable → Lookup by action name
│
└── actions: dict[str, callable]
    Each action signature:
      fn(actions: Actions, params: dict) → Result
      Result: { success: bool, data: any, error: str | None }
```

Selector lists live in `selectors/<site>.json`.
Skills load them at init. Actions pass full lists to `core/actions`.
Skills never resolve selectors themselves.

---

## 🎬 YouTube Skill — Phase 11 Full Platform Agent

### Mode Detection
```python
if "/shorts/" in url:
    mode = "shorts"
else:
    mode = "video"
```
All actions adapt automatically based on detected mode.

### Complete Action Map

| Category | Action | Description |
|---|---|---|
| **Search / Read** | `search(query)` | Fill search box → Enter → wait results |
| | `click_first_video()` | Wait → click first result → wait player |
| | `read_title()` | Get video title text |
| | `read_result_title()` | Get first search result title |
| | `open_top_results(n, content_type)` | Open N results in background tabs |
| **Engagement** | `like()` | Like video/short — idempotent (checks aria-pressed) |
| | `unlike()` | Remove like — idempotent |
| | `subscribe()` | Subscribe to channel — idempotent |
| | `unsubscribe()` | Unsubscribe — handles confirmation dialog |
| | `save_to_watch_later()` | Save to Watch Later playlist — idempotent |
| | `remove_from_watch_later()` | Remove from Watch Later — idempotent |
| **Playback** | `play()` | Resume via JS `video.play()` |
| | `pause()` | Pause via JS `video.pause()` |
| | `toggle_play()` | Toggle based on current `video.paused` state |
| | `set_speed(value)` | JS `video.playbackRate` — valid: 0.25→2.0, clamps |
| | `seek(seconds)` | Absolute seek via `video.currentTime` |
| | `forward_10s()` | Skip ahead 10s via relative seek |
| | `back_10s()` | Skip back 10s via relative seek |
| | `toggle_subtitles()` | Click CC button (video mode only) |
| | `toggle_autoplay()` | Click autoplay toggle button |
| | `set_quality(quality)` | Settings → Quality menu → select item |
| | `fullscreen()` | `requestFullscreen()` — idempotent |
| | `exit_fullscreen()` | `document.exitFullscreen()` — idempotent |
| **Shorts** | `next_short()` | Click nav button or ArrowDown keyboard |
| | `prev_short()` | Click nav button or ArrowUp keyboard |
| **Navigation** | `go_home()` | Navigate to `youtube.com` |
| | `go_shorts_home()` | Navigate to `youtube.com/shorts` |
| | `go_to_channel()` | Click channel link from current video |
| | `go_to_channel_by_name(name)` | Try `@handle` URL, fallback to search |
| | `open_comments()` | Scroll `#comments` into view |
| | `next_video()` | Click `.ytp-next-button` |
| | `previous_video()` | `history.back()` + wait |
| | `play_nth_next(n)` | Extract sidebar links → navigate to nth |
| **Library** | `open_history()` | Navigate to `/feed/history` |
| | `open_liked_videos()` | Navigate to `/playlist?list=LL` |
| | `open_playlists()` | Navigate to `/feed/library` |
| | `open_watch_later()` | Navigate to `/playlist?list=WL` |
| **Playlists** | `add_to_playlist(name)` | Open save menu → find by name → check |
| | `remove_from_playlist(name)` | Open save menu → find by name → uncheck |
| **Recommended** | `open_recommended(index)` | Open Nth sidebar video (alias for play_nth_next) |
| | `open_top_recommended(n)` | Open top N recommendations in background tabs |

### Smart State Detection (Idempotent Actions)
All engagement actions check current state before acting:
- `like()` — reads `button[aria-pressed]` → skips if already liked
- `subscribe()` — reads `aria-label` for "Unsubscrib" → skips if already subscribed
- `save_to_watch_later()` — reads WL checkbox state → skips if already saved
- `fullscreen()` — checks `document.fullscreenElement` → skips if already fullscreen

### Selector Groups (`selectors/youtube.json`)
All keys are `list[str]` with CSS fallbacks in priority order:

| Key | Purpose |
|---|---|
| `like_button` | Like/unlike toggle button |
| `subscribe_button` | Channel subscribe button |
| `save_button` | Save-to-playlist popup trigger |
| `watch_later_item` | Watch Later checkbox in save popup |
| `playlist_menu` | Save popup container |
| `settings_button` | Player ⚙ settings button |
| `settings_menu` | Settings popup panel |
| `speed_menu` | Speed menu item in settings |
| `quality_menu_item` | Quality menu item in settings |
| `subtitles_button` | CC/Subtitles button in player |
| `autoplay_toggle` | Autoplay on/off button |
| `next_button` | Player next-video button |
| `channel_link` | Channel name anchor |
| `comments_section` | Comments root element |
| `shorts_next_button` | Shorts navigation: next |
| `shorts_prev_button` | Shorts navigation: previous |
| `shorts_container` | Shorts player container |

---

## 🛒 Amazon Skill — Phase 11 Full Platform Agent

### Complete Action Map

| Category | Action | Description |
|---|---|---|
| **Search / Read** | `search(query)` | Fill search box → Enter → wait results |
| | `click_first_result()` | Wait → click first product → wait title |
| | `read_result_title()` | Get first search result title |
| | `read_product_title()` | Get product page title |
| | `open_top_results(n)` | Open N products in background tabs |
| **Shopping** | `add_to_cart()` | Click add-to-cart, wait for confirmation |
| | `remove_from_cart()` | Navigate to cart if needed, click delete |
| | `add_to_wishlist()` | Click wishlist button, handle modal |
| | `remove_from_wishlist()` | Navigate to wishlist if needed, click delete |
| | `buy_now()` | Click buy-now (initiates checkout) |
| **Account Nav** | `open_orders()` | Try nav link, fallback to `/gp/your-account/order-history` |
| | `open_cart()` | Try cart icon, fallback to `/cart` |
| | `open_wishlist()` | Try wishlist link, fallback to `/hz/wishlist/ls` |
| **Product Data** | `read_price()` | Extract price from product page |
| | `read_rating()` | Extract star rating text |
| | `read_reviews(n)` | Scroll to reviews, extract top N via JS |

### Selector Groups (`selectors/amazon.json`)

| Key | Purpose |
|---|---|
| `add_to_cart_button` | "Add to Cart" button on product page |
| `remove_from_cart_button` | Delete button in cart |
| `wishlist_button` | "Add to List/Wishlist" button on product page |
| `remove_from_wishlist_button` | Delete button on wishlist page |
| `buy_now_button` | "Buy Now" button on product page |
| `cart_icon` | Cart icon in top nav |
| `orders_link` | "Returns & Orders" in top nav |
| `wishlist_nav_link` | Wishlist link in nav |
| `price_selector` | Price element (multiple Amazon price formats) |
| `rating_selector` | Star rating element |
| `review_block` | Individual review `[data-hook="review"]` |

---

## 🔗 Planner — Phase 11 On-Page Patterns

### YouTube Patterns (TemplateEngine)

| Pattern | Action | Example |
|---|---|---|
| `"like this video"` | `like` | — |
| `"unlike"` / `"remove like"` | `unlike` | — |
| `"subscribe"` | `subscribe` | — |
| `"unsubscribe"` | `unsubscribe` | — |
| `"like … and subscribe"` | `like` + `subscribe` | Two steps |
| `"save to watch later"` / `"add to watch later"` | `save_to_watch_later` | — |
| `"remove from watch later"` | `remove_from_watch_later` | — |
| `"play"` | `play` | — |
| `"pause"` | `pause` | — |
| `"set speed to 1.5x"` / `"increase speed to 2x"` | `set_speed(1.5)` | Regex extracts float |
| `"skip 10 seconds"` / `"forward 10s"` | `forward_10s` | — |
| `"go back 10"` / `"rewind 10"` | `back_10s` | — |
| `"seek to 90 seconds"` | `seek(90)` | Regex extracts seconds |
| `"play the 3rd next video"` | `play_nth_next(3)` | Ordinal regex |
| `"open next 3 videos"` / `"open top 3 recommended"` | `open_top_recommended(3)` | — |
| `"go to channel MrBeast"` | `go_to_channel_by_name("MrBeast")` | Name extracted |
| `"go to channel"` (no name) | `go_to_channel` | Current video's channel |
| `"open comments"` | `open_comments` | — |
| `"toggle subtitles"` / `"toggle cc"` | `toggle_subtitles` | — |
| `"toggle autoplay"` | `toggle_autoplay` | — |
| `"fullscreen"` | `fullscreen` | — |
| `"exit fullscreen"` | `exit_fullscreen` | — |
| `"next video"` / `"play next"` | `next_video` | — |
| `"previous video"` / `"go back"` | `previous_video` | — |
| `"next short"` | `next_short` | — |
| `"previous short"` | `prev_short` | — |
| `"youtube home"` | `go_home` | — |
| `"shorts home"` | `go_shorts_home` | — |
| `"open history"` | `open_history` | — |
| `"open liked videos"` | `open_liked_videos` | — |
| `"open playlists"` | `open_playlists` | — |
| `"open watch later"` | `open_watch_later` | — |
| `"set quality to 1080p"` | `set_quality("1080p")` | — |
| `"add to playlist Music"` | `add_to_playlist("Music")` | Name extracted |
| `"remove from playlist Favorites"` | `remove_from_playlist("Favorites")` | — |

### Amazon Patterns (TemplateEngine)

| Pattern | Action | Example |
|---|---|---|
| `"add to cart"` / `"add this product to cart"` | `add_to_cart` | — |
| `"remove from cart"` | `remove_from_cart` | — |
| `"add to wishlist"` / `"add this product to wishlist"` | `add_to_wishlist` | — |
| `"remove from wishlist"` | `remove_from_wishlist` | — |
| `"buy now"` / `"purchase now"` | `buy_now` | — |
| `"open cart"` / `"view my cart"` | `open_cart` | — |
| `"open orders"` / `"order history"` | `open_orders` | — |
| `"open wishlist"` / `"view wishlist"` | `open_wishlist` | — |
| `"read price"` / `"what's the price"` | `read_price` | — |
| `"read rating"` / `"what's the rating"` | `read_rating` | — |
| `"read reviews"` / `"show reviews"` | `read_reviews(n=3)` | Default 3 |
| `"read 5 reviews"` / `"show 5 reviews"` | `read_reviews(5)` | Regex extracts n |

---

## ⚡ Executor — Phase 11 Safety Features

### Idempotency Guard
```python
# Actions that return "skipped_*" in their data.action field
# are treated as immediate success — no verify loop, no retry.
# This prevents: like → verify → retry → double-like
_IDEMPOTENT_SKIP_PREFIXES = ("skipped_already_", "skipped_not_", "skipped_")
```

### Retry-Safety
- Params are **deep-copied** per repetition — retried actions never receive mutated params
- On-page engagement steps use lenient `verify_conditions` (only `url_contains`) to prevent false retry loops caused by transient DOM changes after an action

### Tab-Tracking
- `open_top_results` and `open_top_recommended` results are accumulated in `self._opened_tabs`
- Returned in `run()` as `"opened_tabs": [{"tab_index", "url", "title", "verified", "paused"}]`

---

## ✅ Implementation Progress

### Phase 1 — Core ✅ ABGESCHLOSSEN

- [x] `config.py` — Chrome debug port, timeouts, MAX_RETRIES, LOG_LEVEL, PLANNER_ENGINE
- [x] `core/browser.py` — `BrowserConnection` — connect to running Chrome via CDP (`connect_over_cdp`)
- [x] `core/browser.py` — expose `Browser` object with `active_page` (settable), `context`, `browser`
- [x] `core/browser.py` — Context Manager support (`with BrowserConnection() as conn:`)
- [x] `core/actions.py` — `click(selectors: list[str])`
- [x] `core/actions.py` — `type_text(selectors: list[str], text: str)`
- [x] `core/actions.py` — `wait_for(selectors: list[str], timeout)`
- [x] `core/actions.py` — `get_text(selectors: list[str]) → str`
- [x] `core/actions.py` — `scroll(direction, amount)`
- [x] `core/actions.py` — `navigate(url)` + `press_key(key)` (Bonus-Primitiven)
- [x] `core/actions.py` — selector fallback loop via `_try_selector()` (versucht jeden, loggt Winner)
- [x] `core/actions.py` — retry logic on transient failure (`PlaywrightTimeoutError`)
- [x] `core/actions.py` — `ActionError` mit vollständiger Fehlerliste aller Versuche
- [x] `core/tab_manager.py` — `list_tabs()` — live, kein Cache, gibt `list[TabInfo]` zurück
- [x] `core/tab_manager.py` — `switch_to_url(fragment)` — URL-Match + `bring_to_front()` + setzt `conn.active_page`
- [x] `core/tab_manager.py` — `switch_to_title(fragment)` + `switch_to_index(index)`
- [x] `core/tab_manager.py` — `open_tab(url)` — öffnet neuen Tab, navigiert, aktiviert ihn
- [x] `core/tab_manager.py` — `close_tab(tab)` — schließt Tab, aktiviert vorherigen
- [x] `core/__init__.py` — Package-Export aller Core-Klassen
- [x] `main.py` — vollständige Demo: verbinden → tabs listen → YouTube öffnen → suchen → Titel lesen → scrollen → schließen
- [x] `requirements.txt` — `playwright>=1.44.0`
- [x] `README.md` — Schnellstart, Ordnerstruktur, Konfigurationstabelle, stabile Contracts

---

### Phase 2 — Skill System ✅ ABGESCHLOSSEN

- [x] `skills/base_skill.py` — abstrakte Klasse `BaseSkill` mit `can_handle`, `get_action`
- [x] `skills/base_skill.py` — `Result`-Typ (`Result.ok`, `Result.fail`) als einheitliches Rückgabeobjekt
- [x] `skills/base_skill.py` — `_load_selectors(site)` lädt `skills/selectors/<site>.json` mit Caching
- [x] `skills/__init__.py` — Package-Export: `BaseSkill`, `Result`, `YouTubeSkill`, `GenericSkill`
- [x] `skills/selectors/youtube.json` — `search_box`, `search_button`, `video_result_item`, `first_video_link`, `play_button`, `video_title`, `video_result_title`, `result_links`
- [x] `skills/youtube_skill.py` — `can_handle(url)` → True wenn `"youtube.com" in url`
- [x] `skills/youtube_skill.py` — `search(query)` action (wait → fill → Enter → wait results)
- [x] `skills/youtube_skill.py` — `click_first_video()` action (wait → click title link → wait player)
- [x] `skills/youtube_skill.py` — `read_title()` action (wait → get_text → strip)
- [x] Alle Actions geben `Result`-Objekt zurück; Exceptions werden intern gefangen (Skill bricht nie ab)
- [x] Skills rufen ausschließlich `core/actions.py`-Methoden auf — kein Playwright-Direktzugriff

---

### Phase 3 — Verifier ✅ ABGESCHLOSSEN

- [x] `agent/verifier.py` — `VerifyResult` Dataclass + Properties `.passed`, `.should_retry`, `.failed`
- [x] `agent/verifier.py` — conditions: `url_contains`, `url_equals`, `element_exists`, `element_absent`, `text_contains`
- [x] `agent/verifier.py` — Retry-Wrapper, Early-Exit, Detailliertes Logging
- [x] `tests/test_verifier.py` — 22 Unit-Tests

---

### Phase 4 — Executor ✅ ABGESCHLOSSEN

- [x] `agent/planner.py` — `Step` Dataclass + `Planner` mit `plan(goal) → list[Step]`
- [x] `agent/planner.py` — `_TemplateEngine` + `_LLMEngine` Stub
- [x] `agent/executor.py` — Skill-Routing → Action → Verifier → Retry/Fail
- [x] `tests/test_executor.py` — 24 Unit-Tests

---

### Phase 5/6 — Planner & Wiring ✅ ABGESCHLOSSEN & REVIEWED

- [x] `_plan_yt_navigate()`: `element_exists` für Suchfeld ergänzt [FIX]
- [x] `read_result_title`-Step: `url_contains: "results"` ergänzt [FIX]
- [x] `read_title`-Step: `url_contains: "watch"` ergänzt [FIX]
- [x] `logging.basicConfig()` VOR Modul-Imports [FIX]
- [x] CLI `phase5`/`phase6` als Aliases für `phase4` [FIX]
- [x] `tests/test_planner.py` — 32 Unit-Tests

---

### Phase 7c — Adaptive Execution Engine ✅ ABGESCHLOSSEN

- [x] `config.py` — `EXECUTION_MODE: str = "auto"` ("fast" | "human" | "auto")
- [x] `core/mode_resolver.py` — URL-Pattern-Matching für FAST/HUMAN/AUTO
- [x] `core/actions.py` — `wait_for_page_ready()` (DOM + networkidle + spinner + DOM-stability)
- [x] `core/actions.py` — `click`, `type_text`, `get_text` mit `mode=None` (auto-resolve)
- [x] `core/actions.py` — HUMAN-Modus: scroll_into_view → stability → mouse → delay → action
- [x] `core/actions.py` — `navigate(url)` ruft `wait_for_page_ready()` auf

---

### Phase 7 — Second Skill / Amazon ✅ ABGESCHLOSSEN

- [x] `skills/selectors/amazon.json` — 7 Selector-Gruppen
- [x] `skills/amazon_skill.py` — 4 Actions: `search`, `click_first_result`, `read_result_title`, `read_product_title`
- [x] `skill_manager/manager.py` — `AmazonSkill` registriert
- [x] `agent/planner.py` — `_RE_AMZ_SEARCH` + 3 Plan-Templates
- [x] `main.py` — `phase7` / `phase7b` CLI

---

### Phase 8 — LLM Planner ✅ ABGESCHLOSSEN

- [x] `_LLMEngine`: Primär `phi4:14b` → Fallback `llama3.3:8b` → Fallback TemplateEngine
- [x] `validate_steps()`: Keys, Typen, gültige Actions, verify_conditions nicht leer
- [x] `main.py` — `phase8` / `phase8b` / `phase8c` CLI

---

### Phase 9 — Multi-Tab Execution ✅ ABGESCHLOSSEN

**Kern-Designentscheidung — Single-Tab vs. Multi-Tab Wiedergabe:**

| Szenario | Action | Video |
|---|---|---|
| 1 Video öffnen | `click_first_video()` | ▶️ **spielt sofort** |
| N Videos öffnen | `open_top_results(n)` | ⏸ **alle pausiert** |

- [x] `core/actions.py` — `get_all_hrefs(selectors, limit) → list[str]`
- [x] `core/actions.py` — `open_new_tab(url) → Page`
- [x] `core/actions.py` — `evaluate_js(script, page=None) → any`
- [x] `youtube_skill.py` — `open_top_results(n)`: `get_all_hrefs` → N × `open_new_tab` → pause → read title
- [x] `amazon_skill.py` — `open_top_results(n)`: analog, verification via `/dp/`
- [x] `agent/executor.py` — Repeat-Support: `step.params["repeat"] = N`
- [x] `agent/executor.py` — Tab-Tracking: `_collect_tab_data()` + `self._opened_tabs`
- [x] `agent/executor.py` — Result-Schema: `run()` gibt `"opened_tabs": [...]` zurück
- [x] `agent/planner.py` — `_RE_YT_TOP_N` / `_RE_AMZ_TOP_N` mit content_type-Parsing
- [x] `agent/planner.py` — `_plan_yt_open_top` / `_plan_amz_open_top`

---

### Phase 10 — Interrupt System ✅ ABGESCHLOSSEN

**New file:** `core/interrupts.py`

- [x] `core/interrupts.py` — `InterruptHandler` class with `handle(page) → bool`
- [x] Interrupt group 1 — **Blocking overlays / modals** (highest priority)
- [x] Interrupt group 2 — **Cookie banners** (OneTrust, Cookiebot, generic EN/DE/FR)
- [x] Interrupt group 3 — **YouTube ads** (`.ytp-skip-ad-button`, text-based skip)
- [x] `is_visible()` pre-check before every click — ~1ms fast path when nothing active
- [x] All exceptions caught internally — never raises, never stalls main flow
- [x] `InterruptHandler` exported from `core/__init__.py`
- [x] `core/actions.py` — `Actions.__init__`: `self._interrupts = InterruptHandler()`
- [x] `core/actions.py` — `click()`, `type_text()`, `navigate()` call `_handle_interrupts()`
- [x] `core/actions.py` — `_try_selector()` retry loop calls `_handle_interrupts()` on `attempt > 1`

---

### Phase 11 — Full Platform Agent ✅ ABGESCHLOSSEN

**Goal:** Transform system from automation tool → full platform agent.  
Both YouTube and Amazon behave like a real human controlling the platform intelligently.

#### 🎬 YouTube — Advanced Skill

**`skills/youtube_skill.py`** — fully rewritten with:

- [x] **Mode detection** — `"shorts" in url` → `mode = "shorts"`, else `mode = "video"`; all actions adapt
- [x] **Engagement actions** (all idempotent, state-aware):
  - [x] `like()` — reads `aria-pressed` → skips if already liked
  - [x] `unlike()` — reads `aria-pressed` → skips if not liked
  - [x] `subscribe()` — reads `aria-label` for "Unsubscrib" → skips if already subscribed
  - [x] `unsubscribe()` — dismisses confirmation dialog after click
  - [x] `save_to_watch_later()` — opens save menu, reads WL checkbox state
  - [x] `remove_from_watch_later()` — opens save menu, unchecks WL item
- [x] **Playback actions** (all via JavaScript, no hardcoded sleeps):
  - [x] `play()` — `video.play()`
  - [x] `pause()` — `video.pause()`
  - [x] `toggle_play()` — reads `video.paused`, calls play or pause accordingly
  - [x] `set_speed(value)` — `video.playbackRate`, clamps to valid values {0.25…2.0}
  - [x] `seek(seconds)` — `video.currentTime = seconds`
  - [x] `forward_10s()` — `video.currentTime += 10`
  - [x] `back_10s()` — `video.currentTime -= 10`, clamped to 0
  - [x] `toggle_subtitles()` — CC button click (guards against shorts mode)
  - [x] `toggle_autoplay()` — autoplay toggle click
  - [x] `set_quality(quality)` — Settings → Quality submenu → JS click matching item
  - [x] `fullscreen()` — `requestFullscreen()`, idempotent via `document.fullscreenElement`
  - [x] `exit_fullscreen()` — `document.exitFullscreen()`, idempotent
- [x] **Shorts actions**:
  - [x] `next_short()` — nav button click, fallback to `ArrowDown` keyboard
  - [x] `prev_short()` — nav button click, fallback to `ArrowUp` keyboard
- [x] **Navigation actions**:
  - [x] `go_home()` — navigate to `youtube.com`
  - [x] `go_shorts_home()` — navigate to `youtube.com/shorts`
  - [x] `go_to_channel()` — click channel link from current video
  - [x] `go_to_channel_by_name(name)` — try `@handle` direct URL, fallback to channel search
  - [x] `open_comments()` — JS `scrollIntoView` on `#comments`, wait for section
  - [x] `next_video()` — click `.ytp-next-button`
  - [x] `previous_video()` — `history.back()` + `wait_for_page_ready()`
  - [x] `play_nth_next(n)` — extract N+2 sidebar links via JS, navigate to index n-1
- [x] **Library access**:
  - [x] `open_history()` — `/feed/history`
  - [x] `open_liked_videos()` — `/playlist?list=LL`
  - [x] `open_playlists()` — `/feed/library`
  - [x] `open_watch_later()` — `/playlist?list=WL`
- [x] **Playlist management**:
  - [x] `add_to_playlist(name)` — open save menu → JS find by name → check if needed
  - [x] `remove_from_playlist(name)` — open save menu → JS find by name → uncheck if needed
- [x] **Recommended video control**:
  - [x] `open_recommended(index)` — alias for `play_nth_next`
  - [x] `open_top_recommended(n)` — open N sidebar videos in background tabs (all paused)

**`skills/selectors/youtube.json`** — extended with new groups:
- [x] `like_button` — 4 fallback selectors
- [x] `subscribe_button` — 5 fallback selectors
- [x] `save_button` — 5 fallback selectors
- [x] `watch_later_item` — 4 fallback selectors
- [x] `playlist_menu` — 4 fallback selectors
- [x] `settings_button` — 3 fallback selectors
- [x] `speed_menu` — 3 fallback selectors
- [x] `quality_menu_item` / `quality_panel_items` — quality submenu selectors
- [x] `subtitles_button` — 4 fallback selectors
- [x] `autoplay_toggle` — 4 fallback selectors
- [x] `next_button` — 4 fallback selectors
- [x] `channel_link` — 5 fallback selectors
- [x] `comments_section` — 3 fallback selectors
- [x] `shorts_next_button` / `shorts_prev_button` / `shorts_container` — Shorts navigation

#### 🛒 Amazon — Advanced Skill

**`skills/amazon_skill.py`** — extended with:

- [x] **Shopping actions** (state-aware where possible):
  - [x] `add_to_cart()` — validates product page, clicks button, waits for confirmation element or cart count delta
  - [x] `remove_from_cart()` — auto-navigates to cart if not there, clicks delete
  - [x] `add_to_wishlist()` — clicks wishlist button, handles potential confirm modal
  - [x] `remove_from_wishlist()` — auto-navigates to wishlist if not there, clicks delete
  - [x] `buy_now()` — validates product page, initiates checkout (logs warning)
- [x] **Account navigation** (try nav link, fallback to direct URL):
  - [x] `open_orders()` — `/gp/your-account/order-history`
  - [x] `open_cart()` — `/cart`
  - [x] `open_wishlist()` — `/hz/wishlist/ls`
- [x] **Product data**:
  - [x] `read_price()` — multi-selector price extraction
  - [x] `read_rating()` — star rating text extraction
  - [x] `read_reviews(n)` — scroll to reviews, JS extract top N `[data-hook="review"]` blocks

**`skills/selectors/amazon.json`** — extended with new groups:
- [x] `add_to_cart_button` — 5 fallback selectors (input + button variants)
- [x] `remove_from_cart_button` — 4 fallback selectors
- [x] `wishlist_button` — 6 fallback selectors
- [x] `wishlist_confirm_button` — confirm step selectors
- [x] `remove_from_wishlist_button` — wishlist delete selectors
- [x] `buy_now_button` — 4 fallback selectors
- [x] `cart_icon` — 4 fallback selectors
- [x] `orders_link` — 4 fallback selectors
- [x] `wishlist_nav_link` — 4 fallback selectors
- [x] `price_selector` — 6 fallback selectors (all Amazon price formats)
- [x] `rating_selector` — 4 fallback selectors
- [x] `review_block` / `review_title` / `review_body` — review extraction selectors

#### 🔗 Planner Extension

**`agent/planner.py`** — `_TemplateEngine` extended:

- [x] `_try_yt_on_page(g)` resolver — 30+ YouTube on-page patterns
  - Combined: `"like … and subscribe"` → two steps in one parse
  - Speed: regex extracts float from `"set speed to 1.5x"`, `"increase speed 2"`
  - Seek: regex extracts seconds from `"seek to 90 seconds"`, `"go to 120s"`
  - Forward/back 10s: `"skip 10"`, `"forward 10"`, `"back 10"`, `"rewind 10"`
  - Nth next: ordinal regex `"play the 3rd next video"` → `play_nth_next(3)`
  - Top N recommended: `"open next 3 videos"` → `open_top_recommended(3)`
  - Named channel: `"go to channel MrBeast"` → `go_to_channel_by_name("MrBeast")`
  - Quality: `"set quality to 1080p"` → `set_quality("1080p")`
  - Playlist: `"add to playlist Music"` → `add_to_playlist("Music")`
  - All remaining actions as keyword table (25 patterns)
- [x] `_try_amz_on_page(g)` resolver — 11 Amazon on-page patterns
  - Reviews: `"read 5 reviews"` → `read_reviews(5)` (regex extracts n)
  - All shopping / navigation / data actions
- [x] `_VALID_ACTIONS` frozenset expanded — all 50+ action names registered
- [x] `_LLMEngine` system prompt updated — all new actions documented

#### ⚡ Executor Safety

**`agent/executor.py`** — Phase 11 additions:

- [x] `_result_data_is_idempotent_skip(data)` — detects `"skipped_*"` in `data["action"]`
- [x] Idempotency guard in `_execute_with_retry()` — immediate success on skip result, no verify loop
- [x] Deep-copy of params **per repetition** — `copy.deepcopy(action_params)` before each rep
- [x] `_IDEMPOTENT_SKIP_PREFIXES` tuple — `("skipped_already_", "skipped_not_", "skipped_")`
- [x] On-page action `verify_conditions` use lenient checks (just `url_contains`) to prevent false retry loops after DOM mutations from engagement actions

---

### 🧩 Phase 12 — Data Layer
- [ ] Ergebnisse sammeln
- [ ] strukturieren
- [ ] speichern

---

### 🧩 Phase 13 — Research Mode
- [ ] mehrere Quellen
- [ ] vergleichen
- [ ] zusammenfassen

---

## 📋 Changelog Phase-11 — Full Platform Agent

### 🎯 Goal
Transform system from automation tool → full platform agent.
Enable commands like "like this video", "subscribe", "add to cart", "go to channel MrBeast" and more.

### ✅ Modified Files

| File | Changes |
|---|---|
| `skills/youtube_skill.py` | +30 actions, JS helpers, mode detection, smart state detection |
| `skills/amazon_skill.py` | +8 actions, cart/wishlist/order navigation, price/rating/review extraction |
| `skills/selectors/youtube.json` | +15 selector groups (engagement, playback, navigation, shorts) |
| `skills/selectors/amazon.json` | +10 selector groups (cart, wishlist, buy, price, rating, reviews) |
| `agent/planner.py` | `_try_yt_on_page` (30+ patterns), `_try_amz_on_page` (11 patterns), `_VALID_ACTIONS` expanded |
| `agent/executor.py` | Idempotency guard, deep-copy params per rep, `_IDEMPOTENT_SKIP_PREFIXES` |

### 🔒 Stable Contracts — Unverletzt

| Contract | Status |
|---|---|
| `actions.py` signatures: `fn(selectors: list[str], ...)` | ✅ unverändert |
| `planner.plan(goal: str) → list[Step]` | ✅ unverändert |
| `skill.get_action(name) → callable` | ✅ unverändert |
| `verifier.verify(dict) → VerifyResult` | ✅ unverändert |
| `skill_manager.get_skill(url) → Skill` | ✅ unverändert |
| `executor.run(steps: list[Step]) → dict` | ✅ unverändert |
| `core/browser.py` | ✅ unverändert |
| `core/tab_manager.py` | ✅ unverändert |
| `agent/verifier.py` | ✅ unverändert |
| `skill_manager/manager.py` | ✅ unverändert |

### 🧠 Architecture Notes

**Why JS over click() for playback:**
`video.pause()`, `video.play()`, `video.playbackRate`, `video.currentTime` are all idempotent and bypass the need for visible player buttons. Clicking a pause button would toggle — if the video was already paused, it would start playing. JS is the correct primitive for playback control.

**Why `_focus_player()` uses JS focus not click:**
Clicking the player center toggles play/pause. We use `element.focus()` via JS to give keyboard focus without side effects — needed before sending `ArrowDown`/`ArrowUp` in Shorts.

**Why verify_conditions for on-page steps are lenient:**
After `like()` runs, the DOM changes (aria-pressed updates, animations fire). A strict `element_exists` check might transiently fail, triggering an unwanted retry that would then un-like the video. Using only `url_contains: "youtube.com"` prevents false retry loops on engagement actions.

**Why idempotency guard skips verify entirely:**
If `like()` returns `{"action": "skipped_already_liked"}`, the state was already correct before the action ran. Running `verify()` is redundant — and potentially dangerous if verify triggers a retry that re-enters the like() → already-liked → skip cycle. The guard short-circuits to immediate success.

**`go_to_channel_by_name` fallback strategy:**
Direct `@handle` URL works for channels with a known handle. For channels where the handle is unknown or the URL fails, a filtered search (`&sp=EgIQAg%3D%3D` = channels only) finds the channel page. Both paths are transparent to the planner.

---

## 📋 Changelog Phase-10 — Interrupt System

### ✅ New File
- `core/interrupts.py` — `InterruptHandler` with three priority-ordered interrupt groups

### 🔧 Modified Files
- `core/actions.py`
  - Import `InterruptHandler`
  - `Actions.__init__`: `self._interrupts = InterruptHandler()`
  - New method `_handle_interrupts()` — thin delegate
  - `click()`: `_handle_interrupts()` called before `wait_for_page_ready`
  - `type_text()`: `_handle_interrupts()` called at start (before fill)
  - `navigate()`: `_handle_interrupts()` called after `wait_for_page_ready`
  - `_try_selector()`: `_handle_interrupts()` on `attempt > 1` inside retry loop
- `core/__init__.py` — `InterruptHandler` added to exports and `__all__`

---

## 📋 Phase 7 Full System Audit Findings

> Performed after full implementation of Phase 7.
> Every file in `core/`, `agent/`, and `skill_manager/` was read and validated.
> All stable contracts were verified against real code.

### 🔒 Stable Contract Verification

| Contract | Status | Notes |
|---|---|---|
| `actions.py` fn(selectors: list[str], ...) | ✅ VERIFIED | All 5 primitives accept `list[str]`. `_try_selector()` handles iteration + retry + logging. |
| `planner.plan(goal: str) → list[Step]` | ✅ VERIFIED | Public interface stable. Engine swappable via `config.PLANNER_ENGINE`. |
| `skill.get_action(name) → callable` | ✅ VERIFIED | All three skills (YouTube, Amazon, Generic) return `callable` or `None`. |
| `verifier.verify(dict) → VerifyResult` | ✅ VERIFIED | Dispatcher pattern. `element_exists` handles `str` and `list[str]`. |
| `skill_manager.get_skill(url) → Skill` | ✅ VERIFIED | Order: `[YouTubeSkill, AmazonSkill, GenericSkill]`. GenericSkill always last. |
| `executor.run(steps: list[Step]) → dict` | ✅ VERIFIED | Returns `{success, steps_completed, data, error, opened_tabs}`. |

### 🐛 Bugs Found

#### Bug 1 — `amazon.json`: Wrong key name `search_button` instead of `search_submit`
**File:** `skills/selectors/amazon.json`
**Fix:** Renamed `"search_button"` → `"search_submit"` in `amazon.json`

#### Bug 2 — `base_skill.py`: Shared mutable class-level default + untyped `_SELECTORS_DIR`
**File:** `skills/base_skill.py`
**Fix:** Removed class-level `_selectors = {}`; added `_SELECTORS_DIR: ClassVar[Path]`

---

## 🔒 What Never Changes (Stable Contracts)

| Contract | Why it must stay stable |
|---|---|
| `actions.py` signatures: `fn(selectors: list[str], ...)` | Skills depend on this |
| `planner.plan(goal: str) → list[Step]` | Executor depends on this |
| `skill.get_action(name) → callable` | Executor + skill_manager depend on this |
| `verifier.verify(dict) → VerifyResult` | Executor depends on this |
| `skill_manager.get_skill(url) → Skill` | Executor depends on this |
| `executor.run(steps: list[Step]) → dict` | main.py + tests depend on this |

Internals of each module are free to change. These interfaces are not.

---

## 🚀 Quick Start

```bash
# Chrome starten (einmalig)
chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\tmp\chrome_debug

# ── YouTube ────────────────────────────────────────────────────────────────────
python main.py phase4                              # Suche
python main.py phase4b                             # Suche + erstes Video anklicken
python main.py phase9                              # Top 3 Videos in neuen Tabs (alle pausiert)
python main.py phase9 "machine learning" 5        # eigener Begriff, 5 Tabs

# ── YouTube On-Page (Phase 11) ─────────────────────────────────────────────────
# (Muss bereits auf einem YouTube-Video sein)
python main.py cmd "like this video"
python main.py cmd "like this video and subscribe"
python main.py cmd "set speed to 1.5x"
python main.py cmd "skip 10 seconds"
python main.py cmd "open next 3 videos"
python main.py cmd "play the 3rd next video"
python main.py cmd "add this to watch later"
python main.py cmd "go to channel MrBeast"
python main.py cmd "increase speed to 2x"
python main.py cmd "next short"

# ── Amazon ─────────────────────────────────────────────────────────────────────
python main.py phase7                              # Suche
python main.py phase7b                             # Suche + ersten Treffer anklicken
python main.py phase9b                             # Top 3 Produkte in neuen Tabs
python main.py phase9b "gaming mouse" 4           # eigener Begriff, 4 Tabs

# ── Amazon On-Page (Phase 11) ──────────────────────────────────────────────────
# (Muss bereits auf einer Amazon-Produktseite sein)
python main.py cmd "add this product to cart"
python main.py cmd "add to wishlist"
python main.py cmd "open cart"
python main.py cmd "open orders"
python main.py cmd "read price"
python main.py cmd "read 5 reviews"

# ── LLM-Planner ────────────────────────────────────────────────────────────────
python main.py phase8                              # Ollama phi4:14b / llama3.3:8b

# ── Unit-Tests ──────────────────────────────────────────────────────────────────
python -m pytest tests/test_planner.py -v         # Phase 5 (32 Tests)
python -m pytest tests/test_executor.py -v        # Phase 4 (24 Tests)
python -m pytest tests/test_verifier.py -v        # Phase 3 (22 Tests)
python -m pytest tests/ -v                        # Alle Tests
```
