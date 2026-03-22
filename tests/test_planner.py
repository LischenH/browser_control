"""
tests/test_planner.py — Unit-Tests für agent/planner.py (Phase 5 + Phase 8)

Testet die TemplateEngine, den Planner, die validate_steps-Funktion
und die LLMEngine vollständig isoliert — kein Browser, kein Playwright,
kein Ollama, keine externen Abhängigkeiten.

Test-Kategorien:
  1.  Step-Dataclass (Felder, Defaults, __repr__)
  2.  TemplateEngine — bekannte Ziele → korrekte Step-Sequenzen
  3.  TemplateEngine — Edge-Cases (Groß-/Kleinschreibung, Leerzeichen, Varianten)
  4.  TemplateEngine — unbekannte Ziele → leere Liste + Warning
  5.  Planner — stabile öffentliche Schnittstelle
  6.  Planner — Engine-Auswahl via config und Override
  7.  validate_steps — Validierungslogik (Phase 8)
  8.  LLMEngine — Ollama-Aufruf und Fehlerbehandlung (Phase 8, gemockt)
  9.  verify_conditions Vollständigkeit pro Template

Ausführen:
    python -m pytest tests/test_planner.py -v
"""

from __future__ import annotations

import sys
import os

# ── Pfad-Setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import pytest
from unittest.mock import patch, MagicMock

import config
from agent.planner import (
    Step,
    Planner,
    _TemplateEngine,
    _LLMEngine,
    validate_steps,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Step-Dataclass
# ═══════════════════════════════════════════════════════════════════════════════

class TestStepDataclass:

    def test_required_field_action_name(self):
        """action_name ist Pflicht."""
        step = Step(action_name="navigate")
        assert step.action_name == "navigate"

    def test_defaults(self):
        """Alle optionalen Felder haben sinnvolle Defaults."""
        step = Step(action_name="noop")
        assert step.url == ""
        assert step.params == {}
        assert step.verify_conditions == {}
        assert step.description == ""

    def test_full_initialization(self):
        """Alle Felder können gesetzt werden."""
        step = Step(
            action_name="search",
            url="youtube.com",
            params={"query": "lo-fi"},
            verify_conditions={"url_contains": "results"},
            description="YouTube-Suche",
        )
        assert step.action_name == "search"
        assert step.url == "youtube.com"
        assert step.params == {"query": "lo-fi"}
        assert step.verify_conditions == {"url_contains": "results"}
        assert step.description == "YouTube-Suche"

    def test_repr_with_description(self):
        step = Step(action_name="navigate", description="Gehe zu YouTube")
        r = repr(step)
        assert "navigate" in r
        assert "Gehe zu YouTube" in r

    def test_repr_without_description(self):
        step = Step(action_name="search", url="youtube.com")
        r = repr(step)
        assert "search" in r
        assert "youtube.com" in r

    def test_params_default_is_independent(self):
        """Mutable defaults müssen unabhängig sein (field(default_factory))."""
        s1 = Step(action_name="a")
        s2 = Step(action_name="b")
        s1.params["key"] = "value"
        assert "key" not in s2.params  # Kein shared-state!

    def test_verify_conditions_default_is_independent(self):
        s1 = Step(action_name="a")
        s2 = Step(action_name="b")
        s1.verify_conditions["url_contains"] = "test"
        assert "url_contains" not in s2.verify_conditions


# ═══════════════════════════════════════════════════════════════════════════════
# 2. TemplateEngine — bekannte Ziele
# ═══════════════════════════════════════════════════════════════════════════════

class TestTemplateEngineKnownGoals:

    def setup_method(self):
        self.engine = _TemplateEngine()

    def test_open_youtube_returns_navigate_step(self):
        """'open YouTube' → 1 Step: navigate."""
        steps = self.engine.plan("open YouTube")
        assert len(steps) == 1
        assert steps[0].action_name == "navigate"
        assert steps[0].params["url"] == "https://www.youtube.com"

    def test_open_youtube_case_insensitive(self):
        steps = self.engine.plan("open youtube")
        assert len(steps) == 1
        assert steps[0].action_name == "navigate"

    def test_open_youtube_uppercase(self):
        steps = self.engine.plan("OPEN YOUTUBE")
        assert len(steps) == 1

    def test_search_youtube_for_query_returns_3_steps(self):
        """'search YouTube for lo-fi music' → 3 Steps: navigate, search, read_result_title."""
        steps = self.engine.plan("search YouTube for lo-fi music")
        assert len(steps) == 3

    def test_search_youtube_action_sequence(self):
        steps = self.engine.plan("search YouTube for Python")
        actions = [s.action_name for s in steps]
        assert actions == ["navigate", "search", "read_result_title"]

    def test_search_youtube_query_extracted_correctly(self):
        """Query wird korrekt aus dem Ziel extrahiert."""
        steps = self.engine.plan("search YouTube for lo-fi beats")
        search_step = steps[1]
        assert search_step.params.get("query") == "lo-fi beats"

    def test_search_youtube_query_with_spaces(self):
        steps = self.engine.plan("search YouTube for React Hooks tutorial")
        assert steps[1].params["query"] == "React Hooks tutorial"

    def test_search_and_click_returns_4_steps(self):
        """'search YouTube for X and click first video' → 4 Steps."""
        steps = self.engine.plan("search YouTube for Python and click first video")
        assert len(steps) == 4

    def test_search_and_click_action_sequence(self):
        steps = self.engine.plan("search YouTube for lo-fi and click first video")
        actions = [s.action_name for s in steps]
        assert actions == ["navigate", "search", "click_first_video", "read_title"]

    def test_search_and_click_query_extracted(self):
        """Query endet vor 'and click'."""
        steps = self.engine.plan("search YouTube for lo-fi music and click first video")
        assert steps[1].params["query"] == "lo-fi music"

    def test_search_and_click_shorthand(self):
        """'and click' (ohne 'first video') wird ebenfalls erkannt."""
        steps = self.engine.plan("search YouTube for Python and click")
        actions = [s.action_name for s in steps]
        assert actions == ["navigate", "search", "click_first_video", "read_title"]

    def test_search_youtube_case_insensitive(self):
        steps = self.engine.plan("SEARCH YOUTUBE FOR python tutorial")
        assert len(steps) == 3
        assert steps[1].params["query"] == "python tutorial"

    def test_search_youtube_mixed_case(self):
        steps = self.engine.plan("Search YouTube For lo-fi")
        assert len(steps) == 3

    def test_search_with_leading_trailing_whitespace(self):
        steps = self.engine.plan("  search YouTube for  lo-fi music  ")
        assert len(steps) == 3
        assert steps[1].params["query"] == "lo-fi music"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. TemplateEngine — unbekannte Ziele
# ═══════════════════════════════════════════════════════════════════════════════

class TestTemplateEngineUnknownGoals:

    def setup_method(self):
        self.engine = _TemplateEngine()

    def test_unknown_goal_returns_empty_list(self):
        """Unbekanntes Ziel → leere Liste."""
        steps = self.engine.plan("fly to the moon")
        assert steps == []

    def test_empty_string_returns_empty_list(self):
        steps = self.engine.plan("")
        assert steps == []

    def test_only_spaces_returns_empty_list(self):
        steps = self.engine.plan("   ")
        assert steps == []

    def test_partial_keyword_no_match(self):
        """'search youtube' ohne 'for' ergibt kein Match."""
        steps = self.engine.plan("search youtube")
        assert steps == []

    def test_unknown_goal_logs_warning(self):
        """Unbekanntes Ziel löst Warning im Logger aus."""
        import logging
        with patch.object(logging.getLogger("agent.planner"), "warning") as mock_warn:
            self.engine.plan("do something weird")
        mock_warn.assert_called_once()
        assert "Unbekanntes Ziel" in mock_warn.call_args[0][0] or "weird" in str(mock_warn.call_args)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. verify_conditions Vollständigkeit
# ═══════════════════════════════════════════════════════════════════════════════

class TestVerifyConditionsCompleteness:
    """
    Stellt sicher, dass alle Steps sinnvolle verify_conditions haben.

    Jeder Step muss mindestens eine Condition haben, und kritische Steps
    (read_result_title, read_title) müssen url_contains enthalten.
    """

    def setup_method(self):
        self.engine = _TemplateEngine()

    def _get_steps(self, goal: str) -> list[Step]:
        return self.engine.plan(goal)

    def test_navigate_step_has_url_contains(self):
        steps = self._get_steps("open YouTube")
        nav = steps[0]
        assert "url_contains" in nav.verify_conditions
        assert "youtube.com" in nav.verify_conditions["url_contains"]

    def test_navigate_step_has_element_exists(self):
        """navigate-Step prüft auch, dass Suchfeld sichtbar ist."""
        steps = self._get_steps("search YouTube for Python")
        nav = steps[0]
        assert "element_exists" in nav.verify_conditions
        assert len(nav.verify_conditions["element_exists"]) >= 1

    def test_search_step_has_url_contains_results(self):
        steps = self._get_steps("search YouTube for Python")
        search_step = steps[1]
        assert "url_contains" in search_step.verify_conditions
        assert search_step.verify_conditions["url_contains"] == "results"

    def test_search_step_has_element_exists(self):
        steps = self._get_steps("search YouTube for Python")
        search_step = steps[1]
        assert "element_exists" in search_step.verify_conditions

    def test_read_result_title_has_url_contains(self):
        """read_result_title muss url_contains: results haben."""
        steps = self._get_steps("search YouTube for Python")
        read_step = steps[2]
        assert read_step.action_name == "read_result_title"
        assert "url_contains" in read_step.verify_conditions
        assert read_step.verify_conditions["url_contains"] == "results"

    def test_read_result_title_has_element_exists(self):
        steps = self._get_steps("search YouTube for Python")
        read_step = steps[2]
        assert "element_exists" in read_step.verify_conditions

    def test_click_first_video_has_url_contains_watch(self):
        steps = self._get_steps("search YouTube for Python and click first video")
        click_step = steps[2]
        assert click_step.action_name == "click_first_video"
        assert "url_contains" in click_step.verify_conditions
        assert click_step.verify_conditions["url_contains"] == "watch"

    def test_read_title_has_url_contains_watch(self):
        """read_title muss url_contains: watch haben (Watch-Page-Guard)."""
        steps = self._get_steps("search YouTube for Python and click first video")
        read_step = steps[3]
        assert read_step.action_name == "read_title"
        assert "url_contains" in read_step.verify_conditions
        assert read_step.verify_conditions["url_contains"] == "watch"

    def test_read_title_has_element_exists(self):
        steps = self._get_steps("search YouTube for Python and click first video")
        read_step = steps[3]
        assert "element_exists" in read_step.verify_conditions

    def test_all_steps_have_at_least_one_condition(self):
        """Kein Step darf leere verify_conditions haben."""
        for goal in [
            "open YouTube",
            "search YouTube for Python",
            "search YouTube for lo-fi and click first video",
        ]:
            steps = self.engine.plan(goal)
            for step in steps:
                assert step.verify_conditions, (
                    f"Step '{step.action_name}' in '{goal}' hat keine verify_conditions!"
                )


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Planner — Öffentliche Schnittstelle (Stable Contract)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlannerPublicInterface:

    def test_plan_returns_list(self):
        """plan() gibt immer eine Liste zurück, nie None."""
        planner = Planner(engine="template")
        result = planner.plan("search YouTube for Python")
        assert isinstance(result, list)

    def test_plan_returns_step_instances(self):
        """Alle Elemente der Liste sind Step-Instanzen."""
        planner = Planner(engine="template")
        steps = planner.plan("search YouTube for Python")
        assert all(isinstance(s, Step) for s in steps)

    def test_plan_empty_on_unknown_goal(self):
        planner = Planner(engine="template")
        steps = planner.plan("do something impossible")
        assert steps == []

    def test_plan_logs_goal(self):
        """plan() loggt das Ziel und die Anzahl der Steps."""
        import logging
        planner = Planner(engine="template")
        with patch.object(logging.getLogger("agent.planner"), "info") as mock_info:
            planner.plan("search YouTube for Python")
        calls = [str(c) for c in mock_info.call_args_list]
        assert any("Python" in c for c in calls)

    def test_interface_signature_stable(self):
        """plan() akzeptiert genau einen positional str-Parameter."""
        import inspect
        sig = inspect.signature(Planner.plan)
        params = list(sig.parameters.keys())
        assert params == ["self", "goal"]

    def test_multiple_calls_independent(self):
        """Mehrere plan()-Aufrufe beeinflussen sich nicht gegenseitig."""
        planner = Planner(engine="template")
        steps1 = planner.plan("search YouTube for lo-fi")
        steps2 = planner.plan("open YouTube")
        assert len(steps1) == 3
        assert len(steps2) == 1
        # Kein shared state
        steps1[0].params["url"] = "MODIFIED"
        steps3 = planner.plan("open YouTube")
        assert steps3[0].params.get("url") == "https://www.youtube.com"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Planner — Engine-Auswahl
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlannerEngineSelection:

    def test_default_engine_is_template(self):
        """Standard-Engine ist TemplateEngine (aus config.PLANNER_ENGINE)."""
        with patch.object(config, "PLANNER_ENGINE", "template"):
            planner = Planner()
        # Wenn TemplateEngine aktiv, liefert "open YouTube" 1 Step
        assert len(planner.plan("open YouTube")) == 1

    def test_override_engine_template(self):
        planner = Planner(engine="template")
        steps = planner.plan("search YouTube for Python")
        assert len(steps) == 3

    def test_override_engine_llm_uses_llm_engine(self):
        """engine='llm' erzeugt _LLMEngine-Instanz."""
        planner = Planner(engine="llm")
        assert isinstance(planner._engine, _LLMEngine)

    def test_unknown_engine_falls_back_to_template(self):
        """Unbekannter Engine-Name → Warnung + Fallback auf TemplateEngine."""
        import logging
        with patch.object(logging.getLogger("agent.planner"), "warning") as mock_warn:
            planner = Planner(engine="unknown_engine_xyz")
        mock_warn.assert_called_once()
        # Trotzdem funktionsfähig als TemplateEngine
        steps = planner.plan("open YouTube")
        assert len(steps) == 1

    def test_engine_override_ignores_config(self):
        """Expliziter engine-Parameter hat Vorrang vor config.PLANNER_ENGINE."""
        with patch.object(config, "PLANNER_ENGINE", "llm"):
            planner = Planner(engine="template")  # explizit override
        # TemplateEngine aktiv
        assert isinstance(planner._engine, _TemplateEngine)
        steps = planner.plan("open YouTube")
        assert len(steps) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# 7. validate_steps — Validierungslogik (Phase 8)
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidateSteps:
    """Tests für die validate_steps()-Funktion."""

    def _valid_step_dict(self, action_name: str = "navigate") -> dict:
        """Gibt ein valides Step-Dict zurück."""
        return {
            "action_name": action_name,
            "url": "youtube.com",
            "params": {},
            "verify_conditions": {"url_contains": "youtube.com"},
            "description": "Test step",
        }

    # ── Erfolgreiche Validierung ──────────────────────────────────────────────

    def test_valid_single_step(self):
        data = [self._valid_step_dict("navigate")]
        result = validate_steps(data)
        assert result is not None
        assert len(result) == 1
        assert isinstance(result[0], Step)

    def test_valid_multiple_steps(self):
        data = [
            self._valid_step_dict("navigate"),
            self._valid_step_dict("search"),
            self._valid_step_dict("read_title"),
        ]
        result = validate_steps(data)
        assert result is not None
        assert len(result) == 3

    def test_all_valid_actions_accepted(self):
        """Alle 5 erlaubten Actions müssen akzeptiert werden."""
        valid_actions = ["navigate", "search", "click_first_video", "click_first_result", "read_title"]
        for action in valid_actions:
            data = [self._valid_step_dict(action)]
            result = validate_steps(data)
            assert result is not None, f"Action '{action}' sollte valide sein"

    def test_step_fields_correctly_mapped(self):
        """Felder werden korrekt auf Step-Dataclass gemappt."""
        data = [{
            "action_name": "search",
            "url": "youtube.com",
            "params": {"query": "Python"},
            "verify_conditions": {"url_contains": "results", "element_exists": ["ytd-video-renderer"]},
            "description": "Suche nach Python",
        }]
        result = validate_steps(data)
        assert result is not None
        step = result[0]
        assert step.action_name == "search"
        assert step.url == "youtube.com"
        assert step.params == {"query": "Python"}
        assert step.verify_conditions["url_contains"] == "results"
        assert step.description == "Suche nach Python"

    def test_empty_params_accepted(self):
        """params={} ist valide."""
        data = [self._valid_step_dict("navigate")]
        result = validate_steps(data)
        assert result is not None

    def test_empty_url_accepted(self):
        """url='' ist valide (aktuell geladene Seite)."""
        d = self._valid_step_dict("navigate")
        d["url"] = ""
        result = validate_steps([d])
        assert result is not None

    # ── Fehlerfälle ───────────────────────────────────────────────────────────

    def test_not_a_list_returns_none(self):
        assert validate_steps({}) is None
        assert validate_steps("string") is None
        assert validate_steps(42) is None
        assert validate_steps(None) is None

    def test_empty_list_returns_none(self):
        assert validate_steps([]) is None

    def test_non_dict_element_returns_none(self):
        assert validate_steps(["not a dict"]) is None
        assert validate_steps([42]) is None

    def test_missing_action_name_returns_none(self):
        d = self._valid_step_dict()
        del d["action_name"]
        assert validate_steps([d]) is None

    def test_missing_url_returns_none(self):
        d = self._valid_step_dict()
        del d["url"]
        assert validate_steps([d]) is None

    def test_missing_params_returns_none(self):
        d = self._valid_step_dict()
        del d["params"]
        assert validate_steps([d]) is None

    def test_missing_verify_conditions_returns_none(self):
        d = self._valid_step_dict()
        del d["verify_conditions"]
        assert validate_steps([d]) is None

    def test_missing_description_returns_none(self):
        d = self._valid_step_dict()
        del d["description"]
        assert validate_steps([d]) is None

    def test_invalid_action_name_returns_none(self):
        d = self._valid_step_dict()
        d["action_name"] = "fly_to_moon"
        assert validate_steps([d]) is None

    def test_hallucinated_action_returns_none(self):
        d = self._valid_step_dict()
        d["action_name"] = "click_all_ads"
        assert validate_steps([d]) is None

    def test_wrong_type_action_name_returns_none(self):
        d = self._valid_step_dict()
        d["action_name"] = 42
        assert validate_steps([d]) is None

    def test_wrong_type_url_returns_none(self):
        d = self._valid_step_dict()
        d["url"] = ["list", "not", "str"]
        assert validate_steps([d]) is None

    def test_wrong_type_params_returns_none(self):
        d = self._valid_step_dict()
        d["params"] = "not a dict"
        assert validate_steps([d]) is None

    def test_wrong_type_verify_conditions_returns_none(self):
        d = self._valid_step_dict()
        d["verify_conditions"] = ["not", "a", "dict"]
        assert validate_steps([d]) is None

    def test_empty_verify_conditions_returns_none(self):
        """verify_conditions darf nicht leer sein."""
        d = self._valid_step_dict()
        d["verify_conditions"] = {}
        assert validate_steps([d]) is None

    def test_one_invalid_step_invalidates_all(self):
        """Ein ungültiger Step macht die gesamte Liste ungültig."""
        data = [
            self._valid_step_dict("navigate"),   # gültig
            self._valid_step_dict("navigate"),   # gültig
            {                                     # UNGÜLTIG: action fehlt
                "url": "youtube.com",
                "params": {},
                "verify_conditions": {"url_contains": "x"},
                "description": "oops",
            },
        ]
        assert validate_steps(data) is None


# ═══════════════════════════════════════════════════════════════════════════════
# 8. LLMEngine — Ollama-Aufruf und Fehlerbehandlung (Phase 8, gemockt)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLLMEngine:
    """
    Tests für _LLMEngine. Alle Netzwerkaufrufe werden gemockt —
    kein Ollama erforderlich.
    """

    def _make_ollama_response(self, steps: list[dict]) -> bytes:
        """Erstellt eine gemockte Ollama-HTTP-Antwort."""
        body = {"model": "phi4:14b", "response": json.dumps(steps)}
        return json.dumps(body).encode("utf-8")

    def _valid_step_dict(self, action: str = "navigate") -> dict:
        return {
            "action_name": action,
            "url": "youtube.com",
            "params": {},
            "verify_conditions": {"url_contains": "youtube.com", "element_exists": ["#search-input"]},
            "description": f"Test {action}",
        }

    def _mock_urlopen(self, response_bytes: bytes):
        """Liefert einen Context-Manager-Mock für urllib.request.urlopen."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_bytes
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    # ── Erfolgreiche LLM-Antwort ──────────────────────────────────────────────

    def test_successful_plan_returns_steps(self):
        """Valide LLM-Antwort → korrekte Step-Liste."""
        steps_data = [
            self._valid_step_dict("navigate"),
            self._valid_step_dict("search"),
        ]
        resp_bytes = self._make_ollama_response(steps_data)

        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(resp_bytes)):
            engine = _LLMEngine()
            result = engine.plan("search YouTube for Python")

        assert isinstance(result, list)
        assert len(result) == 2
        assert all(isinstance(s, Step) for s in result)

    def test_successful_plan_action_names(self):
        """Korrekte action_names werden aus LLM-Antwort übernommen."""
        steps_data = [
            self._valid_step_dict("navigate"),
            self._valid_step_dict("search"),
            self._valid_step_dict("click_first_video"),
            self._valid_step_dict("read_title"),
        ]
        resp_bytes = self._make_ollama_response(steps_data)

        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(resp_bytes)):
            engine = _LLMEngine()
            result = engine.plan("any goal")

        actions = [s.action_name for s in result]
        assert actions == ["navigate", "search", "click_first_video", "read_title"]

    def test_primary_model_used_first(self):
        """Primär-Modell phi4:14b wird als erstes versucht."""
        steps_data = [self._valid_step_dict("navigate")]
        resp_bytes = self._make_ollama_response(steps_data)

        captured_requests = []

        def mock_urlopen(req, timeout=None):
            captured_requests.append(req)
            return self._mock_urlopen(resp_bytes)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            engine = _LLMEngine()
            engine.plan("open YouTube")

        # Nur ein Request nötig, da primäres Modell erfolgreich war
        assert len(captured_requests) == 1
        body = json.loads(captured_requests[0].data.decode("utf-8"))
        assert body["model"] == _LLMEngine.PRIMARY_MODEL

    def test_fallback_model_used_when_primary_fails(self):
        """Wenn phi4:14b fehlschlägt, wird llama3.3:8b verwendet."""
        import urllib.error

        steps_data = [self._valid_step_dict("navigate")]
        fallback_bytes = self._make_ollama_response(steps_data)

        call_count = [0]

        def mock_urlopen(req, timeout=None):
            call_count[0] += 1
            body = json.loads(req.data.decode("utf-8"))
            if body["model"] == _LLMEngine.PRIMARY_MODEL:
                raise urllib.error.URLError("primary model not available")
            return self._mock_urlopen(fallback_bytes)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            engine = _LLMEngine()
            result = engine.plan("any goal")

        assert call_count[0] == 2  # Primary + Fallback
        assert len(result) >= 1

    def test_fallback_to_template_when_both_models_fail(self):
        """Wenn beide Modelle fehlschlagen → TemplateEngine-Fallback."""
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            engine = _LLMEngine()
            # "open YouTube" ist von TemplateEngine bekannt → 1 Step
            result = engine.plan("open YouTube")

        # TemplateEngine-Fallback liefert navigate-Step
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].action_name == "navigate"

    def test_template_fallback_on_json_parse_error(self):
        """Ungültige LLM-Antwort (kein JSON) → TemplateEngine-Fallback."""
        # Antwort enthält kein valides JSON-Array
        bad_body = json.dumps({"response": "This is not JSON at all!"}).encode("utf-8")
        bad_resp = self._mock_urlopen(bad_body)

        with patch("urllib.request.urlopen", return_value=bad_resp):
            engine = _LLMEngine()
            result = engine.plan("open YouTube")

        # TemplateEngine-Fallback
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].action_name == "navigate"

    def test_template_fallback_on_validation_failure(self):
        """Valides JSON, aber ungültige Struktur → TemplateEngine-Fallback."""
        invalid_steps = [{"wrong_key": "wrong_value"}]
        bad_body = json.dumps({"response": json.dumps(invalid_steps)}).encode("utf-8")
        bad_resp = self._mock_urlopen(bad_body)

        with patch("urllib.request.urlopen", return_value=bad_resp):
            engine = _LLMEngine()
            result = engine.plan("search YouTube for Python")

        # TemplateEngine-Fallback mit bekanntem Ziel
        assert isinstance(result, list)
        assert len(result) == 3  # navigate + search + read_result_title

    def test_never_raises_exception(self):
        """plan() darf nie eine Exception werfen, auch nicht bei schwerem Fehler."""
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=Exception("catastrophic failure")):
            engine = _LLMEngine()
            # Darf nicht werfen:
            result = engine.plan("any goal")

        assert isinstance(result, list)

    def test_markdown_code_blocks_stripped(self):
        """LLM-Antworten mit Markdown-Fencing werden korrekt bereinigt."""
        steps_data = [self._valid_step_dict("navigate")]
        # Simuliere LLM mit Markdown-Fencing
        steps_json = json.dumps(steps_data)
        body_with_fencing = json.dumps({
            "response": f"```json\n{steps_json}\n```"
        }).encode("utf-8")

        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(body_with_fencing)):
            engine = _LLMEngine()
            result = engine.plan("open YouTube")

        assert result is not None
        assert len(result) == 1
        assert result[0].action_name == "navigate"

    def test_plan_returns_list_always(self):
        """plan() gibt immer list zurück, nie None."""
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("x")):
            engine = _LLMEngine()
            result = engine.plan("unknown goal xyz")

        assert result is not None
        assert isinstance(result, list)

    # ── LLMEngine-Konfiguration ───────────────────────────────────────────────

    def test_primary_model_is_phi4(self):
        assert _LLMEngine.PRIMARY_MODEL == "phi4:14b"

    def test_fallback_model_is_llama3(self):
        assert _LLMEngine.FALLBACK_MODEL == "llama3.3:8b"

    def test_ollama_url_is_local(self):
        """Ollama-URL zeigt auf localhost — kein externer API-Aufruf."""
        assert "localhost" in _LLMEngine.OLLAMA_URL
        assert "11434" in _LLMEngine.OLLAMA_URL

    def test_system_prompt_contains_valid_actions(self):
        """System-Prompt erwähnt alle erlaubten Actions."""
        prompt = _LLMEngine._SYSTEM_PROMPT
        for action in ["navigate", "search", "click_first_video", "click_first_result", "read_title"]:
            assert action in prompt, f"Action '{action}' fehlt im System-Prompt"

    def test_system_prompt_contains_verify_requirements(self):
        """System-Prompt fordert url_contains und element_exists."""
        prompt = _LLMEngine._SYSTEM_PROMPT
        assert "url_contains" in prompt
        assert "element_exists" in prompt

    def test_planner_with_llm_engine_uses_llm(self):
        """Planner(engine='llm') verwendet _LLMEngine."""
        planner = Planner(engine="llm")
        assert isinstance(planner._engine, _LLMEngine)

    def test_stream_false_in_request(self):
        """Ollama-Request enthält stream: false."""
        steps_data = [self._valid_step_dict("navigate")]
        resp_bytes = self._make_ollama_response(steps_data)

        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(req)
            return self._mock_urlopen(resp_bytes)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            engine = _LLMEngine()
            engine.plan("open YouTube")

        body = json.loads(captured[0].data.decode("utf-8"))
        assert body["stream"] is False


# ── Einstiegspunkt ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
