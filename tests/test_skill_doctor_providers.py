"""Focused regression tests for skill-doctor provider checks."""

from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from autoresearch.cli import skills
from autoresearch.providers.capabilities import Capability
from autoresearch.providers.config import ProviderConfig
from autoresearch.providers.errors import ErrorKind, ProviderError
from autoresearch.providers.llm import OpenCodeCLIProvider, OpenAICompatibleProvider
from autoresearch.providers.registry import ProviderRegistry

REQUIRED_ROLES = ("literature_extraction", "research_reasoning", "scientific_critic")


def _cli_config() -> ProviderConfig:
    return ProviderConfig(
        name="reasoning",
        kind="opencode_cli",
        base_url="",
        api_key_env="",
        capabilities=frozenset({Capability.TEXT}),
        cli_executable="opencode",
    )


def _api_config() -> ProviderConfig:
    return ProviderConfig(
        name="reasoning",
        kind="openai_compatible",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DOCTOR_TEST_MISSING_KEY",
        capabilities=frozenset({Capability.TEXT}),
    )


def _invoke_doctor(monkeypatch, provider):
    registry = ProviderRegistry()
    registry.register_llm(provider.name, provider)

    async def close():
        await registry.close()

    fake = SimpleNamespace(
        router=SimpleNamespace(
            assignments={role: SimpleNamespace(provider=provider.name) for role in REQUIRED_ROLES},
            registry=registry,
        ),
        close=close,
    )
    monkeypatch.setattr(skills, "ApplicationContainer", lambda _: fake)
    result = CliRunner().invoke(skills.skill_app, ["doctor"])
    checks = json.loads(result.output)["checks"]
    return next(check for check in checks if check["check"] == "providers")


def test_doctor_opencode_cli_needs_no_api_key(monkeypatch):
    import autoresearch.providers.llm.opencode_cli as module

    monkeypatch.setattr(module, "resolve_executable", lambda configured=None: "opencode")
    resolved: list[ProviderConfig] = []

    def record(config):
        resolved.append(config)
        return "should-not-be-used"

    monkeypatch.setattr(skills, "resolve_api_key", record)

    check = _invoke_doctor(monkeypatch, OpenCodeCLIProvider(_cli_config()))

    assert check["ok"] is True
    assert "OpenCode CLI executable available" in check["detail"]
    assert "live model authentication not checked" in check["detail"]
    assert "credentials resolved" not in check["detail"]
    assert resolved == []


def test_doctor_reports_missing_opencode_executable(monkeypatch):
    import autoresearch.providers.llm.opencode_cli as module

    def missing(configured=None):
        raise ProviderError(
            "OpenCode CLI executable was not found. Install OpenCode or set 'cli_executable' on the provider.",
            ErrorKind.UNAVAILABLE,
            "reasoning",
        )

    monkeypatch.setattr(module, "resolve_executable", missing)
    resolved: list[ProviderConfig] = []
    monkeypatch.setattr(skills, "resolve_api_key", lambda config: resolved.append(config) or "should-not-be-used")

    check = _invoke_doctor(monkeypatch, OpenCodeCLIProvider(_cli_config()))

    assert check["ok"] is False
    assert "OpenCode CLI executable unavailable" in check["detail"]
    assert "not found" in check["detail"]
    assert "credentials unavailable" not in check["detail"]
    assert resolved == []


def test_doctor_still_requires_credentials_for_api_provider(monkeypatch):
    provider = OpenAICompatibleProvider(_api_config())
    resolved: list[ProviderConfig] = []

    def missing(config):
        resolved.append(config)
        raise ProviderError("No API key is available.", ErrorKind.AUTHENTICATION, "reasoning")

    async def unexpected_health_check():
        raise AssertionError("HTTP providers must use credential resolution, not health_check()")

    monkeypatch.setattr(skills, "resolve_api_key", missing)
    monkeypatch.setattr(provider, "health_check", unexpected_health_check)

    check = _invoke_doctor(monkeypatch, provider)

    assert check["ok"] is False
    assert "credentials unavailable for: reasoning" in check["detail"]
    assert "OpenCode CLI" not in check["detail"]
    assert resolved == [provider.config]


def test_doctor_api_success_still_reports_resolved_credentials(monkeypatch):
    provider = OpenAICompatibleProvider(_api_config())
    resolved: list[ProviderConfig] = []

    async def unexpected_health_check():
        raise AssertionError("HTTP providers must use credential resolution, not health_check()")

    monkeypatch.setattr(skills, "resolve_api_key", lambda config: resolved.append(config) or "fixture-key")
    monkeypatch.setattr(provider, "health_check", unexpected_health_check)

    check = _invoke_doctor(monkeypatch, provider)

    assert check["ok"] is True
    assert "API credentials resolved locally" in check["detail"]
    assert "OpenCode CLI" not in check["detail"]
    assert "live model authentication not checked" in check["detail"]
    assert resolved == [provider.config]
