"""Test isolation for machine-local state.

The production defaults write project state under ``<project>/.autoresearch``
and a project registry under ``~/.autoresearch``. Tests must never touch the
developer's real registry, research databases, or checkout. This autouse
fixture redirects all three paths to the test's temporary directory; tests are
free to override them again with ``monkeypatch``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_local_state(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AUTORESEARCH_REGISTRY_PATH", str(tmp_path / "project-registry.json")
    )
    monkeypatch.setenv(
        "AUTORESEARCH_DATA_ROOT", str(tmp_path / "autoresearch-data")
    )
    # An empty, valid config keeps tests from auto-detecting a developer's
    # real OpenCode session credentials. Tests that exercise auto-detection
    # build their own isolated credential store explicitly.
    provider_config = tmp_path / "autoresearch-config.toml"
    provider_config.write_text("[providers]\n\n[models]\n", encoding="utf-8")
    monkeypatch.setenv("AUTORESEARCH_PROVIDER_CONFIG", str(provider_config))
