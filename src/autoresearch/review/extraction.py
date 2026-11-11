"""Extract evidence, rigor, and reproducibility in one source-grounded pass."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from autoresearch.domain.models import ArtifactProvenance, EvidenceClaim, PaperRecord, PaperReview
from autoresearch.ingestion.chunking import DocumentChunk
from autoresearch.providers.capabilities import ModelRole
from autoresearch.providers.router import ModelRouter

REVIEW_PROMPT_VERSION = "paper-review-v2"
HIERARCHICAL_REVIEW_PROMPT_VERSION = "paper-review-hierarchical-v1"
_STRING_ARRAY = {"type": "array", "items": {"type": "string"}}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "problem": {"type": "string"},
        "research_question": {"type": "string"},
        "method": {"type": "string"},
        "datasets": _STRING_ARRAY,
        "experimental_protocol": {"type": "string"},
        "results": _STRING_ARRAY,
        "ablations": _STRING_ARRAY,
        "limitations": _STRING_ARRAY,
        "failure_modes": _STRING_ARRAY,
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_spans": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim_type": {"type": "string"},
                    "statement": {"type": "string"},
                    "excerpt": {"type": "string"},
                    "page": {"type": ["integer", "null"]},
                    "section": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["claim_type", "statement", "excerpt", "page", "section", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["problem", "research_question", "method", "datasets", "experimental_protocol", "results", "ablations", "limitations", "failure_modes", "evidence_spans", "confidence"],
    "additionalProperties": False,
}

# Shared verbatim-quoting rule appended to every perspective prompt. Models
# routinely paraphrase otherwise; evidence claims are rejected unless the
# excerpt appears verbatim in the source, so compliance is mandatory.
_VERBATIM_RULE = (
    "Every evidence item MUST quote a single contiguous excerpt copied "
    "character-for-character from the SOURCE text. Do not paraphrase, shorten, "
    "or rephrase the excerpt. If you cannot find an exact verbatim quote in the "
    "SOURCE, OMIT that evidence span entirely rather than approximating it."
)

_RIGOR_SYSTEM_PROMPT = (
    "Act as a rigorous scientific reviewer. Every evidence item must quote an "
    "exact contiguous excerpt from the supplied source. Do not infer missing "
    "results. Assess methodological rigor, reproducibility, and the claimed "
    "contribution together in one concise review. Distinguish measured results "
    "from author claims and missing information. " + _VERBATIM_RULE
)

_EXTRACTION_USER_TEMPLATE = (
    "Prompt version: {prompt_version}\n"
    "Paper ID: {paper_id}\n"
    "Title: {paper_title}\n\n"
    "SOURCE:\n{source}\n\n"
    "Return one JSON object with problem, research_question, method, "
    "datasets[], experimental_protocol, results[], ablations[], limitations[], "
    "failure_modes[], confidence, and evidence_spans[]. Each evidence span "
    "requires claim_type, statement, excerpt, page, section, confidence.\n\n"
    "CRITICAL: every excerpt must be copied word-for-word from the SOURCE "
    "above, including mathematical symbols. Omit any span you cannot quote "
    "verbatim. Fewer verified spans are better than paraphrased ones.\n\n"
    "OUTPUT BUDGET (hard limits — the response is machine-verified and "
    "overlong responses fail): evidence_spans MUST contain AT MOST 10 items, "
    "sorted by importance; each excerpt MUST be at most 50 words; results[], "
    "ablations[], limitations[], failure_modes[], datasets[] MUST each contain "
    "at most 8 items; every other field at most 3 sentences. Select only the "
    "most decision-relevant evidence."
)


@dataclass(frozen=True)
class ReviewBundle:
    review: PaperReview
    claims: tuple[EvidenceClaim, ...]


class PaperReviewExtractor:
    def __init__(self, router: ModelRouter) -> None:
        self.router = router

    async def extract(self, paper: PaperRecord, *, run_id: str, chunks: tuple[DocumentChunk, ...], fallback_text: str = "", hierarchical: bool = False) -> ReviewBundle:
        chunk_text = "\n\n".join(f"[PAGE {chunk.page_start}; SECTION {chunk.section}]\n{chunk.text}" for chunk in chunks)
        source_text = chunk_text
        evidence_level = "full_text" if chunk_text.strip() else "abstract"
        if not source_text.strip():
            source_text = fallback_text or paper.abstract
        if not source_text.strip():
            raise ValueError(f"Paper '{paper.title}' has neither extractable PDF text nor abstract metadata.")

        if not hierarchical or len(source_text) <= 40_000:
            return await self._extract_source(
                paper, run_id=run_id, source_text=source_text,
                prompt_version=HIERARCHICAL_REVIEW_PROMPT_VERSION if hierarchical else f"{REVIEW_PROMPT_VERSION}-integrated-v1", operation="paper_review",
                evidence_level=evidence_level,
            )

        groups = _source_groups(chunks, max_characters=35_000)
        if not groups:
            groups = (source_text,)
        # Groups are independent sections of the same paper: review them
        # concurrently (globally bounded by the provider's concurrency gate)
        # and merge the survivors — one group failing must not discard the
        # evidence the other groups produced, nor serialize them behind it.
        leaves_results = await asyncio.gather(
            *(self._extract_source(
                paper, run_id=run_id, source_text=group,
                prompt_version=HIERARCHICAL_REVIEW_PROMPT_VERSION, operation="paper_review_section",
                evidence_level="full_text",
            ) for group in groups),
            return_exceptions=True,
        )
        leaves = [result for result in leaves_results if isinstance(result, ReviewBundle)]
        if not leaves:
            first_error = next((result for result in leaves_results if isinstance(result, Exception)), None)
            raise first_error or ValueError(f"Paper '{paper.title}' yielded no evidence-backed review.")
        return self._merge_bundles(paper, run_id, leaves, prompt_version=HIERARCHICAL_REVIEW_PROMPT_VERSION)

    async def extract_ensemble(
        self,
        paper: PaperRecord,
        *,
        run_id: str,
        chunks: tuple[DocumentChunk, ...],
        fallback_text: str = "",
        hierarchical: bool = False,
    ) -> ReviewBundle:
        """Two independent parallel extraction legs merged into one review.

        The second leg sees the source in reversed chunk order — a diversity
        signal for evidence-span coverage without any prompt/API change. Both
        legs run concurrently, so the ensemble costs the wall-clock of one
        leg. A single failing leg degrades to the survivor; only a total
        failure raises.
        """
        ordered = tuple(chunks)
        reversed_chunks = tuple(reversed(ordered))
        results = await asyncio.gather(
            self.extract(paper, run_id=run_id, chunks=ordered, fallback_text=fallback_text, hierarchical=hierarchical),
            self.extract(paper, run_id=run_id, chunks=reversed_chunks, fallback_text=fallback_text, hierarchical=hierarchical),
            return_exceptions=True,
        )
        bundles = [result for result in results if isinstance(result, ReviewBundle)]
        if not bundles:
            first_error = next((result for result in results if isinstance(result, Exception)), None)
            raise first_error or ValueError(f"Paper '{paper.title}' ensemble yielded no evidence-backed review.")
        if len(bundles) == 1:
            return bundles[0]
        return self._merge_bundles(
            paper, run_id, bundles,
            prompt_version=f"{REVIEW_PROMPT_VERSION}-ensemble-v1",
        )

    async def _extract_source(self, paper: PaperRecord, *, run_id: str, source_text: str, prompt_version: str, operation: str, system_prompt: str | None = None, evidence_level: str = "unknown") -> ReviewBundle:
        system = system_prompt or _RIGOR_SYSTEM_PROMPT
        payload = await self.router.structured_output(
            ModelRole.LITERATURE_EXTRACTION,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": _EXTRACTION_USER_TEMPLATE.format(paper_id=paper.id, paper_title=paper.title, prompt_version=prompt_version, source=source_text[:100_000])},
            ],
            schema={"name": "paper_review", "schema": REVIEW_SCHEMA},
            project_id=paper.project_id,
            run_id=run_id,
            operation=operation,
        )
        _validate_review_payload(payload, paper_title=paper.title)
        provider, model = self.router.identity(ModelRole.LITERATURE_EXTRACTION)
        claims = self._claims(
            payload.get("evidence_spans"), paper, run_id, source_text,
            provider, model, prompt_version, evidence_level=evidence_level,
        )
        if not claims:
            raise ValueError(f"Paper '{paper.title}' review did not contain a verifiable source excerpt.")
        review_content = {
            "problem": _required(payload, "problem"), "research_question": _required(payload, "research_question"),
            "method": _required(payload, "method"), "datasets": _strings(payload.get("datasets")),
            "experimental_protocol": _required(payload, "experimental_protocol"), "results": _strings(payload.get("results")),
            "ablations": _strings(payload.get("ablations")), "limitations": _strings(payload.get("limitations")),
            "failure_modes": _strings(payload.get("failure_modes")), "evidence_claim_ids": tuple(claim.id for claim in claims),
            "confidence": _confidence(payload.get("confidence"), field="review.confidence"),
        }
        field_evidence = _field_evidence(review_content, claims)
        provenance = ArtifactProvenance.create(project_id=paper.project_id, run_id=run_id, source_evidence=tuple(claim.id for claim in claims), provider=provider, model=model, prompt_version=prompt_version, content={**review_content, "evidence_level": evidence_level, "field_evidence": field_evidence})
        review = PaperReview(uuid4().hex, paper.project_id, run_id, paper.id, review_content["problem"], review_content["research_question"], review_content["method"], review_content["datasets"], review_content["experimental_protocol"], review_content["results"], review_content["ablations"], review_content["limitations"], review_content["failure_modes"], review_content["evidence_claim_ids"], review_content["confidence"], provenance, evidence_level, field_evidence)
        return ReviewBundle(review, claims)

    def _merge_bundles(self, paper: PaperRecord, run_id: str, bundles: list[ReviewBundle], *, prompt_version: str) -> ReviewBundle:
        all_claims = [claim for b in bundles for claim in b.claims]
        # A front-matter/reference-only chunk is not evidence that the paper
        # has no methods/results. Prefer bundles that contain body claims for
        # narrative fields, while retaining the full merged claim set for
        # provenance.
        substantive = [b for b in bundles if _has_body_claim(b)] or list(bundles)
        reviews = [b.review for b in substantive]
        provider = reviews[0].provenance.provider
        model = reviews[0].provenance.model

        merged_claims: list[EvidenceClaim] = []
        seen_evidence: set[tuple[str, str, str]] = set()
        for claim in all_claims:
            key = (_normalize(claim.statement), _normalize(claim.excerpt), claim.section or "")
            if key in seen_evidence:
                continue
            seen_evidence.add(key)
            merged_claims.append(claim)
        merged_claim_ids = [claim.id for claim in merged_claims]

        has_body_evidence = any(_is_body_claim(claim) for claim in merged_claims)
        content = {
            "problem": _consistent_longest((review.problem for review in reviews), has_body_evidence),
            "research_question": _consistent_longest((review.research_question for review in reviews), has_body_evidence),
            "method": _consistent_longest((review.method for review in reviews), has_body_evidence),
            "datasets": _unique(item for review in reviews for item in review.datasets),
            "experimental_protocol": _consistent_longest((review.experimental_protocol for review in reviews), has_body_evidence),
            "results": _unique(item for review in reviews for item in review.results),
            "ablations": _unique(item for review in reviews for item in review.ablations),
            "limitations": _consistent_unique(
                (item for review in reviews for item in review.limitations), has_body_evidence
            ),
            "failure_modes": _consistent_unique(
                (item for review in reviews for item in review.failure_modes), has_body_evidence
            ),
            "evidence_claim_ids": tuple(merged_claim_ids),
            "confidence": round(sum(review.confidence for review in reviews) / len(reviews), 6),
        }
        evidence_levels = {review.evidence_level for review in reviews}
        evidence_level = "full_text" if "full_text" in evidence_levels else "abstract" if "abstract" in evidence_levels else "unknown"
        field_evidence = _field_evidence(content, merged_claims)
        provenance = ArtifactProvenance.create(
            project_id=paper.project_id, run_id=run_id,
            source_evidence=content["evidence_claim_ids"],
            provider=provider, model=model,
            prompt_version=prompt_version,
            content={**content, "evidence_level": evidence_level, "field_evidence": field_evidence, "sections": [r.provenance.checksum for r in reviews]},
        )
        review = PaperReview(
            uuid4().hex, paper.project_id, run_id, paper.id, content["problem"],
            content["research_question"], content["method"], content["datasets"],
            content["experimental_protocol"], content["results"], content["ablations"],
            content["limitations"], content["failure_modes"], content["evidence_claim_ids"],
            content["confidence"], provenance, evidence_level, field_evidence,
        )
        return ReviewBundle(review, tuple(merged_claims))

    @staticmethod
    def _claims(value: object, paper: PaperRecord, run_id: str, source_text: str, provider: str, model: str, prompt_version: str = REVIEW_PROMPT_VERSION, evidence_level: str = "unknown") -> tuple[EvidenceClaim, ...]:
        if not isinstance(value, list):
            return ()
        claims: list[EvidenceClaim] = []
        normalized_source = " ".join(source_text.split())
        for raw in value:
            if not isinstance(raw, dict):
                continue
            excerpt = _string(raw.get("excerpt"))
            statement = _string(raw.get("statement"))
            if not excerpt or not statement or excerpt not in normalized_source:
                continue
            content = {"paper_id": paper.id, "statement": statement, "excerpt": excerpt, "page": raw.get("page"), "section": raw.get("section")}
            provenance = ArtifactProvenance.create(project_id=paper.project_id, run_id=run_id, source_evidence=(f"paper:{paper.id}",), provider=provider, model=model, prompt_version=prompt_version, content=content)
            page = _page(raw.get("page"))
            claims.append(EvidenceClaim(uuid4().hex, paper.project_id, run_id, paper.id, _string(raw.get("claim_type")) or "finding", statement, excerpt, page, _string(raw.get("section")) or None, _confidence(raw.get("confidence"), field="evidence_spans[].confidence"), provenance, evidence_level))
        return tuple(claims)


def _required(payload: dict[str, Any], key: str) -> str:
    value = _string(payload.get(key))
    if not value:
        raise ValueError(f"Paper review field '{key}' is required.")
    return value


def _string(value: object) -> str:
    return " ".join(str(value or "").split())


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for raw in value if (item := _string(raw)))


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.lower()))


def _confidence(value: object, *, field: str = "confidence") -> float:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"Paper review field '{field}' is required and must be numeric.")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Paper review field '{field}' must be numeric.") from exc
    if not 0.0 <= parsed <= 1.0:
        raise ValueError(f"Paper review field '{field}' must be between 0 and 1.")
    return parsed


def _page(value: object) -> int | None:
    try:
        page = int(value)
        return page if page > 0 else None
    except (TypeError, ValueError):
        return None


_REQUIRED_TEXT_FIELDS = (
    "problem",
    "research_question",
    "method",
    "experimental_protocol",
)
_REQUIRED_LIST_FIELDS = (
    "datasets",
    "results",
    "ablations",
    "limitations",
    "failure_modes",
)


def _validate_review_payload(payload: object, *, paper_title: str) -> None:
    if not isinstance(payload, dict):
        raise ValueError(f"Paper '{paper_title}' review payload must be a JSON object.")
    for key in _REQUIRED_TEXT_FIELDS:
        _required(payload, key)
    for key in _REQUIRED_LIST_FIELDS:
        if not isinstance(payload.get(key), list):
            raise ValueError(f"Paper '{paper_title}' review field '{key}' must be a list.")
    _confidence(payload.get("confidence"), field="review.confidence")
    spans = payload.get("evidence_spans")
    if not isinstance(spans, list) or not spans:
        raise ValueError(
            f"Paper '{paper_title}' review must contain at least one evidence span."
        )
    for index, raw in enumerate(spans):
        if not isinstance(raw, dict):
            raise ValueError(
                f"Paper '{paper_title}' evidence_spans[{index}] must be an object."
            )
        for key in ("claim_type", "statement", "excerpt", "page", "section", "confidence"):
            if key not in raw:
                raise ValueError(
                    f"Paper '{paper_title}' evidence_spans[{index}] is missing '{key}'."
                )
        if not _string(raw.get("claim_type")):
            raise ValueError(
                f"Paper '{paper_title}' evidence_spans[{index}].claim_type is required."
            )
        if not _string(raw.get("statement")) or not _string(raw.get("excerpt")):
            raise ValueError(
                f"Paper '{paper_title}' evidence_spans[{index}] requires statement and excerpt."
            )
        _confidence(
            raw.get("confidence"),
            field=f"evidence_spans[{index}].confidence",
        )


_FIELD_TYPE_TOKENS: dict[str, tuple[str, ...]] = {
    "problem": ("problem", "motivation", "gap", "premise", "scope"),
    "research_question": ("question", "problem", "gap"),
    "method": ("method", "dataset", "experimental", "endpoint", "mechanism"),
    "datasets": ("dataset",),
    "experimental_protocol": ("experimental", "protocol", "method"),
    "results": ("result", "measured", "ablation", "finding", "theoretical", "contribution"),
    "ablations": ("ablation",),
    "limitations": ("limitation", "failure", "missing", "reproducibility"),
    "failure_modes": ("failure", "limitation"),
}


def _field_evidence(content: dict[str, object], claims: tuple[EvidenceClaim, ...]) -> dict[str, tuple[str, ...]]:
    """Map each review field to claims of a compatible type.

    Fields with content but no compatible claim are explicitly listed as
    reviewer inference, so downstream artifacts never present them as
    source-supported findings.
    """
    result: dict[str, tuple[str, ...]] = {}
    for field_name, tokens in _FIELD_TYPE_TOKENS.items():
        value = content.get(field_name)
        if not value:
            result[field_name] = ()
            continue
        ids: list[str] = []
        for claim in claims:
            claim_type = claim.claim_type.lower()
            if any(token in claim_type for token in tokens):
                ids.append(claim.id)
        result[field_name] = tuple(dict.fromkeys(ids))
    result["reviewer_inference_fields"] = tuple(
        field_name
        for field_name in _FIELD_TYPE_TOKENS
        if content.get(field_name) and not result.get(field_name)
    )
    return result


_NO_CONTENT_MARKERS = (
    "front-matter",
    "front matter",
    "fragment of the reference list",
    "reference list only",
    "no methods, results, or data",
    "no substantive content",
    "source lacks all substantive content",
    "cannot assess methodological rigor",
    "no claims, methods, or results",
)


def _no_content_statement(value: str) -> bool:
    lowered = str(value or "").lower()
    return any(marker in lowered for marker in _NO_CONTENT_MARKERS)


def _is_body_claim(claim: EvidenceClaim) -> bool:
    if claim.page is None or claim.page <= 1:
        return False
    claim_type = claim.claim_type.lower()
    return not any(token in claim_type for token in ("metadata", "scope", "reference", "bibliographic"))


def _has_body_claim(bundle: ReviewBundle) -> bool:
    return any(_is_body_claim(claim) for claim in bundle.claims)


def _consistent_longest(values, has_body_evidence: bool) -> str:
    candidates = [value for value in values if value]
    if has_body_evidence:
        filtered = [value for value in candidates if not _no_content_statement(value)]
        if filtered:
            candidates = filtered
        elif candidates:
            raise ValueError(
                "Hierarchical review is internally inconsistent: body evidence "
                "exists but every narrative candidate claims the source has no content."
            )
    return max(candidates, key=len) if candidates else ""


def _consistent_unique(values, has_body_evidence: bool) -> tuple[str, ...]:
    items = [value for value in values if value]
    if has_body_evidence:
        items = [value for value in items if not _no_content_statement(value)]
    return tuple(dict.fromkeys(items))


def _source_groups(chunks: tuple[DocumentChunk, ...], *, max_characters: int) -> tuple[str, ...]:
    groups: list[str] = []
    current: list[str] = []
    size = 0
    for chunk in chunks:
        rendered = f"[PAGE {chunk.page_start}; SECTION {chunk.section}]\n{chunk.text}"
        if current and size + len(rendered) > max_characters:
            groups.append("\n\n".join(current))
            current, size = [], 0
        current.append(rendered)
        size += len(rendered)
    if current:
        groups.append("\n\n".join(current))
    return tuple(groups)


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _longest(values) -> str:
    return max((value for value in values if value), key=len)
