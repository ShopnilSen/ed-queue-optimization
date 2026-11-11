# Architecture

AutoResearch is a local Python package with a Typer CLI, a SQLite state store,
and a set of deterministic evidence gates. There is no server, web UI, or
background daemon in this release.

## Modules

| Package | Responsibility |
|---|---|
| `application` | Composition root, workflow orchestration, review/novelty/experiment services, manuscript generation, readiness gates, recovery, findings memory |
| `audit` | Deterministic citation, number, claim, disclosure, and reproducibility audits used by the manuscript and contract paths |
| `cli` | The `autoresearch` command group and the JSON skill bridge |
| `domain` | Typed records for projects, papers, claims, gaps, candidates, specs, runs, reports, and provenance |
| `experiments` | Spec planning, implementation generation/verification, execution policy, runtime perception, measurement ingestion, analysis, reporting |
| `ingestion` | PDF/document parsing, chunking, and extraction helpers |
| `novelty` | Candidate generation, prior-art checking, adversarial critique, methodology freezing, portfolio selection |
| `papers` | Scholarly metadata adapters, PDF resolution, deduplication, ranking, relevance policy |
| `providers` | Provider configuration, credential resolution, HTTP/CLI adapters, routing, budgets, usage, redaction |
| `review` | Evidence extraction, priority ranking, gap analysis, conflict candidates, synthesis, evidence matrix |
| `skills` | Canonical `SKILL.md` files and the manuscript template assets shipped in the wheel |
| `storage` | SQLite repository, schema migrations, artifact/usage bookkeeping |
| `workers` | Local typed worker that executes an approved action vector |

## State layout

```text
<project-dir>/
  .autoresearch/
    config.toml            # user-owned provider config (never committed)
    data/
      autoresearch.sqlite3 # SQLite state (WAL mode)
      artifacts/           # checksummed reports, corpora, manifests
      downloads/           # acquired PDFs
      workspace/           # experiment execution workspace
```

A machine-local registry at `~/.autoresearch/project-registry.json` maps a
project ID to the database path. `autoresearch skill state --project <id>`
works from any directory once the registry entry exists.

`AUTORESEARCH_DATA_ROOT`, `AUTORESEARCH_DATABASE_PATH`,
`AUTORESEARCH_ARTIFACT_ROOT`, `AUTORESEARCH_DOWNLOAD_ROOT`,
`AUTORESEARCH_PROVIDER_CONFIG`, and `AUTORESEARCH_REGISTRY_PATH` override the
defaults.

## Determinism and evidence

- Paper claims are accepted only when the quoted excerpt is found in the
  parsed source text.
- The review audit labels cross-paper conflicts as *candidate conflicts*, not
  verified contradictions.
- Experiment specifications and approvals are checksum-bound.
- Generated implementations are statically verified before an approval can
  bind them.
- The statistical analysis is recomputed from ingested measurements.
- Manuscript numbers are checked against a claim ledger built from the
  validated lineage.
- Simulation and fixture provenance cannot pass the empirical publication
  gate.

## Execution boundary

The local worker:

1. accepts only actions produced by the typed policy layer,
2. runs with the repository as the working directory inside the project
   workspace,
3. filters environment variables,
4. records output and result checksums,
5. enforces an optional monitor contract by reading probe lines.

It is not an OS sandbox. A user who approves generated code is trusting that
code to run with the user's operating-system permissions.
