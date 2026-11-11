"""Phase 1 P0 repair acceptance tests (offline; no live model calls)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from autoresearch.application.services import (
    LiteratureReviewService,
    _review_fanout,
)
from autoresearch.cli import skills
from autoresearch.domain.models import (
    ArtifactProvenance,
    PaperRecord,
    PaperReview,
    PipelineKind,
    RunStatus,
    SeedPaper,
)
from autoresearch.papers.deduplication import DeduplicatedCandidate, SearchProvenance
from autoresearch.papers.metadata_sources import MetadataPaper, SourceSearchResult
from autoresearch.papers.pdf_resolver import PdfResolution
from autoresearch.papers.ranking import CandidateScore
from autoresearch.papers.relevance import SelectionPolicy, select_candidates
from autoresearch.papers.search_pipeline import SearchStrategy, StudySearchPipeline
from autoresearch.domain.models import content_checksum
from autoresearch.providers.capabilities import Capability, ModelRole
from autoresearch.providers.config import ModelAssignment, ProviderConfig
from autoresearch.providers.errors import ErrorKind, ProviderError
from autoresearch.providers.llm.opencode_cli import (
    OpenCodeCLIProvider,
    _kind_for_exit,
)
from autoresearch.providers.registry import ProviderRegistry
from autoresearch.providers.router import ModelRouter
from autoresearch.providers.testing import FakeLLMProvider
from autoresearch.storage import ResearchRepository


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _provider_config(**overrides) -> ProviderConfig:
    values = {
        "name": "reasoning",
        "kind": "opencode_cli",
        "base_url": "",
        "api_key_env": "",
        "capabilities": frozenset({Capability.TEXT, Capability.STRUCTURED_OUTPUT}),
        "cli_executable": "opencode",
        "cli_agent": "plan",
        "cli_timeout": 5.0,
        "max_retries": 0,
        "concurrency": 1,
    }
    values.update(overrides)
    return ProviderConfig(**values)


def _text_event(text: str = "ok") -> bytes:
    return json.dumps(
        {
            "type": "text",
            "sessionID": "ses_test",
            "part": {"type": "text", "text": text},
        }
    ).encode()


class _Tracker:
    def __init__(self) -> None:
        self.spawned = 0
        self.active = 0
        self.max_active = 0


class _ConcurrentProcess:
    def __init__(self, tracker: _Tracker, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.tracker = tracker
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        try:
            await asyncio.sleep(0.01)
            return self.stdout, self.stderr
        finally:
            self.tracker.active -= 1

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


@pytest.mark.anyio
async def test_concurrency_one_never_runs_more_than_one_cli_child(monkeypatch):
    import autoresearch.providers.llm.opencode_cli as module

    tracker = _Tracker()
    monkeypatch.setattr(module, "resolve_executable", lambda configured=None: "opencode")

    async def fake_spawn(*args, **kwargs):
        tracker.spawned += 1
        tracker.active += 1
        tracker.max_active = max(tracker.max_active, tracker.active)
        return _ConcurrentProcess(tracker, stdout=_text_event("ok"))

    monkeypatch.setattr(module, "_create_subprocess", fake_spawn)
    provider = OpenCodeCLIProvider(_provider_config(concurrency=1, max_concurrency=None))

    results = await asyncio.gather(
        *(
            provider.complete(
                model="deepseek/deepseek-flash#high",
                messages=[{"role": "user", "content": f"request {index}"}],
            )
            for index in range(8)
        )
    )

    assert [item.text for item in results] == ["ok"] * 8
    assert tracker.spawned == 8
    assert tracker.max_active == 1


def test_review_fanout_honours_concurrency_one():
    registry = ProviderRegistry()
    registry.register_llm("reasoning", OpenCodeCLIProvider(_provider_config(concurrency=1)))
    assignments = {
        ModelRole.LITERATURE_EXTRACTION: ModelAssignment(
            ModelRole.LITERATURE_EXTRACTION, "reasoning", "model"
        )
    }
    assert _review_fanout(ModelRouter(registry, assignments)) == 1


@pytest.mark.parametrize(
    ("returncode", "stderr", "kind"),
    [
        (3221226505, "", ErrorKind.UNAVAILABLE),
        (-1073740791, "", ErrorKind.UNAVAILABLE),
        (1, "The paging file is too small for this operation to complete. (os error 1455)", ErrorKind.UNAVAILABLE),
        (1, "Free usage limit reached. Connect a provider or subscribe to OpenCode Go.", ErrorKind.QUOTA),
        (1, "QuotaExceeded: insufficient quota", ErrorKind.QUOTA),
        (429, "", ErrorKind.RATE_LIMIT),
        (403, "", ErrorKind.AUTHENTICATION),
        (400, "", ErrorKind.INVALID_REQUEST),
        (124, "", ErrorKind.TIMEOUT),
        (1, "connection refused", ErrorKind.UNAVAILABLE),
        (401, "", ErrorKind.AUTHENTICATION),
        (500, "", ErrorKind.SERVER),
    ],
)
def test_windows_and_http_errors_are_classified(returncode, stderr, kind):
    assert _kind_for_exit(returncode, stderr=stderr) is kind


class _ScriptedProcess:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


@pytest.mark.anyio
async def test_provider_respects_max_retries_and_retries_429(monkeypatch):
    import autoresearch.providers.llm.opencode_cli as module

    outcomes = [
        _ScriptedProcess(429, b"", b"HTTP 429 Too Many Requests"),
        _ScriptedProcess(0, _text_event("recovered"), b""),
    ]
    spawns = {"count": 0}

    async def fake_spawn(*args, **kwargs):
        item = outcomes[spawns["count"]]
        spawns["count"] += 1
        return item

    monkeypatch.setattr(module, "resolve_executable", lambda configured=None: "opencode")
    monkeypatch.setattr(module, "_create_subprocess", fake_spawn)
    provider = OpenCodeCLIProvider(_provider_config(max_retries=1))

    result = await provider.complete(model="model", messages=[{"role": "user", "content": "hi"}])

    assert result.text == "recovered"
    assert spawns["count"] == 2


@pytest.mark.anyio
async def test_quota_failure_is_not_retried(monkeypatch):
    import autoresearch.providers.llm.opencode_cli as module

    spawns = {"count": 0}

    async def fake_spawn(*args, **kwargs):
        spawns["count"] += 1
        return _ScriptedProcess(
            1,
            b"",
            b"Free usage limit reached. Connect a provider or subscribe to OpenCode Go.",
        )

    monkeypatch.setattr(module, "resolve_executable", lambda configured=None: "opencode")
    monkeypatch.setattr(module, "_create_subprocess", fake_spawn)
    provider = OpenCodeCLIProvider(_provider_config(max_retries=5))

    with pytest.raises(ProviderError) as exc:
        await provider.complete(model="model", messages=[{"role": "user", "content": "hi"}])

    assert exc.value.kind is ErrorKind.QUOTA
    assert exc.value.retryable is False
    assert spawns["count"] == 1


@pytest.mark.anyio
async def test_quota_exhaustion_stops_further_review_dispatch(tmp_path):
    repository, project, run = _seed_environment(tmp_path)
    service = _patch_collaborators(LiteratureReviewService(repository, _router()), repository)
    calls = {"count": 0}

    async def review_one(paper, *args, **kwargs):
        calls["count"] += 1
        if paper.id == "paper-0":
            raise ProviderError("quota exhausted", ErrorKind.QUOTA, "reasoning")
        await asyncio.sleep(2)
        return _bundle(paper)

    service._review_one = review_one

    with pytest.raises(RuntimeError) as exc:
        await service._execute_review(
            run.id,
            project.id,
            top_k=5,
            hierarchical=False,
            max_runtime_seconds=None,
            fresh=True,
        )

    assert "review_aborted" in str(exc.value)
    assert calls["count"] < 8


# ---------------------------------------------------------------------------
# Relevance gate
# ---------------------------------------------------------------------------


def _candidate(title: str, abstract: str, *, doi: str | None = None, rank: int = 1):
    paper = MetadataPaper(
        title=title,
        abstract=abstract,
        authors=("Author",),
        year=2026,
        doi=doi,
        source="test",
    )
    provenance = SearchProvenance("test", "strategy", "query", rank, "checksum")
    return DeduplicatedCandidate(paper, (provenance,))


def _score(relevance: float = 0.10) -> CandidateScore:
    return CandidateScore(relevance, relevance, 1.0, 0.0, 0.33, 1.0, 0.35)


def test_off_topic_papers_fail_relevance_filter_and_fewer_than_limit_is_allowed():
    ranked = [
        (_candidate("Causal inference with calibrated models", "Treatment-effect estimation under uncertainty."), _score(0.12)),
        (_candidate("Causal inference under distribution shift", "Robust treatment-effect estimation."), _score(0.11)),
        (_candidate("Quantum metric scaling in flat bands", "Condensed matter localization length."), _score(0.40)),
        (_candidate("Deep learning for lymphoma survival", "Oncology prediction and calibration."), _score(0.38)),
        (_candidate("Language models orchestrate edge-cloud services", "Edge cloud continuum orchestration."), _score(0.36)),
        (_candidate("Calibration for clinical prediction", "Uncertainty in hospital mortality models."), _score(0.30)),
    ]
    policy = SelectionPolicy(
        minimum_relevance=0.06,
        required_terms=("causal inference",),
        excluded_terms=("quantum", "oncology", "edge cloud"),
    )
    result = select_candidates(ranked, policy, limit=10)

    titles = [candidate.paper.title for candidate, _ in result.selected]
    assert titles == [
        "Causal inference with calibrated models",
        "Causal inference under distribution shift",
    ]
    assert len(result.selected) == 2  # fewer than the requested limit
    assert result.included_count == 2
    assert result.excluded_count == 4
    assert len(result.manifest) == 6
    by_title = {item["title"]: item for item in result.manifest}
    assert "out-of-domain term 'quantum'" in by_title["Quantum metric scaling in flat bands"]["reason"]
    assert "out-of-domain term" in by_title["Deep learning for lymphoma survival"]["reason"]
    assert "out-of-domain term" in by_title["Language models orchestrate edge-cloud services"]["reason"]
    assert "no required topic anchor" in by_title["Calibration for clinical prediction"]["reason"]


def test_manifest_records_limit_reached_and_explicit_override():
    ranked = [
        (_candidate("Causal inference method A", "Treatment-effect estimation."), _score(0.20)),
        (_candidate("Causal inference method B", "Robust estimation."), _score(0.19)),
        (_candidate("Quantum flat band", "Quantum metric.", doi="10.5555/allowed"), _score(0.18)),
    ]
    policy = SelectionPolicy(
        minimum_relevance=0.06,
        required_terms=("causal inference",),
        excluded_terms=("quantum",),
        allowed_dois=frozenset({"10.5555/allowed"}),
    )
    result = select_candidates(ranked, policy, limit=2)

    assert [candidate.paper.title for candidate, _ in result.selected] == [
        "Causal inference method A",
        "Causal inference method B",
    ]
    assert "limit 2 reached" in result.manifest[2]["reason"]  # quantum was allowed but over limit
    assert "explicitly allowlisted" not in result.manifest[0]["reason"]


def test_explicit_override_can_include_excluded_domain():
    ranked = [(_candidate("Quantum flat band", "Quantum metric.", doi="10.5555/allowed"), _score())]
    policy = SelectionPolicy(minimum_relevance=0.0, allowed_dois=frozenset({"10.5555/allowed"}))
    result = select_candidates(ranked, policy, limit=5)
    assert len(result.selected) == 1
    assert "policy override" in result.manifest[0]["reason"]


class _StaticSource:
    name = "static"

    async def search(self, query: str, *, limit: int, since_year: int | None = None):
        papers = tuple(
            MetadataPaper(
                title=title,
                abstract=abstract,
                authors=("Author",),
                year=2026,
                doi=doi,
                source=self.name,
            )
            for title, abstract, doi in (
                (
                    "Causal inference with calibrated models",
                    "Treatment-effect estimation under uncertainty.",
                    "10.5555/causal-1",
                ),
                (
                    "Causal inference under distribution shift",
                    "Robust treatment-effect estimation.",
                    "10.5555/causal-2",
                ),
                (
                    "Quantum metric scaling in flat bands",
                    "Condensed matter localization length.",
                    "10.5555/quantum",
                ),
            )
        )
        return SourceSearchResult(self.name, query, papers[:limit], content_checksum(query))

    async def close(self) -> None:
        return None


class _NoPdfResolver:
    async def resolve(self, paper):
        return PdfResolution(False, None, "test", None, None, "no pdf in unit test")

    async def download(self, resolution, target):
        raise AssertionError("download must not be called")

    async def close(self) -> None:
        return None


@pytest.mark.anyio
async def test_pipeline_selection_manifest_and_fewer_than_limit(tmp_path):
    repository, project, run = _seed_environment(tmp_path, count=0)
    pipeline = StudySearchPipeline(
        repository,
        [_StaticSource()],
        _NoPdfResolver(),
        tmp_path / "downloads",
        citation_expansion=False,
        selection_policy=SelectionPolicy(
            minimum_relevance=0.0,
            required_terms=("causal inference",),
            excluded_terms=("quantum",),
        ),
    )
    seed = repository.get_seed(project.id)
    result = await pipeline.discover(
        project_id=project.id,
        run_id=run.id,
        seed=seed,
        strategies=(SearchStrategy("topic", "causal inference"),),
        limit=5,
    )

    assert {paper.title for paper in result.papers} == {
        "Causal inference with calibrated models",
        "Causal inference under distribution shift",
    }
    assert len(result.papers) == 2
    assert len(result.selection_manifest) == 3
    assert result.selection_policy is not None
    assert result.selection_policy["required_terms"] == ["causal inference"]
    quantum = next(item for item in result.selection_manifest if "Quantum" in item["title"])
    assert quantum["included"] is False
    assert "out-of-domain term" in quantum["reason"]


# ---------------------------------------------------------------------------
# Selective review recovery
# ---------------------------------------------------------------------------


def _seed_environment(tmp_path, count=8, run_input=None):
    repository = ResearchRepository(tmp_path / "state.sqlite3", tmp_path / "artifacts")
    project = repository.create_project("phase1")
    run = repository.create_run(project.id, PipelineKind.REVIEW, run_input or {"top_k": 5})
    provenance = ArtifactProvenance.create(
        project_id=project.id,
        run_id=run.id,
        source_evidence=(),
        provider="test",
        model="test",
        prompt_version="v1",
        content={},
    )
    repository.save_seed(
        SeedPaper(
            id=uuid4().hex,
            project_id=project.id,
            run_id=run.id,
            input_type="topic",
            input_value="seed",
            title="Seed",
            text="seed text",
            topic="causal inference",
            research_question="q",
            checksum="c",
            provenance=provenance,
        )
    )
    for index in range(count):
        repository.save_paper(
            PaperRecord(
                id=f"paper-{index}",
                project_id=project.id,
                run_id=run.id,
                title=f"Paper {index}",
                abstract=f"abstract {index}",
                authors=("A",),
                year=2025,
                publication_date=None,
                doi=None,
                arxiv_id=None,
                url=None,
                source_names=("test",),
            )
        )
    return repository, project, run


def _router():
    registry = ProviderRegistry()
    registry.register_llm("fake", FakeLLMProvider(responder=lambda messages: "{}"))
    assignments = {
        ModelRole.LITERATURE_EXTRACTION: ModelAssignment(
            ModelRole.LITERATURE_EXTRACTION, "fake", "fake-model"
        )
    }
    return ModelRouter(registry, assignments)


def _bundle(paper, *, run_id=None):
    return SimpleNamespace(
        review=SimpleNamespace(
            paper_id=paper.id,
            run_id=run_id or paper.run_id,
            evidence_claim_ids=(),
        ),
        claims=(),
    )


def _patch_collaborators(service, repository):
    async def gap(**kwargs):
        return SimpleNamespace(id="gap-1", provenance=SimpleNamespace(checksum="c"))

    async def rank(**kwargs):
        return [
            SimpleNamespace(paper_id=paper.id, total=1.0)
            for paper in kwargs["papers"]
        ]

    def writer(**kwargs):
        return SimpleNamespace(path="review.json"), SimpleNamespace(path="review.md")

    service.gap_analyzer = SimpleNamespace(analyze=gap)
    service.ranker = SimpleNamespace(rank=rank)
    service.writer = SimpleNamespace(write=writer)
    repository.save_score = lambda score: None
    repository.save_gap = lambda gap: None
    return service


@pytest.mark.anyio
async def test_review_allowlist_selects_only_specified_papers(tmp_path):
    repository, project, run = _seed_environment(tmp_path)
    service = _patch_collaborators(LiteratureReviewService(repository, _router()), repository)
    called: list[str] = []

    async def review_one(paper, *args, **kwargs):
        called.append(paper.id)
        return _bundle(paper)

    service._review_one = review_one

    outcome = await service._execute_review(
        run.id,
        project.id,
        top_k=2,
        hierarchical=False,
        max_runtime_seconds=None,
        fresh=True,
        paper_ids=("paper-1", "paper-3"),
    )

    assert called == ["paper-1", "paper-3"]
    assert outcome.reviewed == 2
    assert set(outcome.top_paper_ids) <= {"paper-1", "paper-3"}


@pytest.mark.anyio
async def test_completed_review_is_reused_without_model_call(tmp_path):
    repository, project, run = _seed_environment(tmp_path, count=3)
    service = LiteratureReviewService(repository, _router())
    provenance = ArtifactProvenance.create(
        project_id=project.id,
        run_id=run.id,
        source_evidence=("claim-1",),
        provider="test",
        model="test",
        prompt_version="paper-review-v2-ensemble-v1",
        content={},
    )
    existing = PaperReview(
        "review-1",
        project.id,
        run.id,
        "paper-0",
        "problem",
        "question",
        "method",
        (),
        "protocol",
        (),
        (),
        (),
        (),
        ("claim-1",),
        0.5,
        provenance,
    )
    repository.save_review(existing)
    repository.complete_checkpoint(run.id, "paper_review", "paper-0", payload={"review_id": existing.id})

    calls = {"count": 0}

    async def must_not_call(*args, **kwargs):
        calls["count"] += 1
        raise AssertionError("completed review was re-extracted")

    service.extractor = SimpleNamespace(
        extract_ensemble=must_not_call,
        extract=must_not_call,
    )

    paper = repository.get_paper("paper-0")
    bundle = await service._review_one(paper, run.id, hierarchical=False, allow_reuse=True)

    assert bundle.review.id == "review-1"
    assert calls["count"] == 0


@pytest.mark.anyio
async def test_scoped_resume_retries_only_selected_failed_papers(tmp_path):
    repository, project, run = _seed_environment(tmp_path, count=6, run_input={"top_k": 2})
    for index in range(4):
        repository.complete_checkpoint(
            run.id, "paper_review", f"paper-{index}", payload={"review_id": f"old-{index}"}
        )
    for index in range(4, 6):
        repository.fail_checkpoint(
            run.id, "paper_review", f"paper-{index}", error="old failure"
        )
    service = _patch_collaborators(LiteratureReviewService(repository, _router()), repository)
    called: list[str] = []

    async def review_one(paper, *args, **kwargs):
        called.append(paper.id)
        return _bundle(paper)

    service._review_one = review_one

    outcome = await service.resume(run.id, paper_ids=("paper-4", "paper-5"))

    assert called == ["paper-4", "paper-5"]
    assert outcome.reviewed == 2
    assert repository.get_run(run.id).status is RunStatus.COMPLETED


def test_review_resume_cli_delegates_with_allowlist(tmp_path, monkeypatch):
    runner = CliRunner()
    project_id = "project-1"
    run_id = "run-1"
    run = SimpleNamespace(
        id=run_id,
        project_id=project_id,
        kind=PipelineKind.REVIEW,
        status=RunStatus.FAILED,
        input_data={"top_k": 2},
    )
    calls = {}

    class FakeRepository:
        def get_run(self, candidate):
            return run if candidate == run_id else None

    class FakeReview:
        async def resume(self, candidate, *, paper_ids=None):
            calls["run"] = candidate
            calls["paper_ids"] = paper_ids
            return SimpleNamespace(
                project_id=project_id,
                run_id=run_id,
                reviewed=2,
                reused=0,
                failed=0,
                top_paper_ids=("paper-4", "paper-5"),
                gap=SimpleNamespace(
                    id="gap-1",
                    statement="s",
                    classification=SimpleNamespace(value="hypothesis"),
                    confidence=0.5,
                    supporting_paper_ids=(),
                    supporting_claim_ids=(),
                    counter_evidence_claim_ids=(),
                    why_unsolved="w",
                    missing_experiment="m",
                    falsification_test="f",
                ),
                json_report="report.json",
                markdown_report="report.md",
                review_corpus_path=None,
                citation_contexts_path=None,
                review_audit_path=None,
                publication_gaps=(),
            )

    async def close():
        return None

    fake_container = SimpleNamespace(
        repository=FakeRepository(),
        review=FakeReview(),
        close=close,
    )
    monkeypatch.setattr(skills, "ApplicationContainer", lambda _settings: fake_container)
    monkeypatch.setattr(skills, "_run_lookup", lambda candidate: (project_id, tmp_path / "state.sqlite3"))
    monkeypatch.setattr(
        "autoresearch.review.matrix.project_keywords",
        lambda repository, *, project_id, run_id: [],
    )

    result = runner.invoke(
        skills.skill_app,
        ["review-resume", "--run", run_id, "--paper-ids", "paper-4,paper-5"],
    )

    assert result.exit_code == 0, result.output
    assert calls == {"run": run_id, "paper_ids": ("paper-4", "paper-5")}
    payload = json.loads(result.output)
    assert payload["operation"] == "review_resume"
    assert payload["reviewed"] == 2
