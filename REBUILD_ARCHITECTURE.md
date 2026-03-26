9# 🧠 Browser Control — Rebuild Architecture

> **Status:** Design-complete, pre-implementation  
> **Based on:** Previous architecture session + analysis of `browser_automation` v2  
> **Author:** Principal AI Systems Architect  
> **Principle:** Intent-first. The system knows *what* to do; it figures out *where* independently.

---

## 1. Vision & Goals

### The core problem with the current system

The current `browser_control` system treats selectors as first-class citizens. Skills encode brittle
CSS paths that break the moment a site updates its DOM. The result is constant maintenance overhead
and a system that is fundamentally coupled to UI implementation details it has no control over.

### What the rebuilt system must achieve

**UI-resistance**  
No action in the public API references a DOM selector directly. Selectors are an internal
implementation detail of the `SelectorEngine`. Skills declare *intent*; the engine resolves
*location*. A site can rename every CSS class and the system continues working.

**Self-healing**  
When a learned selector stops working, the system does not throw and die. It re-discovers the
element, validates the candidate, promotes it to the per-domain cache, and continues. The failure
is logged but execution is not interrupted for recoverable cases.

**Deterministic automation**  
Execution timing is driven by observable DOM/network signals, not static sleeps. Every action
runs only when the page is genuinely ready. The system produces the same outcome whether the
page loaded in 0.3s or 4s.

**Multi-tab intelligence**  
Each tab has a tracked intent, content type, and active task. The executor always knows which
tab it is operating on. Background tabs do not receive accidental actions. Tab switches are
explicit, logged, and reversible.

**Observability by default**  
Every action execution emits a structured trace event: what was attempted, which selector tier
resolved it, how long it took, and whether healing was invoked. Selector success rates are
tracked per domain and surfaced when they fall below a threshold.

---

## 2. Core Architecture

### Component overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        PUBLIC API LAYER                         │
│   ActionDispatcher  ←  ActionRegistry  ←  action_defs.yaml     │
│           │                                                     │
│           ↓                                                     │
│       SafetyGate  (classify → confirm | allow | block)         │
└─────────────────────────┬───────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│                    INTELLIGENCE LAYER                           │
│                                                                 │
│   SelectorEngine ──── SelectorCache ──── DOMIntelligence       │
│        │                   ↑                   │               │
│        │ (on fail)         │ (promote)         │               │
│        └────── HealingLoop ┘ ←─────────────────┘               │
└─────────────────────────┬───────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│                    EXECUTION LAYER                              │
│                                                                 │
│   AdaptiveExecutor ──── LoadDetector ──── TabContext           │
│        │                                      │               │
│        └──── InterruptHandler ────────────────┘               │
└─────────────────────────┬───────────────────────────────────────┘
                          │
┌─────────────────────────▼───────────────────────────────────────┐
│                   OBSERVABILITY LAYER                           │
│                                                                 │
│   ActionTracer ──── SelectorMetrics ──── FailureHeatmap        │
└─────────────────────────────────────────────────────────────────┘
```

### Data flow — single action execution

```
Caller: perform("like_video")
  │
  ▼
ActionRegistry.resolve("like_video")
  → returns ActionSpec { hints: [...], risk: LOW, domain_override: None }
  │
  ▼
SafetyGate.check(spec)
  → LOW risk → allow immediately
  │
  ▼
SelectorCache.get("youtube.com", "like_video")
  → HIT  → skip SelectorEngine, use cached selector directly
  → MISS → proceed to SelectorEngine
  │
  ▼ (cache miss path)
SelectorEngine.find(spec, context)
  → P1: data-* attributes     → FOUND (score 95) → return
  → P2: aria-* attributes     → skip if P1 succeeded
  → P3: role selectors        → fallback if P1 failed
  → P4: structural anchors    → fallback if P1-P3 failed
  → P5: text content          → last resort
  → EXHAUSTED → HealingLoop.attempt_heal(spec, context)
  │
  ▼
AdaptiveExecutor.execute(action, element)
  → LoadDetector.wait_for_ready()
  → InterruptHandler.clear_overlays()
  → perform DOM action
  → ActionTracer.record(trace_event)
  │
  ▼
SelectorCache.put(domain, action, winning_selector, score)
  → SelectorMetrics.record_success(selector, tier)
```

### Execution lifecycle

1. **Intent declaration** — caller names an action (`subscribe`, `download_3mf`), not a selector
2. **Risk gate** — `SafetyGate` classifies the action and may require confirmation
3. **Cache probe** — `SelectorCache` returns a pre-validated selector if available
4. **Resolution** — `SelectorEngine` walks the priority chain on cache miss
5. **Readiness wait** — `LoadDetector` waits for DOM + network idle signals
6. **Interrupt clearing** — `InterruptHandler` dismisses any blocking overlays
7. **Execution** — element is acted on via the browser driver
8. **Post-execution** — trace event emitted, cache updated, metrics updated

---

## 3. Core Systems

### 3.1 SelectorEngine

The most critical component. All element resolution flows through it. The engine never exposes
selectors to callers — it accepts an `ActionSpec` and returns an `ElementHandle`.

#### Priority ranking

| Priority | Strategy | Stability | Score |
|----------|----------|-----------|-------|
| P1 | `data-*` attributes (`data-testid`, `data-action`, `data-component`) | Very high | 100 |
| P2 | `aria-*` attributes (`aria-label`, `aria-role`, `aria-describedby`) | High | 85 |
| P3 | Role + type selectors (`button[type=submit]`, `[role=button]`) | Medium-high | 70 |
| P4 | Structural anchors (parent-child path, nth-child relationships) | Medium | 45 |
| P5 | Text content match (`:contains`, `innerText` scan) | Fragile | 20 |
| P6 | Vision fallback (screenshot → vision model → coordinates) | Last resort | 5 |

P6 is new relative to the initial design — absorbed from `browser_automation`'s `VisionController`.
It is activated only when P1–P5 all fail AND a vision model is configured.

#### Fallback strategy

Each tier is attempted in order. On finding a candidate at any tier, the engine:
1. Validates the candidate is visible and has a non-zero bounding box
2. Computes a confidence score using the scoring JS (see §3.1 below)
3. Returns immediately — does not continue to lower-priority tiers

If the candidate is found but fails the visibility/bounding-box check, the engine continues
to the next tier rather than failing immediately. This handles elements that exist in the DOM
but are not interactable (hidden, zero-size, in an offscreen container).

#### Scoring system

The scoring function is a pure JS probe injected into the page. It is absorbed and generalized
from `browser_automation`'s `_COLLECT_BUTTONS_JS` pattern, which demonstrated that a weighted
scoring approach with exact-match bonuses produces significantly better candidates than simple
selector matching alone.

Scoring weights:
```
aria-label  exact match  → 100
innerText   exact match  → 90
title attr  exact match  → 80
value attr  exact match  → 70
aria-label  includes     → 60
innerText   includes     → 50
title attr  includes     → 40
value attr  includes     → 30
name attr   includes     → 20
```

The engine evaluates all candidates at a given tier and returns the highest-scoring visible one.
This produces dramatically better results on sites where multiple elements partially match a query
(e.g. a "like" button and a "liked" counter both containing the text "like").

#### Key design rule

The `SelectorEngine` must never leak selector strings to callers. The `find()` method returns
an `ElementHandle` (coordinates + metadata). The selector that produced it is stored only
internally in the `SelectorCache`.

---

### 3.2 DOM Intelligence Layer

The `DOMIntelligence` component performs semantic analysis of the DOM to assist the `SelectorEngine`
and `HealingLoop`. It is the system's "understanding" of what elements mean, not just where they are.

#### Semantic detection

`DOMIntelligence` runs a full snapshot of the page's interactive elements on demand, modeled on
`browser_automation`'s `SnapshotGenerator`. The snapshot assigns a stable `ref-ID` (`e1`, `e2`, ...)
to each visible interactive element and records: tag, role, label, coordinates, checked/disabled state,
href, and type.

The key innovation absorbed from `browser_automation`: the snapshot is the reconciliation point
after a healing attempt. When `HealingLoop` needs candidates, it calls `DOMIntelligence.capture_snapshot()`
which gives it a fresh, ref-tagged picture of the current DOM state to score against.

#### Role classification

`DOMIntelligence.classify_element()` infers a semantic role for any element. Roles are defined
in `role_taxonomy.yaml` (not hardcoded) and can be extended per-domain. Examples:

| Inferred role | Detection signals |
|--------------|------------------|
| `play` | aria-label contains "play", SVG play-triangle inside, type=button near `<video>` |
| `like` | aria-label contains "like/gefällt", thumbs-up SVG path, proximity to engagement metrics |
| `subscribe` | aria-label contains "subscribe/abonnieren", bell icon sibling, proximity to channel name |
| `download` | aria-label contains "download", down-arrow SVG, href with file extension |
| `search_submit` | type=submit, sibling is search input, aria-label contains "search" |
| `sort_trigger` | aria-haspopup=listbox/true, siblings include order-related text |
| `close_dialog` | role=button, aria-label="close/schließen", position in top-right of overlay |

#### Layout awareness

`DOMIntelligence.detect_layout_anchor()` identifies the structural context of an element — what
container it lives in, what siblings it has, and what its positional relationship is to landmark
elements. This information feeds P4 (structural anchor) selector generation and is stored in the
`ActionSpec` after a successful healing pass.

---

### 3.3 Action Abstraction Layer

The fundamental shift from the current system: callers declare *what* to do, not *how* to do it.

#### Intent-based actions

Actions are defined in `config/action_definitions.yaml`. Each entry specifies:

```yaml
like_video:
  description: "Engage the like/thumbs-up action on the current video"
  risk: low
  domain_hints:
    youtube.com:
      p1_hint: 'button[data-testid*="like"]'
      p2_hint: 'button[aria-label*="like" i]'
      semantic_role: like
  global_hints:
    p2_hint: '[aria-label*="like" i]'
    p3_hint: '[role="button"]'
    semantic_role: like
  confirmation_required: false
  reversible: true
  inverse_action: unlike_video
```

`domain_hints` are promoted to P1/P2 when the action runs on the matching domain.
`global_hints` are the fallback for unknown domains.
`semantic_role` tells `DOMIntelligence` which role to look for during healing.

#### Mapping system

`ActionRegistry` loads all YAML definitions at startup and exposes:
- `resolve(name) → ActionSpec` — called by `ActionDispatcher`
- `register(name, spec)` — runtime extension (used by domain plugins)
- `list(domain=None) → [ActionSpec]` — for introspection

The mapping from action name to resolution strategy is entirely data-driven. Adding support
for a new site-specific action requires only a YAML entry, not code changes.

#### Decoupling from UI

An important implication: the same action name (`like_video`) works on YouTube, Vimeo, Twitch,
and any future video platform, provided each has a `domain_hints` entry or the global hints
cover it via semantic role matching. Skills that call `perform("like_video")` are completely
insulated from platform differences.

---

### 3.4 Self-Healing System

The `HealingLoop` is invoked only after `SelectorEngine` has exhausted all tiers including P6.
It performs a broader, unguided search of the DOM using the action's semantic role and known
behavioral signals.

#### Fallback logic

```
HealingLoop.attempt_heal(spec, context):
  1. Call DOMIntelligence.capture_snapshot()
     → fresh ref-ID tagged picture of the current DOM
  2. For each element in snapshot:
     → DOMIntelligence.score_semantic_match(element, spec.semantic_role)
     → collect all elements with score > HEALING_THRESHOLD (default: 40)
  3. Sort candidates by score descending
  4. For each candidate (highest score first):
     → verify visible + non-zero bounding box
     → if valid → return as HealResult(candidate, score)
  5. If no candidates above threshold → return HealResult.failure()
```

#### Selector learning

When `HealingLoop` returns a successful candidate, `ActionDispatcher` calls
`SelectorCache.put()` with:
- The domain (from `context.url`)
- The action name
- A minimal stable selector derived from the candidate's ref-snapshot data
  (preferring data-* or aria-* attributes extracted from the element)
- The confidence score

The written selector is **not** the original failed selector. It is a freshly derived
selector from the element the healing process actually found. This means stale selectors
are never re-promoted — only proven ones are cached.

#### Caching strategy

`SelectorCache` is backed by SQLite. Schema:

```sql
CREATE TABLE selector_cache (
    domain_hash  TEXT NOT NULL,
    action_name  TEXT NOT NULL,
    selector     TEXT NOT NULL,
    score        REAL NOT NULL,
    tier         INTEGER NOT NULL,    -- which priority tier produced it
    verified_at  INTEGER NOT NULL,    -- unix timestamp
    fail_count   INTEGER DEFAULT 0,
    PRIMARY KEY (domain_hash, action_name)
);
```

Cache behavior:
- **TTL:** entries older than `CACHE_TTL_DAYS` (default: 14) are evicted
- **Failure invalidation:** when a cached selector fails, `fail_count` is incremented.
  At `fail_count >= 3`, the entry is invalidated and the next action triggers re-healing.
- **Score threshold for write:** only selectors with score ≥ 60 are cached.
  Low-confidence healed selectors (P4/P5 outcomes) are used once but not persisted.

---

### 3.5 Executor 2.0 (AdaptiveExecutor)

The current system has made significant progress on adaptive timing in `actions.py` (Phase 10).
The rebuilt executor absorbs and extends this work.

#### Adaptive timing

Absorbed directly from `browser_control`'s `wait_for_page_ready()`:

**Phase 1 — DOM readiness fast-path**  
Synchronous JS check: `document.readyState`. If already `"complete"`, the network idle phase
is skipped entirely. Pre-action cost on a loaded page: ~50ms (one DOM stability poll).

**Phase 2 — Network idle**  
Run only on freshly-navigated pages. Skipped for SPAs that never reach true networkidle.
Timeout: configurable, default 3s. Non-fatal on timeout.

**Phase 3 — Spinner detection**  
Check known spinner selectors with `is_visible()` first (non-blocking, ~1ms per check).
Only if a spinner IS visible do we wait for it to disappear. This is the key optimization:
zero wait on spinners that aren't present.

**Phase 4 — DOM stability**  
MutationObserver with `lastMutation = 0` start value. Resolves in ~50ms on a stable DOM.
Only extends to the full `observe_ms` window when DOM mutations are actively occurring.

#### Retry intelligence

Absorbed from `browser_control`'s `_try_selector()` pattern and generalized:

- **Pre-check:** `is_visible()` before any blocking action. If not visible, skip immediately
  (~1ms) instead of burning the full timeout.
- **Post-timeout re-check:** after a `TimeoutError`, re-check visibility. If the element
  has disappeared, skip remaining retries for this attempt.
- **Force-click fallback:** on `click()` failure, try `force=True` before trying JS `.click()`.
  Three-tier action execution:  normal → force → JS fallback.
- **No static sleeps between retries.** The browser driver already waited `DEFAULT_TIMEOUT`.
  Adding another sleep is pure waste.

#### Tab-aware execution

Before every action, `AdaptiveExecutor` calls `TabContext.ensure_active(tab_id)` which:
1. Calls `bring_to_front()` on the target tab (absorbed from `_ensure_tab_focus()`)
2. Verifies the tab's URL matches the expected context
3. Logs the tab switch if it occurred

This makes multi-tab bugs immediately visible in the trace log rather than silently producing
actions on the wrong tab.

#### Interrupt handling

`InterruptHandler` is carried forward from the current system and runs before every action.
It clears cookie banners, consent dialogs, ad overlays, and any modal that would intercept
the intended click. The handler is domain-aware: different sites get different interrupt
detection rules.

---

### 3.6 Safety Layer

#### Risk classification

Every action in `action_definitions.yaml` carries a `risk` field:

| Level | Definition | Examples |
|-------|-----------|---------|
| `low` | Read, scroll, navigate, hover | get_text, scroll, goto |
| `medium` | Form submit, upload, comment | post_comment, fill_form, upload_file |
| `high` | Account-level change, subscription, collect | subscribe, unsubscribe, create_playlist |
| `critical` | Financial transaction, irreversible delete | purchase, delete_account, confirm_payment |

#### Confirmation rules

`SafetyGate.check(action_spec)` returns one of three verdicts:
- `ALLOW` — execute immediately (low + medium in non-interactive mode)
- `CONFIRM_REQUIRED` — block execution, surface confirmation prompt to caller
- `BLOCK` — refuse execution regardless of confirmation (reserved for future policy use)

Default policy:
- `low` → always `ALLOW`
- `medium` → `ALLOW` in scripted mode, `CONFIRM_REQUIRED` in interactive mode
- `high` → always `CONFIRM_REQUIRED`
- `critical` → always `CONFIRM_REQUIRED` with explicit action description shown

#### Critical action protection

`ConfirmationHandler` is a protocol (interface), not a concrete class. The execution environment
supplies its own implementation:
- CLI mode → text prompt
- GUI mode → dialog box
- Automated test mode → auto-approve (gated by test-only config flag)

This decoupling ensures the Safety Layer works in every deployment context without modification.

---

### 3.7 Observability

#### Action trace log

`ActionTracer` records a structured `TraceEvent` for every action attempt. Fields:

```python
@dataclass
class TraceEvent:
    session_id:    str        # groups all events in one automation run
    action_name:   str        # e.g. "like_video"
    domain:        str        # e.g. "youtube.com"
    selector_used: str        # the winning selector
    tier:          int        # which priority tier (1-6)
    score:         float      # confidence score at resolution
    duration_ms:   int        # total action duration
    healed:        bool       # was HealingLoop invoked?
    outcome:       str        # "success" | "failure" | "confirmed" | "blocked"
    error:         str | None # exception message on failure
    timestamp:     datetime
```

Trace events are written to an append-only JSONL file. The file can be tailed in real-time
during development, or imported into any log analysis tool.

#### Selector success rate tracking

`SelectorMetrics` aggregates `TraceEvent` data into per-selector and per-domain statistics:
- Success rate per `(domain, action_name)` pair
- Tier distribution: what % of resolutions use P1 vs P2 vs P3, etc.
- Mean time-to-resolution per tier
- Heal invocation rate

When the success rate for a `(domain, action_name)` pair falls below `ALERT_THRESHOLD`
(default: 0.7), `SelectorMetrics` emits a warning log and flags the entry for proactive
re-validation on the next execution.

#### Failure heatmap

`FailureHeatmap` aggregates failures by domain and action to identify systemic breakage
patterns. A "heatmap" entry fires when:
- 3+ consecutive failures occur for the same `(domain, action_name)` within a 24-hour window

This signals that a site has likely deployed a UI change that needs a healing pass or a
manual `action_definitions.yaml` update.

#### Debugging hooks

Every component accepts an optional `debug_mode: bool` flag. In debug mode:
- `DOMIntelligence` outputs a `print_snapshot()` to stdout (absorbed from `browser_automation`)
- `SelectorEngine` logs all candidates at every tier with scores, not just the winner
- `LoadDetector` logs `[READY] dom=Xms network=Xms stable=Xms total=Xms` for every wait
  (absorbed directly from `browser_control`'s Phase 10 timing logs)
- `HealingLoop` prints the candidate pool with scores before selecting

---

### 3.8 Multi-Tab Intelligence

#### Tab intent tracking

`TabContext` maintains a registry of open tabs, each with:

```python
@dataclass
class TabMeta:
    tab_id:        str
    url:           str
    title:         str
    content_type:  ContentType   # VIDEO | ARTICLE | CHECKOUT | SEARCH | FORM | UNKNOWN
    intent:        str | None    # e.g. "collect_model", "watch_video", "search_results"
    active_action: str | None    # currently executing action name
    opened_at:     datetime
    opener_tab_id: str | None    # which tab spawned this one
```

`ContentClassifier.infer_type(tab_id)` inspects the DOM of a tab to determine its content
type. Detection is heuristic: presence of `<video>`, URL patterns, landmark element presence.

#### Context switching

`TabManager.switch_to(intent)` finds the tab currently holding the given intent and brings
it to front. This replaces the current system's manual `bring_to_front()` calls scattered
across skills with a single, logged, intent-driven switch.

The absorbed pattern from `browser_automation`'s `open_new_tab()` is important here:
new tabs must be opened with `window.open(url, '_blank')` via JS, NOT with
`context.new_page()`. The latter activates the new tab immediately in Chrome, disturbing
the focus history of all existing tabs. `window.open()` opens a background tab without
changing focus. This distinction is critical for multi-tab automation flows.

#### Tab-task mapping

`TabContext.assign_task(tab_id, task_name)` binds an automation task to a specific tab.
`ActionDispatcher` queries this mapping before each action to ensure it is operating
on the correct tab. Cross-tab action errors (acting on Tab A when Tab B was intended)
are surfaced as explicit `TabMismatchError` rather than silent wrong-tab behavior.

---

## 4. Reference System Analysis (browser_automation)

### Key strengths

**1. Pure CDP transport layer**  
`browser_automation` operates directly over the Chrome DevTools Protocol via WebSocket,
bypassing Playwright entirely. This gives it lower overhead per action and direct access
to CDP domains that Playwright abstracts away (notably `Input.dispatchMouseEvent` for
precise coordinate-based clicks). However, this comes at the cost of Playwright's
battle-tested reliability guarantees and its superior SPA handling.
*Integration decision: keep Playwright as the driver in the new system; adopt CDP patterns
as fallback mechanisms inside `DOMIntelligence`.*

**2. Ref-ID snapshot system** (`snapshot_generator.py`)  
The most distinctive architectural feature. `SnapshotGenerator` injects a JS probe that
assigns deterministic `e1`, `e2`, ... ref-IDs to every interactive element visible at a
given moment. Actions can reference elements by ref-ID rather than by selector. Because
the snapshot is regenerated on every action call, the ref-IDs stay aligned with the
current DOM state.
*This is a fundamentally different model from selector-based addressing, and it is the
right model for the `HealingLoop`. The new system absorbs it as `DOMIntelligence.capture_snapshot()`.*

**3. Scored candidate search** (`_COLLECT_BUTTONS_JS` in `generic_browser.py`)  
The scoring system is sophisticated: it runs inside the page's JS context, evaluates
multiple attribute matches with weighted exact/partial bonuses, and returns the top-N
candidates ranked by score. This is strictly better than CSS selector matching alone
because it handles ambiguous cases (multiple elements partially matching a label) with
explicit ranking rather than first-match-wins.
*Absorbed directly into `SelectorEngine`'s scoring function.*

**4. Three-tier element classification** (`GenericBrowser`)  
Semantic attributes → visible text → vision fallback. The three tiers map cleanly onto
the new system's P2 (aria-*), P5 (text), and P6 (vision). The important insight from
`GenericBrowser` is that these three tiers are not separate strategies — they are a single
fallback function with escalating cost. The new `SelectorEngine` formalizes this into the
priority chain.

**5. Domain-specific flow abstractions** (`makerworld_advanced_search.py`)  
The MakerWorld sort interaction is a textbook example of a *flow action*: it takes multiple
atomic steps (find sort trigger → click → wait for dropdown → find option → click → wait
for results) and exposes them as a single named operation. The method injection pattern
(injecting methods into `MakerWorldController` at import time) is clever but fragile.
*The new system absorbs the concept as `DomainFlowPlugin` (see §5), generalizing it without
the fragile monkey-patching.*

**6. Vision model integration** (`vision_controller.py`)  
`VisionController.set_vision_model(fn)` accepts any function that takes raw screenshot
bytes and returns bounding boxes with labels. This is an elegant protocol: the system
defines the interface, the caller provides the model. Zero coupling to any specific
vision framework.
*Absorbed as P6 in `SelectorEngine` and documented in the plugin interface.*

### Design patterns used

- **Facade pattern** — `BrowserAutomation` presents a unified API over 7+ subsystems
- **Strategy pattern** — three-tier element finding, swappable at runtime
- **Template method** — every `_COLLECT_*_JS` follows the same probe-score-return shape
- **Monkey patching for extension** — MakerWorld advanced search injects into controller
  (works but not architecturally clean; replaced by plugin system in new design)

### What it does better than the current system

| Capability | browser_automation | browser_control (current) |
|-----------|-------------------|--------------------------|
| Element scoring | Weighted multi-attribute scoring | First matching selector wins |
| Snapshot-based addressing | ✓ (ref-IDs) | ✗ |
| Vision fallback | ✓ (pluggable) | ✗ |
| Domain-specific flows | ✓ (MakerWorld, YouTube) | Via skills (less structured) |
| Page structure analysis | ✓ (`print_page_structure()`) | ✗ |
| CDP-level control | ✓ (direct) | Via Playwright abstraction |
| DOM stability wait | ✗ (fixed sleeps) | ✓ (Phase 10 MutationObserver) |
| Tab focus guarantee | ✗ | ✓ (bring_to_front every action) |
| Interrupt handling | ✗ | ✓ (InterruptHandler) |
| Execution mode (FAST/HUMAN) | ✗ | ✓ |

---

## 5. Smart Integration Plan

### 5.1 Ref-ID snapshot system → DOMIntelligence.capture_snapshot()

**What is integrated**  
The `SnapshotGenerator` JS probe from `browser_automation`. It scans the live DOM, tags every
visible interactive element with a stable ref-ID (`e1`, `e2`, ...), and records coordinates,
role, label, tag, href, and state.

**Where it fits**  
`DOMIntelligence.capture_snapshot()` wraps the probe. Called by `HealingLoop` at the start
of every healing attempt and by debug tooling.

**Why it improves the system**  
The healing loop needs a structured, scored picture of the current DOM to work from. The
snapshot gives it exactly that: a list of candidates with labels and coordinates, without
requiring the healer to compose new selectors from scratch.

**How it differs from the original**  
In `browser_automation`, ref-IDs are used directly by callers (`ba.click_ref("e3")`). In the
new system, ref-IDs are an *internal* healing mechanism. Callers never see them. The snapshot
is a working surface for `DOMIntelligence` and `HealingLoop`, not part of the public API.

---

### 5.2 Weighted element scoring → SelectorEngine scoring function

**What is integrated**  
The JS scoring logic from `_COLLECT_BUTTONS_JS`: weighted exact/partial attribute matches
producing a 0–100 confidence score per candidate.

**Where it fits**  
`SelectorEngine._score_candidate()` runs the scoring function as a JS IIFE injected into the
page. At each priority tier, all candidates are scored; the highest-scoring visible element wins.

**Why it improves the system**  
The current system uses first-match-wins selector logic. On pages where multiple elements
partially match a label (e.g. both a "Like" button and a "Likes: 42K" counter contain "like"),
first-match-wins produces unpredictable results. Scoring picks the right candidate.

**How it differs from the original**  
`browser_automation` runs scoring only in `GenericBrowser` (tier 2, text-based). The new system
runs scoring at *every* tier, including P1 (data-*) and P2 (aria-*). A data-testid that only
partially matches still gets a lower score than one that matches exactly.

---

### 5.3 Three-tier element finding → SelectorEngine P1–P6 chain

**What is integrated**  
The semantic → text → vision fallback order from `GenericBrowser`. The vision tier especially:
the concept that a screenshot-based vision model is the ultimate fallback when all DOM strategies
fail.

**Where it fits**  
P2 (aria-*), P5 (text content), and P6 (vision) in the `SelectorEngine` priority chain.
The vision model is registered via `SelectorEngine.set_vision_provider(fn)` — same pluggable
interface as `VisionController.set_vision_model()`.

**Why it improves the system**  
P6 means there is now a genuine last resort for completely novel DOM structures. Even if a site
uses fully obfuscated class names, server-side rendering, and custom elements, a vision model
can find "the big blue Download button" by what it looks like.

**How it differs from the original**  
In `browser_automation`, vision is a first-class tier that the caller can explicitly invoke
(`click_visually("Download")`). In the new system, vision is strictly a fallback — it fires
automatically when P1–P5 are exhausted, not as a primary strategy. Vision is slow and
non-deterministic; it must not be the first choice.

---

### 5.4 Domain-specific multi-step flows → DomainFlowPlugin system

**What is integrated**  
The conceptual pattern from `makerworld_advanced_search.py`: a named, multi-step interaction
sequence specific to one domain (open sort dropdown → click trigger → wait → click option).

**Where it fits**  
`ActionRegistry` supports a `flow_steps` field in `action_definitions.yaml`. When present,
`ActionDispatcher` delegates to `FlowExecutor` instead of the single-element `AdaptiveExecutor`.

Example YAML:
```yaml
search_sort_makerworld:
  description: "Apply a sort order on MakerWorld search results"
  risk: low
  domain_required: makerworld.com
  flow_steps:
    - action: click
      target_role: sort_trigger
      wait_after_ms: 900
    - action: click
      target_role: sort_option
      target_label_from_param: sort_value
      wait_after_ms: 2500
```

**Why it improves the system**  
Multi-step flows in the current system are encoded in skill Python code, tightly coupled to
specific selectors. Moving them to YAML makes them maintainable without code changes, and
makes `DomainFlowPlugin` the extension point for any site-specific automation.

**How it differs from the original**  
`browser_automation` used Python monkey-patching to inject methods into controller classes.
This is fragile (import order matters, type checkers don't understand it) and not testable in
isolation. The new system uses a data-driven approach: flows are YAML configuration, executed
by a generic `FlowExecutor`, with no monkey-patching anywhere.

---

### 5.5 Page structure analysis → DOMIntelligence.print_page_structure() (debug)

**What is integrated**  
`GenericBrowser.print_page_structure()` and `SnapshotGenerator.print_snapshot()` — human-readable
summaries of all buttons, links, and inputs visible on the page.

**Where it fits**  
`DOMIntelligence` exposes `print_debug_summary()` which calls both and outputs a combined view.
Available in debug mode and via CLI command `browser_control debug-snapshot <url>`.

**Why it improves the system**  
During development of new `action_definitions.yaml` entries, developers need to see what
elements are available on a page to write good hints. The current system has no equivalent
— developers must inspect the DOM manually in browser DevTools.

**How it differs from the original**  
In `browser_automation`, this is a runtime print statement. In the new system, it is a
structured output that can also be exported as JSON for automated action-hint generation.

---

### 5.6 DOM stability detection → LoadDetector (enhanced)

**What is integrated**  
The MutationObserver-based DOM stability approach from `browser_control`'s `_wait_for_dom_stable()`.
The critical optimization: `lastMutation = 0` start value so a stable DOM resolves in ~50ms
rather than waiting the full `observe_ms` window.

**Where it fits**  
`LoadDetector.wait_for_dom_stable()` — called as Phase 4 of `wait_for_ready()`.

**Why it improves the system**  
Fixed timeouts (`time.sleep(2)`) are the #1 source of both slowness (waiting too long) and
flakiness (not waiting long enough). The MutationObserver approach is reactive: it waits
exactly as long as the DOM is mutating, and no longer.

**How it differs from the original**  
Extended from the current system with domain-specific stability thresholds. Sites like YouTube
that have continuous background mutations (analytics, ad bidding) would never stabilize under
a naive observer. The new `LoadDetector` has a configurable "relevant mutation filter" that
ignores mutations in non-interactive subtrees (e.g. script-injected hidden divs).

---

### 5.7 Interrupt handling → InterruptHandler (preserved and extended)

**What is integrated**  
The `InterruptHandler` from `browser_control` is carried forward unchanged in contract,
extended with domain-specific rules.

**Where it fits**  
Called by `AdaptiveExecutor` before every action (current behavior preserved).
`browser_automation` has no equivalent — this is a genuine advantage of the current system.

**Why it improves the system**  
Cookie banners and consent dialogs break automation silently: the click lands on the banner
instead of the intended target, no error is raised, and the action appears to succeed. The
`InterruptHandler` prevents this class of failure entirely.

**How it differs from the original**  
Extended with YAML-configurable interrupt rules per domain, replacing the hardcoded list.
Rules are hot-reloadable without restart.

---

## 6. Migration Strategy

### Overview

Migration is structured in four phases. At every phase boundary, the old system remains
runnable via feature flags. A rollback to the previous phase takes seconds.

### Phase 1 — Shim layer (zero behavior change)

**Duration:** 1–2 days  
**Goal:** Wrap existing `actions.py` calls behind the new `ActionDispatcher` interface.

Steps:
1. Create `core/action_dispatcher.py` with a `perform(action_name, **kwargs)` method
2. Map each current skill's selector calls to named action strings
3. `ActionDispatcher` in Phase 1 simply calls the old `Actions` primitives internally
4. No selector or behavior changes — pure API reshaping
5. Enable `ActionTracer` to begin collecting baseline data

**What gets replaced:** Nothing yet. The shim is an additive wrapper.  
**What gets reused:** All of `core/actions.py`, all skills, all selectors.  
**Risk:** Very low. Zero behavior change.

---

### Phase 2 — Intelligence layer (shadow mode)

**Duration:** 3–5 days  
**Goal:** Deploy `SelectorEngine`, `SelectorCache`, and `DOMIntelligence`. Run them in
parallel with the old selector system and compare outcomes.

Steps:
1. Implement `SelectorEngine` with P1–P5 tiers
2. Implement `SelectorCache` (SQLite backend)
3. Implement `DOMIntelligence` with snapshot + scoring
4. In `ActionDispatcher`, run the new engine alongside the old selectors
5. Log which system would have won each resolution — do not use new system yet
6. Build selector success baseline from 100+ real executions

**What gets replaced:** Nothing in production yet.  
**What gets reused:** All existing selectors become seed data for `action_definitions.yaml` as P4 hints.  
**Risk:** Low. New code paths run but do not affect execution.

---

### Phase 3 — Execution and safety (canary)

**Duration:** 3–5 days  
**Goal:** Enable the full new stack for a subset of actions. Retire fixed timeouts.

Steps:
1. Implement `AdaptiveExecutor` with `LoadDetector`
2. Implement `SafetyGate` and `ConfirmationHandler`
3. Enable new stack for low-risk read-only actions first (`get_text`, `scroll`, `navigate`)
4. Retire all `time.sleep()` calls in these paths
5. Enable `HealingLoop` (log-only mode first — heal but don't cache yet)
6. Add `TabContext` and `TabManager`, enable tab focus guarantee

**What gets replaced:** Static sleeps, bare `actions.py` calls for enabled actions.  
**What gets reused:** `InterruptHandler`, `ModeResolver`, all Playwright page management.  
**Risk:** Medium. Monitor `ActionTracer` for regressions.

---

### Phase 4 — Full migration and decommission

**Duration:** 2–3 days  
**Goal:** All actions route through the new stack. Old system decommissioned.

Steps:
1. Enable `HealingLoop` with cache-write for all action types
2. Move all remaining actions to `action_definitions.yaml`
3. Enable `FailureHeatmap` and `SelectorMetrics` dashboards
4. Remove all hardcoded selector strings from skill Python files
5. Delete or archive the old `actions.py` selector-list logic
6. Enable P6 (vision fallback) if a vision model is available

**What gets replaced:** `actions.py` selector logic, all hardcoded CSS in skills.  
**What gets reused:** Playwright integration, browser launcher, config, test fixtures.  
**Risk:** Low by this phase — the new stack has been running in parallel for 1–2 weeks.

### Risk areas

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| SelectorEngine misses elements that old selectors caught | Medium | Phase 2 shadow mode catches regressions before go-live |
| HealingLoop promotes wrong element | Medium | Score threshold (≥60) + visibility check before cache write |
| Vision fallback produces incorrect coordinates | Low | P6 is last resort; wrong-coordinate clicks are caught by trace + no-op detection |
| SQLite cache corruption on crash | Low | WAL mode + periodic vacuum; cache miss just falls back to SelectorEngine |
| MutationObserver hang on high-mutation pages | Low | Hard cap at `observe_ms × 3`; non-fatal timeout behavior |

---

## 7. Future Extensions

### Plugin system (Skills 2.0)

The `DomainFlowPlugin` pattern (§5.4) is the foundation of a first-class plugin architecture.
A plugin is a YAML file + optional Python module that registers:
- New named actions for a domain
- Domain-specific interrupt rules
- Domain-specific `DOMIntelligence` role hints
- Domain-specific `LoadDetector` stability filters

Plugins are loaded from a `plugins/` directory at startup. No core code changes are required
to add support for a new website.

### Cross-website generalization

The `SelectorEngine` P2 (aria-*) + P3 (role) tiers already generalize across websites.
The next step is a "universal action vocabulary" where high-level actions (`engage_with_content`,
`initiate_download`, `complete_form`) resolve to site-specific sub-actions based on the
current domain. This turns the action registry into a two-level hierarchy: universal verbs
that dispatch to domain-specific implementations.

### Learning system (optional)

`SelectorMetrics` already collects the data needed for a learning system. An optional
`SelectorLearner` background process could:
1. Identify `(domain, action)` pairs with declining success rates
2. Run a background healing pass during idle time
3. Promote newly discovered selectors to the cache proactively
4. Alert when no valid selector exists (requires human attention)

This is explicitly optional — the system is fully functional without it. The learning
component adds reliability but introduces scheduling complexity.

---

## 8. Final Evaluation

### How much more robust is the new system vs old?

The key axis of robustness is **selector fragility**. The current system has selectors
encoded in skill Python files. A single site deployment that renames CSS classes breaks
every skill that targets that class. Recovery requires a developer to manually inspect
the new DOM and update the code.

The rebuilt system eliminates this fragility structurally:
- P1/P2 selectors (data-* and aria-*) are stable by design — they are accessibility and
  testing attributes that sites rarely change
- The cache-invalidation + healing loop means that even when a P3/P4 selector does break,
  the system self-recovers within the same session
- The trace log and heatmap mean breakage is surfaced immediately rather than discovered
  by a user experiencing a failure

Estimated reliability improvement on sites with frequent UI changes: **3–5× fewer broken
automation flows per month**, based on the proportion of current failures attributable to
selector staleness.

### Remaining weaknesses

**1. Vision fallback latency**  
P6 is slow (screenshot + model inference). If P1–P5 all fail on a common action,
the user experiences a noticeable pause. Mitigation: instrument P6 call frequency
and trigger an alert when it fires more than once per session — this indicates a
healing failure that needs manual attention.

**2. YAML action catalog maintenance**  
`action_definitions.yaml` is a new maintenance surface. If a developer adds site-specific
automation without adding a YAML entry, the action won't benefit from the new system.
Mitigation: enforce at test time that all `perform()` calls reference registered actions.

**3. MutationObserver on mutation-heavy SPAs**  
Some SPAs (YouTube's background analytics, live score sites) never reach true DOM stability.
The `3 × observe_ms` hard cap prevents hanging, but it means the executor starts acting
on a still-mutating DOM. Mitigation: per-domain stability filter (§5.6) that ignores
non-interactive mutation subtrees.

**4. SQLite cache in multi-process environments**  
If multiple `browser_control` instances run against the same cache file, WAL mode handles
concurrent reads but write contention is possible. Mitigation: per-process cache files with
a periodic merge process, or Redis for high-concurrency deployments.

### Next best step

**Implement Phase 1 (the shim layer) and enable `ActionTracer`.**

This is the single highest-leverage action. It:
- Commits the new API surface without changing any behavior
- Starts collecting the baseline trace data needed to validate Phase 2
- Forces all skill code to reference named actions instead of calling primitives directly
- Takes 1–2 days and is entirely reversible

Everything else in this document depends on having real execution data to validate against.
Start the tracer first.
