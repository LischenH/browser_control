"""
core/__init__.py

Macht `core` zu einem Python-Package.
Exportiert die wichtigsten Klassen für bequemen Import.

Verwendung in anderen Modulen:
    from core import BrowserConnection, Actions, TabManager
"""

from core.browser import BrowserConnection
from core.actions import Actions, ActionError, wait_for_page_ready
from core.tab_manager import TabManager, TabInfo
from core.mode_resolver import ModeResolver, resolve_mode
from core.interrupts import InterruptHandler

__all__ = [
    "BrowserConnection",
    "Actions",
    "ActionError",
    "wait_for_page_ready",
    "TabManager",
    "TabInfo",
    "ModeResolver",
    "resolve_mode",
    "InterruptHandler",
]
