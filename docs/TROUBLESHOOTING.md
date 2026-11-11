# Troubleshooting

## Configuration and credentials

**`provider_unconfigured`**

Run `autoresearch skill doctor`. Confirm that
`<project>/.autoresearch/config.toml` assigns `literature_extraction`,
`research_reasoning`, and `scientific_critic`, and that each `api_key_env`
matches an exported variable.

**`provider_unauthenticated`**

The named environment variable is empty, or the credential store has no
matching active API key. Export the variable in the same shell that runs the
CLI. Restarting the shell after setting a persisted Windows variable is often
required.

**A resumed project ignores my config**

Resumed projects use the config beside their recorded database:
`<project>/.autoresearch/config.toml`. A config in the current working
directory is not substituted.

## Literature discovery

**No candidates**

Check network access to the configured scholarly sources. Try a more specific
seed or a different `--limit`. A restrictive `discovery_required_terms` or
`discovery_min_relevance` can also remove every candidate; inspect the
selection manifest in the discovery result.

**Many failed reviews**

Image-only PDFs, paywalls, and malformed documents may produce no
verbatim-verifiable quotes. The review result reports `failed_reviews`
separately from `reviewed`.

## Experiment lifecycle

**`approval_required`**

Run the preview first:

```bash
autoresearch skill experiment approve --candidate-id <id>
```

Review the exact action plan, then record approval with `--accept`. If the
spec changed after approval, the old checksum is invalid.

**`worker_unavailable`**

The local worker could not start the job. Check the Python executable, the
workspace path, and the job output artifact.

**`implementation_integrity`**

The script on disk no longer matches the verified implementation that was
approved. Do not edit approved code in place; regenerate and approve again.

**A run terminated early**

Read the `execution_monitor` artifact. A monitor abort is an operational
event, not evidence against the hypothesis.

## Manuscript

**`status: incomplete`**

The lineage is missing a required stage. Use `autoresearch skill state
--project <id>` to see what is present and what is pending.

**`submission_ready: false`**

Read `readiness_report.json` and `submission_checklist.json`. Typical blockers
are too few reviewed references, missing external baselines, missing
ablations, unestablished power, simulation-only evidence, or missing dataset
and commit identities.

**LaTeX will not compile**

The Python pipeline writes LaTeX but does not bundle a TeX distribution.
Install one (for example MiKTeX or TeX Live) or treat the `.tex` output as a
portable source file.

## Windows

**PowerShell refuses to activate `.venv`**

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

**`autoresearch` is not recognized**

Activate the environment, confirm `pip show autoresearch`, and check that the
Python `Scripts` directory is on `PATH`.

**Path with spaces**

Quote seed and catalog paths: `--seed-paper "C:\My Research\seed.pdf"`.
