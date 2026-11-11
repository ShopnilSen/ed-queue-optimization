# ED Queue Optimization — Research Automation Toolkit

This repository packages the reusable **AutoResearch** tooling used for an
emergency-department (ED) queue-optimization research project. It is shared so
other students and researchers can use the same evidence-first workflow in
their own work.

> **What is not here:** the study's datasets, derived cohort, predictions,
> models, results, figures, manuscripts, submission packages, internal reports,
> and credentials are intentionally excluded. This repository contains software
> and documentation only. `scripts/verify_public_release.py` enforces this.

An evidence-backed research cycle in five skills:

```text
review -> novelty -> experiment -> manuscript
        \________ workflow (runs the loop) ________/
```

AutoResearch is a local Python pipeline for moving from a seed paper to a
reviewed research question, a falsifiable experiment, and an
evidence-grounded manuscript draft. It is built around explicit evidence,
checksummed artifacts, and approval gates rather than autonomous claims.

> **Status:** this is a research tool, not a substitute for scientific
> judgement. It can produce an honestly incomplete draft or stop with
> `insufficient_evidence`; it never turns a failed or simulated run into a
> positive result.

## Repository scope

**Included:** the `autoresearch` Python package, CLI, five skills, offline test
suite, documentation, and the public-release guard.

**Excluded:** the ED study's raw and derived data, cohort and prediction files,
trained models, metrics, figures, manuscripts, submission packages, internal
reports, and all credentials. These are intentionally not part of this
repository. The `.gitignore` rules and `scripts/verify_public_release.py` check
are fail-closed.

If you are a collaborator who needs the study materials, ask the repository
owner directly; do not commit them here.

## 1. What AutoResearch does

The pipeline coordinates five reusable stages:

1. **Review** — discovers literature from Semantic Scholar, OpenAlex,
   Crossref, arXiv, and OpenReview; extracts verbatim-verified claims;
   ranks papers; writes a review corpus, citation contexts, a review audit,
   and a cited research gap.
2. **Novelty** — proposes candidate methods grounded in the reviewed
   evidence, checks them against prior art, critiques them adversarially,
   and freezes a versioned methodology with a publication-design slice.
3. **Experiment** — turns the methodology into a falsifiable spec and
   implementation, validates the environment, requires an exact-checksum
   approval, executes through a policy-controlled local worker, ingests
   measurements, and writes a deterministic report.
4. **Manuscript** — assembles Markdown, LaTeX, BibTeX, tables, and a
   manifest from the validated lineage, behind a publication-readiness gate.
5. **Workflow** — runs the four stages as one durable, resumable loop and
   stops at the requested stage.

Every analyzed outcome is stored in **Findings Memory**, including negative
results and refutations, so later novelty runs can avoid repeating failed
approaches.

## 2. The research workflow

```text
seed paper / DOI / URL / local catalog
        |
        v
   discovery  ->  review corpus + cited gap
                        |
                        v
                 novelty portfolio
                        |
                        v
              frozen methodology + design
                        |
                        v
        approval (exact specification checksum)
                        |
                        v
       execution -> measurements -> analysis -> report
                        |
                        v
        manuscript + readiness gate + audit artifacts
```

The pipeline is resumable. `--project <id>` reopens the project's own
database from any working directory. Completed stages are reused unless you
ask for a fresh run.

## 3. The five skills

| Skill | Purpose | Primary command |
|---|---|---|
| Review | Literature discovery, evidence extraction, gap analysis | `autoresearch skill review --project <id>` |
| Novelty | Candidate generation, prior art, adversarial critique, methodology freeze | `autoresearch skill novelty --project <id>` |
| Experiment | Plan, validate, approve, execute, and report a falsifiable experiment | `autoresearch skill experiment plan --candidate-id <id>` |
| Manuscript | Evidence-grounded Markdown/LaTeX draft and publication gate | `autoresearch skill manuscript --project <id>` |
| Workflow | One durable loop over the four stages | `autoresearch skill workflow --seed-paper <input>` |

The canonical skill instructions ship inside the package under
`src/autoresearch/skills/`. Install them into an agent host with:

```bash
autoresearch skill install --target opencode   # or: claude, codex, all
autoresearch skill list
```

## 4. Supported integrations

**Scholarly metadata sources** (no API key required unless noted):

- Semantic Scholar (optional `SEMANTIC_SCHOLAR_API_KEY` for higher limits)
- OpenAlex (optional `OPENALEX_MAILTO`)
- Crossref (optional `CROSSREF_MAILTO`)
- arXiv
- OpenReview

**LLM provider kinds:**

- `openai_compatible` — OpenAI, DeepSeek, OpenRouter, local vLLM, and other
  Chat Completions-compatible endpoints
- `anthropic` — Anthropic Messages API
- `gemini` — Google Gemini `generateContent`
- `opencode_cli` — a locally installed and authenticated OpenCode CLI
- `external_embeddings` — optional OpenAI-compatible embeddings endpoint

Provider support is implemented in the package. The release test suite uses
deterministic fakes and does **not** make paid API calls, so live
authentication against each vendor is the user's responsibility.

## 5. System architecture

```text
src/autoresearch/
  application/   orchestration services, workflow, gates, manuscript
  audit/         citation, number, claim, disclosure, reproducibility audits
  cli/           Typer CLI and the stable skill bridge
  domain/        typed domain models
  experiments/   planning, implementation, execution policy, perception
  ingestion/     document parsing, chunking, extraction
  novelty/       candidate generation, prior art, critique, methodology
  papers/        metadata sources, PDF resolution, deduplication, ranking
  providers/     LLM/embedding adapters, routing, budgets, redaction
  review/        evidence extraction, ranking, conflicts, synthesis
  skills/        canonical SKILL.md files shipped in the wheel
  storage/       SQLite repository and schema migrations
  workers/       local typed execution worker
tests/           offline unit, integration, and end-to-end tests
```

Durable state is stored in SQLite. Evidence and generated artifacts are
written to disk and referenced with checksums. The worker executes only an
approved action plan from the local workspace; it is workspace containment,
not an operating-system sandbox.

## 6. Installation requirements

- Python **3.11 or newer** (the package uses `tomllib` and `StrEnum`)
- `pip`
- Git, if installing from a clone
- Network access for scholarly APIs and any HTTP model provider
- Optional: a LaTeX distribution (for compiling generated LaTeX to PDF)
- Optional: an OpenCode, Claude Code, or Codex installation (for skill
  installation into an agent host)

## 7. Python environment setup

### Windows PowerShell

```powershell
git clone https://github.com/ShopnilSen/ed-queue-optimization.git
cd ed-queue-optimization
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e .
autoresearch --help
```

If PowerShell blocks the activation script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### Linux / macOS

```bash
git clone https://github.com/ShopnilSen/ed-queue-optimization.git
cd ed-queue-optimization
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
autoresearch --help
```

Install the test dependencies when you want to run the suite:

```bash
pip install -e ".[test]"
```

## 8. Provider configuration

Create the project state directory and copy the template **inside your
research project** (not inside the source checkout):

```bash
mkdir -p .autoresearch
cp /path/to/ed-queue-optimization/config.example.toml .autoresearch/config.toml
```

On Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force .autoresearch | Out-Null
Copy-Item C:\path\to\ed-queue-optimization\config.example.toml .autoresearch\config.toml
```

Edit `.autoresearch/config.toml`:

- Set `base_url` for your provider.
- Set `api_key_env` to the **name** of an environment variable.
- Assign the three roles: `literature_extraction`, `research_reasoning`,
  `scientific_critic`.
- Optionally add an embeddings provider; without one, review ranking falls
  back to deterministic lexical similarity.

The three required model roles are enough for the default pipeline. The
configuration is validated before a provider call; a project-scoped command
fails closed if its config is missing or invalid.

## 9. API-key setup

Never put a key in TOML, source code, or a commit. Export it in the shell that
runs `autoresearch`.

Windows PowerShell (current session):

```powershell
$env:OPENAI_API_KEY = "<your-key>"
```

Windows PowerShell (persist for your user):

```powershell
[Environment]::SetEnvironmentVariable("OPENAI_API_KEY", "<your-key>", "User")
```

Linux / macOS:

```bash
export OPENAI_API_KEY="<your-key>"
```

Use the variable names your config references, for example
`DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, or `GEMINI_API_KEY`. See
`.env.example` for the common names. `.env.example` is a reference list; the
CLI reads the process environment, not the file automatically.

## 10. Project initialization

There is no separate `init` command. A project is created by the first
`workflow` run from the directory where its state should live.

```bash
mkdir my-research
cd my-research
mkdir -p .autoresearch
cp /path/to/ed-queue-optimization/config.example.toml .autoresearch/config.toml
# edit .autoresearch/config.toml and export your API key

autoresearch skill workflow --seed-paper ./seed.pdf --until question
```

The JSON result contains a `project_id`. The state is stored under:

```text
my-research/
  .autoresearch/
    config.toml
    data/
      autoresearch.sqlite3
      artifacts/
      downloads/
      workspace/
```

A machine-local registry at `~/.autoresearch/project-registry.json` maps
project IDs to those state paths so `--project <id>` works from any
directory. Override its location with `AUTORESEARCH_REGISTRY_PATH` if needed.
Never commit either location.

## 11. Running the workflow

New project from a seed paper, DOI, or HTTPS URL:

```bash
autoresearch skill workflow --seed-paper <pdf|doi|url> --until design
```

Resume or extend an existing project:

```bash
autoresearch skill workflow --project <project_id> --until manuscript
```

Publication track (larger review corpus and early publication gates):

```bash
autoresearch skill workflow \
  --seed-paper <pdf|doi|url> \
  --limit 40 --top-k 20 \
  --publication --venue generic_journal \
  --until manuscript
```

Valid `--until` stops are `review`, `question`, `hypotheses`, `methodology`,
`design`, `experiment`, and `manuscript`. Use `--max-cost-usd` and
`--max-tokens` to bound project spend.

Run stages individually when you want manual control:

```bash
autoresearch skill review --project <id> --top-k 20
autoresearch skill novelty --project <id>
autoresearch skill experiment plan --candidate-id <id>
autoresearch skill manuscript --project <id>
autoresearch skill state --project <id>
```

## 12. Approval gates

Experiment execution is never implicit:

```bash
# 1. Plan: creates the spec and action plan; no code runs.
autoresearch skill experiment plan --candidate-id <id>

# 2. Validate: checks the environment; no code runs.
autoresearch skill experiment validate --candidate-id <id>

# 3. Preview the exact approval; writes nothing.
autoresearch skill experiment approve --candidate-id <id>

# 4. Record approval only after reviewing the preview.
autoresearch skill experiment approve --candidate-id <id> --accept

# 5. Execute the approved plan.
autoresearch skill experiment execute --candidate-id <id>
```

The approval binds the specification checksum, action plan, worker,
implementation digest (when one exists), and monitor contract. Changing the
spec invalidates the approval. A failed execution writes a checksummed
recovery decision; expensive follow-up work is recommended, not automatic.

## 13. Literature and novelty review

Review uses verbatim verification: a quoted claim must appear in the source
text. Per-paper reviews are checkpointed and reused. The output includes:

- `project_id` and `review_run_id`
- `gap.id`, `gap.statement`, confidence, and counter-evidence
- review corpus, citation contexts, and `review_audit.json`
- publication gaps such as `reviewed_papers_below_minimum`

Novelty output includes a portfolio with low-risk, balanced, and high-risk
candidates. The system deliberately says "appears distinct under retrieved
evidence" rather than "proven novel"; unresolved overlap remains visible.

## 14. Experiment execution

The planner records the hypothesis, falsification condition, metrics, seeds,
statistical plan, compute budget, dataset identity, and baseline commit. The
implementation generator produces a script and statically verifies syntax,
imports, identity, seeds, artifact types, and the measurement contract.
Execution goes through the local typed worker, which accepts only the
approved action vector. Runtime monitor contracts can terminate a run on
NaN/Inf, bound violations, persistent trend violations, stalls, or guard
events.

## 15. Reproducibility and evidence auditing

- Review claims are verbatim-verified against source text.
- Experiment specs carry checksums; approvals are bound to the exact spec.
- Dataset checksums and baseline commits are computed from the real target
  when available.
- Worker results and generated artifacts are checksummed and recorded.
- The analysis recomputes tests from ingested measurements.
- The manuscript's claim ledger traces every number to an evidence artifact.
- Fixture-labelled or simulation measurements cannot pass the empirical
  publication gate.
- Negative, null, and failed outcomes remain visible in Findings Memory.

## 16. Manuscript generation

```bash
autoresearch skill manuscript --project <id>
```

Outputs include Markdown, LaTeX, BibTeX, tables, a manifest, a readiness
report, venue lint, a reviewer panel, a claim ledger, and a submission
checklist. `submission_ready` is the decision signal. A complete lineage with
weak evidence is a `draft` or `technical_report`, not a submission.

Venue profiles: `generic_journal` (default), `fasttrack`, `neurips`, `acl`,
`cvpr`, `workshop`, and `technical_report`. Set the profile in
`.autoresearch/config.toml` or with `AUTORESEARCH_MANUSCRIPT_VENUE`.

## 17. Troubleshooting

**`provider_unconfigured` / `provider_unauthenticated`**

- Confirm `.autoresearch/config.toml` exists in the project directory.
- Confirm the role names are assigned.
- Confirm `api_key_env` names the variable you actually exported.
- Run `autoresearch skill doctor` and read the provider checks.

**`FileNotFoundError: Provider configuration not found`**

A resumed project requires its own `.autoresearch/config.toml`. Create it
beside the project's `.autoresearch/data/` directory.

**No papers discovered**

Check network access, the seed text, and the configured sources. Publication
track runs need `--limit` and `--top-k` large enough for the venue profile.

**Reviews fail or return no claims**

Image-only or paywalled PDFs may yield no verbatim excerpts. `failed_reviews`
is reported rather than hidden.

**Experiment execution refused**

The approval is missing, expired, or bound to a different checksum. Re-run
the preview, review it, and approve again.

**`No module named autoresearch`**

Activate the virtual environment and reinstall with `pip install -e .`.

**PowerShell will not activate the venv**

Use `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` for the
current shell only.

## 18. Limitations

- The release test suite is offline and uses deterministic fixtures; it does
  not prove live provider compatibility, PDF availability, or scientific
  validity.
- The local worker provides workspace containment and policy checks, not an
  OS-level sandbox. Only run code you are willing to execute on your machine.
- SQLite state is not encrypted. Protect the project directory and your API
  keys with normal operating-system permissions.
- Cross-paper conflict detection is a heuristic retrieval aid, not semantic
  entailment or expert-verified contradiction detection.
- Automatic reverse loops (experiment failure back to novelty/review) are
  recommendations; re-entry requires explicit user action.
- Generated manuscripts are drafts. The readiness gate can reject them, and
  a passing gate is not peer review.
- The optional FastTrack formatting preset is a local specification. The
  third-party teaching PDF it was derived from is **not bundled**; see
  `THIRD_PARTY_NOTICES.md`.

## 19. Security practices

- Secrets are read from environment variables or an agent session's
  credential store; provider configs store only environment-variable names.
- CLI output is passed through secret redaction.
- Runtime state (`.autoresearch/`), databases, downloads, artifacts,
  manuscripts, and `.env` files are excluded by `.gitignore`.
- Never commit a project state directory or a machine-local registry.
- Enable GitHub secret scanning and push protection in the repository
  settings; never bypass a push-protection block.
- Report vulnerabilities privately using the process in `SECURITY.md`.
- Do not weaken approval gates, evidence checks, or audit logic in a fork.

## 20. Contributing

See `CONTRIBUTING.md`. In short:

```bash
pip install -e ".[test]"
python -m pytest tests/ -q
```

Keep changes evidence-first, add offline tests for behavior changes, do not
add hidden credentials, and do not claim capabilities the code does not
implement.

## License

MIT. See `LICENSE`. The original copyright line is preserved unchanged.
Third-party assets are listed in `THIRD_PARTY_NOTICES.md`.
