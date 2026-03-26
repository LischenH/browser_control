"""
data/schema.py — Structured result types for the Data Layer (Phase E).

Hierarchy:
    SessionResult
        └── TabResult          (one per browser tab that was operated on)
                └── StepResult (one per Executor step inside that tab)

All types are plain dataclasses — no external dependencies.
They serialize to plain dicts via .to_dict() so writer.py can dump them
as JSON or JSONL without extra serialisation logic.

Design constraints:
  - No circular imports: schema.py imports NOTHING from the project.
  - All fields have defaults so instances can be built incrementally.
  - Timestamps are UTC ISO-8601 strings (no datetime objects in JSON).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string without external deps."""
    # time.gmtime() + manual formatting avoids a datetime import.
    t = time.gmtime()
    return (
        f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
        f"T{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}Z"
    )


# ──────────────────────────────────────────────────────────────────────────────
# StepResult
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class StepResult:
    """
    Records the outcome of a single Executor step.

    Fields:
        step_index      : Zero-based index within the plan.
        action_name     : The action that was executed (e.g. "search", "like_video").
        description     : Human-readable step description (from Step.description).
        success         : True if the step completed and verified successfully.
        data            : The raw return value from the skill action (may be None).
        error_message   : Non-empty only when success=False.
        verify_status   : "pass" | "retry" | "fail" | "none" (no conditions).
        verify_reason   : Human-readable verify outcome.
        retries_used    : How many retry attempts were consumed (0 = first try worked).
        duration_ms     : Wall-clock time for the step in milliseconds.
        timestamp_start : ISO-8601 UTC timestamp when the step started.
    """

    step_index: int = 0
    action_name: str = ""
    description: str = ""
    success: bool = False
    data: Any = None
    error_message: str = ""
    verify_status: str = "none"   # "pass" | "retry" | "fail" | "none"
    verify_reason: str = ""
    retries_used: int = 0
    duration_ms: float = 0.0
    timestamp_start: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index":      self.step_index,
            "action_name":     self.action_name,
            "description":     self.description,
            "success":         self.success,
            "data":            self.data,
            "error_message":   self.error_message,
            "verify_status":   self.verify_status,
            "verify_reason":   self.verify_reason,
            "retries_used":    self.retries_used,
            "duration_ms":     round(self.duration_ms, 1),
            "timestamp_start": self.timestamp_start,
        }


# ──────────────────────────────────────────────────────────────────────────────
# TabResult
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TabResult:
    """
    Records all step activity that occurred on a single browser tab.

    A tab is identified by its URL at the time the first step ran on it.
    For multi-tab flows the Executor may operate on several tabs in sequence;
    each gets its own TabResult inside SessionResult.

    Fields:
        tab_index    : Browser tab index (integer, from TabManager).
        url          : URL of the tab when steps started on it.
        title        : Page title at end of execution (best-effort).
        steps        : Ordered list of StepResult for this tab.
        opened_tabs  : List of {"tab_index", "url", "title", "verified"} dicts
                       produced by open_top_results on this tab (may be empty).
    """

    tab_index: int = 0
    url: str = ""
    title: str = ""
    steps: list[StepResult] = field(default_factory=list)
    opened_tabs: list[dict[str, Any]] = field(default_factory=list)

    # ── Convenience ───────────────────────────────────────────────────────────

    @property
    def success(self) -> bool:
        """True only when every step in this tab succeeded."""
        return bool(self.steps) and all(s.success for s in self.steps)

    @property
    def steps_completed(self) -> int:
        return sum(1 for s in self.steps if s.success)

    @property
    def total_duration_ms(self) -> float:
        return sum(s.duration_ms for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tab_index":       self.tab_index,
            "url":             self.url,
            "title":           self.title,
            "success":         self.success,
            "steps_completed": self.steps_completed,
            "total_duration_ms": round(self.total_duration_ms, 1),
            "steps":           [s.to_dict() for s in self.steps],
            "opened_tabs":     self.opened_tabs,
        }


# ──────────────────────────────────────────────────────────────────────────────
# SessionResult
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SessionResult:
    """
    Top-level result record for one full Executor.run() call.

    A session maps 1-to-1 with a single call to executor.run(steps).
    It contains metadata about the overall plan and a list of TabResults
    for each tab that was operated on.

    Fields:
        session_id      : Unique identifier (epoch seconds + pid, no uuid dep).
        goal            : Human-readable goal string passed to the Planner.
        skill_names     : Skills that were active (from SkillManager).
        success         : True when the overall plan completed without failure.
        steps_total     : Total planned steps.
        steps_completed : Steps that completed successfully.
        error_message   : Non-empty when success=False.
        tabs            : One TabResult per tab that was operated on.
        opened_tabs     : Aggregated list of tabs opened by open_top_results.
        timestamp_start : ISO-8601 UTC when executor.run() started.
        timestamp_end   : ISO-8601 UTC when executor.run() finished.
        duration_ms     : Wall-clock time of the full session in ms.
        executor_version: Semantic version tag for forward-compat filtering.
    """

    session_id: str = field(default_factory=lambda: _make_session_id())
    goal: str = ""
    skill_names: list[str] = field(default_factory=list)
    success: bool = False
    steps_total: int = 0
    steps_completed: int = 0
    error_message: str = ""
    tabs: list[TabResult] = field(default_factory=list)
    opened_tabs: list[dict[str, Any]] = field(default_factory=list)
    timestamp_start: str = field(default_factory=_now_iso)
    timestamp_end: str = ""
    duration_ms: float = 0.0
    executor_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id":       self.session_id,
            "goal":             self.goal,
            "skill_names":      self.skill_names,
            "success":          self.success,
            "steps_total":      self.steps_total,
            "steps_completed":  self.steps_completed,
            "error_message":    self.error_message,
            "tabs":             [t.to_dict() for t in self.tabs],
            "opened_tabs":      self.opened_tabs,
            "timestamp_start":  self.timestamp_start,
            "timestamp_end":    self.timestamp_end,
            "duration_ms":      round(self.duration_ms, 1),
            "executor_version": self.executor_version,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_session_id() -> str:
    """
    Generate a unique session ID without external dependencies.
    Format: bcXXXXXXXX_PPPP
      XXXXXXXX = hex of epoch seconds (8 chars)
      PPPP     = hex of process ID (4 chars, truncated)
    """
    import os
    epoch_hex = format(int(time.time()), "08x")
    pid_hex = format(os.getpid() & 0xFFFF, "04x")
    return f"bc{epoch_hex}_{pid_hex}"
