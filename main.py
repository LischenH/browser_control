"""
main.py — Demo-Skript für alle Phasen des Browser Control Systems

Phase 1: Direkte Core-Aktionen (connect → tabs → search → read → close)
Phase 2: Skill-System (YouTubeSkill abstrahiert alle Aktionen)
Phase 3: Verifier (Multi-Condition-Prüfung nach jeder Skill-Aktion)
Phase 4: Executor + Planner (vollständige Step-Orchestrierung)
Phase 5: TemplateEngine (in Phase 4 integriert — Alias: phase5 = phase4)
Phase 6: Wiring & First Run (in Phase 4 integriert — Alias: phase6 = phase4)
Phase 7: Amazon-Skill — zweiter Skill, beweist Erweiterbarkeit
Phase 8: LLM-Planner — lokales Ollama-Modell (phi4:14b / llama3.3:8b)
Phase 9: Multi-Tab-Execution — open_top_results öffnet N Videos/Produkte in neuen Tabs

Voraussetzung:
  Chrome muss mit folgendem Flag gestartet sein:
    Windows: chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\tmp\\chrome_debug
    macOS:   /Applications/Google Chrome.app/.../Google Chrome --remote-debugging-port=9222
    Linux:   google-chrome --remote-debugging-port=9222

  Playwright installieren (einmalig):
    pip install playwright
    playwright install chromium

Ausführen:
  python main.py                          → Phase 4 Demo (Standard): search lo-fi
  python main.py phase4                   → Phase 4 Demo: search lo-fi
  python main.py phase4b                  → Phase 4 Demo: search + click first video
  python main.py phase4 "React Hooks"     → Phase 4 mit eigenem Suchbegriff
  python main.py phase5                   → Alias für phase4 (TemplateEngine E2E)
  python main.py phase6                   → Alias für phase4 (Wiring E2E)
  python main.py phase7                   → Phase 7 Demo: Amazon-Suche
  python main.py phase7b                  → Phase 7 Demo: Amazon-Suche + ersten Treffer klicken
  python main.py phase7 "gaming mouse"    → Phase 7 mit eigenem Suchbegriff
  python main.py phase8                   → Phase 8 Demo: LLM-Planner (YouTube, Ollama)
  python main.py phase8b                  → Phase 8 Demo: LLM-Planner (Amazon + Click)
  python main.py phase8c                  → Phase 8 Demo: LLM-Planner (nur YouTube öffnen)
  python main.py phase9                         → Phase 9 Demo: YouTube Top 3 in neuen Tabs (Videos + Shorts)
  python main.py phase9b                        → Phase 9 Demo: Amazon Top 3 in neuen Tabs
  python main.py phase9 "vibe coding" 5         → Top 5 (Videos + Shorts gemischt)
  python main.py phase9 "vibe coding" 5 videos  → nur normale Videos
  python main.py phase9 "vibe coding" 5 shorts  → nur YouTube Shorts
  python main.py phase9 "vibe coding" shorts     → 3 Shorts (N optional, Default=3)
  python main.py phase3                   → Phase 3 Demo
  python main.py phase2                   → Phase 2 Demo
  python main.py phase1                   → Phase 1 Demo
"""

import logging
import sys

# ── Logging VOR allen anderen Imports konfigurieren ───────────────────────────
# Wichtig: basicConfig muss vor dem ersten Modul-Import stehen, damit alle
# Sublogger (core, agent, skills, …) denselben Handler erhalten.
import config

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.DEBUG),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("main")

# ── Projekt-Imports ───────────────────────────────────────────────────────────
from core.browser import BrowserConnection
from core.actions import Actions, ActionError
from core.tab_manager import TabManager
from skills.youtube_skill import YouTubeSkill
from agent.verifier import Verifier, VerifyResult
from agent.executor import Executor
from agent.planner import Planner
from skill_manager.manager import SkillManager


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 1 — Direkte Core-Aktionen
# ═══════════════════════════════════════════════════════════════════════════════

def demo_youtube_search(query: str = "lo-fi music") -> None:
    """
    Vollständiger Demo-Workflow: YouTube-Suche ohne Skill-Layer.
    """
    logger.info("=" * 60)
    logger.info("Browser Control — Phase 1 Demo")
    logger.info(f"Ziel: YouTube nach '{query}' suchen")
    logger.info("=" * 60)

    with BrowserConnection() as conn:

        tab_manager = TabManager(conn)
        actions = Actions(conn.active_page)

        logger.info("\n--- Offene Tabs ---")
        tabs = tab_manager.list_tabs()
        for tab in tabs:
            logger.info(f"  [{tab.index}] {tab.title[:50]} | {tab.url[:70]}")

        logger.info("\n--- Öffne neuen Tab → YouTube ---")
        new_tab = tab_manager.open_tab("https://www.youtube.com")
        actions = Actions(conn.active_page)

        logger.info("\n--- Warte auf YouTube-Suchfeld ---")
        actions.wait_for(
            selectors=["#search-input", "input[name='search_query']", "input[type='text']"],
            timeout=15.0,
        )

        logger.info(f"\n--- Tippe Suchbegriff: '{query}' ---")
        actions.type_text(
            selectors=["#search-input", "input[name='search_query']"],
            text=query,
        )

        logger.info("\n--- Enter drücken ---")
        actions.press_key("Enter")

        logger.info("\n--- Warte auf Suchergebnisse ---")
        actions.wait_for(
            selectors=["ytd-video-renderer", "#contents ytd-item-section-renderer", "#video-title"],
            timeout=15.0,
        )

        logger.info("\n--- Lese ersten Video-Titel ---")
        try:
            title = actions.get_text(
                selectors=["ytd-video-renderer #video-title", "#video-title", "a#video-title"]
            )
            logger.info(f"\n✅ Erstes Ergebnis: '{title}'")
        except ActionError as e:
            logger.warning(f"Titel konnte nicht gelesen werden: {e}")

        logger.info("\n--- Scrolle nach unten ---")
        actions.scroll(direction="down", amount=600)

        logger.info("\n--- Finale Tab-Liste ---")
        for tab in tab_manager.list_tabs():
            logger.info(f"  [{tab.index}] {tab.title[:50]} | {tab.url[:70]}")

        logger.info("\n--- Schließe Demo-Tab ---")
        tab_manager.close_tab(new_tab)

    logger.info("\n" + "=" * 60)
    logger.info("Phase-1-Demo abgeschlossen. Verbindung getrennt.")
    logger.info("=" * 60)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2 — Skill-System
# ═══════════════════════════════════════════════════════════════════════════════

def demo_phase2_skill(query: str = "Python Tutorial") -> None:
    """
    Phase-2-Demo: YouTube-Skill-System testen.
    """
    logger.info("=" * 60)
    logger.info("Browser Control — Phase 2 Demo: Skill System")
    logger.info(f"Ziel: YouTube-Skill → '{query}'")
    logger.info("=" * 60)

    skill = YouTubeSkill()
    logger.info(f"\nSkill '{skill.name}' geladen.")
    logger.info(f"can_handle('https://www.youtube.com') → {skill.can_handle('https://www.youtube.com')}")
    logger.info(f"can_handle('https://www.amazon.de')  → {skill.can_handle('https://www.amazon.de')}")

    with BrowserConnection() as conn:

        tab_manager = TabManager(conn)

        logger.info("\n--- Öffne neuen Tab → YouTube ---")
        new_tab = tab_manager.open_tab("https://www.youtube.com")
        actions = Actions(conn.active_page)

        logger.info(f"\n--- Skill-Action: search('{query}') ---")
        search_fn = skill.get_action("search")
        result = search_fn(actions, query=query)
        logger.info(f"search() → {result}")

        if not result.success:
            logger.error(f"❌ Suche fehlgeschlagen: {result.error}")
            tab_manager.close_tab(new_tab)
            return

        logger.info("\n--- Skill-Action: click_first_video() ---")
        click_fn = skill.get_action("click_first_video")
        result = click_fn(actions)
        logger.info(f"click_first_video() → {result}")

        if not result.success:
            logger.warning(f"⚠️  click_first_video fehlgeschlagen: {result.error}")

        logger.info("\n--- Skill-Action: read_title() ---")
        title_fn = skill.get_action("read_title")
        result = title_fn(actions)
        logger.info(f"read_title() → {result}")

        if result.success:
            logger.info(f"\n✅ Videotitel: '{result.data}'")
        else:
            logger.warning(f"⚠️  Titel konnte nicht gelesen werden: {result.error}")

        logger.info("\n--- Schließe Demo-Tab ---")
        tab_manager.close_tab(new_tab)

    logger.info("\n" + "=" * 60)
    logger.info("Phase-2-Demo abgeschlossen.")
    logger.info("=" * 60)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3 — Verifier
# ═══════════════════════════════════════════════════════════════════════════════

def demo_phase3_verifier(query: str = "Python Tutorial") -> None:
    """
    Phase-3-Demo: Verifier nach jeder YouTube-Skill-Aktion einsetzen.
    """
    logger.info("=" * 60)
    logger.info("Browser Control — Phase 3 Demo: Verifier")
    logger.info(f"Ziel: YouTube → '{query}' → Verifier nach jedem Schritt")
    logger.info("=" * 60)

    skill = YouTubeSkill()

    with BrowserConnection() as conn:
        tab_manager = TabManager(conn)

        logger.info("\n--- Öffne neuen Tab → YouTube ---")
        new_tab = tab_manager.open_tab("https://www.youtube.com")
        actions = Actions(conn.active_page)
        verifier = Verifier(conn.active_page)

        # ── Schritt 1: Suche ─────────────────────────────────────────────────
        logger.info(f"\n--- Skill-Action: search('{query}') ---")
        search_fn = skill.get_action("search")
        result = search_fn(actions, query=query)
        logger.info(f"search() → {result}")

        if not result.success:
            logger.error(f"❌ Suche fehlgeschlagen: {result.error}")
            tab_manager.close_tab(new_tab)
            return

        logger.info("\n--- Verifier: Nach search() ---")
        vr = verifier.verify({
            "url_contains":   "results",
            "element_exists": [
                "ytd-video-renderer",
                "#contents ytd-item-section-renderer",
            ],
        })
        _print_verify_result("Nach search()", vr)

        if vr.failed:
            logger.error("❌ Verifikation fehlgeschlagen — Abbruch.")
            tab_manager.close_tab(new_tab)
            return

        if vr.should_retry:
            logger.warning("⚠️  Transienter Fehler — im Executor würde retry folgen.")

        # ── Schritt 2: Erstes Video anklicken ────────────────────────────────
        logger.info("\n--- Skill-Action: click_first_video() ---")
        click_fn = skill.get_action("click_first_video")
        result = click_fn(actions)
        logger.info(f"click_first_video() → {result}")

        logger.info("\n--- Verifier: Nach click_first_video() ---")
        vr = verifier.verify({
            "url_contains":   "watch",
            "element_exists": [".ytp-play-button", "button[aria-label='Play']"],
        })
        _print_verify_result("Nach click_first_video()", vr)

        # ── Schritt 3: Titel lesen ────────────────────────────────────────────
        logger.info("\n--- Skill-Action: read_title() ---")
        title_fn = skill.get_action("read_title")
        result = title_fn(actions)
        logger.info(f"read_title() → {result}")

        if result.success and result.data:
            title_snippet = result.data[:30]
            logger.info(f"\n--- Verifier: Titel '{title_snippet}' im Seitentext? ---")
            vr = verifier.verify({
                "text_contains": title_snippet,
            })
            _print_verify_result("Titeltext sichtbar", vr)

        logger.info("\n--- Schließe Demo-Tab ---")
        tab_manager.close_tab(new_tab)

    logger.info("\n" + "=" * 60)
    logger.info("Phase-3-Demo abgeschlossen.")
    logger.info("=" * 60)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 4 — Executor + Planner
# ═══════════════════════════════════════════════════════════════════════════════

def demo_phase4_executor(goal: str) -> None:
    """
    Phase-4-Demo: Vollständige Step-Orchestrierung via Planner → Executor.

    Workflow:
        1. Planner wandelt goal in Step-Liste um
        2. Executor führt Steps aus (Skill → Action → Verifier → Retry/Fail)
        3. Strukturiertes Ergebnis wird ausgegeben

    Args:
        goal: Freitext-Ziel, z.B. "search YouTube for lo-fi music"
    """
    _print_phase4_header(goal)

    planner = Planner()
    steps = planner.plan(goal)

    if not steps:
        logger.error(
            f"❌ Planner konnte keinen Plan für '{goal}' erstellen.\n"
            "   Unterstützte Ziele:\n"
            "   - 'search YouTube for <query>'\n"
            "   - 'search YouTube for <query> and click first video'\n"
            "   - 'search Amazon for <query>'\n"
            "   - 'search Amazon for <query> and click first result'"
        )
        return

    logger.info(f"\n📋 Plan ({len(steps)} Steps):")
    for i, step in enumerate(steps):
        desc = f" — {step.description}" if step.description else ""
        logger.info(f"   {i + 1}. [{step.action_name}]{desc}")

    with BrowserConnection() as conn:
        tab_manager = TabManager(conn)

        logger.info("\n--- Öffne neuen Tab ---")
        new_tab = tab_manager.open_tab("about:blank")

        skill_manager = SkillManager()
        verifier = Verifier(conn.active_page)
        executor = Executor(
            page=conn.active_page,
            skill_manager=skill_manager,
            verifier=verifier,
        )

        logger.info("\n--- Starte Executor ---")
        result = executor.run(steps)

        logger.info("\n--- Schließe Tab ---")
        try:
            tab_manager.close_tab(new_tab)
        except Exception:
            pass  # Tab könnte schon weg sein

    _print_phase4_result(result)


def _print_phase4_header(goal: str) -> None:
    logger.info("\n" + "=" * 60)
    logger.info("Browser Control — Phase 4 Demo: Executor + Planner")
    logger.info(f"Ziel: \"{goal}\"")
    logger.info("=" * 60)


def _print_phase4_result(result: dict) -> None:
    """Gibt das Executor-Ergebnis übersichtlich aus."""
    logger.info("\n" + "═" * 60)

    if result["success"]:
        logger.info(f"✅ PLAN ERFOLGREICH")
        logger.info(f"   Steps abgeschlossen : {result['steps_completed']}")
        logger.info(f"   Gesammelte Daten    :")
        for i, data in enumerate(result["data"]):
            if data is not None:
                logger.info(f"     Step {i + 1}: {str(data)[:80]!r}")
    else:
        logger.info(f"❌ PLAN FEHLGESCHLAGEN")
        logger.info(f"   Steps abgeschlossen : {result['steps_completed']}")
        error = result["error"]
        if error:
            logger.info(f"   Fehlgeschlagener Step : {error['step'].action_name!r}")
            logger.info(f"   Beschreibung          : {error['step'].description!r}")
            logger.info(f"   Fehlergrund           : {error['message']}")
            vr = error.get("verify_result")
            if vr:
                logger.info(f"   Verify-Status         : {vr.status.upper()} — {vr.reason}")

    # Phase 9: Opened tabs summary
    opened_tabs = result.get("opened_tabs", [])
    if opened_tabs:
        logger.info(f"\n   🗂️  Geöffnete Tabs ({len(opened_tabs)}):")
        for tab in opened_tabs:
            verified_icon = "✅" if tab.get("verified") else "⚠️ "
            logger.info(
                f"     {verified_icon} Tab {tab.get('tab_index', '?')}: "
                f"'{tab.get('title', '')[:60]}'\n"
                f"          URL: {tab.get('url', '')[:80]}"
            )

    logger.info("═" * 60 + "\n")


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 8 — LLM-Planner E2E Demo
# ═══════════════════════════════════════════════════════════════════════════════

def demo_phase8_llm(goal: str) -> None:
    """
    Phase-8-Demo: LLM-Planner via lokalem Ollama-Modell.

    Setzt config.PLANNER_ENGINE = "llm" und führt den vollständigen
    Planner → Executor Stack aus. Ollama muss lokal laufen.

    Modelle (automatische Auswahl):
      Primär  : phi4:14b
      Fallback: llama3.3:8b

    Args:
        goal: Freitext-Ziel, z.B. "search YouTube for Python tutorial"
    """
    logger.info("\n" + "=" * 60)
    logger.info("Browser Control — Phase 8 Demo: LLM-Planner (Ollama)")
    logger.info(f'Ziel: "{goal}"')
    logger.info("Modell: phi4:14b  |  Fallback: llama3.3:8b")
    logger.info("=" * 60)

    # LLM-Engine aktivieren
    planner = Planner(engine="llm")
    steps = planner.plan(goal)

    if not steps:
        logger.error(
            f"❌ Planner konnte keinen Plan für '{goal}' erstellen.\n"
            "   Ollama läuft? → ollama serve"
        )
        return

    logger.info(f"\n📋 Plan ({len(steps)} Steps):")
    for i, step in enumerate(steps):
        desc = f" — {step.description}" if step.description else ""
        logger.info(f"   {i + 1}. [{step.action_name}]{desc}")

    with BrowserConnection() as conn:
        tab_manager = TabManager(conn)

        logger.info("\n--- Öffne neuen Tab ---")
        new_tab = tab_manager.open_tab("about:blank")

        skill_manager = SkillManager()
        verifier = Verifier(conn.active_page)
        executor = Executor(
            page=conn.active_page,
            skill_manager=skill_manager,
            verifier=verifier,
        )

        logger.info("\n--- Starte Executor ---")
        result = executor.run(steps)

        logger.info("\n--- Schließe Tab ---")
        try:
            tab_manager.close_tab(new_tab)
        except Exception:
            pass

    _print_phase4_result(result)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 7 — Amazon-Skill E2E Demo
# ═══════════════════════════════════════════════════════════════════════════════

def demo_phase7_amazon(goal: str) -> None:
    """
    Phase-7-Demo: Amazon-Skill über den vollständigen Planner → Executor Stack.

    Beweist Erweiterbarkeit: Kein einziger Byte in core/, agent/ oder
    skill_manager/ musste verändert werden — nur neue Dateien wurden hinzugefügt.

    Workflow identisch zu Phase 4:
        1. Planner erkennt "search Amazon for …" → Amazon-Step-Plan
        2. Executor führt Steps aus → AmazonSkill.search() + verify
        3. Strukturiertes Ergebnis ausgeben

    Args:
        goal: Freitext-Ziel, z.B. "search Amazon for wireless headphones"
    """
    logger.info("\n" + "=" * 60)
    logger.info("Browser Control — Phase 7 Demo: Amazon-Skill")
    logger.info(f"Ziel: \"{goal}\"")
    logger.info("Beweis: Kein Core / Agent / SkillManager wurde verändert.")
    logger.info("=" * 60)

    planner = Planner()
    steps = planner.plan(goal)

    if not steps:
        logger.error(
            f"❌ Planner konnte keinen Plan für '{goal}' erstellen.\n"
            "   Unterstützte Amazon-Ziele:\n"
            "   - 'search Amazon for <query>'\n"
            "   - 'search Amazon for <query> and click first result'"
        )
        return

    logger.info(f"\n📋 Plan ({len(steps)} Steps):")
    for i, step in enumerate(steps):
        desc = f" — {step.description}" if step.description else ""
        logger.info(f"   {i + 1}. [{step.action_name}]{desc}")

    with BrowserConnection() as conn:
        tab_manager = TabManager(conn)

        logger.info("\n--- Öffne neuen Tab ---")
        new_tab = tab_manager.open_tab("about:blank")

        skill_manager = SkillManager()

        # Zeige welche Skills registriert sind
        logger.info(f"\n--- Registrierte Skills: {skill_manager.skill_names} ---")

        verifier = Verifier(conn.active_page)
        executor = Executor(
            page=conn.active_page,
            skill_manager=skill_manager,
            verifier=verifier,
        )

        logger.info("\n--- Starte Executor ---")
        result = executor.run(steps)

        logger.info("\n--- Schließe Tab ---")
        try:
            tab_manager.close_tab(new_tab)
        except Exception:
            pass

    _print_phase4_result(result)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 9 — Multi-Tab Execution
# ═══════════════════════════════════════════════════════════════════════════════

def demo_phase9_multitab(goal: str) -> None:
    """
    Phase-9-Demo: Multi-Tab-Execution via open_top_results.

    Beweist Phase 9:
      - Planner erkennt "… and open top N …" → Step mit open_top_results(n)
      - Executor führt Plan aus
      - open_top_results öffnet N neue Tabs
      - Pro Tab: Seite verifiziert + Titel gelesen
      - result["opened_tabs"] enthält alle Tab-Daten

    Test-Ziele (phase9):
      "search YouTube for Python tutorial and open top 3 videos"

    Test-Ziele (phase9b):
      "search Amazon for wireless headphones and open top 3 results"

    Args:
        goal: Freitext-Ziel mit "and open top N" oder "and open first N".
    """
    logger.info("\n" + "=" * 60)
    logger.info("Browser Control — Phase 9 Demo: Multi-Tab Execution")
    logger.info(f'Ziel: "{goal}"')
    logger.info("=" * 60)

    planner = Planner()
    steps = planner.plan(goal)

    if not steps:
        logger.error(
            f"❌ Planner konnte keinen Plan für '{goal}' erstellen.\n"
            "   Phase-9-Ziele:\n"
            "   - 'search YouTube for <query> and open top N videos'\n"
            "   - 'search YouTube for <query> and open first N'\n"
            "   - 'search Amazon for <query> and open top N results'\n"
            "   - 'search Amazon for <query> and open first N'"
        )
        return

    logger.info(f"\n📋 Plan ({len(steps)} Steps):")
    for i, step in enumerate(steps):
        desc = f" — {step.description}" if step.description else ""
        params_str = f" | params={step.params}" if step.params else ""
        logger.info(f"   {i + 1}. [{step.action_name}]{desc}{params_str}")

    with BrowserConnection() as conn:
        tab_manager = TabManager(conn)

        logger.info("\n--- Öffne neuen Haupt-Tab ---")
        main_tab = tab_manager.open_tab("about:blank")

        skill_manager = SkillManager()
        logger.info(f"--- Registrierte Skills: {skill_manager.skill_names} ---")

        verifier = Verifier(conn.active_page)
        executor = Executor(
            page=conn.active_page,
            skill_manager=skill_manager,
            verifier=verifier,
        )

        logger.info("\n--- Starte Multi-Tab-Executor ---")
        result = executor.run(steps)

        # Phase 9: Tab-Summary direkt nach run()
        opened = result.get("opened_tabs", [])
        if opened:
            logger.info(f"\n🗂️  {len(opened)} Tab(s) geöffnet und verifiziert:")
            for tab in opened:
                verified_icon = "✅" if tab.get("verified") else "⚠️ "
                logger.info(
                    f"  {verified_icon} [{tab.get('tab_index', '?')}] "
                    f"'{tab.get('title', '')[:70]}'"
                )
                logger.info(f"       URL: {tab.get('url', '')[:90]}")

        logger.info("\n--- Schließe Haupt-Tab (neue Tabs bleiben geöffnet) ---")
        try:
            tab_manager.close_tab(main_tab)
        except Exception:
            pass

    _print_phase4_result(result)


# ═══════════════════════════════════════════════════════════════════════════════
# Shared Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _print_verify_result(label: str, result: VerifyResult) -> None:
    """Gibt ein VerifyResult übersichtlich in der Konsole aus."""
    icons = {"pass": "✅", "retry": "⚠️ ", "fail": "❌"}
    icon = icons.get(result.status, "?")

    logger.info(f"\n{icon}  Verifier [{label}]")
    logger.info(f"   Status : {result.status.upper()}")
    logger.info(f"   Reason : {result.reason}")

    if result.details:
        logger.info("   Details:")
        for key, detail in result.details.items():
            status_char = "✓" if detail["passed"] else ("~" if detail.get("transient") else "✗")
            expected_str = str(detail.get("expected", ""))[:50]
            actual_str   = str(detail.get("actual", ""))[:60]
            logger.info(
                f"     [{status_char}] {key:<22} "
                f"erwartet={expected_str!r}  "
                f"aktuell={actual_str!r}"
            )
            if detail.get("note"):
                logger.info(f"          Hinweis: {detail['note']}")


# ═══════════════════════════════════════════════════════════════════════════════
# Einstiegspunkt
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    args = sys.argv[1:]
    mode = "phase4"
    query_parts = []

    # Aliase: phase5/6 → phase4, phase7b = Amazon + Click
    _PHASE_ALIASES = {
        "phase5": "phase4",
        "phase6": "phase4",
    }

    # Phase 8 Aliases
    _PHASE_ALIASES["phase8"]  = "phase8"
    _PHASE_ALIASES["phase8b"] = "phase8b"
    _PHASE_ALIASES["phase8c"] = "phase8c"

    # Phase 9 Aliases
    _PHASE_ALIASES["phase9"]  = "phase9"
    _PHASE_ALIASES["phase9b"] = "phase9b"

    for arg in args:
        canonical = _PHASE_ALIASES.get(arg, arg)
        if canonical in ("phase1", "phase2", "phase3", "phase4", "phase4b",
                         "phase7", "phase7b",
                         "phase8", "phase8b", "phase8c",
                         "phase9", "phase9b"):
            mode = canonical
        else:
            query_parts.append(arg)

    # Standard-Ziele je Modus
    _DEFAULTS = {
        "phase1":  "lo-fi music",
        "phase2":  "Python Tutorial",
        "phase3":  "Python Tutorial",
        "phase4":  "search YouTube for lo-fi music",
        "phase4b": "search YouTube for Python tutorial and click first video",
        "phase7":  "search Amazon for wireless headphones",
        "phase7b": "search Amazon for wireless headphones and click first result",
        "phase8":  "search YouTube for Python tutorial and click first video",
        "phase8b": "search Amazon for wireless headphones and open first result",
        "phase8c": "open YouTube",
        "phase9":  "search YouTube for Python tutorial and open top 3 videos",
        "phase9b": "search Amazon for wireless headphones and open top 3 results",
    }

    if query_parts:
        user_input = " ".join(query_parts)

        if mode in ("phase4", "phase4b"):
            if not user_input.lower().startswith("search"):
                goal = f"search YouTube for {user_input}"
            else:
                goal = user_input

        elif mode in ("phase7", "phase7b"):
            if not user_input.lower().startswith("search"):
                goal = f"search Amazon for {user_input}"
            else:
                goal = user_input

        elif mode in ("phase8", "phase8b", "phase8c"):
            # Für Phase 8: freier Zieltext direkt an LLM
            goal = user_input

        elif mode == "phase9":
            # phase9 <query> [N] [videos|shorts]
            # Parst optionale Typ-Angabe und optionale Zahl in beliebiger Reihenfolge
            # am Ende des Strings.
            parts = user_input.split()
            content_type_word = ""
            n = 3

            # Prüfe ob letztes Token ein Typ-Keyword ist
            if parts and parts[-1].lower() in ("videos", "video", "shorts", "short", "both", "any"):
                content_type_word = parts.pop().lower()

            # Prüfe ob letztes verbleibendes Token eine Zahl ist
            if parts and parts[-1].isdigit():
                n = int(parts.pop())

            # Falls Keyword noch nicht gefunden, prüfe nochmal ("5 shorts" Reihenfolge schon ok;
            # aber auch "shorts 5" möglich wenn Nutzer anders tippt)
            if not content_type_word and parts and parts[-1].lower() in ("videos", "video", "shorts", "short", "both", "any"):
                content_type_word = parts.pop().lower()

            query = " ".join(parts) if parts else "python tutorial"

            # Normalisiere content_type
            if "short" in content_type_word:
                ct_suffix = "shorts"
            elif "video" in content_type_word:
                ct_suffix = "videos"
            else:
                ct_suffix = ""  # leer = "any"

            goal = f"search YouTube for {query} and open top {n}" + (
                f" {ct_suffix}" if ct_suffix else ""
            )

        elif mode == "phase9b":
            # phase9b <query> [N]  →  "search Amazon for <query> and open top N results"
            parts = user_input.rsplit(None, 1)
            if len(parts) == 2 and parts[1].isdigit():
                query, n = parts[0], int(parts[1])
            else:
                query, n = user_input, 3
            goal = f"search Amazon for {query} and open top {n} results"

        else:
            goal = user_input
    else:
        goal = _DEFAULTS[mode]

    try:
        if mode in ("phase8", "phase8b", "phase8c"):
            demo_phase8_llm(goal=goal)
        elif mode == "phase1":
            demo_youtube_search(query=goal)
        elif mode == "phase2":
            demo_phase2_skill(query=goal)
        elif mode == "phase3":
            demo_phase3_verifier(query=goal)
        elif mode in ("phase4", "phase4b"):
            demo_phase4_executor(goal=goal)
        elif mode in ("phase7", "phase7b"):
            demo_phase7_amazon(goal=goal)
        elif mode in ("phase9", "phase9b"):
            demo_phase9_multitab(goal=goal)

    except ConnectionError as e:
        logger.error(f"\n❌ Verbindungsfehler: {e}")
        logger.error(
            "\nLösung: Starte Chrome NEU mit Debug-Port:\n\n"
            "  Option A — PowerShell (empfohlen):\n"
            "    Start-Process 'chrome' '--remote-debugging-port=9222 --user-data-dir=C:\\\\tmp\\\\chrome_debug'\n\n"
            "  Option B — CMD:\n"
            "    chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\\\tmp\\\\chrome_debug\n\n"
            "  Danach im Browser öffnen: http://localhost:9222\n"
            "  Wenn du eine JSON-Seite siehst → Chrome ist bereit.\n"
        )
        sys.exit(1)

    except ActionError as e:
        logger.error(f"\n❌ Aktionsfehler: {e}")
        sys.exit(1)

    except KeyboardInterrupt:
        logger.info("\nAbgebrochen.")
        sys.exit(0)
