"""
agent/flow.py — Reusable Flow Pattern (Phase E)

Provides a high-level, composable runner for the most common automation
pattern:

    search   → planner generates search + open_top_results steps
    open     → executor runs the steps, N tabs are opened
    extract  → for each opened tab: switch + run extract_action
    collect  → gather all per-tab results into a FlowResult

Design constraints:
  - Thin wrapper over Planner + Executor + TabManager.
  - No new browser primitives — reuses existing Actions / Skills.
  - Non-fatal per-tab extraction: failed extract records the error but
    does NOT abort the flow.  All tabs are attempted.
  - Multi-tab aware: uses TabManager to switch tabs safely.
  - Structured output: returns FlowResult dict.

Usage:
    from agent.flow import SearchFlow
    from agent.executor import Executor
    from agent.planner import Planner
    from core.tab_manager import TabManager

    flow = SearchFlow(executor=executor, planner=Planner(),
                      tab_manager=TabManager(connection))
    result = flow.run(
        search_goal="search youtube for lo-fi music and open top 3 videos",
        extract_action="read_title",
        url_fragment="youtube.com",
    )
    # result["extractions"]  -> list of {"tab_index","url","data","error",...}
    # result["opened_tabs"]  -> list from executor (url, title, verified)
    # result["success"]      -> True when >=1 extraction succeeded

Stable Contract:
    flow.run(search_goal, extract_action, **kwargs) -> FlowResult dict
"""

from __future__ import annotations

import logging
import time
from typing import Any

import config
from agent.executor import Executor
from agent.planner import Planner
from core.actions import Actions, wait_for_page_ready
from skill_manager.manager import SkillManager

logger = logging.getLogger(__name__)


class SearchFlow:
    """
    Orchestrates the canonical multi-tab extraction flow:

        1. SEARCH  — planner generates a search+open_top_results plan
        2. OPEN    — executor runs the plan; N tabs are created
        3. EXTRACT — for each opened tab: switch + run extract_action
        4. COLLECT — gather all per-tab results into a FlowResult

    Args:
        executor:      Configured Executor (must have a connection for tab-switch).
        planner:       Configured Planner.
        tab_manager:   Optional TabManager for safe tab switching.
                       If None, extraction uses whichever tab executor last left on.
        skill_manager: Optional override; defaults to executor's SkillManager.
    """

    def __init__(
        self,
        executor: Executor,
        planner: Planner,
        tab_manager=None,          # Optional[TabManager]
        skill_manager: SkillManager | None = None,
    ) -> None:
        self._executor = executor
        self._planner = planner
        self._tab_manager = tab_manager
        self._skill_manager = skill_manager or executor._skill_manager

    # ── Public API ────────────────────────────────────────────────────────────

    def run(
        self,
        search_goal: str,
        extract_action: str,
        url_fragment: str = "",
        extract_params: dict[str, Any] | None = None,
        max_extract_tabs: int | None = None,
    ) -> dict[str, Any]:
        """
        Execute a full search → open → extract flow.

        Args:
            search_goal:      Planner goal for the search+open phase.
                              e.g. "search youtube for lo-fi and open top 3 videos"
            extract_action:   Action name to run on each opened tab.
                              e.g. "read_title", "read_price", "get_model_info"
            url_fragment:     URL fragment for skill routing of extract_action.
                              e.g. "youtube.com", "amazon", "makerworld.com"
                              Empty string -> use each tab's actual URL.
            extract_params:   Optional kwargs passed to extract_action.
            max_extract_tabs: Cap extraction to this many tabs (None = all).

        Returns:
            dict with keys:
                "success"     : bool  — True when >=1 extraction succeeded
                "extractions" : list[dict] — one entry per tab
                "opened_tabs" : list[dict] — raw from executor
                "error"       : str | None
        """
        extract_params = extract_params or {}

        logger.info(
            "[SearchFlow] Start | goal='%s' | extract='%s'",
            search_goal, extract_action,
        )

        # ── Phase 1: Search + Open ─────────────────────────────────────────
        steps = self._planner.plan(search_goal)
        if not steps:
            msg = f"Planner returned no steps for goal: '{search_goal}'"
            logger.error("[SearchFlow] %s", msg)
            return _build_result(False, [], [], msg)

        run_result = self._executor.run(steps)
        opened_tabs: list[dict] = run_result.get("opened_tabs", [])

        if not run_result.get("success"):
            err_info = run_result.get("error") or {}
            msg = err_info.get("message", "Search/open phase failed")
            logger.error("[SearchFlow] Search phase failed: %s", msg)
            return _build_result(False, [], opened_tabs, msg)

        if not opened_tabs:
            logger.warning("[SearchFlow] No tabs opened — nothing to extract.")
            return _build_result(False, [], [], "open_top_results opened no tabs")

        # ── Phase 2: Extract per tab ───────────────────────────────────────
        to_process = opened_tabs[:max_extract_tabs] if max_extract_tabs else opened_tabs
        extractions: list[dict] = []

        for i, tab_info in enumerate(to_process):
            tab_url = tab_info.get("url", "")
            logger.info(
                "[SearchFlow] Tab %d/%d | url='%s'",
                i + 1, len(to_process), tab_url[:80],
            )
            extraction = self._extract_from_tab(
                tab_info=tab_info,
                tab_index=i,
                extract_action=extract_action,
                url_fragment=url_fragment or tab_url,
                extract_params=extract_params,
            )
            extractions.append(extraction)

        succeeded = sum(1 for e in extractions if e.get("success"))
        logger.info(
            "[SearchFlow] Done | %d/%d succeeded | total_tabs=%d",
            succeeded, len(extractions), len(opened_tabs),
        )
        return _build_result(succeeded > 0, extractions, opened_tabs, None)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _extract_from_tab(
        self,
        tab_info: dict,
        tab_index: int,
        extract_action: str,
        url_fragment: str,
        extract_params: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Switch to a specific tab (if TabManager available) and run extract_action.
        Non-fatal: any exception is caught and recorded.
        """
        tab_url = tab_info.get("url", "")

        # ── Switch to tab ──────────────────────────────────────────────────
        page = None
        if self._tab_manager is not None and tab_url:
            try:
                tab_id = tab_info.get("tab_id")
                if tab_id is not None:
                    # Prefer stable ID — immune to duplicate URLs
                    switched = self._tab_manager.switch_to_tab_id(tab_id)
                else:
                    # Fallback: URL substring (may fail with duplicate URLs)
                    switched = self._tab_manager.switch_to_url(tab_url)
                page = switched.page
                logger.debug("[SearchFlow] Switched to '%s'", tab_url[:60])
            except Exception as exc:
                logger.warning(
                    "[SearchFlow] Cannot switch to '%s': %s — using current page",
                    tab_url[:60], exc,
                )

        if page is None:
            page = self._executor._page

        # ── Wait for ready ─────────────────────────────────────────────────
        try:
            wait_for_page_ready(page, timeout=config.PAGE_READY_DOM_TIMEOUT)
        except Exception:
            pass

        # ── Route skill + execute ──────────────────────────────────────────
        routing_url = url_fragment or page.url
        skill = self._skill_manager.get_skill(routing_url)
        action_fn = skill.get_action(extract_action)

        if action_fn is None:
            msg = (
                f"Action '{extract_action}' not found on skill '{skill.name}' "
                f"(routing via '{routing_url[:60]}')"
            )
            logger.warning("[SearchFlow] %s", msg)
            return _tab_entry(tab_index, tab_url, tab_info.get("title", ""),
                              False, None, msg, 0.0)

        t_start = time.perf_counter()
        try:
            actions = Actions(page, interrupt_handler=self._executor._interrupt_handler)
            result = action_fn(actions, **extract_params)
            dur = (time.perf_counter() - t_start) * 1000
            logger.info(
                "[SearchFlow] Tab %d '%s' -> success=%s",
                tab_index + 1, extract_action, result.success,
            )
            return _tab_entry(tab_index, tab_url, tab_info.get("title", ""),
                              result.success, result.data, result.error or "", dur)
        except Exception as exc:
            dur = (time.perf_counter() - t_start) * 1000
            msg = f"{type(exc).__name__}: {exc}"
            logger.error("[SearchFlow] Tab %d exception: %s", tab_index + 1, msg)
            return _tab_entry(tab_index, tab_url, tab_info.get("title", ""),
                              False, None, msg, dur)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tab_entry(
    tab_index: int,
    url: str,
    title: str,
    success: bool,
    data: Any,
    error: str,
    duration_ms: float,
) -> dict[str, Any]:
    return {
        "tab_index": tab_index,
        "url": url,
        "title": title,
        "success": success,
        "data": data,
        "error": error,
        "duration_ms": round(duration_ms, 1),
    }


def _build_result(
    success: bool,
    extractions: list[dict],
    opened_tabs: list[dict],
    error: str | None,
) -> dict[str, Any]:
    return {
        "success": success,
        "extractions": extractions,
        "opened_tabs": opened_tabs,
        "error": error,
    }
