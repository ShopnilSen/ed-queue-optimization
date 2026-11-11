# Workflow reference

## Stages

```text
review -> question -> hypotheses -> methodology -> design -> experiment -> manuscript
```

`autoresearch skill workflow --until <stage>` stops after the requested stage.
Valid values: `review`, `question`, `hypotheses`, `methodology`, `design`,
`experiment`, `manuscript`.

## Commands

```bash
# New project from a seed paper (PDF path, DOI, or HTTPS URL).
autoresearch skill workflow --seed-paper ./seed.pdf --until question

# New project from a local JSON catalog.
autoresearch skill workflow --from-catalog ./catalog.json --until review

# Resume an existing project from any working directory.
autoresearch skill workflow --project <project_id> --until design

# Publication track.
autoresearch skill workflow --seed-paper <input> \
  --limit 40 --top-k 20 --publication --venue generic_journal \
  --until manuscript
```

`--seed-paper`, `--from-catalog`, and `--project` are mutually exclusive; pass
exactly one.

## Individual stages

```bash
autoresearch skill review --project <id> [--top-k 20] [--hierarchical]
autoresearch skill review-resume --run <review_run_id>
autoresearch skill novelty --project <id> [--gap-id <gap_id>]
autoresearch skill experiment plan --candidate-id <id>
autoresearch skill experiment implement --candidate-id <id>
autoresearch skill experiment validate --candidate-id <id>
autoresearch skill experiment approve --candidate-id <id>
autoresearch skill experiment approve --candidate-id <id> --accept
autoresearch skill experiment execute --candidate-id <id>
autoresearch skill experiment report --candidate-id <id>
autoresearch skill experiment inspect --candidate-id <id>
autoresearch skill experiment cancel --run-id <run_id>
autoresearch skill manuscript --project <id>
autoresearch skill state --project <id>
```

## Stage outputs

| Stage | Durable output |
|---|---|
| Review | `gap-report.json/.md`, `review_corpus.json`, `citation_contexts.json`, `review_audit.json`, `evidence-matrix.json`, publication gaps |
| Novelty | candidate portfolio, prior-art assessments, critiques, `methodology-v{N}.json`, publication-design gaps |
| Design | experiment spec, environment manifest, generated/verified implementation |
| Approval | exact-spec approval record with checksum and action plan |
| Execution | worker jobs, captured output, execution monitor, measurements |
| Analysis | statistical analysis and experiment report/decision |
| Manuscript | Markdown, LaTeX, BibTeX, tables, manifest, readiness report, venue lint, reviewer panel, claim ledger, submission checklist |

Artifact paths are absolute and returned in the JSON result. They live under
the project's `.autoresearch/data/artifacts/` unless the project config or
environment overrides the data root.

## Honest stop states

The workflow may stop at `review_gate`, `methodology_gate`, or `design_gate`
in publication mode when the evidence cannot support a submission-grade
paper. It may also return `awaiting_approval`, `execution_failed`,
`analysis_pending`, `worker_unavailable`, or `insufficient_evidence`. These
are outcomes, not crashes; do not report them as success.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (including an honestly incomplete manuscript) |
| 1 | Internal error or unhealthy doctor report |
| 2 | Usage error |
| 3 | Approval required or expired |
| 4 | Not found |
| 5 | Insufficient evidence |
| 6 | Worker unavailable |

The CLI writes one JSON document to stdout; progress events go to stderr.
