"""Build provider registries and role assignments from secret-free TOML."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .budget import ProviderBudgetGuard
from .capabilities import Capability, ModelRole
from .config import ModelAssignment, ProviderConfig, reject_secret_fields
from .embeddings import ExternalEmbeddingProvider
from .llm import AnthropicProvider, GeminiProvider, OpenCodeCLIProvider, OpenAICompatibleProvider
from .llm.opencode_cli import DEFAULT_AGENT, OPENCODE_CLI_ALLOWED_CAPABILITIES, OPENCODE_CLI_DEFAULT_CAPABILITIES
from .registry import ProviderRegistry
from .router import ModelRouter, UsageSink


DEFAULT_CAPABILITIES: dict[str, frozenset[Capability]] = {
    "openai_compatible": frozenset({Capability.TEXT, Capability.STREAMING, Capability.STRUCTURED_OUTPUT, Capability.TOOL_USE, Capability.VISION, Capability.LONG_CONTEXT, Capability.REASONING, Capability.CODE}),
    "anthropic": frozenset({Capability.TEXT, Capability.STREAMING, Capability.STRUCTURED_OUTPUT, Capability.TOOL_USE, Capability.VISION, Capability.LONG_CONTEXT, Capability.REASONING, Capability.CODE}),
    "gemini": frozenset({Capability.TEXT, Capability.STREAMING, Capability.STRUCTURED_OUTPUT, Capability.TOOL_USE, Capability.VISION, Capability.LONG_CONTEXT, Capability.REASONING, Capability.CODE}),
    "external_embeddings": frozenset({Capability.EMBEDDINGS}),
    "opencode_cli": OPENCODE_CLI_DEFAULT_CAPABILITIES,
}


def load_provider_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Provider configuration not found: {path}")
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    if not isinstance(document, dict):
        raise ValueError("Provider configuration must be a TOML table.")
    return document


def provider_config(name: str, raw: dict[str, Any]) -> ProviderConfig:
    reject_secret_fields(raw, location=f"providers.{name}")
    kind = str(raw.get("kind") or name)
    if kind not in DEFAULT_CAPABILITIES:
        raise ValueError(f"Unsupported provider kind '{kind}'.")
    capabilities = frozenset(Capability(value) for value in raw.get("capabilities", DEFAULT_CAPABILITIES[kind]))
    if kind == "opencode_cli":
        unsupported = capabilities - OPENCODE_CLI_ALLOWED_CAPABILITIES
        if unsupported:
            names = ", ".join(sorted(item.value for item in unsupported))
            raise ValueError(f"Provider '{name}' kind 'opencode_cli' cannot advertise: {names}.")
    base_url = str(raw.get("base_url") or "").rstrip("/")
    if not base_url and kind != "opencode_cli":
        defaults = {"anthropic": "https://api.anthropic.com", "gemini": "https://generativelanguage.googleapis.com"}
        base_url = defaults.get(kind, "")
    if not base_url and kind != "opencode_cli":
        raise ValueError(f"Provider '{name}' requires base_url.")
    cli_timeout_default = raw.get("timeout", 600.0) if kind == "opencode_cli" else 600.0
    cli_timeout_raw = raw.get("cli_timeout", cli_timeout_default)
    return ProviderConfig(
        name=name,
        kind=kind,
        base_url=base_url,
        api_key_env=str(raw.get("api_key_env") or ""),
        capabilities=capabilities,
        connect_timeout=float(raw.get("connect_timeout", 5.0)),
        read_timeout=float(raw.get("read_timeout", 0.0)),
        total_timeout=float(raw.get("total_timeout", 0.0)),
        max_retries=int(raw.get("max_retries", 2)),
        concurrency=int(raw.get("concurrency", 4)),
        max_concurrency=int(raw["max_concurrency"]) if raw.get("max_concurrency") is not None else None,
        requests_per_second=float(raw["requests_per_second"]) if raw.get("requests_per_second") is not None else None,
        thinking_budget=int(raw["thinking_budget"]) if raw.get("thinking_budget") is not None else None,
        embedding_dimension=int(raw["embedding_dimension"]) if raw.get("embedding_dimension") is not None else None,
        embedding_batch_size=int(raw.get("embedding_batch_size", 64)),
        structured_output_mode=str(raw.get("structured_output_mode", "json_schema")),
        pricing=dict(raw.get("pricing") or {}),
        cli_executable=str(raw.get("cli_executable") or raw.get("executable") or ""),
        cli_agent=str(raw.get("cli_agent") or raw.get("agent") or DEFAULT_AGENT),
        cli_timeout=float(cli_timeout_raw),
    )


def build_registry(document: dict[str, Any]) -> ProviderRegistry:
    registry = ProviderRegistry()
    for name, values in (document.get("providers") or {}).items():
        if not isinstance(values, dict):
            raise ValueError(f"providers.{name} must be a table.")
        config = provider_config(str(name), values)
        if config.kind == "external_embeddings":
            registry.register_embeddings(str(name), ExternalEmbeddingProvider(config))
        elif config.kind == "anthropic":
            registry.register_llm(str(name), AnthropicProvider(config))
        elif config.kind == "gemini":
            registry.register_llm(str(name), GeminiProvider(config))
        elif config.kind == "opencode_cli":
            registry.register_llm(str(name), OpenCodeCLIProvider(config))
        else:
            registry.register_llm(str(name), OpenAICompatibleProvider(config))
    return registry


def build_assignments(document: dict[str, Any]) -> dict[ModelRole, ModelAssignment]:
    assignments: dict[ModelRole, ModelAssignment] = {}
    for role_name, values in (document.get("models") or {}).items():
        if not isinstance(values, dict):
            raise ValueError(f"models.{role_name} must be a table.")
        reject_secret_fields(values, location=f"models.{role_name}")
        role = ModelRole(role_name)
        fallbacks = tuple((str(item["provider"]), str(item["model"])) for item in values.get("fallbacks", []))
        assignments[role] = ModelAssignment(role, str(values.get("provider") or ""), str(values.get("model") or ""), fallbacks, values.get("max_input_tokens"), values.get("max_output_tokens"))
    return assignments


def _autodetected_document() -> dict[str, Any] | None:
    """Synthesize a provider document from the OpenCode session's own
    credentials (no config file needed): the skills follow the session's AI
    model provider automatically. Returns None when nothing is usable."""
    from .credentials import detect_session_provider

    detected = detect_session_provider()
    if not detected:
        return None
    roles = {
        "literature_extraction": "literature_extraction",
        "research_reasoning": "research_reasoning",
        "scientific_critic": "scientific_critic",
    }
    models = detected.get("models") or {}
    provider = "reasoning"
    document: dict[str, Any] = {
        "providers": {
            provider: {
                "kind": str(detected["kind"]),
                "base_url": str(detected["base_url"]),
                "api_key_env": f"OPENCODE_SESSION_{str(detected['integration_id']).upper().replace('-', '_')}",
            }
        },
        "models": {
            role_name: {
                "provider": provider,
                "model": str(models.get(key) or models.get("research_reasoning") or ""),
            }
            for role_name, key in roles.items()
        },
    }
    if detected.get("structured_output_mode"):
        document["providers"][provider]["structured_output_mode"] = str(detected["structured_output_mode"])
    return document


def validate_router_identity(document: dict[str, Any], router: ModelRouter) -> None:
    """Fail closed if the effective router differs from the TOML document.

    This is deliberately checked *before* any provider call. It catches the
    exact failure mode where a missing project config silently falls back to
    an auto-detected session provider with a different name/model.
    """
    providers = document.get("providers") or {}
    for role_name, values in (document.get("models") or {}).items():
        if not isinstance(values, dict):
            continue
        role = ModelRole(str(role_name))
        assignment = router.assignments.get(role)
        expected_provider = str(values.get("provider") or "")
        expected_model = str(values.get("model") or "")
        if assignment is None:
            raise ValueError(
                f"Provider identity mismatch: role '{role.value}' is not assigned."
            )
        if assignment.provider != expected_provider or assignment.model != expected_model:
            raise ValueError(
                "Provider identity mismatch for role "
                f"'{role.value}': effective {assignment.provider}/{assignment.model}, "
                f"configured {expected_provider}/{expected_model}."
            )
        provider = router.registry.get(expected_provider)
        raw = providers.get(expected_provider) or {}
        kind = str(raw.get("kind") or expected_provider)
        if kind == "opencode_cli":
            if not isinstance(provider, OpenCodeCLIProvider):
                raise ValueError(
                    f"Provider identity mismatch: '{expected_provider}' is configured "
                    "as opencode_cli but the effective provider is "
                    f"{type(provider).__name__}."
                )
            expected_agent = str(raw.get("cli_agent") or raw.get("agent") or DEFAULT_AGENT)
            actual_agent = str(getattr(getattr(provider, "config", None), "cli_agent", "") or "")
            if actual_agent != expected_agent:
                raise ValueError(
                    f"Provider identity mismatch: agent {actual_agent!r}, "
                    f"configured {expected_agent!r}."
                )


def build_router(
    path: Path,
    *,
    usage_sink: UsageSink | None = None,
    budget_guard: ProviderBudgetGuard | None = None,
    auto_provider: bool = False,
    require_config: bool = False,
) -> ModelRouter:
    # A missing provider document is a valid local empty state UNLESS the
    # OpenCode session carries a usable credential: then the skills
    # auto-follow the session's model provider (design requirement — no
    # separate API-key setup when running inside an agent session).
    #
    # Project-scoped calls set require_config=True: the project's own config
    # must exist and parse, and its effective identity is checked before any
    # paid request. Session auto-detection is never a project fallback.
    if require_config:
        document = load_provider_document(path)
        router = ModelRouter(
            build_registry(document),
            build_assignments(document),
            usage_sink=usage_sink,
            budget_guard=budget_guard,
        )
        validate_router_identity(document, router)
        return router
    if not path.exists() or auto_provider:
        detected = _autodetected_document()
        if detected:
            return ModelRouter(build_registry(detected), build_assignments(detected), usage_sink=usage_sink, budget_guard=budget_guard)
        if not path.exists():
            return ModelRouter(ProviderRegistry(), {}, usage_sink=usage_sink, budget_guard=budget_guard)
    document = load_provider_document(path)
    router = ModelRouter(
        build_registry(document),
        build_assignments(document),
        usage_sink=usage_sink,
        budget_guard=budget_guard,
    )
    validate_router_identity(document, router)
    return router
