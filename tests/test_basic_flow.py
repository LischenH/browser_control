"""
tests/test_basic_flow.py -- Minimaler Integrationstest (Phase E)

Testet drei Plattform-Flows in Reihenfolge:
  1. YouTube  : Suche → erstes Video öffnen → liken → pausieren
  2. Amazon   : Suche → erstes Produkt öffnen → Titel lesen
  3. MakerWorld: Suche → erstes Modell öffnen → Modelldaten abrufen

Voraussetzungen:
  - Playwright-Browser ist gestartet (chromium/firefox/webkit)
  - Die Skill-Klassen sind importierbar (PYTHONPATH = browser_control/)
  - config.py ist vorhanden und korrekt gesetzt

Ausführen:
  cd browser_control
  python -m pytest tests/test_basic_flow.py -v
  # oder direkt:
  python tests/test_basic_flow.py
"""

from __future__ import annotations

import sys
import os
import logging

# Sicherstellen, dass browser_control/ im Suchpfad ist
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")


# ---------------------------------------------------------------------------
# Hilfsfunktion: Executor + Steps bauen und ausführen
# ---------------------------------------------------------------------------

def _run_flow(executor, steps_raw: list[dict], label: str) -> dict:
    """
    Wandelt rohe Dicts in Step-Objekte um, führt sie aus und gibt das
    Ergebnis zurück.  Gibt außerdem eine Zusammenfassung auf stdout aus.

    Step-Dict-Felder:
      action  : str        -- Action-Name
      kwargs  : dict       -- Keyword-Argumente für die Action
      desc    : str        -- Beschreibung (optional)
      url     : str        -- URL-Hint für Skill-Routing (optional).
                             Wenn gesetzt, wird der Step auf dem Skill geroutet,
                             der diese URL verarbeiten kann (statt aktuellem Tab).
    """
    from agent.planner import Step

    steps = [
        Step(
            action_name=s["action"],
            params=s.get("kwargs", {}),
            description=s.get("desc", ""),
            verify_conditions={},
            url=s.get("url", ""),  # URL-Hint → korrektes Skill-Routing pro Flow
        )
        for s in steps_raw
    ]

    result = executor.run(steps)
    status = "OK" if result["success"] else "FAIL"
    print(
        f"[{label}] {status} — "
        f"{result['steps_completed']}/{len(steps)} Schritte erfolgreich"
        + (f" | Fehler: {result['error']['message']}" if result.get("error") else "")
    )
    return result


# ---------------------------------------------------------------------------
# Haupt-Testfunktion
# ---------------------------------------------------------------------------

def test_basic_flow():
    """
    Vollständiger Drei-Plattform-Flow:
      YouTube → Amazon → MakerWorld

    Jeder Flow gibt einen URL-Hint ("url") in den Step-Dicts an, damit der
    Executor den richtigen Skill auswählt – auch wenn der Browser gerade
    auf einer anderen Plattform ist.
    """
    # -- Imports ---------------------------------------------------------------
    from core.browser import BrowserConnection
    from agent.executor import Executor
    from agent.verifier import Verifier
    from skill_manager.manager import SkillManager
    from skills.youtube_skill import YouTubeSkill
    from skills.amazon_skill import AmazonSkill
    from skills.makerworld_skill import MakerWorldSkill

    # -- Browser + Skills vorbereiten -----------------------------------------
    conn = BrowserConnection()
    conn.connect()  # verbindet mit dem bereits laufenden Chrome via CDP
    #
    # HINWEIS: BrowserConnection verbindet sich mit einem LAUFENDEN Chrome-Prozess
    # (gestartet mit --remote-debugging-port=9222). Sie startet keinen neuen Browser.

    skill_mgr = SkillManager(
        skills=[YouTubeSkill(), AmazonSkill(), MakerWorldSkill()]
    )

    # Ein Executor für alle Flows; connection= sorgt für aktive Tab-Synchronisation
    executor = Executor(
        page=conn.active_page,
        skill_manager=skill_mgr,
        verifier=Verifier(conn.active_page),
        max_retries=2,
        connection=conn,
        goal="test_basic_flow: YouTube + Amazon + MakerWorld",
    )

    # ── Flow 1: YouTube ────────────────────────────────────────────────────────
    # url-Hint stellt sicher, dass YouTubeSkill geroutet wird, egal welche
    # Seite gerade aktiv ist.
    yt_steps = [
        {"action": "search",             "kwargs": {"query": "lofi"},  "desc": "Suche: lofi",        "url": "https://www.youtube.com"},
        {"action": "open_search_result", "kwargs": {"index": 0},       "desc": "Erstes Video öffnen","url": "https://www.youtube.com"},
        {"action": "like_video",         "kwargs": {},                  "desc": "Video liken"},
        {"action": "pause",              "kwargs": {},                  "desc": "Video pausieren"},
    ]
    yt_result = _run_flow(executor, yt_steps, "YouTube")

    # ── Flow 2: Amazon ─────────────────────────────────────────────────────────
    amz_steps = [
        {"action": "search",             "kwargs": {"query": "usb c kabel"}, "desc": "Suche: USB-C",          "url": "https://www.amazon.de"},
        {"action": "open_search_result", "kwargs": {"index": 0},            "desc": "Erstes Produkt öffnen",  "url": "https://www.amazon.de"},
        {"action": "read_product_title", "kwargs": {},                       "desc": "Titel lesen"},
    ]
    amz_result = _run_flow(executor, amz_steps, "Amazon")

    # ── Flow 3: MakerWorld ─────────────────────────────────────────────────────
    # mw_* Aktionen sind eindeutig → kein URL-Hint nötig
    mw_steps = [
        {"action": "mw_search",   "kwargs": {"query": "benchy"}, "desc": "Suche: benchy"},
        {"action": "mw_open_top", "kwargs": {},                  "desc": "Erstes Modell öffnen"},
        {"action": "mw_get_info", "kwargs": {},                  "desc": "Modelldaten lesen"},
    ]
    mw_result = _run_flow(executor, mw_steps, "MakerWorld")

    # -- Browser schließen -----------------------------------------------------
    conn.close()

    # -- Assertions ------------------------------------------------------------
    # Jeder Flow muss mindestens einen Schritt erfolgreich abgeschlossen haben
    assert yt_result["steps_completed"] > 0,  "YouTube-Flow: kein einziger Schritt erfolgreich"
    assert amz_result["steps_completed"] > 0, "Amazon-Flow: kein einziger Schritt erfolgreich"
    assert mw_result["steps_completed"] > 0,  "MakerWorld-Flow: kein einziger Schritt erfolgreich"

    print("\n[test_basic_flow] ALLE FLOWS ABGESCHLOSSEN.")


# ---------------------------------------------------------------------------
# Direktaufruf (ohne pytest)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_basic_flow()
