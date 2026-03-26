"""
data/writer.py — Persistent result writer for the Data Layer (Phase E).

Writes SessionResult objects to disk in either JSON or JSONL format,
as configured by config.DATA_FORMAT and config.DATA_OUTPUT_DIR.

Design constraints:
  - Non-fatal: all write failures are logged, never raised.
    A write error must NEVER interrupt an automation run.
  - Thread-safe for single-process use (one file per session).
  - No external dependencies beyond stdlib.
  - Output directory is created automatically on first write.

Formats:
  "json"  → one pretty-printed JSON file per session
            filename: <session_id>.json
  "jsonl" → one line appended to a rolling file per day
            filename: sessions_<YYYY-MM-DD>.jsonl
            (compact, good for log aggregators)

Usage:
    from data.writer import ResultWriter
    from data.schema import SessionResult

    writer = ResultWriter()
    writer.write(session_result)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import config

if TYPE_CHECKING:
    from data.schema import SessionResult

logger = logging.getLogger(__name__)

# Supported format identifiers (case-insensitive match in write())
_SUPPORTED_FORMATS = {"json", "jsonl"}


class ResultWriter:
    """
    Writes a SessionResult to disk.

    Constructor reads config once; the instance is reusable across sessions.

    Attributes:
        output_dir : Resolved Path for the output directory.
        fmt        : Normalised format string ("json" or "jsonl").
    """

    def __init__(
        self,
        output_dir: str | None = None,
        fmt: str | None = None,
    ) -> None:
        """
        Args:
            output_dir: Override for config.DATA_OUTPUT_DIR.
            fmt:        Override for config.DATA_FORMAT ("json" | "jsonl").
        """
        raw_dir = output_dir or getattr(config, "DATA_OUTPUT_DIR", "data/results")
        self.output_dir = Path(raw_dir)

        raw_fmt = (fmt or getattr(config, "DATA_FORMAT", "json")).lower().strip()
        if raw_fmt not in _SUPPORTED_FORMATS:
            logger.warning(
                "[ResultWriter] Unknown format %r — falling back to 'json'.", raw_fmt
            )
            raw_fmt = "json"
        self.fmt = raw_fmt

        logger.debug(
            "[ResultWriter] Initialized | dir=%s | format=%s",
            self.output_dir,
            self.fmt,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def write(self, session: "SessionResult") -> str | None:
        """
        Persist a SessionResult to disk.

        Returns:
            Absolute path of the written file on success, None on failure.
        """
        try:
            self._ensure_output_dir()
            if self.fmt == "jsonl":
                return self._write_jsonl(session)
            return self._write_json(session)
        except Exception as exc:
            logger.error(
                "[ResultWriter] Failed to write session %s: %s: %s",
                getattr(session, "session_id", "?"),
                type(exc).__name__,
                exc,
            )
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # Format writers
    # ──────────────────────────────────────────────────────────────────────────

    def _write_json(self, session: "SessionResult") -> str:
        """Write one pretty-printed JSON file per session."""
        path = self.output_dir / f"{session.session_id}.json"
        payload = json.dumps(session.to_dict(), ensure_ascii=False, indent=2)
        path.write_text(payload, encoding="utf-8")
        logger.info(
            "[ResultWriter] JSON written → %s  (%.1f KB)",
            path,
            len(payload) / 1024,
        )
        return str(path.resolve())

    def _write_jsonl(self, session: "SessionResult") -> str:
        """Append one compact JSON line to a daily rolling JSONL file."""
        date_str = _today_date_str()
        path = self.output_dir / f"sessions_{date_str}.jsonl"
        line = json.dumps(session.to_dict(), ensure_ascii=False, separators=(",", ":"))
        # 'a' mode: creates file if absent, appends otherwise
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        logger.info(
            "[ResultWriter] JSONL line appended → %s",
            path,
        )
        return str(path.resolve())

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _ensure_output_dir(self) -> None:
        """Create output directory (and parents) if it does not exist."""
        self.output_dir.mkdir(parents=True, exist_ok=True)


def _today_date_str() -> str:
    """Return today's date as YYYY-MM-DD using only stdlib."""
    t = time.gmtime()
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
