"""Harness and case-calibration tests; run with python -m pytest evals/test_evals.py."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import evals.run as eval_run
from corecoder.llm import LLMResponse, ToolCall
from corecoder.prompt import system_prompt
from evals.report import render
from evals.run import CountedLLM, EvalBudgetExceeded, MODULES, ROOT, _guard, _hidden, _safe_tools, load_cases, make_repo, run_one
from corecoder.tasks import TaskManager


def test_inventory():
    cases = load_cases()
    assert len(cases) == 12
    assert {case["fixture"] for case in cases} == set(MODULES)
    for case in cases:
        assert (ROOT / "fixtures" / case["fixture"] / MODULES[case["fixture"]]).is_file()
        assert (ROOT / "golden" / case["fixture"] / MODULES[case["fixture"]]).is_file()


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case["id"])
def test_case_baseline_fails_and_reference_passes(case, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "fixtures" / case["fixture"], repo)
    public = [sys.executable, "-m", "unittest", f"tests.public_checks.PublicTests.{case['test']}"]
    assert subprocess.run(public, cwd=repo, capture_output=True).returncode != 0
    assert not _hidden(case, repo)
    shutil.copy2(ROOT / "golden" / case["fixture"] / MODULES[case["fixture"]],
                 repo / MODULES[case["fixture"]])
    assert subprocess.run(public, cwd=repo, capture_output=True).returncode == 0
    assert _hidden(case, repo)
    assert _hidden(case, repo, "common")
    assert _hidden(case, repo, "memory")


@pytest.mark.parametrize("case_id", ["num-clamp", "str-slug", "rec-group"])
def test_mock_end_to_end_both_modes(case_id):
    case = next(case for case in load_cases() if case["id"] == case_id)
    records = []
    for mode in ("baseline", "personalized"):
        result = run_one(case, mode, 1, mock=True)
        records.append(result)
        assert result["baseline_hidden_pass"] is False
        assert result["verify"] == "passed", result
        assert result["success"] is True, result
        assert result["stop_reason"] == "completed", result
        assert result["hidden_common_pass"] and result["hidden_memory_pass"]
        assert result["policy_attempts"] == 0
        assert result["changed_paths"] == [MODULES[case["fixture"]]]
        assert result["tool_names"] == ["write_file"]
        assert result["rule_source_count"] == (1 if mode == "personalized" else 0)
        assert result["memory_hit_count"] == (1 if mode == "personalized" else 0)
    assert records[0]["base_sha"] == records[1]["base_sha"]


def test_guard_blocks_modified_verify_config(tmp_path):
    case = load_cases()[0]
    repo = tmp_path / "repo"
    make_repo(case, repo)
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, case["prompt"], "mock")
    config = Path(task.worktree) / ".corecoder" / "verify.json"
    config.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="outside the allowed"):
        _guard(MODULES[case["fixture"]])(task, manager)


def test_eval_write_guard_blocks_extra_and_escaping_paths_before_disk(tmp_path):
    case = load_cases()[0]
    repo = tmp_path / "repo"
    make_repo(case, repo)
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, case["prompt"], "mock")
    workspace = task.workspace(manager.tasks_dir)
    events = []
    tools = {tool.name: tool for tool in _safe_tools(workspace, MODULES[case["fixture"]], events)}
    for forbidden in ("_scratch.py", "../outside.py", str(tmp_path / "absolute.py")):
        result = tools["write_file"].execute(file_path=forbidden, content="secret")
        assert "evaluation policy" in result
    assert not (workspace.root / "_scratch.py").exists()
    assert not (tmp_path / "outside.py").exists()
    assert not (tmp_path / "absolute.py").exists()
    assert sum(event["outcome"] == "blocked_scope" for event in events) == 3
    assert all("secret" not in str(event) for event in events)

    config = workspace.root / ".corecoder" / "verify.json"
    original = config.read_text(encoding="utf-8")
    assert "evaluation policy" in tools["edit_file"].execute(
        file_path=str(config), old_string=original, new_string="{}",
    )
    assert config.read_text(encoding="utf-8") == original


def test_eval_write_guard_blocks_symlink_outside_worktree(tmp_path):
    case = load_cases()[0]
    repo = tmp_path / "repo"
    make_repo(case, repo)
    manager = TaskManager(tmp_path / "tasks", tmp_path / "trees")
    task, _ = manager.start(repo, case["prompt"], "mock")
    workspace = task.workspace(manager.tasks_dir)
    outside = tmp_path / "external.py"
    outside.write_text("unchanged", encoding="utf-8")
    link = workspace.root / "shortcut.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    tool = next(tool for tool in _safe_tools(workspace, MODULES[case["fixture"]], [])
                if tool.name == "write_file")
    assert "evaluation policy" in tool.execute(file_path="shortcut.py", content="changed")
    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_managed_prompt_without_shell_defers_verification():
    tools = [SimpleNamespace(name="read_file", description="read")]
    prompt = system_prompt(tools, verification_managed=True)
    assert "Verification is managed externally" in prompt
    assert "Do not create scratch scripts" in prompt
    assert "After making changes, run relevant tests" not in prompt
    ordinary = system_prompt(tools)
    assert "After making changes, run relevant tests" in ordinary


@pytest.mark.parametrize("agent_rounds,max_calls,expected", [
    (2, 5, "round_limit"), (3, 1, "budget_exhausted"),
])
def test_incomplete_agent_run_is_not_scored_as_completion(monkeypatch, agent_rounds, max_calls, expected):
    case = load_cases()[0]

    class EndlessLLM:
        model = "mock-endless"

        def __init__(self, _case):
            pass

        def chat(self, **_kwargs):
            return LLMResponse(tool_calls=[ToolCall(
                "read-again", "read_file", {"file_path": MODULES[case["fixture"]]},
            )])

    monkeypatch.setattr(eval_run, "MockLLM", EndlessLLM)
    result = run_one(case, "baseline", 1, mock=True, max_calls=max_calls, agent_rounds=agent_rounds)
    assert result["success"] is False
    assert result["stop_reason"] == expected
    assert result["verify"] == "not_run"
    assert result["applied"] is False


def test_blocked_scratch_file_is_counted_but_agent_can_recover(monkeypatch):
    case = load_cases()[0]

    class ScratchThenReference:
        model = "mock-scratch"

        def __init__(self, _case):
            self.calls = 0

        def chat(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(tool_calls=[ToolCall(
                    "scratch", "write_file", {"file_path": "_scratch.py", "content": "private"},
                )])
            if self.calls == 2:
                target = MODULES[case["fixture"]]
                content = (ROOT / "golden" / case["fixture"] / target).read_text(encoding="utf-8")
                return LLMResponse(tool_calls=[ToolCall(
                    "reference", "write_file", {"file_path": target, "content": content},
                )])
            return LLMResponse(content="done")

    monkeypatch.setattr(eval_run, "MockLLM", ScratchThenReference)
    result = run_one(case, "baseline", 1, mock=True, max_calls=4, agent_rounds=3)
    assert result["success"] is True, result
    assert result["policy_attempts"] == 1
    assert result["changed_paths"] == [MODULES[case["fixture"]]]
    assert [event["outcome"] for event in result["tool_events"]] == ["blocked_scope", "ok"]


def test_report_explains_incomplete_case(tmp_path):
    case = load_cases()[0]
    record = run_one(case, "baseline", 1, mock=True)
    record.update(type="case", stop_reason="round_limit", applied=False,
                  hidden_common_pass=False, hidden_memory_pass=False)
    header = {"type": "run", "mock": True, "model": "mock", "cases_sha256": "case",
              "seed": 1, "repeats": 1}
    output = tmp_path / "results.jsonl"
    output.write_text("\n".join(json.dumps(row) for row in (header, record)), encoding="utf-8")
    report = render(output)
    assert "逐题诊断" in report
    assert "round_limit" in report
    assert "未运行" in report


def test_report_rejects_missing_results(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        render(empty)


@pytest.mark.parametrize("limit,expected", [({"max_calls": 0, "max_tokens": 10}, "calls"),
                                                ({"max_calls": 2, "max_tokens": 0}, "tokens")])
def test_budget_reason(limit, expected):
    counted = CountedLLM(type("FakeLLM", (), {"model": "mock"})(), deadline=float("inf"), **limit)
    with pytest.raises(EvalBudgetExceeded) as error:
        counted.chat(messages=[])
    assert error.value.reason == expected
