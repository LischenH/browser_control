
---

## 📋 Changelog Phase-9 — Multi-Tab Execution

### 🎯 Ziel
Aufgaben wie "öffne die Top 5 YouTube-Videos in neuen Tabs" oder "öffne die besten Amazon-Ergebnisse" automatisiert ausführen.

### 🧠 Design-Entscheidung — Single-Tab vs. Multi-Tab Wiedergabe

| Szenario | Action | Video-Verhalten |
|---|---|---|
| Einzelnes Video öffnen | `click_first_video()` | **Spielt sofort** — kein Pause-Aufruf |
| N Videos in Tabs öffnen | `open_top_results(n)` | **Sofort pausiert** — alle Tabs bereit, keiner spielt |

**Warum?**
Wenn `open_top_results(5)` 5 neue Tabs öffnet, würden ohne Pause 5 Audiostreams gleichzeitig laufen. Jeder Tab wird daher nach dem Laden via `evaluate_js("video.pause()")` sofort stumm gestellt. Der Nutzer sieht 5 geöffnete Tabs und wählt selbst, welches er schaut.

`click_first_video()` hingegen öffnet exakt einen Tab und enthält **keinen** Pause-Aufruf — das ist das erwartete Einzelvideo-Verhalten.

### ✅ Neue Features

**1. `core/actions.py` — 3 neue Primitive (additiv, rückwärts-kompatibel)**

| Primitive | Signatur | Beschreibung |
|---|---|---|
| `get_all_hrefs` | `(selectors, limit) → list[str]` | Extrahiert alle `href`-Attribute passender Elemente via `eval_on_selector_all` |
| `open_new_tab` | `(url) → Page` | Öffnet neuen Tab, navigiert zu URL, wartet auf `page_ready`. Ändert `self._page` NICHT |
| `evaluate_js` | `(script, page=None) → any` | Führt beliebigen JS-Ausdruck auf einer Page aus. Ermöglicht `video.pause()` ohne Playwright-Direktzugriff im Skill |

**2. `youtube_skill.py` — neue Action `open_top_results(n)`**
- Extrahiert Top-N-Links via `get_all_hrefs(result_links, limit=n)`
- Pro Link: `open_new_tab(url)` → wartet auf Player → `evaluate_js(_JS_PAUSE_VIDEO)` → liest Titel
- `_JS_PAUSE_VIDEO`: Arrow-Function, pausiert `document.querySelector('video')`, gibt `None/True/False` zurück
- `click_first_video()` bleibt **unverändert** — spielt weiter

**3. `amazon_skill.py` — neue Action `open_top_results(n)`**
- Analog zu YouTube, aber Verifikation via `"/dp/" in url` (ASIN-Format)
- Kein Pause-Aufruf bei Amazon (Videos existieren nicht auf Produktseiten)

**4. `agent/executor.py` — 3 Erweiterungen**
- **Repeat-Loop**: `step.params["repeat"] = N` → Step wird N-mal ausgeführt (Param wird vor Action-Call extrahiert und nicht übergeben)
- **Tab-Tracking**: `_collect_tab_data()` erkennt `open_top_results`-Output (`list[dict]` mit `url`+`title`) und akkumuliert in `self._opened_tabs`
- **Result-Schema**: `run()` gibt jetzt zusätzlich `"opened_tabs": [...]` zurück

**5. `agent/planner.py` — Phase-9-Patterns**
- `_RE_YT_TOP_N`: `"search YouTube for <query> and open top/first N [videos]"`
- `_RE_AMZ_TOP_N`: `"search Amazon for <query> and open top/first N [results]"`
- `_plan_yt_open_top(query, n)`: `navigate → search → open_top_results(n)`
- `_plan_amz_open_top(query, n)`: analog für Amazon
- Beide `_RE_*_TOP_N`-Patterns werden **vor** den allgemeineren `_RE_*_SEARCH`-Patterns geprüft
- `_VALID_ACTIONS` erweitert: `open_top_results`, `read_result_title`, `read_product_title`

**6. `main.py` — neue Demo + CLI**
- `demo_phase9_multitab(goal)`: vollständiger E2E-Flow inkl. Tab-Summary-Ausgabe
- `phase9`:  YouTube Top 3 (default) / eigener Query + optionale N
- `phase9b`: Amazon Top 3 (default) / eigener Query + optionale N

### 🧪 Test-Befehle

```bash
# Standard-Test: "search YouTube for Python tutorial and open top 3 videos"
python main.py phase9

# Standard-Test: "search Amazon for wireless headphones and open top 3 results"
python main.py phase9b

# Eigener Suchbegriff (YouTube, 3 Tabs)
python main.py phase9 "machine learning"

# Eigener Suchbegriff + eigene Anzahl
python main.py phase9 "Python tutorial" 5
python main.py phase9b "noise cancelling headphones" 4
```

### 🔒 Stable Contracts — unverletzt

| Contract | Status |
|---|---|
| `actions.py` Signaturen | ✅ Nur additive Methoden, keine Änderungen an bestehenden |
| `planner.plan(goal) → list[Step]` | ✅ unverändert |
| `skill.get_action(name) → callable` | ✅ unverändert |
| `verifier.verify(dict) → VerifyResult` | ✅ unverändert |
| `skill_manager.get_skill(url) → Skill` | ✅ unverändert |
| `executor.run(steps) → dict` | ✅ additiv — neues Feld `opened_tabs` ergänzt |
| `core/browser.py` | ✅ unverändert |
| `core/tab_manager.py` | ✅ unverändert |
| `agent/verifier.py` | ✅ unverändert |
| `skill_manager/manager.py` | ✅ unverändert |

### 📝 Architektur-Notizen

**`evaluate_js` als Primitive statt `click(pause_button)`:**
Ein `click` auf den Pause-Button würde toggle-artig arbeiten — bei einem bereits pausierten Video würde es starten. `video.pause()` ist idempotent und direkt. Außerdem entfällt die Abhängigkeit von einem sichtbaren Button-Selector.

**Fallback bei fehlendem `<video>`-Element:**
YouTube rendert den Player asynchron. Wenn `wait_for(play_button)` einen Timeout hat aber `evaluate_js` trotzdem läuft, gibt `_JS_PAUSE_VIDEO` `null` zurück (kein Element vorhanden). Der Tab wird trotzdem in `tab_results` aufgenommen — nur mit `paused: False`. Kein Abbruch.

**`open_new_tab()` ändert `self._page` nicht:**
Die Haupt-Actions-Instanz (auf der Suchergebnis-Seite) bleibt unberührt. Jeder neue Tab bekommt seine eigene `Actions(new_page)`-Instanz. Das Haupt-Tab bleibt nach `open_top_results` auf der Suchergebnis-Seite — verify-Conditions prüfen das explizit.

**Phase 10 Readiness:**
`evaluate_js` ist generisch genug für weitere JS-basierte Interaktionen: Formular-Submits, Cookie-Banners, Shadow-DOM-Zugriff etc.
