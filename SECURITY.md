# Security policy

## Reporting a vulnerability

Do **not** open a public issue for a security problem.

Use GitHub's private vulnerability reporting on this repository:
**Security -> Advisories -> Report a vulnerability**. Include:

- affected version or commit,
- a minimal reproduction,
- the impact you observed,
- whether credentials or research data may have been exposed.

Do not include real API keys, patient-level data, unpublished manuscripts, or
private project-state files in a report. If you must demonstrate a leaked
credential, redact it and state that it was rotated.

This project does not currently offer a bug bounty. Reports are reviewed on a
best-effort basis.

## Scope

Security-relevant areas include:

- provider credential resolution and redaction,
- the local experiment worker and approval gate,
- project-state isolation and registry handling,
- prompt/response handling that could lead to command execution,
- dependency vulnerabilities in the runtime and test stack.

## Security design

- **Credentials:** provider adapters read keys from environment variables
  named by `api_key_env`; TOML files contain names, not secrets. A credential
  may also resolve from an agent session's credential store where that is
  explicitly supported by the adapter. OAuth/subscription tokens are not
  repurposed as API keys.
- **Redaction:** CLI output and provider errors pass through the redaction
  helpers before printing.
- **State isolation:** each project stores its database, artifacts,
  downloads, and workspace under `<project>/.autoresearch/`. A machine-local
  registry maps project IDs to state paths. Neither location is part of the
  source release.
- **Execution:** the experiment worker accepts only an approved action
  vector and runs in the project workspace. This is workspace containment,
  not an OS sandbox.
- **Evidence:** execution requires an exact specification checksum; fixture
  and simulation artifacts are labelled and cannot pass empirical publication
  gates.

## If a secret is exposed

1. Revoke and rotate the credential at the provider immediately.
2. Remove it from the working tree and from the Git history before publishing.
3. Check CI logs, caches, issue attachments, and release artifacts.
4. Never assume `.gitignore` removes a secret that was already committed.

## Supported versions

Security fixes are applied to the current release line. There is no
long-term-support branch at this time.
