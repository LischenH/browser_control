"""
core/actions.py — Primitive Browser-Aktionen (Phase 1 + Phase 7c + Phase 9).

Design-Prinzipien (aus dem Designdokument):
  1. Jede Primitive akzeptiert eine LISTE von Selektoren, nicht einen einzelnen.
     Core probiert sie der Reihe nach durch. Skills müssen nichts fallback-logisch
     selbst implementieren.

  2. Retry bei transientem Fehler (N-mal, konfigurierbar via config.MAX_RETRIES).
     Ein "transienter Fehler" ist z.B. ein Timeout, weil die Seite noch lädt,
     oder ein "element not attached" nach einem Partial-Reload.

  3. Logging welcher Selector gewonnen hat oder warum alle scheiterten.

  4. Skills rufen NUR diese Primitiven auf. Skills kennen keine Playwright-API.

Phase 7c — Adaptive Execution:
  5. Actions erkennen über ModeResolver ob FAST oder HUMAN-Modus gilt.
  6. FAST: direkte Ausführung, minimale Wartezeiten, kein Maus-Theater.
  7. HUMAN: scroll-into-view → Stabilitätsprüfung → zufällige Verzögerung
             → optionale Mausbewegung → Ausführung.
  8. wait_for_page_ready(): intelligente Seiten-Ladeprüfung ohne static sleep.

Phase 9 — Multi-Tab:
  9. get_all_hrefs(): extrahiert alle href-Attribute passender Elemente.
  10. open_new_tab(): öffnet neuen Tab und navigiert zu einer URL.
  11. evaluate_js(): führt JavaScript auf einer Page aus (z.B. video.pause()).

Phase 10 — Instant & Reactive (Speed Overhaul):
  - wait_for_page_ready() has a fast-path: if readyState=="complete", skips
    networkidle entirely. Pre-action calls cost ~50ms instead of 1.5s+.
  - _wait_for_dom_stable() uses lastMutation=0 so a stable DOM resolves after
    one 50ms poll, not after the full observe_ms window.
  - _try_selector() no longer sleeps after PlaywrightTimeoutError: Playwright
    already waited DEFAULT_TIMEOUT (10s); an extra 500ms is pure waste.
  - click(), type_text(), get_text() call wait_for_page_ready() before acting
    so they react to DOM state, not to fixed delays.
  - All timing is logged: [READY] dom=Xms network=Xms stable=Xms total=Xms

Kontrakt (stabil, ändert sich nie):
  click(selectors: list[str]) → None
  type_text(selectors: list[str], text: str) → None
  wait_for(selectors: list[str], timeout: float) → str   # gibt den matchenden Selector zurück
  get_text(selectors: list[str]) → str
  scroll(direction: str, amount: int) → None

Neue Phase-9-Primitive (additiv, rückwärts-kompatibel):
  get_all_hrefs(selectors: list[str], limit: int) → list[str]
  open_new_tab(url: str) → Page
  evaluate_js(script: str, page=None) → any

Neuer optionaler Parameter (rückwärts-kompatibel):
  click(selectors, mode=None)      → None
  type_text(selectors, text, mode=None) → None
  get_text(selectors, mode=None)   → str
"""

import logging
import random
import time
from typing import Literal, Optional

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import config
from core.mode_resolver import resolve_mode
from core.interrupts import InterruptHandler

logger = logging.getLogger(__name__)

# Typen für Scroll-Richtung
ScrollDirection = Literal["up", "down", "left", "right"]

# ── Common spinner selectors (zum Warten auf deren Verschwinden) ──────────────
# NOTE: '#spinner' intentionally excluded — YouTube uses a custom <yt-spinner>
# element with id='spinner' that is always present and reports as visible via
# is_visible() even when inactive. It never disappears, so including it causes
# a guaranteed 2s timeout on every YouTube page. If you need to detect a real
# YouTube spinner, use '.ytd-spinner' or 'ytd-spinner[active]' instead.
_SPINNER_SELECTORS: list[str] = [
    "[data-testid='spinner']",
    ".loading-spinner",
    ".spinner",
    # "#spinner" ← excluded: always visible on YouTube, causes 2s timeout
    "[aria-label='Loading']",
    ".ytd-spinner",                          # YouTube (only when actively spinning)
    ".s-spinner-container",                  # Amazon Suche
    "#loading-spinner",
]


class ActionError(Exception):
    """
    Wird geworfen, wenn keine Aktion mit keinem Selector nach allen Retries
    erfolgreich war. Enthält eine Liste aller Fehlermeldungen für Debugging.
    """
    pass


# ── Page-Readiness ────────────────────────────────────────────────────────────

def wait_for_page_ready(page: Page, timeout: Optional[float] = None) -> None:
    """
    Intelligently waits until the page is fully loaded and stable.

    FAST-PATH: if document.readyState is already "complete", the networkidle
    phase is skipped entirely. This is critical for pre-action calls in
    click()/type_text()/get_text() — on already-loaded pages the overhead is
    ~50ms (one DOM stability poll) instead of 1.5s+ (networkidle timeout).

    Phases:
      1. DOM ready    — synchronous JS readyState check; domcontentloaded fallback
      2. Network idle — SKIPPED when page was already complete (SPA-safe)
      3. Spinners     — wait for all known loading indicators to disappear
                        (resolves immediately when no spinners are in the DOM)
      4. DOM stable   — MutationObserver with lastMutation=0: resolves in ~50ms
                        on a stable DOM, up to observe_ms only when mutations
                        are actively occurring

    No static sleeps anywhere. Every phase is time-bounded and fails non-fatally
    (page is still treated as ready even if a phase times out).

    Logs timing on every call:
        [READY] dom=Xms network=Xms stable=Xms total=Xms

    Args:
        page:    Playwright Page.
        timeout: Override for DOM phase timeout in seconds
                 (default: config.PAGE_READY_DOM_TIMEOUT).
    """
    t_start = time.monotonic()
    dom_timeout_ms = int((timeout or config.PAGE_READY_DOM_TIMEOUT) * 1000)

    # ── Phase 1: DOM Ready ────────────────────────────────────────────────────
    # Synchronous JS check first — zero added latency when page is already done.
    t0 = time.monotonic()
    already_complete = False
    try:
        ready_state = page.evaluate("() => document.readyState")
        if ready_state == "complete":
            already_complete = True
            logger.debug("[wait_for_page_ready] ✓ readyState=complete (fast-path)")
        else:
            page.wait_for_load_state("domcontentloaded", timeout=dom_timeout_ms)
            logger.debug("[wait_for_page_ready] ✓ domcontentloaded")
    except PlaywrightTimeoutError:
        logger.debug("[wait_for_page_ready] domcontentloaded timeout — continuing")
    except Exception as exc:
        logger.debug(f"[wait_for_page_ready] DOM phase error: {exc}")
    dom_ms = int((time.monotonic() - t0) * 1000)

    # ── Phase 2: Network Idle ─────────────────────────────────────────────────
    # Skipped when page was already complete — this is the key optimization that
    # makes pre-action calls cheap. SPAs never truly reach networkidle anyway.
    t0 = time.monotonic()
    if not already_complete:
        ni_timeout_ms = int(config.PAGE_READY_NETWORK_IDLE_TIMEOUT * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=ni_timeout_ms)
            logger.debug("[wait_for_page_ready] ✓ networkidle")
        except PlaywrightTimeoutError:
            logger.debug(
                "[wait_for_page_ready] networkidle timeout (expected for SPAs) — continuing"
            )
        except Exception as exc:
            logger.debug(f"[wait_for_page_ready] networkidle error: {exc}")
    net_ms = int((time.monotonic() - t0) * 1000)

    # ── Phase 3: Spinners ─────────────────────────────────────────────────────
    # wait_for_selector(state="hidden") resolves immediately when the element
    # is not in the DOM — so this is fast when no spinners are present.
    _wait_for_no_spinner(page)

    # ── Phase 4: DOM Stability ────────────────────────────────────────────────
    t0 = time.monotonic()
    _wait_for_dom_stable(page)
    stable_ms = int((time.monotonic() - t0) * 1000)

    total_ms = int((time.monotonic() - t_start) * 1000)
    logger.info(
        f"[READY] dom={dom_ms}ms network={net_ms}ms stable={stable_ms}ms total={total_ms}ms"
    )


def _wait_for_no_spinner(page: Page) -> None:
    """
    Waits until ALL known spinner selectors are no longer visible.

    KEY OPTIMIZATION: checks is_visible() first (non-blocking, ~1ms).
    Only if the spinner IS currently visible do we wait for it to disappear.
    This means on a page with no active spinners all 10 selectors are checked
    in ~10ms total instead of potentially 10 × 2000ms = 20s.

    Flow per selector:
      is_visible() == False → skip immediately (~1ms)
      is_visible() == True  → wait_for_selector(state="hidden", timeout=2000ms)

    Non-fatal: timeouts and errors are accepted silently.
    """
    for selector in _SPINNER_SELECTORS:
        try:
            # Fast pre-check: is this spinner currently visible at all?
            # is_visible() is non-blocking and returns immediately.
            if not page.is_visible(selector):
                # Not visible (absent or hidden) — no need to wait
                logger.debug(f"[wait_for_page_ready] spinner '{selector}' not visible (skip)")
                continue

            # Spinner IS currently visible — wait for it to disappear
            logger.debug(f"[wait_for_page_ready] spinner '{selector}' visible — waiting")
            page.wait_for_selector(
                selector,
                state="hidden",
                timeout=2000,  # 2s max to disappear
            )
            logger.debug(f"[wait_for_page_ready] spinner '{selector}' gone")
        except PlaywrightTimeoutError:
            logger.debug(
                f"[wait_for_page_ready] spinner-check '{selector}': timeout (accepted)"
            )
        except Exception:
            # is_visible() or wait_for_selector raised — treat as not visible
            pass


def _wait_for_dom_stable(page: Page) -> None:
    """
    Waits until DOM mutation activity has settled.

    KEY OPTIMIZATION: lastMutation starts at 0 (not Date.now()).
    If no mutations occur before the first 50ms poll the observer resolves
    immediately — adding only ~50ms on a stable page, NOT the full observe_ms.

    When mutations ARE detected, waits until observe_ms of silence has passed,
    with a hard cap of 3× observe_ms to prevent infinite waiting.

    Non-fatal: if JS evaluation fails, continues without waiting.
    """
    observe_ms = config.DOM_STABILITY_OBSERVE_MS
    max_wait_ms = observe_ms * 3

    js_wait_stable = f"""
    () => new Promise(resolve => {{
        let lastMutation = 0;
        const observer = new MutationObserver(() => {{
            lastMutation = Date.now();
        }});
        observer.observe(document.body || document.documentElement, {{
            childList: true,
            subtree: true,
            attributes: true,
            characterData: false,
        }});
        const startTime = Date.now();
        const check = () => {{
            const now = Date.now();
            if (lastMutation === 0 || (now - lastMutation) >= {observe_ms}) {{
                // No mutations seen, or mutations have fully settled
                observer.disconnect();
                resolve(true);
            }} else if (now - startTime >= {max_wait_ms}) {{
                // Hard cap — page is too active, proceed anyway
                observer.disconnect();
                resolve(false);
            }} else {{
                setTimeout(check, 50);
            }}
        }};
        setTimeout(check, 50);
    }})
    """.strip()

    try:
        page.evaluate(js_wait_stable)
        logger.debug(
            f"[wait_for_page_ready] ✓ DOM stable ({observe_ms}ms settle window)"
        )
    except Exception as exc:
        logger.debug(f"[wait_for_page_ready] DOM stability JS failed: {exc}")


# ── Human-Mode Helfer ─────────────────────────────────────────────────────────

def _human_delay() -> None:
    """Wartet eine zufällige Verzögerung zwischen HUMAN_DELAY_MIN_MS und MAX_MS."""
    delay_ms = random.randint(config.HUMAN_DELAY_MIN_MS, config.HUMAN_DELAY_MAX_MS)
    time.sleep(delay_ms / 1000.0)
    logger.debug(f"[human] Verzögerung: {delay_ms}ms")


def _scroll_element_into_view(page: Page, selector: str) -> None:
    """Scrollt ein Element in den sichtbaren Bereich via JS."""
    try:
        page.evaluate(
            f"document.querySelector({selector!r})?.scrollIntoView({{block: 'center', behavior: 'instant'}})"
        )
        logger.debug(f"[human] scroll_into_view: '{selector}'")
    except Exception as exc:
        logger.debug(f"[human] scroll_into_view fehlgeschlagen für '{selector}': {exc}")


def _wait_element_stable(page: Page, selector: str, checks: int = 3, interval_ms: int = 60) -> None:
    """
    Prüft, ob ein Element stabil im DOM ist (gleiche Bounding-Box über mehrere
    Messungen). Verhindert Klicks auf sich noch bewegende/animierte Elemente.
    """
    js = f"""
    () => new Promise(resolve => {{
        const el = document.querySelector({selector!r});
        if (!el) {{ resolve(true); return; }}
        let prev = JSON.stringify(el.getBoundingClientRect());
        let stable = 0;
        const check = () => {{
            const curr = JSON.stringify(el.getBoundingClientRect());
            if (curr === prev) {{
                stable++;
                if (stable >= {checks}) {{ resolve(true); return; }}
            }} else {{
                stable = 0;
                prev = curr;
            }}
            setTimeout(check, {interval_ms});
        }};
        check();
    }})
    """.strip()
    try:
        page.evaluate(js)
        logger.debug(f"[human] Element stabil: '{selector}'")
    except Exception as exc:
        logger.debug(f"[human] Stabilitätsprüfung fehlgeschlagen für '{selector}': {exc}")


def _move_mouse_to_element(page: Page, selector: str) -> None:
    """Bewegt die Maus zur Mitte eines Elements (simuliert echtes Nutzerverhalten)."""
    try:
        loc = page.locator(selector).first
        box = loc.bounding_box()
        if box:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            page.mouse.move(cx, cy)
            logger.debug(f"[human] Maus bewegt zu ({cx:.0f}, {cy:.0f}) für '{selector}'")
    except Exception as exc:
        logger.debug(f"[human] Mausbewegung fehlgeschlagen für '{selector}': {exc}")


class Actions:
    """
    Alle Browser-Primitiven. Braucht eine Playwright-Page.

    Phase 7c: Adaptiver Execution-Mode (FAST / HUMAN).
    Der Modus wird per Konstruktor-Parameter oder automatisch via
    ModeResolver aufgelöst.

    Phase 9: Multi-Tab-Primitive (get_all_hrefs, open_new_tab, evaluate_js).

    Phase 10 Speed Overhaul:
    - click(), type_text(), get_text() call wait_for_page_ready() before acting.
      On already-loaded pages this costs ~50ms (one DOM stability poll).
    - _try_selector() no longer sleeps between retries — Playwright already
      waited up to DEFAULT_TIMEOUT; an additional sleep is pure waste.

    Verwendung:
        # Auto-Mode (Standard aus config.EXECUTION_MODE):
        actions = Actions(page)
        actions.click(["#search-input"])

        # Expliziter Modus:
        actions = Actions(page, mode="human")
        actions.click(["#search-input"])

        # Pro-Action-Override:
        actions.click(["#search-input"], mode="fast")

        # JavaScript ausführen (Phase 9):
        actions.evaluate_js("document.querySelector('video')?.pause()")
    """

    def __init__(self, page: Page, mode: Optional[str] = None) -> None:
        """
        Args:
            page: Playwright-Page.
            mode: "fast" | "human" | "auto" | None.
                  None → liest config.EXECUTION_MODE.
        """
        self._page = page
        self._mode_override = mode  # None = auto-resolve per Aktion
        self._interrupts = InterruptHandler()

    def _get_mode(self, per_action_mode: Optional[str] = None) -> str:
        """
        Löst den effektiven Modus auf (Priorität: per-action → Konstruktor → auto).
        """
        # 1. Pro-Aktion-Override
        if per_action_mode in ("fast", "human"):
            return per_action_mode
        # 2. Konstruktor-Override
        if self._mode_override in ("fast", "human"):
            return self._mode_override
        # 3. Automatische Erkennung via URL + ggf. DOM-Analyse
        current_url = self._page.url or ""
        return resolve_mode(current_url, self._page)

    # ── Interne Hilfsmethoden ─────────────────────────────────────────────────

    def _handle_interrupts(self) -> None:
        """
        Delegates to InterruptHandler.handle() for the current page.

        Called before click/type, after navigate, and on retry.
        Never raises — InterruptHandler catches all exceptions internally.
        """
        self._interrupts.handle(self._page)

    def _ensure_tab_focus(self) -> None:
        """
        TAB FOCUS GUARANTEE — Phase 10.1.

        Brings the current page's tab to the front before any DOM interaction.
        Critical for multi-tab flows where the executor may have switched pages
        without Chrome visually activating the new tab.

        Non-fatal: bring_to_front() is best-effort. If it fails (e.g. tab was
        closed, CDP connection hiccup), execution continues normally — the action
        will simply run on whatever tab Chrome considers focused.

        Called at the top of: click(), type_text(), get_text(), scroll(),
        navigate(), wait_for(), evaluate_js(), get_all_hrefs().
        """
        try:
            self._page.bring_to_front()
            # Log the current tab URL — makes multi-tab debugging explicit.
            try:
                url = self._page.url or "unknown"
            except Exception:
                url = "unknown"
            logger.debug(f"[tab_focus] bring_to_front() ✓ | url={url[:80]}")
        except Exception as exc:
            logger.debug(f"[tab_focus] bring_to_front() failed (non-fatal): {exc}")

    def _try_selector(
        self,
        action_name: str,
        selectors: list[str],
        fn,          # callable(selector: str) → any
        retries: int = None,
    ):
        """
        Kern-Loop: probiert jeden Selector, retry bei transientem Fehler.

        Ablauf:
          for each selector in selectors:
            for attempt in range(MAX_RETRIES):
              try fn(selector)
              if success → return result
              if PlaywrightTimeoutError → retry immediately (no sleep —
                  Playwright already waited DEFAULT_TIMEOUT; extra sleep is waste)
              if hard error → break (nächster Selector)

        Returns:
            Das Ergebnis von fn(winning_selector).

        Raises:
            ActionError: wenn alle Selektoren + alle Retries fehlschlugen.
        """
        if retries is None:
            retries = config.MAX_RETRIES

        all_errors: list[str] = []

        for selector in selectors:
            # ── PRE-CHECK: is_visible() before any blocking call ──────────────
            # This is the critical optimization: before launching an expensive
            # page.fill() / page.click() / page.inner_text() with a 10s timeout,
            # ask Playwright "is this element currently visible?" — non-blocking,
            # returns in ~1ms. If it's not visible, skip to the next selector
            # immediately instead of burning 10 seconds.
            #
            # Example: selectors=['#search-input', 'input[name="search_query"]']
            #   wait_for() already confirmed input[name=...] is visible.
            #   Without pre-check: tries #search-input → 10s timeout → skips
            #   With pre-check:   sees #search-input not visible → skips in 1ms
            try:
                if not self._page.is_visible(selector):
                    logger.debug(
                        f"[{action_name}] pre-check: '{selector}' not visible — skip"
                    )
                    all_errors.append(
                        f"[{action_name}] '{selector}' not visible (pre-check skip)"
                    )
                    continue  # Next selector immediately, no blocking call
            except Exception:
                pass  # Can't check visibility → proceed normally, let fn() handle it

            for attempt in range(1, retries + 1):
                # On retry (attempt > 1), clear any interrupt that may have
                # appeared since the previous attempt failed.
                if attempt > 1:
                    self._handle_interrupts()
                try:
                    result = fn(selector)
                    logger.debug(
                        f"[{action_name}] ✓ Selector='{selector}' "
                        f"(Versuch {attempt}/{retries})"
                    )
                    return result

                except PlaywrightTimeoutError as exc:
                    # Timeout despite passing the is_visible() pre-check.
                    # This is a genuine transient failure (element detached,
                    # mid-animation, SPA re-render). Retry is worthwhile.
                    msg = (
                        f"[{action_name}] Timeout | selector='{selector}' "
                        f"attempt {attempt}/{retries}: {exc}"
                    )
                    logger.debug(msg)
                    all_errors.append(msg)
                    # Post-timeout fast-exit: re-check visibility. If the
                    # element has disappeared, no point retrying.
                    if attempt < retries:
                        try:
                            if not self._page.is_visible(selector):
                                logger.debug(
                                    f"[{action_name}] '{selector}' absent after timeout "
                                    f"— skipping remaining {retries - attempt} retry(s)"
                                )
                                break
                        except Exception:
                            pass

                except Exception as exc:
                    # Hard error (element not found, detached, etc.)
                    # Move to next selector immediately.
                    msg = (
                        f"[{action_name}] Fehler | selector='{selector}' "
                        f"attempt {attempt}/{retries}: {type(exc).__name__}: {exc}"
                    )
                    logger.debug(msg)
                    all_errors.append(msg)
                    break  # No retry on non-transient error

        # Alle Selektoren erschöpft
        error_summary = "\n  ".join(all_errors)
        raise ActionError(
            f"[{action_name}] Alle {len(selectors)} Selektoren fehlgeschlagen "
            f"nach je max {retries} Versuchen.\n  {error_summary}"
        )

    # ── Öffentliche Primitiven ────────────────────────────────────────────────

    def click(self, selectors: list[str], mode: Optional[str] = None) -> None:
        """
        Klickt auf das erste Element, das über einen der Selektoren gefunden wird.

        Calls wait_for_page_ready() before acting to ensure the DOM is stable.
        On an already-loaded page this adds ~50ms (one DOM stability poll).

        FAST-Modus: direkter Playwright-Click, keine Extras.
        HUMAN-Modus: scroll_into_view → Stabilitätsprüfung → Mausbewegung
                     → zufällige Verzögerung → click.

        Args:
            selectors: Liste von CSS-Selektoren, fallback in Reihenfolge.
            mode:      "fast" | "human" | None (auto-resolve).

        Raises:
            ActionError: wenn kein Selector ein klickbares Element findet.
        """
        # TAB FOCUS GUARANTEE: bring this tab to front before any click.
        self._ensure_tab_focus()
        # Clear any interrupt (ad, cookie banner, modal) that could block the click.
        self._handle_interrupts()
        # Ensure page is interactive before attempting to click anything.
        # Fast-path: if readyState=="complete" this costs only ~50ms.
        wait_for_page_ready(self._page)

        effective_mode = self._get_mode(mode)
        logger.info(f"[click] Modus={effective_mode} | Selektoren: {selectors}")

        if effective_mode == "human":
            def _click_human(selector: str) -> None:
                _scroll_element_into_view(self._page, selector)
                _wait_element_stable(self._page, selector)
                _move_mouse_to_element(self._page, selector)
                _human_delay()
                try:
                    self._page.click(selector, timeout=int(config.DEFAULT_TIMEOUT * 1000))
                except Exception as exc:
                    # Step 2: force click — bypasses actionability checks.
                    try:
                        logger.debug(
                            f"[click] Human click failed ('{selector}'), trying force click: {exc}"
                        )
                        self._page.click(
                            selector,
                            force=True,
                            timeout=int(config.DEFAULT_TIMEOUT * 1000),
                        )
                    except Exception as exc2:
                        # Step 3: JS .click() fallback.
                        logger.debug(
                            f"[click] Force click failed ('{selector}'), trying JS fallback: {exc2}"
                        )
                        self._page.evaluate(
                            f"document.querySelector({selector!r})?.click()"
                        )

            self._try_selector("click", selectors, _click_human)
        else:
            def _click_fast(selector: str) -> None:
                # FAST mode: scroll into view if needed, then direct click.
                # Scroll-into-view is fast (JS, instant) and prevents
                # "element outside viewport" failures that look like misclicks.
                _scroll_element_into_view(self._page, selector)

                # Bounding-box check: verify the element actually occupies space
                # on screen. A zero-size element is not truly interactable.
                try:
                    box = self._page.locator(selector).first.bounding_box()
                    if box and box["width"] == 0 and box["height"] == 0:
                        raise Exception(
                            f"Element '{selector}' has zero bounding box (not rendered)"
                        )
                except PlaywrightTimeoutError:
                    pass  # bounding_box() itself timed out — proceed anyway

                try:
                    self._page.click(selector, timeout=int(config.DEFAULT_TIMEOUT * 1000))
                except Exception as exc:
                    # Step 2: force click — bypasses Playwright actionability checks
                    # (pointer-events:none, element covered, not in viewport).
                    try:
                        logger.debug(
                            f"[click] Normal click failed ('{selector}'), trying force click: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        self._page.click(
                            selector,
                            force=True,
                            timeout=int(config.DEFAULT_TIMEOUT * 1000),
                        )
                    except Exception as exc2:
                        # Step 3: JS .click() — final fallback, works even on
                        # elements blocked by overlays or pointer-events:none.
                        logger.debug(
                            f"[click] Force click failed ('{selector}'), trying JS fallback: "
                            f"{type(exc2).__name__}: {exc2}"
                        )
                        result = self._page.evaluate(
                            f"() => {{ const el = document.querySelector({selector!r}); "
                            f"if (el) {{ el.click(); return true; }} return false; }}"
                        )
                        if not result:
                            raise exc  # original exception — let _try_selector handle

            self._try_selector("click", selectors, _click_fast)

        logger.info(f"[click] ✓ Erfolgreich (Modus={effective_mode}).")

    def type_text(
        self,
        selectors: list[str],
        text: str,
        mode: Optional[str] = None,
    ) -> None:
        """
        Fokussiert das erste gefundene Inputfeld und tippt den Text ein.

        Calls wait_for_page_ready() before acting to ensure the DOM is stable.
        On an already-loaded page this adds ~50ms (one DOM stability poll).

        FAST-Modus: fill() direkt.
        HUMAN-Modus: scroll_into_view → Stabilitätsprüfung → Mausbewegung
                     → click (Fokus) → zufällige Verzögerung → fill().

        Verwendet fill() statt type(): fill() ersetzt den Inhalt komplett
        und ist robuster gegen Felder mit vorhandenen Inhalten.

        Args:
            selectors: CSS-Selector-Liste.
            text:      Der einzugebende Text.
            mode:      "fast" | "human" | None (auto-resolve).
        """
        # TAB FOCUS GUARANTEE: bring this tab to front before typing.
        self._ensure_tab_focus()
        # Clear any interrupt before typing — a cookie banner or popup could
        # steal focus and swallow keystrokes.
        self._handle_interrupts()
        effective_mode = self._get_mode(mode)
        logger.info(
            f"[type_text] Modus={effective_mode} | Text='{text}' | Selektoren: {selectors}"
        )
        # NOTE: wait_for_page_ready() intentionally NOT called here.
        # Skills always call wait_for() before type_text(), which already
        # confirms the target element is visible. Calling wait_for_page_ready()
        # here adds a redundant spinner-check that wastes 2s on YouTube.

        if effective_mode == "human":
            def _fill_human(selector: str) -> None:
                _scroll_element_into_view(self._page, selector)
                _wait_element_stable(self._page, selector)
                _move_mouse_to_element(self._page, selector)
                # Klick zum Fokussieren (wie ein echter Nutzer)
                self._page.click(selector, timeout=int(config.DEFAULT_TIMEOUT * 1000))
                _human_delay()
                self._page.fill(selector, text, timeout=int(config.DEFAULT_TIMEOUT * 1000))

            self._try_selector("type_text", selectors, _fill_human)
        else:
            def _fill_fast(selector: str) -> None:
                self._page.fill(selector, text, timeout=int(config.DEFAULT_TIMEOUT * 1000))

            self._try_selector("type_text", selectors, _fill_fast)

        logger.info(f"[type_text] ✓ Text eingegeben (Modus={effective_mode}).")

    def wait_for(
        self,
        selectors: list[str],
        timeout: Optional[float] = None,
    ) -> str:
        """
        Waits until AT LEAST ONE of the selectors appears and is visible.
        Returns the first selector that matched.

        KEY OPTIMIZATION: All selectors are raced simultaneously using a
        CSS multi-selector ("sel1, sel2, sel3"). This means the full timeout
        is shared across all selectors — if sel1 doesn't exist but sel2 is
        already visible, it resolves instantly instead of burning the full
        timeout per selector.

        Example: ['#search-input', 'input[name="search_query"]']
          Before: 15s timeout on #search-input → then finds input[name=...]
          After:  Both raced together → input[name=...] wins immediately

        Args:
            selectors: CSS selector list. All raced simultaneously.
            timeout:   Max wait in seconds. Default: config.DEFAULT_TIMEOUT.

        Returns:
            The selector string that won the race.
        """
        # TAB FOCUS GUARANTEE + interrupt check before waiting.
        self._ensure_tab_focus()
        self._handle_interrupts()
        t = timeout if timeout is not None else config.DEFAULT_TIMEOUT
        logger.info(f"[wait_for] Warte auf Selektoren: {selectors} (timeout={t}s)")

        # Phase B: filter empty/whitespace entries — they produce invalid CSS
        # like "sel1, , sel2" which Playwright rejects, masking the real error.
        valid_selectors = [s for s in selectors if s and s.strip()]
        if not valid_selectors:
            raise ActionError(
                f"[wait_for] Selector-Liste ist leer oder enthält nur ungültige Einträge: {selectors}"
            )

        # Race all selectors simultaneously with a CSS comma-union selector.
        # Playwright's wait_for_selector resolves as soon as ANY of them matches.
        combined = ", ".join(valid_selectors)

        # Phase B: fallback logic — attempt full timeout first, then one retry
        # with a shorter fallback timeout to handle transient DOM fluctuations
        # (e.g. React re-renders) without adding a static sleep.
        last_exc: Exception | None = None
        for _pass, pass_timeout_ms in enumerate(
            (int(t * 1000), max(500, int(t * 300)))
        ):
            try:
                self._page.wait_for_selector(
                    combined,
                    state="visible",
                    timeout=pass_timeout_ms,
                )
                last_exc = None
                break  # found — exit retry loop
            except PlaywrightTimeoutError as exc:
                last_exc = exc
                if _pass == 0:
                    logger.debug(
                        f"[wait_for] Pass 1 timeout ({t}s) — retrying with "
                        f"fallback {pass_timeout_ms // 1000}s: {valid_selectors}"
                    )

        if last_exc is not None:
            raise ActionError(
                f"[wait_for] None of {len(valid_selectors)} selectors appeared "
                f"within {t}s (+ fallback): {valid_selectors}\n  {last_exc}"
            )

        # Identify the winning selector (first one currently visible)
        winning = valid_selectors[0]  # fallback if none individually match (shouldn't happen)
        for sel in valid_selectors:
            try:
                if self._page.is_visible(sel):
                    winning = sel
                    break
            except Exception:
                continue

        logger.info(f"[wait_for] ✓ Selector '{winning}' erschienen.")
        return winning

    def get_text(self, selectors: list[str], mode: Optional[str] = None) -> str:
        """
        Liest den sichtbaren Text des ersten gefundenen Elements.

        Calls wait_for_page_ready() before acting to ensure the DOM is stable.
        On an already-loaded page this adds ~50ms (one DOM stability poll).

        FAST-Modus: inner_text() direkt.
        HUMAN-Modus: scroll_into_view → inner_text().

        Args:
            selectors: CSS-Selector-Liste.
            mode:      "fast" | "human" | None (auto-resolve).

        Returns:
            Getrimmter Textinhalt des Elements.
        """
        # TAB FOCUS GUARANTEE + interrupt check before reading DOM.
        self._ensure_tab_focus()
        self._handle_interrupts()
        effective_mode = self._get_mode(mode)
        logger.info(f"[get_text] Modus={effective_mode} | Selektoren: {selectors}")
        # NOTE: wait_for_page_ready() intentionally NOT called here.
        # Skills always call wait_for() before get_text(), which already
        # confirms the target element is visible. Calling wait_for_page_ready()
        # here adds a redundant spinner-check that wastes 2s on YouTube.

        if effective_mode == "human":
            def _get_human(selector: str) -> str:
                _scroll_element_into_view(self._page, selector)
                text = self._page.inner_text(
                    selector,
                    timeout=int(config.DEFAULT_TIMEOUT * 1000),
                )
                return text.strip()

            result = self._try_selector("get_text", selectors, _get_human)
        else:
            def _get_fast(selector: str) -> str:
                text = self._page.inner_text(
                    selector,
                    timeout=int(config.DEFAULT_TIMEOUT * 1000),
                )
                return text.strip()

            result = self._try_selector("get_text", selectors, _get_fast)

        logger.info(
            f"[get_text] ✓ Text gelesen: "
            f"'{result[:80]}{'...' if len(result) > 80 else ''}' "
            f"(Modus={effective_mode})"
        )
        return result

    def scroll(
        self,
        direction: ScrollDirection = "down",
        amount: int = None,
    ) -> None:
        """
        Scrollt die aktive Page in die angegebene Richtung.

        Implementierung via JavaScript, weil Playwright kein direktes scroll()-
        Primitive auf Page-Ebene hat (nur auf Elementen via locator.scroll_into_view).
        Mode-unabhängig: Scrollen ist immer gleich.

        Args:
            direction: "up" | "down" | "left" | "right"
            amount:    Pixel-Betrag. Default: config.DEFAULT_SCROLL_AMOUNT.
        """
        if amount is None:
            amount = config.DEFAULT_SCROLL_AMOUNT

        x_delta = 0
        y_delta = 0
        if direction == "down":
            y_delta = amount
        elif direction == "up":
            y_delta = -amount
        elif direction == "right":
            x_delta = amount
        elif direction == "left":
            x_delta = -amount
        else:
            raise ValueError(
                f"Unbekannte Scroll-Richtung: '{direction}'. "
                "Erlaubt: up, down, left, right"
            )

        # TAB FOCUS GUARANTEE + interrupt check before scroll.
        self._ensure_tab_focus()
        self._handle_interrupts()
        logger.info(f"[scroll] Richtung='{direction}', Betrag={amount}px")
        self._page.evaluate(f"window.scrollBy({x_delta}, {y_delta})")
        logger.info(f"[scroll] ✓ Gescrollt.")

    def navigate(self, url: str) -> None:
        """
        Navigiert die aktive Page zu einer URL und wartet intelligent auf
        vollständiges Laden (wait_for_page_ready).

        Ablauf:
          1. goto() mit wait_until="domcontentloaded"
          2. wait_for_page_ready(): networkidle + spinner + DOM-Stabilität
             (readyState will NOT be "complete" yet → full wait runs)

        No extra sleeps. Returns as soon as page is genuinely ready.

        Args:
            url: Vollständige URL inkl. Schema (https://...).
        """
        # TAB FOCUS GUARANTEE: ensure this tab is active before navigating.
        self._ensure_tab_focus()
        logger.info(f"[navigate] → {url}")
        self._page.goto(url, wait_until="domcontentloaded")
        logger.info(f"[navigate] domcontentloaded erreicht: {self._page.url}")

        # Full readiness check (networkidle included — page is freshly loading)
        wait_for_page_ready(self._page)
        logger.info(f"[navigate] ✓ Seite bereit: {self._page.url}")
        # After navigation the new page may immediately show a cookie banner,
        # consent dialog, or ad. Clear them before any action proceeds.
        self._handle_interrupts()

    def press_key(self, key: str) -> None:
        """
        Drückt eine Taste (z.B. 'Enter', 'Escape', 'Tab').

        Nützlich nach type_text() um Formulare abzusenden.

        Args:
            key: Playwright-Tastennamen (https://playwright.dev/python/docs/api/class-keyboard)
        """
        logger.info(f"[press_key] Taste: '{key}'")
        self._page.keyboard.press(key)
        logger.info(f"[press_key] ✓")

    def click_and_wait(self, selectors: list[str], mode: Optional[str] = None) -> None:
        """
        Klickt auf ein Element und wartet anschließend auf vollständiges
        Laden der neuen Seite (bei navigierenden Klicks).

        Nützlich wenn ein Klick eine Seitennavigation auslöst und man
        sicherstellen möchte, dass die Zielseite vollständig geladen ist.

        Verwendet wait_for_page_ready() nach dem Klick.

        Args:
            selectors: CSS-Selector-Liste.
            mode:      "fast" | "human" | None (auto-resolve).
        """
        self.click(selectors, mode=mode)
        wait_for_page_ready(self._page)
        logger.info(f"[click_and_wait] ✓ Seite nach Klick bereit: {self._page.url}")

    # ── Phase 9: Multi-Tab-Primitive ──────────────────────────────────────────

    def get_all_hrefs(
        self,
        selectors: list[str],
        limit: Optional[int] = None,
    ) -> list[str]:
        """
        Extracts href attributes from all elements matching the first successful selector.

        Tries each selector in order. Uses the first selector that yields ≥ 1 result.
        Each matched element is checked for an href directly; if none, the nearest
        ancestor <a> is checked.

        Stable Contract (Phase 9):
          get_all_hrefs(selectors: list[str], limit: int = None) → list[str]

        Args:
            selectors: CSS-Selector-Liste (fallback in Reihenfolge).
            limit:     Optional maximum number of hrefs to return. None = all.

        Returns:
            List of non-empty href strings.

        Raises:
            ActionError: if no selector yields any results with non-empty hrefs.
        """
        # TAB FOCUS GUARANTEE + interrupt check before DOM traversal.
        self._ensure_tab_focus()
        self._handle_interrupts()
        logger.info(f"[get_all_hrefs] Selektoren: {selectors} (limit={limit})")

        for selector in selectors:
            try:
                hrefs: list[str] = self._page.eval_on_selector_all(
                    selector,
                    """elements => elements
                        .map(el => {
                            if (el.tagName === 'A') return el.getAttribute('href');
                            const a = el.closest('a') || el.querySelector('a');
                            return a ? a.getAttribute('href') : null;
                        })
                        .filter(h => h && h.trim() !== '')
                    """,
                )
                if hrefs:
                    logger.info(
                        f"[get_all_hrefs] ✓ Selector='{selector}' → "
                        f"{len(hrefs)} Links gefunden"
                    )
                    if limit is not None:
                        hrefs = hrefs[:limit]
                        logger.debug(
                            f"[get_all_hrefs] Auf {len(hrefs)} Links begrenzt (limit={limit})"
                        )
                    return hrefs
                else:
                    logger.debug(
                        f"[get_all_hrefs] Selector='{selector}' → 0 Links (kein Ergebnis)"
                    )
            except Exception as exc:
                logger.debug(
                    f"[get_all_hrefs] Selector='{selector}' fehlgeschlagen: "
                    f"{type(exc).__name__}: {exc}"
                )

        raise ActionError(
            f"[get_all_hrefs] Keine Links gefunden für Selektoren: {selectors}"
        )

    def open_new_tab(self, url: str) -> Page:
        """
        Opens a new browser tab in the BACKGROUND, navigates to the URL,
        and waits for page ready.

        ROOT-CAUSE NOTE — why window.open() instead of context.new_page():
          context.new_page() sends CDP Target.createTarget to Chrome.
          Chrome's native response is to ACTIVATE (bring to front) every
          tab created this way — which visually switches Chrome's focus
          to each new tab as it opens and disturbs the activation history
          of all other background tabs.

          window.open(url, '_blank') via JavaScript is how browsers open
          background tabs. Chrome opens the tab WITHOUT activating it —
          the calling tab stays focused, no existing tabs are disturbed.
          Playwright's context.expect_page() captures the new Page reference
          the moment Chrome creates the target, before it has loaded.

        Does NOT change self._page — the caller receives the new Page and
        can create a new Actions(new_page) for further interactions.

        Stable Contract (Phase 9):
          open_new_tab(url: str) → Page

        Args:
            url: Full URL including schema (https://...).

        Returns:
            The new Playwright Page object for the opened background tab.

        Raises:
            ActionError: if the tab cannot be opened or the URL fails to load.
        """
        logger.info(f"[open_new_tab] Öffne neuen Tab (background) → {url}")
        try:
            # expect_page() listens for the next page Chrome creates in this
            # context. window.open() triggers it immediately.
            with self._page.context.expect_page() as page_event:
                # window.open with '_blank' opens in a background tab.
                # Chrome does NOT activate it — current tab stays in focus.
                self._page.evaluate(f"window.open({url!r}, '_blank')")

            new_page = page_event.value

            # The page is already navigating (window.open started it).
            # Wait for domcontentloaded, then run full readiness checks.
            new_page.wait_for_load_state("domcontentloaded")
            wait_for_page_ready(new_page)

            # Run interrupt handler on the new tab — it may immediately show
            # a cookie consent, YouTube consent bump, or modal overlay.
            try:
                self._interrupts.handle(new_page)
            except Exception as ih_exc:
                logger.debug(f"[open_new_tab] interrupt handler on new tab: {ih_exc}")

            logger.info(
                f"[open_new_tab] ✓ Tab geöffnet (background): {new_page.url} | "
                f"Titel: '{new_page.title()[:60]}'"
            )
            return new_page
        except Exception as exc:
            raise ActionError(
                f"[open_new_tab] Konnte Tab für '{url}' nicht öffnen: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def safe_evaluate_js(
        self,
        script: str,
        default: any = None,
        page: Optional[Page] = None,
    ) -> any:
        """
        Non-raising JavaScript evaluation — returns `default` on any error.

        Drawn from the browser_automation reference system (_safe_js pattern in
        MakerWorldController). Use this for idempotency / state checks where a
        JS error should be treated as "unknown" rather than aborting execution.

        Examples:
            is_liked = actions.safe_evaluate_js(JS_IS_LIKED, default=False)
            cart_count = actions.safe_evaluate_js(JS_GET_CART_COUNT, default=0)

        Args:
            script:  JavaScript expression or arrow-function string.
            default: Value to return if evaluation fails (default: None).
            page:    Optional explicit Page; falls back to self._page.

        Returns:
            The JS return value, or `default` on any exception.
        """
        try:
            return self.evaluate_js(script, page=page)
        except ActionError as exc:
            logger.debug(
                "[safe_evaluate_js] Non-fatal JS error (returning default=%r): %s",
                default, exc,
            )
            return default
        except Exception as exc:
            logger.debug(
                "[safe_evaluate_js] Unexpected error (returning default=%r): %s: %s",
                default, type(exc).__name__, exc,
            )
            return default

    def scroll_container(
        self,
        selector: str,
        amount: int = 300,
        direction: str = "down",
    ) -> None:
        """
        Scroll a specific DOM container by CSS selector.

        Drawn from the browser_automation reference system (DOMController.
        scroll_element pattern). Useful for scrolling inner scrollable panels
        (e.g. YouTube comments list, Amazon review section, MakerWorld sidebar)
        independently from the main window scroll.

        Falls back to window.scrollBy() if the selector matches no element or
        the element is not scrollable.

        Args:
            selector:  CSS selector for the scrollable container element.
            amount:    Pixel distance to scroll (default: 300).
            direction: "down" (default) | "up" | "right" | "left".

        Raises:
            ActionError: if JS evaluation itself fails fatally.
        """
        # TAB FOCUS GUARANTEE + interrupt check before container scroll.
        self._ensure_tab_focus()
        self._handle_interrupts()

        delta_x, delta_y = 0, 0
        if direction == "down":
            delta_y = amount
        elif direction == "up":
            delta_y = -amount
        elif direction == "right":
            delta_x = amount
        elif direction == "left":
            delta_x = -amount
        else:
            raise ValueError(
                f"[scroll_container] Unknown direction: '{direction}'. "
                "Allowed: up, down, left, right"
            )

        escaped = selector.replace("'", "\\'")
        js = f"""
        (function() {{
            const el = document.querySelector('{escaped}');
            if (el && el.scrollHeight > el.clientHeight) {{
                el.scrollBy({delta_x}, {delta_y});
                return true;
            }}
            // Fallback: page-level scroll
            window.scrollBy({delta_x}, {delta_y});
            return false;
        }})()
        """.strip()
        result = self.safe_evaluate_js(js, default=False)
        logger.info(
            "[scroll_container] selector='%s' dir='%s' amount=%dpx %s",
            selector, direction, amount,
            "(container)" if result else "(window fallback)",
        )

    def evaluate_js(self, script: str, page: Optional[Page] = None) -> any:
        """
        Evaluates a JavaScript expression on the given page (or self._page).

        Useful for operations that have no dedicated CSS-selector primitive, e.g.:
          - Pausing a <video> element:
              actions.evaluate_js("document.querySelector('video')?.pause()")
          - Checking player state:
              paused = actions.evaluate_js("document.querySelector('video')?.paused")
          - Scrolling to a position:
              actions.evaluate_js("window.scrollTo(0, 500)")

        Phase 9 use-case — pause video in multi-tab mode:
          YouTubeSkill.open_top_results() calls this on each new tab's Actions
          instance so that videos opened in background tabs don't all auto-play
          simultaneously. Single-tab flows (click_first_video) do NOT call this,
          so the video continues playing normally.

        Stable Contract (Phase 9):
          evaluate_js(script: str, page=None) → any

        Args:
            script: JavaScript expression to evaluate (NOT a statement block).
                    Must be a valid JS expression or an arrow-function string.
                    Example: "document.querySelector('video')?.pause()"
            page:   Optional explicit Playwright Page to run the script on.
                    If None → runs on self._page (this Actions instance's own page).
                    Typically None; only supply a different page when you need to
                    run JS on a tab that is not self._page.

        Returns:
            The JavaScript return value, or None for void expressions like pause().

        Raises:
            ActionError: if the JS evaluation throws an exception.
        """
        target = page if page is not None else self._page
        # TAB FOCUS GUARANTEE: bring target tab to front before JS execution.
        # If an explicit page was given, bring that one to front instead.
        try:
            target.bring_to_front()
        except Exception:
            pass  # non-fatal
        # Run interrupt handler on the target page before JS execution.
        # This ensures ads/popups are cleared even in JS-only flows.
        try:
            self._interrupts.handle(target)
        except Exception:
            pass  # non-fatal
        logger.debug(f"[evaluate_js] Script: {script[:120]!r}")
        try:
            result = target.evaluate(script)
            logger.debug(f"[evaluate_js] ✓ Ergebnis: {result!r}")
            return result
        except Exception as exc:
            raise ActionError(
                f"[evaluate_js] JS-Ausführung fehlgeschlagen: "
                f"{type(exc).__name__}: {exc} | Script: {script[:80]!r}"
            ) from exc
