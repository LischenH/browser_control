"""
skill_manager/__init__.py — Package-Export für den SkillManager

Importiert:
    SkillManager  — URL → Skill-Routing mit GenericSkill-Fallback
"""

from skill_manager.manager import SkillManager

__all__ = ["SkillManager"]
