"""
skills/generic_skill.py — Generischer Fallback-Skill

Dieser Skill wird vom SkillManager zurückgegeben, wenn keine spezifischere
Implementierung für die aktuelle URL gefunden wird.

Unterstützte Actions:
    navigate(url)  → Navigiert zur angegebenen URL
    noop()         → Tut nichts (für Test-Steps ohne echte Aktion)

Design-Prinzipien:
    - can_handle() gibt immer True zurück → echter Fallback
    - Bekannte Actions minimal halten (nur generische Browser-Primitiven)
    - Skills-übergreifende Aktionen die kein Login/Seiten-Wissen brauchen
"""

from __future__ import annotations

import logging
from typing import Callable

from core.actions import Actions, ActionError
from skills.base_skill import BaseSkill, Result

logger = logging.getLogger(__name__)


class GenericSkill(BaseSkill):
    """
    Fallback-Skill für URLs ohne spezifischen Skill.

    Wird vom SkillManager zurückgegeben, wenn kein anderer Skill
    die aktuelle URL verarbeiten kann.

    Unterstützte Actions:
        "navigate"  → navigate(url: str)
        "noop"      → noop()
    """

    name: str = "Generic"
    base_url: str = ""

    def __init__(self) -> None:
        # GenericSkill braucht keine Selectors
        self._selectors: dict = {}
        logger.debug("[Generic] Skill initialisiert (Fallback).")

    # ── can_handle ────────────────────────────────────────────────────────────

    def can_handle(self, url: str) -> bool:
        """
        Gibt immer True zurück — echter Fallback für alle URLs.

        WICHTIG: SkillManager muss GenericSkill ZULETZT prüfen,
        damit spezifischere Skills Vorrang haben.
        """
        return True

    # ── get_action ────────────────────────────────────────────────────────────

    def get_action(self, name: str) -> Callable | None:
        """
        Gibt die Action-Funktion für den gegebenen Namen zurück.

        Verfügbare Actions:
            "navigate"  → navigate(url: str)
            "noop"      → noop()

        Unbekannte Actions → None + Warning
        """
        _action_map: dict[str, Callable] = {
            "navigate": self._action_navigate,
            "noop":     self._action_noop,
        }
        action = _action_map.get(name)
        if action is None:
            logger.warning(
                f"[{self.name}] Unbekannte Action: '{name}'. "
                f"Verfügbar: {list(_action_map.keys())}"
            )
        return action

    # ── Actions ───────────────────────────────────────────────────────────────

    def _action_navigate(self, actions: Actions, url: str = "") -> Result:
        """
        Navigiert zur angegebenen URL.

        Args:
            actions : Actions-Objekt (Core-Abstraktionsschicht).
            url     : Ziel-URL inkl. Schema (z. B. "https://www.youtube.com").

        Returns:
            Result.ok(data=url)    bei Erfolg.
            Result.fail(error=...) bei Fehler.
        """
        if not url:
            return Result.fail(error="navigate(): Kein URL-Parameter angegeben.")

        logger.info(f"[{self.name}] navigate('{url}')")

        try:
            actions.navigate(url)
            logger.info(f"[{self.name}] navigate() ✅ → {url}")
            return Result.ok(data=url)

        except ActionError as e:
            msg = f"navigate('{url}') fehlgeschlagen: {e}"
            logger.error(f"[{self.name}] {msg}")
            return Result.fail(error=msg)

        except Exception as e:  # noqa: BLE001
            msg = f"navigate() unerwarteter Fehler: {type(e).__name__}: {e}"
            logger.error(f"[{self.name}] {msg}")
            return Result.fail(error=msg)

    def _action_noop(self, actions: Actions) -> Result:
        """
        Tut nichts. Nützlich für Placeholder-Steps oder Tests.

        Returns:
            Result.ok(data="noop") immer.
        """
        logger.debug(f"[{self.name}] noop() → kein Effekt")
        return Result.ok(data="noop")
