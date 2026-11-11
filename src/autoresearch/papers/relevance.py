"""Deterministic, auditable pre-review relevance selection.

The discovery ranker produces a numeric ordering, but a numeric ordering alone
must never be treated as permission to fill a fixed paper quota. This module
applies an explicit, user-configured topical contract and records a reason for
every candidate, including candidates excluded because the limit was reached.

The contract is intentionally domain-neutral: set ``required_terms`` to the
topic anchors that a candidate must mention, or leave it empty to rely on the
seed-relative relevance score. ``excluded_terms`` provides an explicit
out-of-domain blocklist. Both are configurable per project.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .deduplication import DeduplicatedCandidate, deduplication_key
from .ranking import CandidateScore

# Default blocklist is empty: the public pipeline must not assume any research
# domain. Projects opt in through ``[research] discovery_excluded_topics``.
DEFAULT_EXCLUDED_TERMS: tuple[str, ...] = ()


def _normalize(value: object) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _contains(text: str, terms: tuple[str, ...]) -> str | None:
    padded = f" {text} "
    for term in terms:
        normalized = _normalize(term)
        if normalized and f" {normalized} " in padded:
            return term
    return None


@dataclass(frozen=True)
class SelectionPolicy:
    """Configurable relevance contract for discovery selection."""

    minimum_relevance: float = 0.0
    required_terms: tuple[str, ...] = ()
    excluded_terms: tuple[str, ...] = DEFAULT_EXCLUDED_TERMS
    allowed_dois: frozenset[str] = field(default_factory=frozenset)
    allowed_titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class SelectionResult:
    selected: tuple[tuple[DeduplicatedCandidate, CandidateScore], ...]
    manifest: tuple[dict, ...]
    included_count: int
    excluded_count: int


def _explicitly_allowed(paper, policy: SelectionPolicy) -> str | None:
    doi = _normalize(paper.doi)
    if doi and any(doi == _normalize(item) for item in policy.allowed_dois):
        return f"DOI {paper.doi} explicitly allowlisted"
    title = _normalize(paper.title)
    for allowed in policy.allowed_titles:
        needle = _normalize(allowed)
        if needle and needle in title:
            return f"title matched explicit allowlist '{allowed}'"
    return None


def _evaluate(
    candidate: DeduplicatedCandidate,
    score: CandidateScore,
    policy: SelectionPolicy,
) -> tuple[bool, str]:
    paper = candidate.paper
    text = _normalize(f"{paper.title} {paper.abstract}")
    allowed = _explicitly_allowed(paper, policy)
    if allowed:
        return True, f"included: {allowed} (policy override)"

    excluded = _contains(text, policy.excluded_terms)
    if excluded:
        return False, f"excluded: out-of-domain term '{excluded}'"

    if policy.required_terms:
        anchor = _contains(text, policy.required_terms)
        if anchor is None:
            terms = ", ".join(policy.required_terms[:5])
            return False, f"excluded: no required topic anchor ({terms})"
    else:
        anchor = None

    if score.relevance < policy.minimum_relevance:
        return (
            False,
            f"excluded: relevance {score.relevance:.4f} below minimum "
            f"{policy.minimum_relevance:.4f}",
        )

    if anchor:
        return (
            True,
            f"included: topic '{anchor}'; relevance {score.relevance:.4f}",
        )
    return True, f"included: relevance {score.relevance:.4f}"


def select_candidates(
    ranked: list[tuple[DeduplicatedCandidate, CandidateScore]],
    policy: SelectionPolicy | None,
    *,
    limit: int,
) -> SelectionResult:
    """Apply the policy in rank order, allowing fewer than ``limit`` results.

    A candidate that passes but is beyond the limit is recorded as excluded
    with an explicit "limit reached" reason. This keeps the manifest complete
    and deterministic without silently dropping candidates.
    """
    if limit < 1:
        raise ValueError("limit must be at least one.")
    if policy is None:
        selected = tuple(ranked[:limit])
        return SelectionResult(selected, (), len(selected), 0)

    selected: list[tuple[DeduplicatedCandidate, CandidateScore]] = []
    manifest: list[dict] = []
    for candidate, score in ranked:
        paper = candidate.paper
        included, reason = _evaluate(candidate, score, policy)
        if included and len(selected) >= limit:
            included = False
            reason = f"eligible but limit {limit} reached"
        if included:
            selected.append((candidate, score))
        manifest.append(
            {
                "candidate_key": deduplication_key(paper),
                "title": paper.title,
                "doi": paper.doi,
                "arxiv_id": paper.arxiv_id,
                "year": paper.year,
                "source_names": sorted({item.source for item in candidate.provenance}),
                "best_source_rank": min(item.source_rank for item in candidate.provenance),
                "relevance": round(score.relevance, 6),
                "total": round(score.total, 6),
                "included": included,
                "reason": reason,
            }
        )
    return SelectionResult(
        tuple(selected),
        tuple(manifest),
        sum(1 for item in manifest if item["included"]),
        sum(1 for item in manifest if not item["included"]),
    )
