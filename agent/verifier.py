"""
agent/verifier.py — Multi-Condition Verification (Phase 3)

Design-Prinzipien:
  - Prüft ALLE Bedingungen im übergebenen dict; nur wenn alle bestehen → "pass"
  - Transiente Fehler (Timeouts) → "retry" (bis config.MAX_RETRIES)
  - Harter Bedingungsfehler (Selector nicht da, URL falsch) → "fail"
  - Jede Prüfung loggt detailliert: Selector, URL, gefundener Text, Status
  - Erweiterbar: neue Condition Keys können ohne Änderungen in Core/Skills
    hinzugefügt werden (Dispatcher-Dict-Muster)

Kontrakt (stabil, ändert sich nie):
  verifier.verify(conditions: dict) → VerifyResult

Unterstützte Condition Keys:
  url_contains   – URL enthält Teilstring
  url_equals     – URL stimmt exakt überein
  element_exists – mindestens ein Selector aus Liste im DOM sichtbar
  element_absent – kein Selector aus Liste im DOM sichtbar (z. B. Spinner weg)
  text_contains  – sichtbarer Seitentext enthält Zeichenkette

VerifyResult:
  status  : "pass" | "retry" | "fail"
  reason  : menschenlesbarer Kurztext
  details : dict mit Debug-Info pro Condition
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

import config

logger = logging.getLogger(__name__)

# ── Typen ─────────────────────────────────────────────────────────────────────

VerifyStatus = Literal["pass", "retry", "fail"]


@dataclass
class VerifyResult:
    """
    Ergebnis einer verify()-Prüfung.

    Felder:
        status  : "pass"  → alle Bedingungen erfüllt
                  "retry" → transienter Fehler, Wiederholung sinnvoll
                  "fail"  → harte Bedingung fehlgeschlagen, Abbruch
        reason  : Kurztext, was passiert ist (für Logs + Nutzer)
        details : dict mit Debug-Info; Key = Condition-Name, Value = CheckDetail
    """
    status: VerifyStatus
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"VerifyResult(status={self.status!r}, reason={self.reason!r}, "
            f"details={self.details})"
        )

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def should_retry(self) -> bool:
        return self.status == "retry"

    @property
    def failed(self) -> bool:
        return self.status == "fail"


@dataclass
class _CheckDetail:
    """Interne Debug-Info für eine einzelne Condition-Prüfung."""
    condition: str          # Name der Condition (z. B. "url_contains")
    expected: Any           # Erwarteter Wert / Selector
    actual: Any             # Was tatsächlich vorgefunden wurde
    passed: bool            # Hat diese Prüfung bestanden?
    transient: bool = False # Handelt es sich um einen transienten Fehler?
    note: str = ""          # Zusätzlicher Hinweis / Fehlertext

    def to_dict(self) -> dict:
        return {
            "condition": self.condition,
            "expected":  self.expected,
            "actual":    self.actual,
            "passed":    self.passed,
            "transient": self.transient,
            "note":      self.note,
        }


# ── Verifier ──────────────────────────────────────────────────────────────────

class Verifier:
    """
    Prüft eine Menge von Bedingungen gegen den aktuellen Browser-Zustand.

    Verwendung:
        verifier = Verifier(page)
        result = verifier.verify({
            "url_contains":   "results",
            "element_exists": ["ytd-video-renderer", "#video-title"],
            "text_contains":  "Python",
        })

        if result.passed:
            ...  # nächster Schritt
        elif result.should_retry:
            ...  # Step wiederholen
        else:
            ...  # Fehler, Abbruch

    Erweiterbarkeit:
        Neue Condition Keys werden nur in _CONDITION_HANDLERS registriert.
        Core, Skills und Executor sind davon unberührt.
    """

    def __init__(self, page: Page, max_retries: int | None = None) -> None:
        """
        Args:
            page:        Playwright-Page, gegen die geprüft wird.
            max_retries: Maximale Retry-Versuche bei transienten Fehlern.
                         Default: config.MAX_RETRIES
        """
        self._page = page
        self._max_retries = max_retries if max_retries is not None else config.MAX_RETRIES

        # Dispatcher: Condition-Key → Handler-Methode
        # Neue Keys hier eintragen, alles andere bleibt unberührt.
        self._CONDITION_HANDLERS: dict[str, Callable[[str, Any], _CheckDetail]] = {
            "url_contains":   self._check_url_contains,
            "url_equals":     self._check_url_equals,
            "element_exists": self._check_element_exists,
            "element_absent": self._check_element_absent,
            "text_contains":  self._check_text_contains,
        }

    # ── Öffentliche API ───────────────────────────────────────────────────────

    def verify(self, conditions: dict[str, Any]) -> VerifyResult:
        """
        Prüft alle Bedingungen im Dict gegen den aktuellen Browser-Zustand.

        Ablauf:
          1. Alle bekannten Conditions werden der Reihe nach ausgeführt.
          2. Unbekannte Keys → Warnung + werden übersprungen (kein Fehler).
          3. Transienter Fehler bei irgendeiner Condition → VerifyResult(retry).
          4. Harte Bedingung fehlgeschlagen → VerifyResult(fail).
          5. Alle bestanden → VerifyResult(pass).

        Args:
            conditions: dict[str, Any] mit Condition-Keys und ihren Werten.
                        Wert kann str oder list[str] sein (je nach Condition).

        Returns:
            VerifyResult mit status, reason und details-Dict.
        """
        if not conditions:
            logger.warning("[Verifier] verify() mit leerem conditions-Dict aufgerufen → pass")
            return VerifyResult(
                status="pass",
                reason="Keine Bedingungen angegeben — trivial erfüllt.",
                details={},
            )

        logger.info(f"[Verifier] Starte Prüfung: {list(conditions.keys())}")

        details: dict[str, dict] = {}
        has_transient = False

        for key, value in conditions.items():
            handler = self._CONDITION_HANDLERS.get(key)
            if handler is None:
                logger.warning(
                    f"[Verifier] Unbekannter Condition-Key: '{key}' — wird übersprungen. "
                    f"Bekannte Keys: {list(self._CONDITION_HANDLERS.keys())}"
                )
                details[key] = {
                    "condition": key,
                    "expected": value,
                    "actual": "—",
                    "passed": False,
                    "transient": False,
                    "note": f"Unbekannter Condition-Key '{key}'. Nicht implementiert.",
                }
                return VerifyResult(
                    status="fail",
                    reason=f"Unbekannter Condition-Key: '{key}'",
                    details=details,
                )

            check = self._run_with_retry(key, handler, value)
            details[key] = check.to_dict()

            if not check.passed:
                if check.transient:
                    has_transient = True
                    logger.info(
                        f"[Verifier] ⚠ '{key}' → transienter Fehler "
                        f"(retry empfohlen): {check.note}"
                    )
                    # Wir prüfen weiter, damit details vollständig ist,
                    # aber das Gesamtergebnis wird 'retry'.
                else:
                    logger.info(
                        f"[Verifier] ✗ '{key}' → FEHLGESCHLAGEN: {check.note}"
                    )
                    # Harter Fehler → sofort fail zurückgeben
                    return VerifyResult(
                        status="fail",
                        reason=f"Bedingung '{key}' nicht erfüllt: {check.note}",
                        details=details,
                    )
            else:
                logger.info(f"[Verifier] ✓ '{key}' → bestanden")

        # Alle Conditions durchgelaufen
        if has_transient:
            return VerifyResult(
                status="retry",
                reason="Transienter Fehler in mindestens einer Bedingung.",
                details=details,
            )

        logger.info("[Verifier] ✅ Alle Bedingungen bestanden.")
        return VerifyResult(
            status="pass",
            reason="Alle Bedingungen erfüllt.",
            details=details,
        )

    # ── Retry-Wrapper ─────────────────────────────────────────────────────────

    def _run_with_retry(
        self,
        key: str,
        handler: Callable[[str, Any], _CheckDetail],
        value: Any,
    ) -> _CheckDetail:
        """
        Führt einen Handler max. self._max_retries mal aus.
        Bei transientem Fehler (PlaywrightTimeoutError) wird wiederholt.
        Bei hartem Fehler oder Erfolg sofort zurückgegeben.
        """
        last_check: _CheckDetail | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                check = handler(key, value)
                if check.passed or not check.transient:
                    # Erfolg oder harter Fehler → kein Retry
                    return check
                # Transienter Fehler → retry
                logger.debug(
                    f"[Verifier] '{key}' transienter Fehler, "
                    f"Versuch {attempt}/{self._max_retries}"
                )
                last_check = check
                if attempt < self._max_retries:
                    time.sleep(config.RETRY_DELAY)

            except PlaywrightTimeoutError as exc:
                # Playwright-Timeout im Handler selbst → transient
                last_check = _CheckDetail(
                    condition=key,
                    expected=value,
                    actual="timeout",
                    passed=False,
                    transient=True,
                    note=f"PlaywrightTimeoutError (Versuch {attempt}/{self._max_retries}): {exc}",
                )
                logger.debug(f"[Verifier] '{key}' PlaywrightTimeoutError: {exc}")
                if attempt < self._max_retries:
                    time.sleep(config.RETRY_DELAY)

            except Exception as exc:
                # Unerwarteter Fehler → harter Fail
                return _CheckDetail(
                    condition=key,
                    expected=value,
                    actual="exception",
                    passed=False,
                    transient=False,
                    note=f"{type(exc).__name__}: {exc}",
                )

        # Alle Retries erschöpft → letzten transient-Check zurückgeben
        if last_check is not None:
            last_check.note += f" [nach {self._max_retries} Versuchen]"
            return last_check

        # Sollte nie eintreten, aber defensiv:
        return _CheckDetail(
            condition=key,
            expected=value,
            actual="unknown",
            passed=False,
            transient=True,
            note=f"Alle {self._max_retries} Versuche fehlgeschlagen (unbekannter Grund).",
        )

    # ── Condition Handler ─────────────────────────────────────────────────────

    def _check_url_contains(self, key: str, substring: str) -> _CheckDetail:
        """
        Prüft ob die aktuelle Tab-URL den angegebenen Teilstring enthält.

        Args:
            key:       Condition-Key (für Logging)
            substring: Gesuchter Teilstring in der URL

        Transient: Nein — URL ist immer sofort verfügbar.
        """
        current_url = self._page.url
        passed = substring in current_url

        logger.debug(
            f"[Verifier] url_contains | "
            f"erwartet='{substring}' | "
            f"aktuell='{current_url}' | "
            f"{'✓' if passed else '✗'}"
        )

        return _CheckDetail(
            condition=key,
            expected=substring,
            actual=current_url,
            passed=passed,
            transient=False,
            note="" if passed else f"URL '{current_url}' enthält '{substring}' nicht.",
        )

    def _check_url_equals(self, key: str, expected_url: str) -> _CheckDetail:
        """
        Prüft ob die aktuelle Tab-URL exakt mit der erwarteten URL übereinstimmt.

        Transient: Nein.
        """
        current_url = self._page.url
        passed = current_url == expected_url

        logger.debug(
            f"[Verifier] url_equals | "
            f"erwartet='{expected_url}' | "
            f"aktuell='{current_url}' | "
            f"{'✓' if passed else '✗'}"
        )

        return _CheckDetail(
            condition=key,
            expected=expected_url,
            actual=current_url,
            passed=passed,
            transient=False,
            note="" if passed else f"URL '{current_url}' ≠ '{expected_url}'.",
        )

    def _check_element_exists(
        self,
        key: str,
        selectors: str | list[str],
    ) -> _CheckDetail:
        """
        Prüft ob mindestens ein Selector aus der Liste im DOM sichtbar ist.

        Verwendet is_visible() mit kurzem Timeout — schnell, nicht blockierend.
        Wenn ein Element nach dem Timeout nicht sichtbar ist, wird der nächste
        Selector versucht.

        Transient: Ja, wenn kein Selector sichtbar ist (Seite könnte noch laden).

        Args:
            selectors: Einzelner Selector (str) oder Liste von Selektoren.
        """
        if isinstance(selectors, str):
            selectors = [selectors]

        # Kurzer Timeout pro Selector: Seite könnte noch laden → transient
        check_timeout = min(config.DEFAULT_TIMEOUT * 1000, 3000)  # max 3s pro Versuch

        found_selector: str | None = None
        tried: list[str] = []

        for sel in selectors:
            try:
                self._page.wait_for_selector(
                    sel,
                    state="visible",
                    timeout=check_timeout,
                )
                found_selector = sel
                break
            except PlaywrightTimeoutError:
                tried.append(f"'{sel}' (timeout)")
            except Exception as exc:
                tried.append(f"'{sel}' ({type(exc).__name__})")

        passed = found_selector is not None

        logger.debug(
            f"[Verifier] element_exists | "
            f"selectors={selectors} | "
            f"gefunden='{found_selector}' | "
            f"{'✓' if passed else '✗'}"
        )

        return _CheckDetail(
            condition=key,
            expected=selectors,
            actual=found_selector if passed else None,
            passed=passed,
            # Transient: wenn kein Selector sichtbar, könnte Seite noch laden
            transient=not passed,
            note=(
                "" if passed
                else f"Kein Selector sichtbar. Versucht: {', '.join(tried)}"
            ),
        )

    def _check_element_absent(
        self,
        key: str,
        selectors: str | list[str],
    ) -> _CheckDetail:
        """
        Prüft ob KEIN Selector aus der Liste im DOM sichtbar ist.

        Nützlich für: "Spinner-Overlay ist verschwunden", "Modal wurde geschlossen".

        Transient: Ja, wenn ein Element noch sichtbar ist (könnte noch verschwinden).

        Args:
            selectors: Einzelner Selector (str) oder Liste von Selektoren.
        """
        if isinstance(selectors, str):
            selectors = [selectors]

        still_visible: list[str] = []

        for sel in selectors:
            try:
                # is_visible() ist non-blocking und wirft keinen Timeout.
                if self._page.is_visible(sel):
                    still_visible.append(sel)
            except Exception as exc:
                # Wenn der Selector selbst einen Fehler wirft, ist das Element
                # vermutlich nicht im DOM → als "absent" werten
                logger.debug(
                    f"[Verifier] element_absent | is_visible('{sel}') Fehler: "
                    f"{type(exc).__name__}: {exc} → als absent gewertet"
                )

        passed = len(still_visible) == 0

        logger.debug(
            f"[Verifier] element_absent | "
            f"selectors={selectors} | "
            f"noch sichtbar={still_visible} | "
            f"{'✓' if passed else '✗'}"
        )

        return _CheckDetail(
            condition=key,
            expected=selectors,
            actual=still_visible if still_visible else None,
            passed=passed,
            # Transient: Element könnte noch verschwinden
            transient=not passed,
            note=(
                "" if passed
                else f"Noch sichtbare Elemente: {still_visible}"
            ),
        )

    def _check_text_contains(self, key: str, substring: str) -> _CheckDetail:
        """
        Prüft ob der sichtbare Text der aktuellen Seite den angegebenen
        Teilstring enthält.

        Verwendet body inner_text() — gibt nur den sichtbaren Text zurück.

        Transient: Ja, wenn der Text nicht gefunden wird (Seite könnte noch laden).

        Args:
            substring: Zu suchende Zeichenkette (case-sensitive).
        """
        try:
            body_text = self._page.inner_text(
                "body",
                timeout=int(config.DEFAULT_TIMEOUT * 1000),
            )
        except PlaywrightTimeoutError as exc:
            return _CheckDetail(
                condition=key,
                expected=substring,
                actual="timeout",
                passed=False,
                transient=True,
                note=f"body inner_text Timeout: {exc}",
            )
        except Exception as exc:
            return _CheckDetail(
                condition=key,
                expected=substring,
                actual="error",
                passed=False,
                transient=False,
                note=f"{type(exc).__name__}: {exc}",
            )

        passed = substring in body_text

        # Zeige nur einen kurzen Ausschnitt des Textes um den Fund-/Fehlerbereich
        preview_len = 120
        if passed:
            idx = body_text.find(substring)
            start = max(0, idx - 30)
            preview = f"...{body_text[start:idx + len(substring) + 30]}..."
        else:
            preview = body_text[:preview_len] + ("..." if len(body_text) > preview_len else "")

        logger.debug(
            f"[Verifier] text_contains | "
            f"erwartet='{substring}' | "
            f"sichtbarer Text (Ausschnitt)='{preview}' | "
            f"{'✓' if passed else '✗'}"
        )

        return _CheckDetail(
            condition=key,
            expected=substring,
            actual=preview,
            passed=passed,
            # Transient: Text könnte noch laden
            transient=not passed,
            note=(
                "" if passed
                else f"Text '{substring}' nicht im sichtbaren Seitentext gefunden."
            ),
        )
