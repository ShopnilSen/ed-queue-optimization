"""Phase 2 integration and review-quality regression tests (offline)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from autoresearch.application.config import load_settings, resolve_settings
from autoresearch.application.services import LiteratureReviewService, _provider_provenance
from autoresearch.domain.models import (
    ArtifactProvenance,
    DownloadRecord,
    DownloadStatus,
    EvidenceClaim,
    PaperRecord,
    PaperReview,
    PipelineKind,
    SeedPaper,
    content_checksum,
)
from autoresearch.papers.deduplication import SearchProvenance
from autoresearch.providers.capabilities import Capability, ModelRole
from autoresearch.providers.config import ModelAssignment
from autoresearch.providers.factory import (
    build_router,
    validate_router_identity,
)
from autoresearch.providers.registry import ProviderRegistry
from autoresearch.providers.router import ModelRouter
from autoresearch.providers.testing import FakeLLMProvider
from autoresearch.review.conflicts import build_review_audit
from autoresearch.review.extraction import (
    PaperReviewExtractor,
    ReviewBundle,
    _confidence,
    _field_evidence,
    _validate_review_payload,
)
from autoresearch.review.matrix import build_evidence_matrix
from autoresearch.review.synthesis import ReviewSynthesisWriter
from autoresearch.storage import ResearchRepository


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _prov() -> ArtifactProvenance:
    return ArtifactProvenance.create(
        project_id="p",
        run_id="r",
        source_evidence=(),
        provider="test",
        model="test",
        prompt_version="v1",
        content={},
    )


def _paper(pid: str, title: str = "Paper", *, project_id="p", run_id="r") -> PaperRecord:
    return PaperRecord(
        pid, project_id, run_id, title, f"abstract {pid}", ("Author",), 2026, None,
        None, None, None, ("test",), None, False, 0, "Venue",
        content_checksum(pid),
    )


def _review(pid: str, *, project_id="p", run_id="r", evidence_level="full_text") -> PaperReview:
    return PaperReview(
        f"review-{pid}", project_id, run_id, pid,
        "Problem", "Question", "Method", ("Dataset",), "Protocol",
        ("Result",), (), ("Limitation",), ("Failure",), (f"claim-{pid}",), 0.8, _prov(),
        evidence_level, {"method": (f"claim-{pid}",), "results": (), "reviewer_inference_fields": ("results",)},
    )


def _claim(pid: str, claim_id: str, *, project_id="p", run_id="r", evidence_level="full_text", page=2, claim_type="result") -> EvidenceClaim:
    return EvidenceClaim(
        claim_id, project_id, run_id, pid, claim_type,
        f"Statement {claim_id}", f"verbatim excerpt {claim_id}", page, "Results", 0.8,
        _prov(), evidence_level,
    )


def _seed_repo(tmp_path):
    repository = ResearchRepository(tmp_path / "state.sqlite3", tmp_path / "artifacts")
    project = repository.create_project("phase2")
    run = repository.create_run(project.id, PipelineKind.REVIEW, {"top_k": 2})
    repository.save_seed(SeedPaper(
        uuid4().hex, project.id, run.id, "topic", "seed", "Seed", "seed",
        "topic", "question", "checksum", _prov(),
    ))
    for pid in ("p1", "p2", "off"):
        repository.save_paper(_paper(pid, project_id=project.id, run_id=run.id))
        repository.save_review(_review(pid, project_id=project.id, run_id=run.id))
        repository.save_claim(_claim(pid, f"claim-{pid}", project_id=project.id, run_id=run.id))
    repository.save_download(DownloadRecord(
        "d1", project.id, run.id, "p1", DownloadStatus.DOWNLOADED, "test",
        None, None, None, "/tmp/p1.pdf", None, "c",
    ))
    repository.save_download(DownloadRecord(
        "d2", project.id, run.id, "p2", DownloadStatus.UNAVAILABLE, "test",
        None, None, None, None, "no pdf", None,
    ))
    repository.save_download(DownloadRecord(
        "doff", project.id, run.id, "off", DownloadStatus.DOWNLOADED, "test",
        None, None, None, "/tmp/off.pdf", None, "c",
    ))
    repository.record_review_failure(project_id=project.id, run_id=run.id, paper_id="p2", reason="failure")
    repository.record_review_failure(project_id=project.id, run_id=run.id, paper_id="off", reason="failure")
    return repository, project, run


# ---------------------------------------------------------------------------
# F1: project-specific provider configuration
# ---------------------------------------------------------------------------


def _write_project_config(project_root):
    config = project_root / ".autoresearch" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """
[providers.opencode]
kind = "opencode_cli"
cli_executable = "opencode"
cli_agent = "plan"
concurrency = 1
capabilities = ["text_generation", "structured_output", "reasoning"]

[models.literature_extraction]
provider = "opencode"
model = "deepseek/deepseek-flash#high"

[models.research_reasoning]
provider = "opencode"
model = "deepseek/deepseek-flash#high"

[models.scientific_critic]
provider = "opencode"
model = "deepseek/deepseek-flash#high"
""".strip(),
        encoding="utf-8",
    )
    return config


def test_resolve_settings_anchors_project_provider_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORESEARCH_REGISTRY_PATH", str(tmp_path / "registry.json"))
    project_root = tmp_path / "project"
    db = project_root / ".autoresearch" / "data" / "state.sqlite3"
    config = _write_project_config(project_root)
    repository = ResearchRepository(db, project_root / ".autoresearch" / "artifacts")
    project = repository.create_project("anchored")

    settings = resolve_settings(project.id)

    assert settings.database_path == db.resolve()
    assert settings.provider_config == config.resolve()
    assert settings.provider_config_required is True
    assert settings.auto_provider is False


def test_project_missing_config_fails_without_auto_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTORESEARCH_REGISTRY_PATH", str(tmp_path / "registry.json"))
    project_root = tmp_path / "project"
    db = project_root / ".autoresearch" / "data" / "state.sqlite3"
    repository = ResearchRepository(db, project_root / ".autoresearch" / "artifacts")
    project = repository.create_project("missing-config")
    settings = resolve_settings(project.id)

    with pytest.raises(FileNotFoundError):
        build_router(
            settings.provider_config,
            require_config=True,
        )


def test_invalid_project_config_fails_closed(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[providers.opencode\nkind = 'broken'", encoding="utf-8")
    with pytest.raises(Exception):
        build_router(config, require_config=True)


def test_valid_project_config_builds_verified_opencode_identity(tmp_path):
    config = _write_project_config(tmp_path / "project")
    router = build_router(config, require_config=True)
    assignment = router.assignments[ModelRole.LITERATURE_EXTRACTION]
    provider = router.registry.get(assignment.provider)
    assert assignment.provider == "opencode"
    assert assignment.model == "deepseek/deepseek-flash#high"
    assert type(provider).__name__ == "OpenCodeCLIProvider"
    assert provider.config.cli_agent == "plan"
    assert provider.config.concurrency == 1


# ---------------------------------------------------------------------------
# F2: provider identity rejection and provenance
# ---------------------------------------------------------------------------


def test_provider_identity_mismatch_is_rejected_before_dispatch():
    document = {
        "providers": {"opencode": {"kind": "opencode_cli", "cli_agent": "plan"}},
        "models": {
            "literature_extraction": {
                "provider": "opencode",
                "model": "deepseek/deepseek-flash#high",
            }
        },
    }
    registry = ProviderRegistry()
    registry.register_llm("reasoning", FakeLLMProvider())
    router = ModelRouter(
        registry,
        {
            ModelRole.LITERATURE_EXTRACTION: ModelAssignment(
                ModelRole.LITERATURE_EXTRACTION, "reasoning", "deepseek-chat"
            )
        },
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_router_identity(document, router)


@pytest.mark.anyio
async def test_review_run_records_effective_provider_identity(tmp_path, monkeypatch):
    repository = ResearchRepository(tmp_path / "state.sqlite3", tmp_path / "artifacts")
    project = repository.create_project("provenance")
    seed_run = repository.create_run(project.id, PipelineKind.REVIEW, {})
    repository.save_seed(SeedPaper(
        uuid4().hex, project.id, seed_run.id, "topic", "seed", "Seed", "seed",
        "topic", "question", "c", _prov(),
    ))
    repository.save_paper(_paper("p1", project_id=project.id, run_id=seed_run.id))
    registry = ProviderRegistry()
    registry.register_llm("fake", FakeLLMProvider())
    router = ModelRouter(
        registry,
        {
            ModelRole.LITERATURE_EXTRACTION: ModelAssignment(
                ModelRole.LITERATURE_EXTRACTION, "fake", "fake-model"
            )
        },
    )
    service = LiteratureReviewService(repository, router)

    async def fake_execute(*args, **kwargs):
        return SimpleNamespace()

    service._execute = fake_execute
    await service.review(project.id, top_k=1)
    run = repository.list_runs(project_id=project.id)[0]
    identity = run.input_data["provider_identity"]
    assert identity["provider"] == "fake"
    assert identity["model"] == "fake-model"
    assert identity["provider_class"] == "FakeLLMProvider"
    assert _provider_provenance(router, ModelRole.LITERATURE_EXTRACTION) == identity


# ---------------------------------------------------------------------------
# F4: evidence artifact scoping
# ---------------------------------------------------------------------------


def test_evidence_matrix_and_audit_are_scoped(tmp_path):
    repository, project, run = _seed_repo(tmp_path)
    matrix = build_evidence_matrix(
        repository, project_id=project.id, run_id=run.id, paper_ids=["p1", "p2"]
    )
    assert [row["paper_id"] for row in matrix["matrix"]] == ["p1", "p2"]
    assert matrix["scope"]["paper_ids"] == ["p1", "p2"]
    assert matrix["retrieval_coverage"]["review_failures_this_run"] == 1

    audit = build_review_audit(
        repository, project_id=project.id, run_id=run.id, paper_ids=["p1", "p2"]
    )
    assert audit["paper_ids"] == ["p1", "p2"]
    assert audit["claims"]["reviewed_papers"] == 2
    assert audit["claims"]["total"] == 2
    assert audit["claims"]["by_evidence_level"] == {"full_text": 2}
    missing = {item["code"]: item for item in audit["missing_evidence"]}
    assert missing["abstract_only_papers"]["paper_ids"] == ["p2"]
    assert missing["review_failures"]["paper_ids"] == ["p2"]


def test_synthesis_coverage_uses_selected_scope(tmp_path):
    repository, project, run = _seed_repo(tmp_path)
    papers = [repository.get_paper("p1"), repository.get_paper("p2")]
    reviews = [repository.review_for_paper(run.id, "p1"), repository.review_for_paper(run.id, "p2")]
    claims = [repository.claims(project.id, ids=(f"claim-p1",))[0], repository.claims(project.id, ids=(f"claim-p2",))[0]]
    gap = SimpleNamespace(
        provenance=_prov(), statement="Gap", classification=SimpleNamespace(value="hypothesis"),
        supporting_paper_ids=(), supporting_claim_ids=(), counter_evidence_claim_ids=(),
        why_unsolved="why", missing_experiment="missing", falsification_test="falsify",
        confidence=0.5,
    )
    artifacts = ReviewSynthesisWriter(repository).write(
        project_id=project.id, run_id=run.id, papers=papers, reviews=reviews,
        scores=[], claims=claims, gap=gap,
    )
    payload = json.loads(open(artifacts[0].path, encoding="utf-8").read())
    assert payload["coverage"]["papers_ingested"] == 2
    assert payload["coverage"]["full_text_papers"] == 1
    assert payload["coverage"]["abstract_only_papers"] == 1
    assert payload["coverage"]["failures"] == 1


# ---------------------------------------------------------------------------
# F5: confidence and evidence-level validation
# ---------------------------------------------------------------------------

def _valid_payload(**overrides):
    payload = {
        "problem": "Problem",
        "research_question": "Question",
        "method": "Method",
        "datasets": ["Dataset"],
        "experimental_protocol": "Protocol",
        "results": ["Result"],
        "ablations": [],
        "limitations": ["Limitation"],
        "failure_modes": ["Failure"],
        "confidence": 0.8,
        "evidence_spans": [{
            "claim_type": "result", "statement": "Result",
            "excerpt": "Body text.", "page": 2, "section": "Results",
            "confidence": 0.9,
        }],
    }
    payload.update(overrides)
    return payload


def test_missing_confidence_is_rejected():
    with pytest.raises(ValueError, match="confidence"):
        _confidence(None, field="review.confidence")
    payload = _valid_payload()
    payload.pop("confidence")
    with pytest.raises(ValueError, match="confidence"):
        _validate_review_payload(payload, paper_title="P")


def test_missing_required_structured_field_is_rejected():
    payload = _valid_payload()
    payload.pop("results")
    with pytest.raises(ValueError, match="results"):
        _validate_review_payload(payload, paper_title="P")


@pytest.mark.anyio
async def test_extraction_marks_full_text_and_abstract_evidence():
    registry = ProviderRegistry()
    registry.register_llm("fake", FakeLLMProvider(responder=lambda messages: _valid_payload()))
    router = ModelRouter(
        registry,
        {
            ModelRole.LITERATURE_EXTRACTION: ModelAssignment(
                ModelRole.LITERATURE_EXTRACTION, "fake", "fake-model"
            )
        },
    )
    extractor = PaperReviewExtractor(router)
    paper = _paper("paper-1")

    full = await extractor._extract_source(
        paper, run_id="r", source_text="Body text.", prompt_version="v1",
        operation="paper_review", evidence_level="full_text",
    )
    assert full.review.evidence_level == "full_text"
    assert all(claim.evidence_level == "full_text" for claim in full.claims)

    abstract = await extractor._extract_source(
        paper, run_id="r", source_text="Body text.", prompt_version="v1",
        operation="paper_review", evidence_level="abstract",
    )
    assert abstract.review.evidence_level == "abstract"
    assert all(claim.evidence_level == "abstract" for claim in abstract.claims)

    field_map = _field_evidence(
        {"method": "Method", "results": "Result"}, full.claims
    )
    assert field_map["results"]
    assert "method" in field_map["reviewer_inference_fields"]
    assert "results" not in field_map["reviewer_inference_fields"]


# ---------------------------------------------------------------------------
# F6: hierarchical merge consistency
# ---------------------------------------------------------------------------


def _bundle(review, claims):
    return ReviewBundle(review, tuple(claims))


def test_front_matter_only_leg_cannot_override_body_evidence():
    paper = _paper("paper-1")
    meta_review = PaperReview(
        "meta", "p", "r", paper.id,
        "The supplied source is only a front-matter page with no methods, results, or data.",
        "Question?", "No methods are available.", (), "No protocol.", (), (), ("Source is a fragment of the reference list only.",), ("Cannot assess methodological rigor.",),
        ("meta-claim",), 0.0, _prov(), "abstract", {},
    )
    body_review = PaperReview(
        "body", "p", "r", paper.id,
        "The paper evaluates calibrated models under temporal shift.",
        "Does calibration hold?", "A multilayer perceptron with conformal deferral.", ("PhysioNet",), "Train/calibrate/test.", ("AUROC improved.",), (), ("Single cohort.",), ("Coverage may degrade.",),
        ("body-claim",), 0.8, _prov(), "full_text", {"method": ("body-claim",)},
    )
    meta = _claim(paper.id, "meta-claim", evidence_level="abstract", page=39, claim_type="scope")
    body = _claim(paper.id, "body-claim", evidence_level="full_text", page=3, claim_type="result")
    extractor = PaperReviewExtractor(
        ModelRouter(ProviderRegistry(), {})
    )

    merged = extractor._merge_bundles(
        paper, "r", [_bundle(meta_review, [meta]), _bundle(body_review, [body])],
        prompt_version="test",
    )

    assert merged.review.problem == body_review.problem
    assert merged.review.method == body_review.method
    assert "front-matter" not in merged.review.problem.lower()
    assert "reference list only" not in " ".join(merged.review.limitations).lower()
    assert merged.review.evidence_level == "full_text"
    assert "results" not in merged.review.field_evidence.get("reviewer_inference_fields", ())
