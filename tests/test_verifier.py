"""
tests/test_verifier.py — Unit-Tests für agent/verifier.py (Phase 3)

Teststrategie:
  Playwright-Page wird vollständig gemockt (unittest.mock.MagicMock).
  Kein echter Browser nötig — Tests laufen offline und deterministisch.

  Jede Condition wird auf drei Szenarien getestet:
    1. pass  — Bedingung erfüllt
    2. fail  — Bedingung klar verletzt (harter Fehler)
    3. retry — Transienter Fehler (Playwright-Timeout)

Ausführen:
    python -m pytest tests/test_verifier.py -v
    # oder direkt:
    python tests/test_verifier.py
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

# ── Pfad-Setup: browser_control/ als Root ────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from agent.verifier import Verifier, VerifyResult, VerifyStatus


# ── Hilfsfunktion: Mock-Page erstellen ───────────────────────────────────────

def make_page(url: str = "https://example.com") -> MagicMock:
    """Erstellt eine Mock-Playwright-Page mit konfigurierbarer URL."""
    page = MagicMock()
    type(page).url = PropertyMock(return_value=url)
    return page


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: url_contains
# ═══════════════════════════════════════════════════════════════════════════════

class TestUrlContains(unittest.TestCase):

    def test_pass_when_url_contains_substring(self):
        page = make_page("https://www.youtube.com/results?search_query=python")
        v = Verifier(page, max_retries=1)
        result = v.verify({"url_contains": "results"})
        self.assertEqual(result.status, "pass")
        self.assertTrue(result.passed)
        self.assertIn("url_contains", result.details)
        self.assertTrue(result.details["url_contains"]["passed"])

    def test_fail_when_url_missing_substring(self):
        page = make_page("https://www.youtube.com/")
        v = Verifier(page, max_retries=1)
        result = v.verify({"url_contains": "results"})
        self.assertEqual(result.status, "fail")
        self.assertTrue(result.failed)
        self.assertFalse(result.details["url_contains"]["passed"])
        self.assertIn("results", result.details["url_contains"]["note"])

    def test_fail_is_not_transient(self):
        """url_contains ist nie transient — URL ist immer sofort da."""
        page = make_page("https://example.com/wrong")
        v = Verifier(page, max_retries=3)
        result = v.verify({"url_contains": "correct"})
        self.assertEqual(result.status, "fail")
        self.assertFalse(result.details["url_contains"]["transient"])


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: url_equals
# ═══════════════════════════════════════════════════════════════════════════════

class TestUrlEquals(unittest.TestCase):

    def test_pass_on_exact_match(self):
        url = "https://www.youtube.com/watch?v=abc123"
        page = make_page(url)
        v = Verifier(page, max_retries=1)
        result = v.verify({"url_equals": url})
        self.assertEqual(result.status, "pass")

    def test_fail_on_partial_match(self):
        page = make_page("https://www.youtube.com/watch?v=abc123&t=10s")
        v = Verifier(page, max_retries=1)
        result = v.verify({"url_equals": "https://www.youtube.com/watch?v=abc123"})
        self.assertEqual(result.status, "fail")
        self.assertIn("≠", result.details["url_equals"]["note"])

    def test_fail_case_sensitive(self):
        page = make_page("https://Example.COM/")
        v = Verifier(page, max_retries=1)
        result = v.verify({"url_equals": "https://example.com/"})
        self.assertEqual(result.status, "fail")


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: element_exists
# ═══════════════════════════════════════════════════════════════════════════════

class TestElementExists(unittest.TestCase):

    def _make_page_with_element(self, selector_to_find: str) -> MagicMock:
        """
        Page, bei der wait_for_selector() nur für einen bestimmten Selector
        erfolgreich ist; alle anderen werfen PlaywrightTimeoutError.
        """
        page = make_page()

        def _wait_for_selector(sel, state="visible", timeout=3000):
            if sel == selector_to_find:
                return MagicMock()  # Erfolg
            raise PlaywrightTimeoutError(f"Timeout wating for '{sel}'")

        page.wait_for_selector.side_effect = _wait_for_selector
        return page

    def test_pass_when_first_selector_found(self):
        page = self._make_page_with_element("ytd-video-renderer")
        v = Verifier(page, max_retries=1)
        result = v.verify({"element_exists": ["ytd-video-renderer", "#video-title"]})
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.details["element_exists"]["actual"], "ytd-video-renderer")

    def test_pass_when_fallback_selector_found(self):
        page = self._make_page_with_element("#video-title")
        v = Verifier(page, max_retries=1)
        result = v.verify({"element_exists": ["ytd-video-renderer", "#video-title"]})
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.details["element_exists"]["actual"], "#video-title")

    def test_retry_when_no_selector_found(self):
        """
        Wenn kein Selector sichtbar ist, ist das ein transienter Fehler
        (Seite könnte noch laden) → retry.
        """
        page = make_page()
        page.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout")
        v = Verifier(page, max_retries=2)
        result = v.verify({"element_exists": ["#missing", ".also-missing"]})
        self.assertEqual(result.status, "retry")
        self.assertTrue(result.should_retry)
        self.assertTrue(result.details["element_exists"]["transient"])

    def test_accepts_single_string_selector(self):
        page = make_page()
        page.wait_for_selector.return_value = MagicMock()
        v = Verifier(page, max_retries=1)
        result = v.verify({"element_exists": "#search-box"})
        self.assertEqual(result.status, "pass")


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: element_absent
# ═══════════════════════════════════════════════════════════════════════════════

class TestElementAbsent(unittest.TestCase):

    def test_pass_when_element_not_visible(self):
        page = make_page()
        page.is_visible.return_value = False  # Spinner nicht sichtbar
        v = Verifier(page, max_retries=1)
        result = v.verify({"element_absent": [".spinner", "#loading-overlay"]})
        self.assertEqual(result.status, "pass")

    def test_retry_when_element_still_visible(self):
        """Spinner noch da → transienter Fehler → retry."""
        page = make_page()
        page.is_visible.return_value = True  # Spinner noch sichtbar
        v = Verifier(page, max_retries=2)
        result = v.verify({"element_absent": [".spinner"]})
        self.assertEqual(result.status, "retry")
        self.assertTrue(result.details["element_absent"]["transient"])

    def test_pass_when_is_visible_raises_exception(self):
        """
        Wenn is_visible() einen Fehler wirft (Element nicht im DOM), gilt
        das als "absent" → pass.
        """
        page = make_page()
        page.is_visible.side_effect = Exception("Element not in DOM")
        v = Verifier(page, max_retries=1)
        result = v.verify({"element_absent": [".spinner"]})
        self.assertEqual(result.status, "pass")

    def test_retry_shows_still_visible_selectors(self):
        page = make_page()
        # Erste Selector sichtbar, zweiter nicht
        def _is_visible(sel):
            return sel == ".spinner"
        page.is_visible.side_effect = _is_visible
        v = Verifier(page, max_retries=1)
        result = v.verify({"element_absent": [".spinner", "#content"]})
        self.assertEqual(result.status, "retry")
        self.assertIn(".spinner", result.details["element_absent"]["actual"])


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: text_contains
# ═══════════════════════════════════════════════════════════════════════════════

class TestTextContains(unittest.TestCase):

    def test_pass_when_text_found(self):
        page = make_page()
        page.inner_text.return_value = "Welcome to Python Tutorial — YouTube"
        v = Verifier(page, max_retries=1)
        result = v.verify({"text_contains": "Python Tutorial"})
        self.assertEqual(result.status, "pass")
        # Vorschau sollte den gefundenen Text einschließen
        self.assertIn("Python Tutorial", result.details["text_contains"]["actual"])

    def test_retry_when_text_not_found(self):
        """Text nicht gefunden → transienter Fehler → retry."""
        page = make_page()
        page.inner_text.return_value = "Loading..."
        v = Verifier(page, max_retries=2)
        result = v.verify({"text_contains": "Python Tutorial"})
        self.assertEqual(result.status, "retry")
        self.assertTrue(result.details["text_contains"]["transient"])

    def test_retry_on_inner_text_timeout(self):
        """Playwright-Timeout beim Lesen von body → transient."""
        page = make_page()
        page.inner_text.side_effect = PlaywrightTimeoutError("body timeout")
        v = Verifier(page, max_retries=2)
        result = v.verify({"text_contains": "Python"})
        self.assertEqual(result.status, "retry")

    def test_fail_on_hard_exception(self):
        """Unerwartete Exception → harter Fehler → fail."""
        page = make_page()
        page.inner_text.side_effect = RuntimeError("Page crashed")
        v = Verifier(page, max_retries=2)
        result = v.verify({"text_contains": "Python"})
        self.assertEqual(result.status, "fail")
        self.assertFalse(result.details["text_contains"]["transient"])


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: Mehrere Conditions kombiniert
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultipleConditions(unittest.TestCase):

    def test_all_pass(self):
        page = make_page("https://www.youtube.com/results?search_query=python")
        page.wait_for_selector.return_value = MagicMock()
        page.inner_text.return_value = "Python Tutorial results..."
        v = Verifier(page, max_retries=1)
        result = v.verify({
            "url_contains":   "results",
            "element_exists": ["ytd-video-renderer"],
            "text_contains":  "Python",
        })
        self.assertEqual(result.status, "pass")
        self.assertEqual(len(result.details), 3)

    def test_first_fails_stops_early(self):
        """
        Wenn url_contains fehlschlägt (hart), werden die restlichen
        Conditions nicht mehr geprüft (early return).
        """
        page = make_page("https://www.youtube.com/")
        page.wait_for_selector.return_value = MagicMock()
        v = Verifier(page, max_retries=1)
        result = v.verify({
            "url_contains":   "results",    # ← schlägt fehl
            "element_exists": ["ytd-video-renderer"],  # ← sollte nicht geprüft werden
        })
        self.assertEqual(result.status, "fail")
        # element_exists sollte nicht in details sein (early exit)
        self.assertNotIn("element_exists", result.details)

    def test_mix_pass_and_transient_gives_retry(self):
        """
        Wenn url_contains besteht, aber element_exists transienten Fehler gibt
        → Gesamtergebnis retry (nicht fail).
        """
        page = make_page("https://www.youtube.com/results?search_query=python")
        page.wait_for_selector.side_effect = PlaywrightTimeoutError("timeout")
        v = Verifier(page, max_retries=1)
        result = v.verify({
            "url_contains":   "results",
            "element_exists": ["ytd-video-renderer"],
        })
        self.assertEqual(result.status, "retry")
        self.assertTrue(result.details["url_contains"]["passed"])
        self.assertFalse(result.details["element_exists"]["passed"])

    def test_unknown_key_returns_fail(self):
        page = make_page()
        v = Verifier(page, max_retries=1)
        result = v.verify({"unknown_condition": "some_value"})
        self.assertEqual(result.status, "fail")
        self.assertIn("Unbekannter", result.reason)

    def test_empty_conditions_pass(self):
        page = make_page()
        v = Verifier(page, max_retries=1)
        result = v.verify({})
        self.assertEqual(result.status, "pass")


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: VerifyResult-Eigenschaften
# ═══════════════════════════════════════════════════════════════════════════════

class TestVerifyResult(unittest.TestCase):

    def test_passed_property(self):
        r = VerifyResult(status="pass", reason="ok")
        self.assertTrue(r.passed)
        self.assertFalse(r.should_retry)
        self.assertFalse(r.failed)

    def test_retry_property(self):
        r = VerifyResult(status="retry", reason="transient")
        self.assertFalse(r.passed)
        self.assertTrue(r.should_retry)
        self.assertFalse(r.failed)

    def test_fail_property(self):
        r = VerifyResult(status="fail", reason="broken")
        self.assertFalse(r.passed)
        self.assertFalse(r.should_retry)
        self.assertTrue(r.failed)

    def test_repr(self):
        r = VerifyResult(status="pass", reason="All good", details={"url_contains": {"passed": True}})
        self.assertIn("pass", repr(r))
        self.assertIn("All good", repr(r))


# ── Einstiegspunkt ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
