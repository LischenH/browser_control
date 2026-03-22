"""
skills/__init__.py — Package-Export für alle Skills

Importiert:
    BaseSkill, Result     — Basistypen
    YouTubeSkill          — YouTube-Implementierung
    GenericSkill          — Fallback-Skill (Phase 4)
    AmazonSkill           — Amazon-Implementierung (Phase 7)
"""

from skills.base_skill import BaseSkill, Result
from skills.youtube_skill import YouTubeSkill
from skills.generic_skill import GenericSkill
from skills.amazon_skill import AmazonSkill

__all__ = [
    "BaseSkill",
    "Result",
    "YouTubeSkill",
    "GenericSkill",
    "AmazonSkill",
]
