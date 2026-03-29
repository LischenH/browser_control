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
            "navigate"    → navigate(url: str)
            "noop"        → noop()
            "scrape_page" → scrape_page() — structured content extraction

        Unbekannte Actions → None + Warning
        """
        _action_map: dict[str, Callable] = {
            "navigate":    self._action_navigate,
            "noop":        self._action_noop,
            "scrape_page": self._action_scrape_page,
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

    def _action_scrape_page(self, actions: Actions) -> Result:
        """
        Extracts structured content from the current page.

        Adapted from browser_automation/automation/scraper.py (Phase E reference
        integration). Works on any URL via a single synchronous JS evaluation.

        Returns a dict with:
            url           – current page URL
            title         – document.title
            description   – meta description
            og_title      – Open Graph title
            og_description– Open Graph description
            headings      – list of {level, text} for h1/h2/h3
            links         – list of {text, href} for visible <a> tags (max 50)
            text_excerpt  – first 2000 chars of visible body text
        """
        logger.info(f"[{self.name}] scrape_page()")
        try:
            data = actions.evaluate_js(
                """
                () => {
                    function getMeta(name) {
                        const el = document.querySelector(
                            'meta[name="' + name + '"], meta[property="' + name + '"]'
                        );
                        return el ? (el.getAttribute('content') || '') : '';
                    }

                    const headings = [];
                    document.querySelectorAll('h1,h2,h3').forEach(h => {
                        const t = (h.innerText || '').trim();
                        if (t) headings.push({
                            level: h.tagName.toLowerCase(),
                            text:  t.slice(0, 200)
                        });
                    });

                    const links = [];
                    document.querySelectorAll('a[href]').forEach(a => {
                        const href = a.href || '';
                        const text = (a.innerText || '').trim();
                        if (href && !href.startsWith('javascript:') && text)
                            links.push({ text: text.slice(0, 120), href });
                    });

                    const bodyText = (document.body
                        ? (document.body.innerText || '')
                        : ''
                    ).replace(/\\n{3,}/g, '\\n\\n').trim();

                    return {
                        url:            window.location.href,
                        title:          document.title,
                        description:    getMeta('description'),
                        og_title:       getMeta('og:title'),
                        og_description: getMeta('og:description'),
                        headings:       headings.slice(0, 20),
                        links:          links.slice(0, 50),
                        text_excerpt:   bodyText.slice(0, 2000),
                    };
                }
                """
            )
            if not data:
                return Result.fail(error="scrape_page(): JS evaluation returned null")
            logger.info(
                f"[{self.name}] scrape_page() ✅ — "
                f"title={str(data.get('title',''))[:60]!r} "
                f"headings={len(data.get('headings', []))} "
                f"links={len(data.get('links', []))}"
            )
            return Result.ok(data=data)
        except ActionError as e:
            return Result.fail(error=f"scrape_page(): {e}")
        except Exception as e:
            return Result.fail(error=f"scrape_page(): {type(e).__name__}: {e}")
