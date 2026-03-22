"""
skills/base_skill.py — Abstrakte Basisklasse für alle Browser-Skills

Jeder Skill (YouTube, Amazon, …) erbt von BaseSkill und implementiert:
  - can_handle(url)   → Entscheidet, ob dieser Skill für die URL zuständig ist
  - get_action(name)  → Gibt die callable Action zurück, oder None

Selector-Listen werden aus einer JSON-Datei geladen:
  skills/selectors/<site>.json

Skills rufen NIEMALS Playwright direkt auf.
Sie kommunizieren ausschließlich über das übergebene Actions-Objekt.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, ClassVar

logger = logging.getLogger(__name__)


# ── Result-Typ ────────────────────────────────────────────────────────────────

class Result:
    """
    Einheitliches Rückgabeobjekt für alle Skill-Actions.

    Felder:
        success (bool):   True wenn die Action erfolgreich abgeschlossen wurde.
        data    (any):    Nutzdaten (z. B. gelesener Text, geklicktes Element).
        error   (str|None): Fehlerbeschreibung, wenn success=False.
    """

    def __init__(
        self,
        success: bool,
        data: Any = None,
        error: str | None = None,
    ) -> None:
        self.success = success
        self.data = data
        self.error = error

    def __repr__(self) -> str:
        if self.success:
            return f"Result(success=True, data={self.data!r})"
        return f"Result(success=False, error={self.error!r})"

    @classmethod
    def ok(cls, data: Any = None) -> "Result":
        """Erstellt ein Erfolgs-Result."""
        return cls(success=True, data=data)

    @classmethod
    def fail(cls, error: str) -> "Result":
        """Erstellt ein Fehler-Result."""
        return cls(success=False, error=error)


# ── Abstrakte Basisklasse ─────────────────────────────────────────────────────

class BaseSkill(ABC):
    """
    Abstrakte Basisklasse für alle Browser-Skills.

    Unterklassen müssen implementieren:
        name        (str):  Anzeigename des Skills
        base_url    (str):  Primäre Domain, z. B. "youtube.com"
        can_handle  (url → bool)
        get_action  (name → callable)

    Selector-Loading:
        Subklassen rufen self._load_selectors("youtube") auf,
        was skills/selectors/youtube.json lädt und als dict zurückgibt.
    """

    #: Anzeigename des Skills (z. B. "YouTube")
    name: str = ""

    #: Primäre Domain, z. B. "youtube.com"
    base_url: str = ""

    # Pfad zu selectors/ relativ zu dieser Datei (unveränderlich nach Klassen-Definition).
    _SELECTORS_DIR: ClassVar[Path] = Path(__file__).parent / "selectors"

    # ── Abstrakte Methoden ────────────────────────────────────────────────────

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """
        Gibt True zurück, wenn dieser Skill für die gegebene URL zuständig ist.

        Args:
            url: Aktuelle Tab-URL.

        Returns:
            True wenn dieser Skill die URL verarbeiten kann.
        """
        ...

    @abstractmethod
    def get_action(self, name: str) -> Callable | None:
        """
        Gibt die callable Action mit dem gegebenen Namen zurück, oder None.

        Args:
            name: Aktionsname, z. B. "search", "click_first_video".

        Returns:
            callable(actions, **params) → Result, oder None wenn nicht gefunden.
        """
        ...

    # ── Selector-Loader ───────────────────────────────────────────────────────

    def _load_selectors(self, site: str) -> dict[str, list[str]]:
        """
        Lädt Selector-Listen aus skills/selectors/<site>.json.

        Das Ergebnis wird gecacht; wiederholte Aufrufe lesen nicht erneut von Disk.

        Args:
            site: Name der JSON-Datei ohne Erweiterung (z. B. "youtube").

        Returns:
            dict[str, list[str]]  —  Selector-Dict, Key → Liste von CSS-Selektoren.

        Raises:
            FileNotFoundError: Wenn die JSON-Datei nicht existiert.
            ValueError: Wenn die JSON-Datei kein valides dict enthält.
        """
        path = self._SELECTORS_DIR / f"{site}.json"

        if not path.exists():
            raise FileNotFoundError(
                f"Selector-Datei nicht gefunden: {path}\n"
                f"Erstelle skills/selectors/{site}.json mit den Selector-Listen."
            )

        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)

        if not isinstance(data, dict):
            raise ValueError(
                f"Selector-Datei {path} muss ein JSON-Objekt (dict) sein, "
                f"nicht {type(data).__name__}."
            )

        logger.debug(f"[{self.name}] Selectors geladen: {list(data.keys())} aus {path.name}")
        return data
