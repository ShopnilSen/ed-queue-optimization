"""Recovery command: ``autoresearch skill resume --run <run_id>``.

The command is a thin, validated surface over ``DiscoveryService.resume``.
These tests prove it (a) reuses a completed seed_hydration checkpoint,
(b) retries the interrupted seed_analysis stage on the same project/run,
(c) rejects unknown and ineligible run ids with machine-readable errors, and
(d) delegates recovery to the existing service instead of duplicating it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import test_smoke_e2e as smoke
from autoresearch.application.services import DiscoveryOutcome
from autoresearch.cli import skills
from autoresearch.domain.models import PipelineKind, RunStatus
from autoresearch.storage.repository import ResearchRepository

runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(skills.skill_app, list(args))


def _payload(result):
    return json.loads(result.output)


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    """Keep project registration inside the test (never the developer's real
    registry or project databases)."""
    monkeypatch.setenv("AUTORESEARCH_REGISTRY_PATH", str(tmp_path / "registry.json"))
    return tmp_path / "registry.json"


@pytest.fixture
def isolated_cli_env(tmp_path, monkeypatch, isolated_registry):
    monkeypatch.setenv("AUTORESEARCH_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("AUTORESEARCH_PROVIDER_CONFIG", str(tmp_path / "missing.toml"))
    # Prevent OpenCode session auto-detection from touching the real HOME.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    return tmp_path


def _repository(tmp_path):
    from autoresearch.application.config import load_settings

    settings = load_settings()
    return ResearchRepository(settings.database_path, settings.artifact_root)


async def test_discovery_resume_reuses_hydration_and_retries_seed_analysis(
    tmp_path, isolated_registry
):
    repository, workflow, seed = smoke._build_loop(tmp_path)
    discovery = workflow.discovery

    original_analyze = discovery.seed_analyzer.analyze

    async def fail_seed_analysis(*args, **kwargs):
        raise RuntimeError("simulated seed analysis failure")

    discovery.seed_analyzer.analyze = fail_seed_analysis
    with pytest.raises(RuntimeError):
        await discovery.discover(str(seed), limit=2)
    discovery.seed_analyzer.analyze = original_analyze

    runs_before = repository.list_runs()
    assert len(runs_before) == 1
    run = runs_before[0]
    assert run.status is RunStatus.FAILED

    hydration_before = repository.checkpoint(run.id, "seed_hydration")
    analysis_before = repository.checkpoint(run.id, "seed_analysis")
    assert hydration_before["status"] == "completed"
    assert hydration_before["attempts"] == 1
    assert hydration_before["payload"]
    assert analysis_before["status"] == "running"
    assert analysis_before["attempts"] == 1
    assert repository.get_seed(run.project_id) is None
    assert repository.list_papers(run.project_id) == []

    outcome = await discovery.resume(run.id)

    hydration_after = repository.checkpoint(run.id, "seed_hydration")
    analysis_after = repository.checkpoint(run.id, "seed_analysis")
    assert hydration_after["status"] == "completed"
    assert hydration_after["attempts"] == 1, "completed hydration must not be re-run"
    assert hydration_after["payload"] == hydration_before["payload"]
    assert analysis_after["status"] == "completed"
    assert analysis_after["attempts"] == 2, "the interrupted stage must be retried once"
    assert repository.get_seed(run.project_id) is not None
    assert outcome.project_id == run.project_id
    assert outcome.run_id == run.id
    assert outcome.retrieved > 0
    assert repository.list_papers(run.project_id)
    assert repository.get_run(run.id).status is RunStatus.COMPLETED

    # No new project and no new discovery run were created.
    assert len(repository.list_runs()) == 1
    assert len(repository.rows("SELECT id FROM projects")) == 1


def test_resume_unknown_run_is_clean_not_found(isolated_cli_env):
    result = _invoke("resume", "--run", "does-not-exist")
    assert result.exit_code == 4, result.output
    assert "Traceback" not in result.output
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "discovery_run_not_found"


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
def test_resume_ineligible_run_is_clean_usage_error(
    tmp_path, isolated_cli_env, terminal
):
    repository = _repository(tmp_path)
    project = repository.create_project("Resume eligibility fixture")
    run = repository.create_run(
        project.id,
        PipelineKind.DISCOVERY,
        {"seed_input": "topic", "limit": 2},
    )
    if terminal == "completed":
        repository.finish_run(run.id)
    else:
        repository.cancel_run(run.id)
    assert repository.get_run(run.id).status is RunStatus(terminal)

    result = _invoke("resume", "--run", run.id)

    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "run_not_resumable"
    # Nothing was resumed or recreated.
    assert repository.get_run(run.id).status is RunStatus(terminal)
    assert len(repository.list_runs()) == 1
    assert len(repository.rows("SELECT id FROM projects")) == 1


def test_resume_delegates_to_discovery_service(tmp_path, isolated_cli_env, monkeypatch):
    project_id = "project-under-test"
    run_id = "0f2a3efc7d774d90bcc1f45b24e8826a"
    run = SimpleNamespace(
        id=run_id,
        project_id=project_id,
        kind=PipelineKind.DISCOVERY,
        status=RunStatus.FAILED,
        input_data={},
    )
    checkpoints = [
        {
            "stage": "seed_hydration",
            "item_key": "main",
            "status": "completed",
            "attempts": 1,
            "payload": {"title": "Preserved hydration"},
        },
        {
            "stage": "seed_analysis",
            "item_key": "main",
            "status": "running",
            "attempts": 1,
            "payload": None,
        },
    ]

    class FakeRepository:
        def get_run(self, candidate):
            return run if candidate == run_id else None

        def checkpoints(self, candidate):
            return [dict(item) for item in checkpoints]

    class FakeDiscovery:
        def __init__(self):
            self.calls: list[str] = []

        async def resume(self, candidate):
            self.calls.append(candidate)
            for item in checkpoints:
                if item["stage"] == "seed_analysis":
                    item["status"] = "completed"
                    item["attempts"] = 2
            return DiscoveryOutcome(
                project_id, run_id, 10, 4, 2, 1, 0, 0, "artifact.jsonl"
            )

    fake_discovery = FakeDiscovery()

    async def close():
        return None

    fake_container = SimpleNamespace(
        repository=FakeRepository(),
        discovery=fake_discovery,
        close=close,
    )
    monkeypatch.setattr(skills, "ApplicationContainer", lambda _settings: fake_container)
    monkeypatch.setattr(
        skills, "_run_lookup", lambda candidate: (project_id, tmp_path / "state.sqlite3")
    )

    result = _invoke("resume", "--run", run_id, "--project", project_id)

    assert result.exit_code == 0, result.output
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["operation"] == "resume"
    assert payload["project_id"] == project_id
    assert payload["run_id"] == run_id
    assert payload["retrieved"] == 4
    assert payload["downloaded"] == 2
    assert payload["unavailable"] == 1
    assert payload["seed_hydration_reused"] is True
    assert payload["seed_analysis"] == {"status": "completed", "attempts": 2}
    assert fake_discovery.calls == [run_id]


def test_resume_rejects_project_mismatch(tmp_path, isolated_cli_env, monkeypatch):
    project_id = "project-under-test"
    run_id = "run-under-test"
    run = SimpleNamespace(
        id=run_id,
        project_id=project_id,
        kind=PipelineKind.DISCOVERY,
        status=RunStatus.FAILED,
        input_data={},
    )

    class FakeRepository:
        def get_run(self, candidate):
            return run if candidate == run_id else None

        def checkpoints(self, candidate):
            return []

    class FakeDiscovery:
        def __init__(self):
            self.calls: list[str] = []

        async def resume(self, candidate):
            self.calls.append(candidate)

    fake_discovery = FakeDiscovery()

    async def close():
        return None

    fake_container = SimpleNamespace(
        repository=FakeRepository(),
        discovery=fake_discovery,
        close=close,
    )
    monkeypatch.setattr(skills, "ApplicationContainer", lambda _settings: fake_container)
    monkeypatch.setattr(
        skills, "_run_lookup", lambda candidate: (project_id, tmp_path / "state.sqlite3")
    )

    result = _invoke("resume", "--run", run_id, "--project", "someone-else")

    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    payload = _payload(result)
    assert payload["error"]["code"] == "run_project_mismatch"
    assert fake_discovery.calls == []
