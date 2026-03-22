"""
skill_manager/manager.py — URL → Skill-Routing

Design:
  - SkillManager hält eine geordnete Liste registrierter Skills.
  - get_skill(url) iteriert die Liste; erster Match gewinnt.
  - GenericSkill ist IMMER letzter Fallback (kann_handle → True).
  - Neue Skills: einfach in _DEFAULT_SKILLS eintragen — keine weiteren Änderungen nötig.

Stable Contract:
  skill_manager.get_skill(url: str) → BaseSkill

Verwendung:
    manager = SkillManager()
    skill = manager.get_skill("https://www.youtube.com/watch?v=...")
    # → YouTubeSkill

    skill = manager.get_skill("https://www.amazon.de/s?k=headphones")
    # → AmazonSkill  (Phase 7)
"""

from __future__ import annotations

import logging

from skills.base_skill import BaseSkill
from skills.youtube_skill import YouTubeSkill
from skills.amazon_skill import AmazonSkill
from skills.generic_skill import GenericSkill

logger = logging.getLogger(__name__)


class SkillManager:
    """
    Vermittelt zwischen URL und dem zuständigen Skill.

    Skill-Reihenfolge ist wichtig:
      Spezifischere Skills werden vor dem GenericSkill-Fallback geprüft.
      GenericSkill wird automatisch als letzter Fallback angehängt
      (auch wenn er in der übergebenen Liste fehlt).

    Verwendung:
        manager = SkillManager()                          # Standard-Skills
        manager = SkillManager(skills=[YouTubeSkill()])   # Eigene Liste

    Stabile Schnittstelle:
        get_skill(url: str) → BaseSkill
    """

    def __init__(self, skills: list[BaseSkill] | None = None) -> None:
        """
        Args:
            skills: Liste von Skill-Instanzen in Prioritätsreihenfolge.
                    Wenn None → Standard-Liste mit allen implementierten Skills.
                    GenericSkill wird automatisch als Fallback angehängt,
                    falls er nicht bereits enthalten ist.
        """
        if skills is None:
            # Standard-Skills: alle konkreten Skills in Prioritätsreihenfolge.
            # Erster Match gewinnt → spezifischere Skills zuerst.
            self._skills: list[BaseSkill] = [
                YouTubeSkill(),
                AmazonSkill(),    # Phase 7 — Amazon-Skill
            ]
        else:
            self._skills = list(skills)

        # GenericSkill immer als letzten Fallback sicherstellen
        has_generic = any(isinstance(s, GenericSkill) for s in self._skills)
        if not has_generic:
            self._skills.append(GenericSkill())

        logger.info(
            f"[SkillManager] {len(self._skills)} Skills registriert: "
            f"{[s.name for s in self._skills]}"
        )

    # ── Öffentliche API ───────────────────────────────────────────────────────

    def get_skill(self, url: str) -> BaseSkill:
        """
        Gibt den ersten Skill zurück, der die URL verarbeiten kann.

        Durchläuft die Skill-Liste in Prioritätsreihenfolge.
        GenericSkill ist immer als letzter Fallback vorhanden.

        Args:
            url: Aktuelle Tab-URL oder URL-Fragment (z. B. "amazon.com").

        Returns:
            BaseSkill-Instanz, die can_handle(url) → True zurückgibt.
            Mindestens GenericSkill (gibt immer True zurück).
        """
        for skill in self._skills:
            if skill.can_handle(url):
                logger.debug(
                    f"[SkillManager] get_skill('{url[:60]}') → {skill.name}"
                )
                return skill

        # Sollte nie eintreten, weil GenericSkill immer True zurückgibt.
        logger.warning(
            f"[SkillManager] Kein Skill für URL '{url}' gefunden! "
            "Fallback auf GenericSkill."
        )
        return GenericSkill()

    def register(self, skill: BaseSkill, *, prepend: bool = False) -> None:
        """
        Registriert einen neuen Skill zur Laufzeit.

        Args:
            skill:   Skill-Instanz.
            prepend: True → höchste Priorität (vor allen anderen).
                     False → vor dem GenericSkill-Fallback (Standard).
        """
        if prepend:
            self._skills.insert(0, skill)
        else:
            # Vor dem GenericSkill einfügen
            generic_idx = next(
                (i for i, s in enumerate(self._skills) if isinstance(s, GenericSkill)),
                len(self._skills),
            )
            self._skills.insert(generic_idx, skill)

        logger.info(
            f"[SkillManager] Skill '{skill.name}' registriert. "
            f"Aktive Skills: {[s.name for s in self._skills]}"
        )

    @property
    def skill_names(self) -> list[str]:
        """Gibt eine Liste aller registrierten Skill-Namen zurück."""
        return [s.name for s in self._skills]
