"""
agent/executor.py -- Step-Orchestrierung (Phase 4 + Phase 9 + Phase 10 + Stability)

FIX HISTORY (Production Stability):
  - Added optional `connection` parameter to __init__.
    When provided, executor re-reads conn.active_page BEFORE each step so that
    after an open_tab() action the next step automatically targets the new tab.
    Without this sync, multi-tab flows ran all subsequent steps on the ORIGINAL
    (now wrong) tab.
  - Page sync rebuilds the Verifier on the new page so verify conditions are
    checked against the correct DOM.
  - Debug logging added for: selected selector, retry counts, failures.

Phase E additions:
  - After run() completes (success or failure), a SessionResult is built from
    the execution trace and persisted by ResultWriter.
  - Per-step timing (duration_ms) is measured with time.perf_counter().
  - Write failures are non-fatal: they are logged but never propagate.

Stable Contract (never changes):
  executor.run(steps: list[Step]) -> dict

Return schema:
  Success: { "success": True,  "steps_completed": n, "data": [...], "error": None,
             "opened_tabs": [...] }
  Failure: { "success": False, "steps_completed": n, "data": [...],
             "error": { "step": Step, "verify_result": VerifyResult, "message": str },
             "opened_tabs": [...] }

  opened_tabs: list of {"tab_index", "url", "title", "verified"} dicts,
               collected from open_top_results / open_top_recommended actions.
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Any

from playwright.sync_api import Page

import config
from agent.planner import Step
from agent.verifier import Verifier, VerifyResult
from core.actions import Actions
from skill_manager.manager import SkillManager
from core.interrupts import InterruptHandler

# Phase E: data layer
from data.schema import SessionResult, TabResult, StepResult
from data.writer import ResultWriter

# Optional import -- only used when connection is passed to Executor.
# Guarded to avoid circular import issues in test environments.
try:
    from core.browser import BrowserConnection as _BrowserConnection
except ImportError:
    _BrowserConnection = None  # type: ignore

logger = logging.getLogger(__name__)

# Actions that are inherently idempotent and should never trigger a retry loop
# even if the verifier says "retry" -- the action already checked state internally
# and returned "skipped_*" meaning it was already in the desired state.
_IDEMPOTENT_SKIP_PREFIXES: tuple[str, ...] = (
    "skipped_already_",
    "skipped_not_",
    "skipped",   # covers plain "skipped" AND all "skipped_*" variants
)

# D4: Navigation action names that may fail due to network timing.
# These get config.NAVIGATION_RETRY_DELAY (0.5s) instead of the default
# config.RETRY_DELAY (0.05s) so the browser has time to complete redirects.
_NAVIGATION_ACTIONS: frozenset[str] = frozenset({
    "navigate",
    "go_home",
    "go_shorts_home",
    "go_to_channel",
    "go_to_channel_by_name",
    "next_video",
    "previous_video",
    "play_nth_next",
    "open_recommended",
    "open_history",
    "open_liked_videos",
    "open_playlists",
    "open_watch_later",
    "open_orders",
    "open_cart",
    "open_wishlist",
    "click_first_video",
    "click_first_result",
})


def _result_data_is_idempotent_skip(data: Any) -> bool:
    """
    Returns True if a step's result.data indicates the action was skipped
    because the system was already in the desired state (idempotent skip).

    Example: like() returns {"liked": True, "action": "skipped_already_liked"}
    -> no retry needed, state is already correct.
    """
    if not isinstance(data, dict):
        return False
    action_val = data.get("action", "")
    if not isinstance(action_val, str):
        return False
    return any(action_val.startswith(pfx) for pfx in _IDEMPOTENT_SKIP_PREFIXES)


class Executor:
    """
    Executes an ordered step list, orchestrating:
      - SkillManager  -> which skill handles this URL?
      - Skill actions -> execute action, receive Result
      - Verifier      -> check state after action
      - Retry logic   -> repeat transient failures up to MAX_RETRIES
      - Repeat loop   -> if step.params["repeat"] = N, repeat N times (Phase 9)
      - Tab tracking  -> collect opened tabs from open_top_results (Phase 9)
      - Idempotency   -> on-page toggle actions never double-fire (Phase 10)
      - Page sync     -> resync active page from connection before each step (FIX)
      - Data persist  -> write SessionResult to disk after run() (Phase E)

    Usage:
        executor = Executor(page=conn.active_page, skill_manager=SkillManager(),
                            connection=conn)
        steps  = planner.plan("open top 5 YouTube videos in new tabs")
        result = executor.run(steps)
    """

    def __init__(
        self,
        page: Page,
        skill_manager: SkillManager,
        verifier: Verifier | None = None,
        max_retries: int | None = None,
        connection=None,  # Optional[BrowserConnection] -- enables active-page sync
        goal: str = "",   # Phase E: stored in SessionResult for traceability
    ) -> None:
        self._page = page
        self._connection = connection  # FIX: re-read active_page before each step
        self._skill_manager = skill_manager
        self._verifier = verifier or Verifier(page)
        self._max_retries = max_retries if max_retries is not None else config.MAX_RETRIES
        self._opened_tabs: list[dict] = []
        self._goal = goal  # Phase E
        self._writer = ResultWriter()  # Phase E: one writer instance per executor
        # D2 FIX: one shared InterruptHandler for the entire run.
        # Its URL-keyed cache (TTL=2s) now survives across steps — if the page
        # URL hasn't changed between steps, the interrupt scan is skipped.
        # Previously a fresh InterruptHandler was created per Actions instance
        # (i.e. per step), so the cache was always empty on entry.
        self._interrupt_handler = InterruptHandler()
        logger.info(
            "[Executor] Initialized | "
            "Skills: %s | MAX_RETRIES=%d | connection=%s",
            skill_manager.skill_names,
            self._max_retries,
            "yes" if connection else "no",
        )

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def run(self, steps: list[Step]) -> dict[str, Any]:
        """
        Executes all steps in the provided list.

        Per-step execution order:
          0. Sync active page from connection (FIX: multi-tab correctness)
          1. Route to skill via SkillManager
          2. Get action function from skill
          3. Extract "repeat" + deep-copy params
          4. Execute action (repeat x)  -> Result
          5. Verify -> VerifyResult
          6. Idempotency guard: immediate success if action returned "skipped_*"
          7. Retry on transient failures (up to MAX_RETRIES)
          8. Hard-fail on permanent failures -> abort plan
          9. Collect tab data from open_top_results / open_top_recommended
         10. (Phase E) Persist SessionResult after plan finishes

        Args:
            steps: Ordered list of Step objects.

        Returns:
            dict with: success, steps_completed, data, error, opened_tabs
        """
        self._opened_tabs = []

        # Phase E: build a SessionResult for this run
        session = SessionResult(
            goal=self._goal,
            skill_names=list(self._skill_manager.skill_names),
            steps_total=len(steps),
        )
        session_start = time.perf_counter()

        # Track per-tab step lists: url -> TabResult
        # We maintain a single active TabResult and close it when the page changes.
        active_tab: TabResult | None = None

        if not steps:
            logger.warning("[Executor] run() called with empty step list.")
            result = self._build_success(steps_completed=0, data=[])
            self._persist_session(session, result, session_start, active_tab)
            return result

        self._log_plan_header(steps)
        collected_data: list[Any] = []

        for step_idx, step in enumerate(steps):
            logger.info(self._step_banner(step_idx, len(steps), step))

            # -- Step 0: Sync active page from connection (FIX) ----------------
            if self._connection is not None:
                try:
                    live_page = self._connection.active_page
                    if live_page is not self._page:
                        logger.info(
                            "[Executor]   Page synced -> '%s'",
                            live_page.url[:60],
                        )
                        # Flush the old TabResult if page changed
                        if active_tab is not None:
                            session.tabs.append(active_tab)
                        active_tab = None
                        self._page = live_page
                        # Rebuild verifier so conditions are checked on new page
                        self._verifier = Verifier(live_page, max_retries=self._max_retries)
                        # D2: Invalidate interrupt cache on page change so the
                        # new tab is immediately scanned (different URL).
                        self._interrupt_handler._last_clean_url = ""
                        self._interrupt_handler._last_clean_time = 0.0
                except Exception as _sync_exc:
                    logger.debug(
                        "[Executor] Page sync skipped (non-fatal): %s", _sync_exc
                    )

            # Phase E: ensure we have an active TabResult for the current page
            if active_tab is None:
                # Resolve the actual tab index so multi-tab sessions record
                # each tab correctly instead of all defaulting to index 0.
                _tab_idx = 0
                if self._connection is not None:
                    try:
                        _pages = self._connection.context.pages
                        if self._page in _pages:
                            _tab_idx = _pages.index(self._page)
                    except Exception:
                        pass
                active_tab = TabResult(
                    tab_index=_tab_idx,
                    url=self._page.url,
                    title="",  # filled after execution
                )

            # -- Step 1: Route to skill ----------------------------------------
            url_for_routing = step.url or self._page.url
            skill = self._skill_manager.get_skill(url_for_routing)
            logger.info(
                "[Executor]   Skill    : %s (routing via '%s')",
                skill.name,
                url_for_routing[:60],
            )

            # -- Step 2: Get action function ------------------------------------
            action_fn = skill.get_action(step.action_name)
            if action_fn is None:
                msg = (
                    f"Action '{step.action_name}' not found on skill '{skill.name}'. "
                    f"Check: get_action('{step.action_name}') must return a Callable."
                )
                logger.error("[Executor] FAIL %s", msg)
                # Phase E: record this failed step before returning
                step_rec = StepResult(
                    step_index=step_idx,
                    action_name=step.action_name,
                    description=step.description or "",
                    success=False,
                    error_message=msg,
                )
                active_tab.steps.append(step_rec)
                result = self._build_error(
                    steps_completed=step_idx,
                    data=collected_data,
                    step=step,
                    verify_result=None,
                    message=msg,
                )
                self._persist_session(session, result, session_start, active_tab)
                return result

            # -- Step 3: Extract "repeat" + deep-copy params -------------------
            action_params = copy.deepcopy(dict(step.params))
            repeat = int(action_params.pop("repeat", 1))
            if repeat < 1:
                repeat = 1
            if repeat > 1:
                logger.info(
                    "[Executor]   Repeat   : %dx for '%s'", repeat, step.action_name
                )

            # -- Step 4: Repeat-Loop -------------------------------------------
            # D2 FIX: pass the shared interrupt handler so its URL-keyed
            # cache survives across all steps in this plan.
            actions = Actions(self._page, interrupt_handler=self._interrupt_handler)

            for rep in range(repeat):
                if repeat > 1:
                    logger.info(
                        "[Executor]   Rep %d/%d for '%s'",
                        rep + 1, repeat, step.action_name,
                    )

                rep_params = copy.deepcopy(action_params)

                # Phase E: time each repetition individually
                step_start_time = time.perf_counter()
                step_start_iso = _now_iso()

                step_result = self._execute_with_retry(
                    step=step,
                    step_idx=step_idx,
                    action_fn=action_fn,
                    actions=actions,
                    action_params=rep_params,
                )

                step_duration_ms = (time.perf_counter() - step_start_time) * 1000

                # Phase E: build StepResult record
                vr = step_result.get("verify_result")
                step_rec = StepResult(
                    step_index=step_idx,
                    action_name=step.action_name,
                    description=step.description or "",
                    success=step_result["success"],
                    data=step_result.get("data"),
                    error_message=step_result.get("message", "") if not step_result["success"] else "",
                    verify_status=vr.status if vr is not None else "none",
                    verify_reason=vr.reason if vr is not None else "",
                    duration_ms=step_duration_ms,
                    timestamp_start=step_start_iso,
                )
                active_tab.steps.append(step_rec)

                if step_result["success"]:
                    step_data = step_result["data"]
                    collected_data.append(step_data)
                    self._collect_tab_data(step_data)
                    logger.info(
                        "[Executor] OK Step %d done: '%s'%s%s",
                        step_idx + 1,
                        step.action_name,
                        f" (rep {rep + 1}/{repeat})" if repeat > 1 else "",
                        f" -> data={str(step_data)[:60]!r}" if step_data is not None else "",
                    )
                else:
                    logger.error(
                        "[Executor] FAIL Step %d: '%s'%s | %s",
                        step_idx + 1,
                        step.action_name,
                        f" (rep {rep + 1}/{repeat})",
                        step_result["message"],
                    )
                    result = self._build_error(
                        steps_completed=step_idx,
                        data=collected_data,
                        step=step,
                        verify_result=step_result.get("verify_result"),
                        message=step_result["message"],
                    )
                    self._persist_session(session, result, session_start, active_tab)
                    return result

        logger.info(
            "[Executor] PLAN COMPLETE -- %d steps succeeded%s",
            len(steps),
            f" | opened_tabs={len(self._opened_tabs)}" if self._opened_tabs else "",
        )
        result = self._build_success(steps_completed=len(steps), data=collected_data)
        self._persist_session(session, result, session_start, active_tab)
        return result

    # -------------------------------------------------------------------------
    # Phase E: session persistence
    # -------------------------------------------------------------------------

    def _persist_session(
        self,
        session: SessionResult,
        run_result: dict[str, Any],
        session_start: float,
        active_tab: TabResult | None,
    ) -> None:
        """
        Finalize SessionResult fields from run_result and write to disk.

        Non-fatal: any exception is caught and logged.
        """
        try:
            # Flush the last active tab
            if active_tab is not None and active_tab not in session.tabs:
                # Best-effort page title
                try:
                    active_tab.title = self._page.title()
                except Exception:
                    pass
                session.tabs.append(active_tab)

            # Fill opened_tabs on each TabResult from the collected list
            if self._opened_tabs:
                for tab_rec in session.tabs:
                    tab_rec.opened_tabs = list(self._opened_tabs)

            # Top-level session fields
            session.success = run_result.get("success", False)
            session.steps_completed = run_result.get("steps_completed", 0)
            session.opened_tabs = list(self._opened_tabs)
            session.duration_ms = (time.perf_counter() - session_start) * 1000
            session.timestamp_end = _now_iso()

            error_info = run_result.get("error")
            if error_info and not session.success:
                session.error_message = error_info.get("message", "")

            self._writer.write(session)
        except Exception as exc:
            logger.error(
                "[Executor] Phase E persistence error (non-fatal): %s: %s",
                type(exc).__name__,
                exc,
            )

    # -------------------------------------------------------------------------
    # Internal Methods
    # -------------------------------------------------------------------------

    def _collect_tab_data(self, step_data: Any) -> None:
        """Collect tab data from open_top_results / open_top_recommended results."""
        if not isinstance(step_data, list):
            return
        for item in step_data:
            if isinstance(item, dict) and "url" in item and "title" in item:
                self._opened_tabs.append(item)

    def _execute_with_retry(
        self,
        step: Step,
        step_idx: int,
        action_fn,
        actions: Actions,
        action_params: dict | None = None,
    ) -> dict[str, Any]:
        """
        Executes Action + Verify for a step, with retry logic.

        Phase 10 additions:
          - Idempotency guard: if action returns "skipped_*" data, treat as success
            immediately without running verify.
          - Params are pre-copied by caller; no mutation risk across retries.

        On VerifyResult.should_retry -> retry the entire Step.
        On VerifyResult.failed       -> abort immediately.
        After MAX_RETRIES exhausted  -> return failure.
        """
        params_to_use = (
            action_params if action_params is not None
            else copy.deepcopy(dict(step.params))
        )
        last_verify: VerifyResult | None = None

        for attempt in range(1, self._max_retries + 1):
            if attempt > 1:
                # D4: Distinguish navigation vs action retry delays.
                # Navigation actions fail due to network/redirect timing —
                # retrying at 50ms is too aggressive (browser hasn't finished
                # the redirect chain).  Action retries keep the fast 50ms path.
                if config.RETRY_BACKOFF:
                    delay = min(config.RETRY_DELAY * (2 ** (attempt - 1)), 2.0)
                elif step.action_name in _NAVIGATION_ACTIONS:
                    delay = getattr(config, "NAVIGATION_RETRY_DELAY", 0.5)
                else:
                    delay = config.RETRY_DELAY
                logger.info(
                    "[Executor]   Retry %d/%d for '%s' (after %.3fs%s)",
                    attempt, self._max_retries, step.action_name, delay,
                    " backoff" if config.RETRY_BACKOFF else
                    " nav-delay" if step.action_name in _NAVIGATION_ACTIONS else "",
                )
                time.sleep(delay)

            # Execute action
            logger.info(
                "[Executor]   Action   : '%s'%s",
                step.action_name,
                f" | params={params_to_use}" if params_to_use else "",
            )
            try:
                result = action_fn(actions, **params_to_use)
            except Exception as exc:
                msg = (
                    f"Unexpected error in action '{step.action_name}': "
                    f"{type(exc).__name__}: {exc}"
                )
                logger.error("[Executor]   FAIL %s", msg)
                return {
                    "success": False, "data": None,
                    "verify_result": None, "message": msg,
                }

            logger.info(
                "[Executor]   Result   : success=%s%s%s",
                result.success,
                f", data={str(result.data)[:60]!r}" if result.data is not None else "",
                f", error={result.error!r}" if result.error else "",
            )

            # Idempotency guard
            if result.success and _result_data_is_idempotent_skip(result.data):
                logger.info(
                    "[Executor]   Idempotent skip -- treating as success immediately"
                )
                return {
                    "success": True, "data": result.data,
                    "verify_result": None, "message": "",
                }

            # Verify
            if step.verify_conditions:
                logger.info(
                    "[Executor]   Verify   : %s", list(step.verify_conditions.keys())
                )
                verify_result = self._verifier.verify(step.verify_conditions)
            else:
                from agent.verifier import VerifyResult as _VR
                verify_result = _VR(
                    status="pass",
                    reason="No verify conditions -- auto pass.",
                    details={},
                )

            last_verify = verify_result
            self._log_verify_result(verify_result, step_idx, attempt)

            # Decision
            if verify_result.passed:
                return {
                    "success": True, "data": result.data,
                    "verify_result": verify_result, "message": "",
                }
            elif verify_result.should_retry:
                if attempt < self._max_retries:
                    continue
                msg = (
                    f"Step '{step.action_name}': all {self._max_retries} retries "
                    f"exhausted (transient failure). "
                    f"Last reason: {verify_result.reason}"
                )
                return {
                    "success": False, "data": None,
                    "verify_result": verify_result, "message": msg,
                }
            elif verify_result.failed:
                msg = (
                    f"Step '{step.action_name}': verification failed. "
                    f"Reason: {verify_result.reason}"
                )
                return {
                    "success": False, "data": None,
                    "verify_result": verify_result, "message": msg,
                }

        msg = (
            f"Step '{step.action_name}': unknown abort after "
            f"{self._max_retries} attempts."
        )
        return {"success": False, "data": None, "verify_result": last_verify, "message": msg}

    # -------------------------------------------------------------------------
    # Logging Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _step_banner(idx: int, total: int, step: Step) -> str:
        desc = f" -- {step.description}" if step.description else ""
        return (
            f"\n[Executor] {'='*48}\n"
            f"[Executor] Step {idx + 1}/{total}: '{step.action_name}'{desc}\n"
            f"[Executor] {'='*48}"
        )

    @staticmethod
    def _log_plan_header(steps: list[Step]) -> None:
        lines = "\n".join(
            f"[Executor]   {i + 1}. {s.action_name}"
            + (f" ({s.description})" if s.description else "")
            for i, s in enumerate(steps)
        )
        logger.info(
            "\n[Executor] PLAN START -- %d Steps:\n%s",
            len(steps),
            lines,
        )

    @staticmethod
    def _log_verify_result(vr: VerifyResult, step_idx: int, attempt: int) -> None:
        icons = {"pass": "OK", "retry": "RETRY", "fail": "FAIL"}
        tag = icons.get(vr.status, "?")
        logger.info("[Executor]   Verify [%s]: %s", tag, vr.reason)
        if vr.details:
            for key, detail in vr.details.items():
                sc = "+" if detail["passed"] else ("~" if detail.get("transient") else "-")
                expected = str(detail.get("expected", ""))[:50]
                actual = str(detail.get("actual", ""))[:60]
                logger.info(
                    "[Executor]     [%s] %-22s expected=%r  actual=%r",
                    sc, key, expected, actual,
                )
                if detail.get("note"):
                    logger.info("[Executor]          note: %s", detail["note"])

    # -------------------------------------------------------------------------
    # Result Builders
    # -------------------------------------------------------------------------

    def _build_success(self, steps_completed: int, data: list) -> dict:
        return {
            "success": True,
            "steps_completed": steps_completed,
            "data": data,
            "error": None,
            "opened_tabs": list(self._opened_tabs),
        }

    def _build_error(
        self,
        steps_completed: int,
        data: list,
        step: Step,
        verify_result: VerifyResult | None,
        message: str,
    ) -> dict:
        return {
            "success": False,
            "steps_completed": steps_completed,
            "data": data,
            "error": {
                "step": step,
                "verify_result": verify_result,
                "message": message,
            },
            "opened_tabs": list(self._opened_tabs),
        }


# ─── Helpers (no external deps) ───────────────────────────────────────────────

def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    t = time.gmtime()
    return (
        f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
        f"T{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}Z"
    )
