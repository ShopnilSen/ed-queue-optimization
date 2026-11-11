# Contributing

Thanks for improving AutoResearch. This project is evidence-first: a change
that makes the pipeline produce more output but less verifiable evidence is
not an improvement.

## Development setup

```bash
git clone https://github.com/ShopnilSen/ed-queue-optimization.git
cd ed-queue-optimization
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[test]"
```

On Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[test]"
```

Run the offline test suite:

```bash
python -m pytest tests/ -q
```

The test suite uses deterministic fixtures and must not make paid API calls,
download papers, or execute real research experiments.

## What to contribute

- Bug fixes with a regression test.
- Offline tests for provider adapters, storage migrations, approval gates,
  evidence checks, and CLI error paths.
- Documentation corrections verified against the actual CLI.
- Improvements to redaction, state isolation, or defensive error handling.

## Rules

1. **No credentials.** Do not add API keys, tokens, passwords, private
   endpoints, `.env` files, or real provider responses containing secrets.
2. **No private research data.** Do not add project state, databases,
   unpublished manuscripts, patient-level data, or private prompts.
3. **Do not weaken gates.** Approval checks, checksum binding, evidence
   verification, and publication-readiness gates must stay fail-closed.
4. **Do not overclaim.** If a provider integration or experiment feature is
   not tested, say so in the documentation.
5. **Keep state out of the source tree.** Tests must use `tmp_path` or a
   temporary state directory.
6. **Preserve compatibility** unless a breaking change is discussed first.

## Pull requests

- Keep the change focused and describe the user-visible behavior.
- Include the exact test command and result in the PR description.
- Update documentation when commands, configuration, or artifacts change.
- Do not force-push a shared branch.

## Reporting security issues

Follow `SECURITY.md`; do not use a public issue.
