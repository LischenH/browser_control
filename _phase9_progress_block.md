### 🧩 Phase 9 — Multi-Tab Engine ✅ ABGESCHLOSSEN

**Single-Tab vs. Multi-Tab Wiedergabe:**

| Szenario | Action | Video-Verhalten |
|---|---|---|
| Einzelnes Video öffnen | `click_first_video()` | **Spielt sofort** — kein Pause-Aufruf |
| N Videos in Tabs öffnen | `open_top_results(n)` | **Sofort pausiert** — alle Tabs bereit, keiner spielt |

- [x] `core/actions.py` — `get_all_hrefs(selectors, limit) → list[str]`
- [x] `core/actions.py` — `open_new_tab(url) → Page` — ändert `self._page` NICHT
- [x] `core/actions.py` — `evaluate_js(script, page=None) → any` — z.B. `video.pause()`
- [x] `youtube_skill.py` — `open_top_results(n)`: Links → N Tabs → Player abwarten → pausieren → Titel lesen
- [x] `youtube_skill.py` — `_JS_PAUSE_VIDEO` Arrow-Function: idempotentes `video.pause()`
- [x] `youtube_skill.py` — `click_first_video()` **unverändert** — Single-Tab, spielt
- [x] `amazon_skill.py` — `open_top_results(n)`: analog, Verifikation via `/dp/`, kein Pause
- [x] `agent/executor.py` — Repeat-Support: `step.params["repeat"] = N`
- [x] `agent/executor.py` — Tab-Tracking: `_collect_tab_data()` + `"opened_tabs"` im Result
- [x] `agent/planner.py` — `_RE_YT_TOP_N` / `_RE_AMZ_TOP_N` Regex + Plan-Templates
- [x] `main.py` — `demo_phase9_multitab(goal)` + CLI `phase9` / `phase9b`

### 🧩 Phase 10 — Interrupt System
- [ ] skip ads
- [ ] close popups
- [ ] cookie handler

läuft parallel

### 🧩 Phase 11 — Data Layer
- [ ] Ergebnisse sammeln
- [ ] strukturieren
- [ ] speichern

### 🧩 Phase 12 — Research Mode
- [ ] mehrere Quellen
- [ ] vergleichen
- [ ] zusammenfassen

