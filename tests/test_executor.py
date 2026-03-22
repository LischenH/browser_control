"""
tests/test_executor.py — Unit-Tests für agent/executor.py (Phase 4)

Alle Tests sind vollständig gemockt — kein Browser, kein Playwright nötig.
Ausführen: python -m pytest tests/test_executor.py -v

Test-Kategorien:
  1. Erfolgreiche Step-Ausführung (single + multi)
  2. Retry bei transienter Condition
  3. Fail bei harter Condition
  4. Kombination: mehrere Steps mit unterschiedlichen Ergebnissen
  5. Edge-Cases: leere Step-Liste, fehlende Action, keine verify_conditions

Mock-Strategie:
  - SkillManager.get_skill() → gibt Mock-Skill zurück
  - Skill.get_action()       → gibt Mock-Action-Funktion zurück
  - Action-Funktion          → gibt kontrollierten Result zurück
  - Verifier.verify()        → gibt kontrollierten VerifyResult zurück
  - Page.url                 → gibt Dummy-URL zurück
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, call

import pytest

from agent.executor import Executor
from agent.planner import Step
from agent.verifier import VerifyResult
from skills.base_skill import Result


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def _make_page(url: str = "https://www.youtube.com") -> MagicMock:
    """Erstellt eine Mock-Page mit fixer URL."""
    page = MagicMock()
    page.url = url
    return page


def _make_skill(action_name: str, action_fn) -> MagicMock:
    """Erstellt einen Mock-Skill mit einer konfigurierten Action."""
    skill = MagicMock()
    skill.name = "MockSkill"
    skill.get_action.return_value = action_fn
    return skill


def _make_skill_manager(skill: MagicMock) -> MagicMock:
    """Erstellt einen Mock-SkillManager, der immer den gegebenen Skill liefert."""
    manager = MagicMock()
    manager.get_skill.return_value = skill
    manager.skill_names = [skill.name]
    return manager


def _make_verifier(results: list[VerifyResult]) -> MagicMock:
    """
    Erstellt einen Mock-Verifier, der die gegebenen VerifyResults
    der Reihe nach zurückgibt.
    """
    verifier = MagicMock()
    verifier.verify.side_effect = results
    return verifier


def _pass_result() -> VerifyResult:
    return VerifyResult(status="pass", reason="Test pass", details={})


def _retry_result() -> VerifyResult:
    return VerifyResult(status="retry", reason="Test transient", details={})


def _fail_result() -> VerifyResult:
    return VerifyResult(status="fail", reason="Test hard fail", details={})


def _ok_action(data=None):
    """Erstellt eine Action-Funktion die immer Result.ok() zurückgibt."""
    def action_fn(actions, **params):
        return Result.ok(data=data)
    return action_fn


def _fail_action(error="action error"):
    """Erstellt eine Action-Funktion die immer Result.fail() zurückgibt."""
    def action_fn(actions, **params):
        return Result.fail(error=error)
    return action_fn


def _make_executor(
    page=None,
    skill=None,
    verifier=None,
    action_fn=None,
    max_retries: int = 3,
) -> tuple[Executor, MagicMock, MagicMock]:
    """
    Hilfsfunktion: erstellt Executor mit Mocks.

    Returns:
        (executor, skill_manager_mock, verifier_mock)
    """
    page = page or _make_page()
    action_fn = action_fn or _ok_action()
    skill = skill or _make_skill("test_action", action_fn)
    manager = _make_skill_manager(skill)
    verifier = verifier or _make_verifier([_pass_result()])
    executor = Executor(page=page, skill_manager=manager, verifier=verifier, max_retries=max_retries)
    return executor, manager, verifier


# ─────────────────────────────────────────────────────────────────────────────
# 1. Erfolgreiche Step-Ausführung
# ─────────────────────────────────────────────────────────────────────────────

class TestSuccessfulExecution:

    def test_single_step_success(self):
        """Ein einzelner Step: Action erfolgreich + Verifier pass."""
        action_fn = _ok_action(data="test_data")
        skill = _make_skill("test_action", action_fn)
        verifier = _make_verifier([_pass_result()])
        executor, manager, _ = _make_executor(skill=skill, verifier=verifier)

        step = Step(action_name="test_action", verify_conditions={"url_contains": "test"})
        result = executor.run([step])

        assert result["success"] is True
        assert result["steps_completed"] == 1
        assert result["data"] == ["test_data"]
        assert result["error"] is None

    def test_single_step_collects_data(self):
        """Result.data wird in data-Liste gesammelt."""
        action_fn = _ok_action(data="collected_value")
        skill = _make_skill("read", action_fn)
        verifier = _make_verifier([_pass_result()])
        executor, _, _ = _make_executor(skill=skill, verifier=verifier)

        result = executor.run([Step(action_name="read", verify_conditions={"url_contains": "x"})])

        assert result["data"] == ["collected_value"]

    def test_multiple_steps_all_success(self):
        """Drei Steps hintereinander — alle erfolgreich."""
        actions_called = []

        def action1(actions, **p): actions_called.append("step1"); return Result.ok(data="d1")
        def action2(actions, **p): actions_called.append("step2"); return Result.ok(data="d2")
        def action3(actions, **p): actions_called.append("step3"); return Result.ok(data="d3")

        skill = MagicMock()
        skill.name = "Multi"
        skill.get_action.side_effect = [action1, action2, action3]

        verifier = _make_verifier([_pass_result(), _pass_result(), _pass_result()])
        manager = _make_skill_manager(skill)
        executor = Executor(page=_make_page(), skill_manager=manager, verifier=verifier)

        steps = [
            Step(action_name="step1", verify_conditions={}),
            Step(action_name="step2", verify_conditions={}),
            Step(action_name="step3", verify_conditions={}),
        ]
        result = executor.run(steps)

        assert result["success"] is True
        assert result["steps_completed"] == 3
        assert result["data"] == ["d1", "d2", "d3"]
        assert actions_called == ["step1", "step2", "step3"]

    def test_empty_step_list_returns_success(self):
        """Leere Step-Liste → sofortiges Erfolgs-Ergebnis."""
        executor, _, _ = _make_executor()
        result = executor.run([])

        assert result["success"] is True
        assert result["steps_completed"] == 0
        assert result["data"] == []
        assert result["error"] is None

    def test_step_without_verify_conditions_passes(self):
        """Step ohne verify_conditions → automatisch pass, kein Verifier-Aufruf."""
        action_fn = _ok_action(data="x")
        skill = _make_skill("noop", action_fn)
        verifier = MagicMock()
        manager = _make_skill_manager(skill)
        executor = Executor(page=_make_page(), skill_manager=manager, verifier=verifier)

        result = executor.run([Step(action_name="noop", verify_conditions={})])

        assert result["success"] is True
        # Verifier.verify() darf NICHT aufgerufen worden sein
        verifier.verify.assert_not_called()

    def test_step_params_passed_to_action(self):
        """params aus Step werden als **kwargs an die Action übergeben."""
        received_params = {}

        def action_fn(actions, **params):
            received_params.update(params)
            return Result.ok()

        skill = _make_skill("search", action_fn)
        verifier = _make_verifier([_pass_result()])
        executor, _, _ = _make_executor(skill=skill, verifier=verifier)

        step = Step(
            action_name="search",
            params={"query": "lo-fi music"},
            verify_conditions={"url_contains": "results"},
        )
        executor.run([step])

        assert received_params == {"query": "lo-fi music"}

    def test_skill_manager_called_with_step_url(self):
        """SkillManager wird mit step.url aufgerufen (wenn gesetzt)."""
        executor, manager, _ = _make_executor(
            verifier=_make_verifier([_pass_result()])
        )
        step = Step(action_name="test_action", url="youtube.com", verify_conditions={})
        executor.run([step])

        manager.get_skill.assert_called_once_with("youtube.com")

    def test_skill_manager_falls_back_to_page_url(self):
        """Wenn step.url leer ist, wird die aktuelle page.url genutzt."""
        page = _make_page(url="https://www.youtube.com/watch?v=abc")
        executor, manager, _ = _make_executor(
            page=page,
            verifier=_make_verifier([_pass_result()])
        )
        step = Step(action_name="test_action", url="", verify_conditions={})
        executor.run([step])

        manager.get_skill.assert_called_once_with("https://www.youtube.com/watch?v=abc")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Retry bei transienter Condition
# ─────────────────────────────────────────────────────────────────────────────

class TestRetryLogic:

    def test_retry_once_then_pass(self):
        """
        Verifier gibt beim ersten Aufruf 'retry', beim zweiten 'pass'.
        → Step wird zweimal ausgeführt, Ergebnis: Erfolg.
        """
        call_count = [0]

        def action_fn(actions, **p):
            call_count[0] += 1
            return Result.ok(data=f"attempt_{call_count[0]}")

        skill = _make_skill("action", action_fn)
        verifier = _make_verifier([_retry_result(), _pass_result()])

        with patch("time.sleep"):  # sleep nicht wirklich warten lassen
            executor, _, _ = _make_executor(skill=skill, verifier=verifier, max_retries=3)
            result = executor.run([Step(action_name="action", verify_conditions={"x": "y"})])

        assert result["success"] is True
        assert call_count[0] == 2  # Action wurde zweimal aufgerufen

    def test_retry_twice_then_pass(self):
        """
        Verifier: retry → retry → pass. Max retries = 3.
        → Action wird 3-mal aufgerufen.
        """
        call_count = [0]
        def action_fn(actions, **p):
            call_count[0] += 1
            return Result.ok()

        skill = _make_skill("act", action_fn)
        verifier = _make_verifier([_retry_result(), _retry_result(), _pass_result()])

        with patch("time.sleep"):
            executor, _, _ = _make_executor(skill=skill, verifier=verifier, max_retries=3)
            result = executor.run([Step(action_name="act", verify_conditions={"x": "y"})])

        assert result["success"] is True
        assert call_count[0] == 3

    def test_all_retries_exhausted_returns_failure(self):
        """
        Verifier gibt immer 'retry'. Nach MAX_RETRIES → Fehler.
        """
        def action_fn(actions, **p): return Result.ok()

        skill = _make_skill("act", action_fn)
        verifier = _make_verifier([_retry_result()] * 5)  # Mehr als genug

        with patch("time.sleep"):
            executor, _, _ = _make_executor(skill=skill, verifier=verifier, max_retries=3)
            result = executor.run([Step(action_name="act", verify_conditions={"x": "y"})])

        assert result["success"] is False
        assert result["steps_completed"] == 0
        assert result["error"] is not None
        # FIX: executor was rewritten with English messages (Phase 12)
        msg = result["error"]["message"]
        assert "exhausted" in msg or "erschöpft" in msg or "Versuche" in msg, (
            f"Expected exhausted-retries message, got: {msg!r}"
        )

    def test_retry_sleeps_between_attempts(self):
        """time.sleep() wird zwischen Retry-Versuchen aufgerufen."""
        def action_fn(actions, **p): return Result.ok()
        skill = _make_skill("act", action_fn)
        verifier = _make_verifier([_retry_result(), _pass_result()])

        with patch("time.sleep") as mock_sleep:
            executor, _, _ = _make_executor(skill=skill, verifier=verifier, max_retries=3)
            executor.run([Step(action_name="act", verify_conditions={"x": "y"})])

        # Sleep muss mindestens einmal zwischen den Versuchen aufgerufen worden sein
        assert mock_sleep.called

    def test_retry_counter_resets_between_steps(self):
        """
        Step 1: retry → pass (2 Versuche)
        Step 2: pass   (1 Versuch)
        Jeder Step bekommt seinen eigenen frischen Retry-Zähler.
        """
        call_counts = [0, 0]

        def action1(actions, **p):
            call_counts[0] += 1
            return Result.ok(data="s1")

        def action2(actions, **p):
            call_counts[1] += 1
            return Result.ok(data="s2")

        skill = MagicMock()
        skill.name = "M"
        skill.get_action.side_effect = [action1, action2]
        manager = _make_skill_manager(skill)

        verifier = _make_verifier([_retry_result(), _pass_result(), _pass_result()])

        with patch("time.sleep"):
            executor = Executor(page=_make_page(), skill_manager=manager, verifier=verifier, max_retries=3)
            result = executor.run([
                Step(action_name="s1", verify_conditions={"x": "y"}),
                Step(action_name="s2", verify_conditions={"x": "y"}),
            ])

        assert result["success"] is True
        assert call_counts[0] == 2  # Step 1: retry + pass
        assert call_counts[1] == 1  # Step 2: direkt pass


# ─────────────────────────────────────────────────────────────────────────────
# 3. Fail bei harter Condition
# ─────────────────────────────────────────────────────────────────────────────

class TestFailHandling:

    def test_hard_fail_stops_plan(self):
        """
        Verifier gibt 'fail'. Executor bricht sofort ab.
        """
        def action_fn(actions, **p): return Result.ok()
        skill = _make_skill("act", action_fn)
        verifier = _make_verifier([_fail_result()])

        executor, _, _ = _make_executor(skill=skill, verifier=verifier)
        result = executor.run([
            Step(action_name="act", verify_conditions={"url_contains": "missing"}),
        ])

        assert result["success"] is False
        assert result["steps_completed"] == 0

    def test_fail_returns_structured_error(self):
        """
        Error-Dict enthält: step, verify_result, message.
        """
        step = Step(action_name="navigate", verify_conditions={"url_contains": "target"})
        fail_vr = VerifyResult(status="fail", reason="URL falsch", details={"url_contains": {"passed": False}})

        def action_fn(actions, **p): return Result.ok()
        skill = _make_skill("navigate", action_fn)
        verifier = _make_verifier([fail_vr])

        executor, _, _ = _make_executor(skill=skill, verifier=verifier)
        result = executor.run([step])

        assert result["success"] is False
        error = result["error"]
        assert error is not None
        assert error["step"] is step
        assert error["verify_result"] is fail_vr
        assert isinstance(error["message"], str)
        assert len(error["message"]) > 0

    def test_fail_on_second_step_reports_correct_steps_completed(self):
        """
        Step 1 erfolgreich, Step 2 fail → steps_completed = 1.
        """
        def action_fn(actions, **p): return Result.ok()
        skill = MagicMock()
        skill.name = "S"
        skill.get_action.return_value = action_fn
        manager = _make_skill_manager(skill)
        verifier = _make_verifier([_pass_result(), _fail_result()])

        executor = Executor(page=_make_page(), skill_manager=manager, verifier=verifier)
        result = executor.run([
            Step(action_name="step1", verify_conditions={"x": "y"}),
            Step(action_name="step2", verify_conditions={"x": "y"}),
        ])

        assert result["success"] is False
        assert result["steps_completed"] == 1  # Step 1 abgeschlossen, Step 2 fehlgeschlagen

    def test_fail_does_not_execute_subsequent_steps(self):
        """Nach einem fail werden keine weiteren Steps ausgeführt."""
        call_count = [0]

        def action_fn(actions, **p):
            call_count[0] += 1
            return Result.ok()

        skill = MagicMock()
        skill.name = "S"
        skill.get_action.return_value = action_fn
        manager = _make_skill_manager(skill)
        # Step 1: fail, danach sollten Step 2 und 3 nie ausgeführt werden
        verifier = _make_verifier([_fail_result()])

        executor = Executor(page=_make_page(), skill_manager=manager, verifier=verifier)
        executor.run([
            Step(action_name="a1", verify_conditions={"x": "y"}),
            Step(action_name="a2", verify_conditions={"x": "y"}),
            Step(action_name="a3", verify_conditions={"x": "y"}),
        ])

        assert call_count[0] == 1  # Nur Step 1 wurde ausgeführt

    def test_action_not_found_returns_error(self):
        """
        Skill gibt None zurück für get_action() → Executor gibt Fehler zurück.
        """
        skill = MagicMock()
        skill.name = "MockSkill"
        skill.get_action.return_value = None  # Action nicht gefunden!
        manager = _make_skill_manager(skill)
        verifier = MagicMock()

        executor = Executor(page=_make_page(), skill_manager=manager, verifier=verifier)
        result = executor.run([Step(action_name="unknown_action", verify_conditions={})])

        assert result["success"] is False
        assert result["steps_completed"] == 0
        assert "unknown_action" in result["error"]["message"]
        # Verifier darf nicht aufgerufen worden sein
        verifier.verify.assert_not_called()

    def test_fail_preserves_data_from_previous_steps(self):
        """
        Daten aus erfolgreichen Steps vor dem Fehler bleiben in data erhalten.
        """
        def action_fn(actions, **p): return Result.ok(data="collected")
        skill = MagicMock()
        skill.name = "S"
        skill.get_action.return_value = action_fn
        manager = _make_skill_manager(skill)
        verifier = _make_verifier([_pass_result(), _fail_result()])

        executor = Executor(page=_make_page(), skill_manager=manager, verifier=verifier)
        result = executor.run([
            Step(action_name="step1", verify_conditions={"x": "y"}),
            Step(action_name="step2", verify_conditions={"x": "y"}),
        ])

        assert result["success"] is False
        assert result["data"] == ["collected"]  # Data aus Step 1 ist noch da


# ─────────────────────────────────────────────────────────────────────────────
# 4. Kombination mehrerer Steps mit unterschiedlichen Ergebnissen
# ─────────────────────────────────────────────────────────────────────────────

class TestMixedStepResults:

    def test_pass_retry_pass_sequence(self):
        """
        Step 1: pass
        Step 2: retry → pass
        Step 3: pass
        Gesamtergebnis: Erfolg, 3 Steps abgeschlossen.
        """
        call_counts = [0, 0, 0]

        def make_action(idx):
            def action_fn(actions, **p):
                call_counts[idx] += 1
                return Result.ok(data=f"data{idx + 1}")
            return action_fn

        skill = MagicMock()
        skill.name = "S"
        skill.get_action.side_effect = [make_action(0), make_action(1), make_action(2)]
        manager = _make_skill_manager(skill)
        verifier = _make_verifier([
            _pass_result(),              # Step 1: pass
            _retry_result(),             # Step 2: Versuch 1 → retry
            _pass_result(),              # Step 2: Versuch 2 → pass
            _pass_result(),              # Step 3: pass
        ])

        with patch("time.sleep"):
            executor = Executor(page=_make_page(), skill_manager=manager, verifier=verifier, max_retries=3)
            result = executor.run([
                Step(action_name="s1", verify_conditions={"x": "1"}),
                Step(action_name="s2", verify_conditions={"x": "2"}),
                Step(action_name="s3", verify_conditions={"x": "3"}),
            ])

        assert result["success"] is True
        assert result["steps_completed"] == 3
        assert call_counts[1] == 2  # Step 2 wurde zweimal versucht

    def test_navigate_search_read_pattern(self):
        """
        Simuliert den typischen YouTube-Workflow:
          navigate → search → read_title
        Alle erfolgreich, Daten werden korrekt gesammelt.
        """
        def nav_action(actions, url="", **p): return Result.ok(data=url)
        def search_action(actions, query="", **p): return Result.ok(data=query)
        def read_action(actions, **p): return Result.ok(data="Video-Titel")

        skill = MagicMock()
        skill.name = "YouTube"
        skill.get_action.side_effect = [nav_action, search_action, read_action]
        manager = _make_skill_manager(skill)
        verifier = _make_verifier([_pass_result(), _pass_result(), _pass_result()])

        executor = Executor(page=_make_page(), skill_manager=manager, verifier=verifier)
        result = executor.run([
            Step(action_name="navigate",   params={"url": "https://www.youtube.com"}, verify_conditions={"url_contains": "youtube.com"}),
            Step(action_name="search",     params={"query": "lo-fi music"},           verify_conditions={"url_contains": "results"}),
            Step(action_name="read_title", params={},                                 verify_conditions={"element_exists": ["#title"]}),
        ])

        assert result["success"] is True
        assert result["steps_completed"] == 3
        assert result["data"] == ["https://www.youtube.com", "lo-fi music", "Video-Titel"]

    def test_fail_on_third_step_of_four(self):
        """
        4 Steps: pass → pass → fail → (Step 4 wird nie ausgeführt)
        steps_completed = 2, error auf Step 3.
        """
        action_fn = lambda actions, **p: Result.ok()
        skill = MagicMock()
        skill.name = "S"
        skill.get_action.return_value = action_fn
        manager = _make_skill_manager(skill)
        verifier = _make_verifier([_pass_result(), _pass_result(), _fail_result()])

        executor = Executor(page=_make_page(), skill_manager=manager, verifier=verifier)
        steps = [Step(action_name=f"s{i}", verify_conditions={"x": "y"}) for i in range(4)]
        result = executor.run(steps)

        assert result["success"] is False
        assert result["steps_completed"] == 2  # Steps 1+2 ok, Step 3 fail, Step 4 nie
        assert result["error"]["step"] is steps[2]

    def test_different_skills_per_step(self):
        """
        Verschiedene Skills für verschiedene Steps via URL-Routing.

        Setup:
          - Page-URL ist "about:blank" (kein youtube.com)
          - Step 1: url=""       → Fallback auf page.url="about:blank" → generic_skill
          - Step 2: url="youtube.com" → explizit → youtube_skill

        Prüft dass:
          1. get_skill() mit dem korrekten URL-Argument aufgerufen wird
          2. Jeder Step den richtigen Skill bekommt
        """
        generic_skill = MagicMock()
        generic_skill.name = "Generic"
        generic_skill.get_action.return_value = lambda actions, **p: Result.ok(data="navigated")

        youtube_skill = MagicMock()
        youtube_skill.name = "YouTube"
        youtube_skill.get_action.return_value = lambda actions, **p: Result.ok(data="searched")

        manager = MagicMock()
        manager.skill_names = ["YouTube", "Generic"]
        # Skill-Auswahl: "youtube.com" in url → YouTube, sonst Generic
        manager.get_skill.side_effect = lambda url: (
            youtube_skill if "youtube.com" in url else generic_skill
        )

        # Page-URL ist "about:blank" → Step 1 (url="") bekommt generic_skill
        page = _make_page(url="about:blank")
        verifier = _make_verifier([_pass_result(), _pass_result()])
        executor = Executor(page=page, skill_manager=manager, verifier=verifier)

        result = executor.run([
            Step(action_name="navigate", url="",            verify_conditions={"url_contains": "youtube.com"}),
            Step(action_name="search",   url="youtube.com", verify_conditions={"url_contains": "results"}),
        ])

        assert result["success"] is True

        # Step 1: url="" → Fallback auf page.url = "about:blank"
        assert manager.get_skill.call_args_list[0] == call("about:blank")
        # Step 2: url="youtube.com" → explizit übergeben
        assert manager.get_skill.call_args_list[1] == call("youtube.com")

        # Korrekte Skills für korrekte Steps
        generic_skill.get_action.assert_called_once_with("navigate")
        youtube_skill.get_action.assert_called_once_with("search")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Edge-Cases
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_action_raises_exception_returns_error(self):
        """
        Action wirft eine nicht abgefangene Exception.
        Executor gibt Fehler zurück (kein crash).
        """
        def exploding_action(actions, **p):
            raise RuntimeError("Unexpected explosion!")

        skill = _make_skill("boom", exploding_action)
        verifier = MagicMock()
        executor, _, _ = _make_executor(skill=skill, verifier=verifier)

        result = executor.run([Step(action_name="boom", verify_conditions={})])

        assert result["success"] is False
        assert "explosion" in result["error"]["message"]
        verifier.verify.assert_not_called()  # Nie bis zu Verify vorgedrungen

    def test_max_retries_one_no_sleep(self):
        """Mit max_retries=1 gibt es keinen Retry, also auch kein sleep."""
        def action_fn(actions, **p): return Result.ok()
        skill = _make_skill("act", action_fn)
        verifier = _make_verifier([_retry_result()])

        with patch("time.sleep") as mock_sleep:
            executor, _, _ = _make_executor(skill=skill, verifier=verifier, max_retries=1)
            result = executor.run([Step(action_name="act", verify_conditions={"x": "y"})])

        assert result["success"] is False
        mock_sleep.assert_not_called()

    def test_result_structure_keys_always_present(self):
        """Rückgabe-Dict enthält immer alle erwarteten Keys."""
        executor, _, _ = _make_executor(
            verifier=_make_verifier([_pass_result()])
        )
        result = executor.run([Step(action_name="test_action", verify_conditions={})])

        assert "success" in result
        assert "steps_completed" in result
        assert "data" in result
        assert "error" in result

    def test_error_result_structure_keys_always_present(self):
        """Error-Dict enthält immer step, verify_result, message."""
        def action_fn(actions, **p): return Result.ok()
        skill = _make_skill("act", action_fn)
        verifier = _make_verifier([_fail_result()])

        executor, _, _ = _make_executor(skill=skill, verifier=verifier)
        result = executor.run([Step(action_name="act", verify_conditions={"x": "y"})])

        assert result["success"] is False
        error = result["error"]
        assert "step" in error
        assert "verify_result" in error
        assert "message" in error
