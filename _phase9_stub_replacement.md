### 🧩 Phase 9 — Multi-Tab Engine ✅ ABGESCHLOSSEN

**Single-Tab vs. Multi-Tab Wiedergabe:**

| Szenario | Action | Verhalten |
|---|---|---|
| Einzelnes Video | `click_first_video()` | **Spielt sofort** |
| N Videos öffnen | `open_top_results(n)` | **Alle sofort pausiert** |

- [x] `core/actions.py` — `get_all_hrefs(selectors, limit)` — extrahiert alle hrefs einer Ergebnisliste
- [x] `core/actions.py` — `open_new_tab(url) → Page` — neuer Tab, wait_for_page_ready, ändert self._page NICHT
- [x] `core/actions.py` — `evaluate_js(script, page=None) → any` — führt JS auf einer Page aus (z.B. `video.pause()`)
- [x] `youtube_skill.py` — `open_top_results(n)`: Links extrahieren → N Tabs öffnen → Player abwarten → pausieren → Titel lesen
- [x] `youtube_skill.py` — `_JS_PAUSE_VIDEO` Arrow-Function: idempotentes `video.pause()`, gibt None/True/False zurück
- [x] `youtube_skill.py` — `click_first_video()` **unverändert** — Single-Tab, kein Pause-Aufruf, Video spielt
- [x] `amazon_skill.py` — `open_top_results(n)`: analog, Verifikation via `/dp/` (kein Pause — keine Videos auf Produktseiten)
- [x] `agent/executor.py` — Repeat-Support: `step.params["repeat"] = N` → Step N-mal ausführen
- [x] `agent/executor.py` — Tab-Tracking: `_collect_tab_data()` akkumuliert `open_top_results`-Output
- [x] `agent/executor.py` — Result-Schema: `run()` gibt zusätzlich `"opened_tabs": [...]` zurück
- [x] `agent/planner.py` — `_RE_YT_TOP_N` / `_RE_AMZ_TOP_N` Regex-Patterns (vor allgemeineren Patterns geprüft)
- [x] `agent/planner.py` — `_plan_yt_open_top(query, n)` / `_plan_amz_open_top(query, n)` Templates
- [x] `agent/planner.py` — `_VALID_ACTIONS` erweitert: `open_top_results`, `read_result_title`, `read_product_title`
- [x] `main.py` — `demo_phase9_multitab(goal)` + CLI `phase9` / `phase9b`

