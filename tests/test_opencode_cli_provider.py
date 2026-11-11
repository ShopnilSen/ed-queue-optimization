"""Offline tests for the OpenCode CLI provider; no live model calls."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress

import pytest

from autoresearch.providers.capabilities import Capability
from autoresearch.providers.config import ProviderConfig
from autoresearch.providers.errors import ErrorKind, ProviderError
from autoresearch.providers.factory import build_registry, provider_config
from autoresearch.providers.llm.opencode_cli import (
    DEFAULT_AGENT,
    DEFAULT_MODEL,
    OpenCodeCLIProvider,
    PROMPT_ATTACHMENT_MESSAGE,
    build_command,
    parse_jsonl_events,
    structured_instructions,
)


class FakeProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, pid: int = 4242, hang: bool = False) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = pid
        self.hang = hang
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.hang:
            await asyncio.Event().wait()
        return self.stdout, self.stderr

    async def wait(self) -> int:
        self.waited = True
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def text_event(text: str, session_id: str = "ses_test") -> str:
    return json.dumps({"type": "text", "sessionID": session_id, "part": {"type": "text", "text": text}})


def config(**overrides) -> ProviderConfig:
    values = {
        "name": "reasoning",
        "kind": "opencode_cli",
        "base_url": "",
        "api_key_env": "",
        "capabilities": frozenset({Capability.TEXT, Capability.STREAMING, Capability.STRUCTURED_OUTPUT}),
        "cli_executable": "opencode",
        "cli_agent": DEFAULT_AGENT,
        "cli_timeout": 5.0,
    }
    values.update(overrides)
    return ProviderConfig(**values)


def offline_provider(monkeypatch, process: FakeProcess, **overrides) -> tuple[OpenCodeCLIProvider, list[list[str]]]:
    import autoresearch.providers.llm.opencode_cli as module

    provider = OpenCodeCLIProvider(config(**overrides))
    captured: list[list[str]] = []

    monkeypatch.setattr(module, "resolve_executable", lambda configured=None: "opencode")

    async def fake_spawn(*args, **kwargs):
        captured.append(list(args))
        return process

    monkeypatch.setattr(module, "_create_subprocess", fake_spawn)
    return provider, captured


def capture_prompt_files(monkeypatch) -> list[tuple[str, str]]:
    """Record every (path, prompt) written for --file transport."""
    import autoresearch.providers.llm.opencode_cli as module

    created: list[tuple[str, str]] = []
    real_write = module._write_prompt_file

    def capture(prompt: str) -> str:
        path = real_write(prompt)
        created.append((path, prompt))
        return path

    monkeypatch.setattr(module, "_write_prompt_file", capture)
    return created


def attachment_arg(command: list[str]) -> str:
    assert "--file" in command
    return command[command.index("--file") + 1]



def test_jsonl_parsing_skips_malformed_lines_and_keeps_valid_text():
    output = "\n".join(
        [
            "not json at all",
            json.dumps({"type": "step_start", "sessionID": "ses_1"}),
            text_event("Hello", "ses_1"),
            json.dumps({"type": "text", "sessionID": "ses_1", "part": {"type": "text", "text": " world"}}),
        ]
    )
    parsed = parse_jsonl_events(output)
    assert parsed.text == "Hello world"
    assert parsed.session_id == "ses_1"
    assert parsed.malformed == 1
    assert parsed.event_count == 3


def test_session_id_extraction_prefers_first_non_empty_value():
    output = "\n".join(
        [
            json.dumps({"type": "step_start", "sessionID": ""}),
            json.dumps({"type": "step_start", "sessionID": "ses_first"}),
            text_event("ok", "ses_second"),
        ]
    )
    parsed = parse_jsonl_events(output)
    assert parsed.session_id == "ses_first"


def test_multiple_text_events_are_concatenated():
    output = "\n".join([text_event("alpha"), text_event(" beta"), text_event(" gamma")])
    assert parse_jsonl_events(output).text == "alpha beta gamma"


async def test_complete_extracts_text_session_and_does_not_invent_usage(monkeypatch):
    stdout = "\n".join(
        [
            json.dumps({"type": "step_start", "sessionID": "ses_abc"}),
            text_event("Hello", "ses_abc"),
            text_event(" world", "ses_abc"),
        ]
    ).encode()
    process = FakeProcess(stdout=stdout)
    provider, captured = offline_provider(monkeypatch, process)
    prompts = capture_prompt_files(monkeypatch)

    result = await provider.complete(model="", messages=[{"role": "user", "content": "hi"}])

    assert result.text == "Hello world"
    assert result.model == DEFAULT_MODEL
    assert result.request_id == "ses_abc"
    assert result.prompt_tokens is None
    assert result.completion_tokens is None
    assert result.estimated_cost is None
    assert result.currency is None
    assert result.raw["tool_calls"] == []
    command = captured[0]
    assert command[0] == "opencode"
    assert command[1:3] == ["run", "--format"]
    assert command[3] == "json"
    assert command[4:6] == ["--model", DEFAULT_MODEL]
    assert command[6:8] == ["--agent", DEFAULT_AGENT]
    assert command[8] == "--file"
    assert command[-1] == PROMPT_ATTACHMENT_MESSAGE
    assert prompts[0][1] == "hi"
    assert not os.path.exists(prompts[0][0])
    assert "--continue" not in command and "--session" not in command


async def test_structured_output_rejects_malformed_json(monkeypatch):
    process = FakeProcess(stdout=text_event("this is not json").encode())
    provider, _ = offline_provider(monkeypatch, process)

    with pytest.raises(ProviderError) as exc:
        await provider.complete(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": "give me json"}],
            response_format={"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object"}}},
        )

    assert exc.value.kind is ErrorKind.SCHEMA


async def test_structured_output_accepts_one_json_object(monkeypatch):
    payload = json.dumps({"answer": 42})
    process = FakeProcess(stdout=text_event(payload).encode())
    provider, captured = offline_provider(monkeypatch, process)
    prompts = capture_prompt_files(monkeypatch)

    result = await provider.complete(
        model=DEFAULT_MODEL,
        messages=[{"role": "user", "content": "give me json"}],
        response_format={"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object"}}},
    )

    assert json.loads(result.text) == {"answer": 42}
    assert "exactly one valid JSON object" in prompts[0][1]
    assert "JSON Schema" in prompts[0][1]
    assert captured[0][-1] == PROMPT_ATTACHMENT_MESSAGE


async def test_missing_text_raises_schema_error(monkeypatch):
    process = FakeProcess(stdout=json.dumps({"type": "step_start", "sessionID": "ses_empty"}).encode())
    provider, _ = offline_provider(monkeypatch, process)

    with pytest.raises(ProviderError) as exc:
        await provider.complete(model=DEFAULT_MODEL, messages=[{"role": "user", "content": "hi"}])

    assert exc.value.kind is ErrorKind.SCHEMA
    assert exc.value.request_id == "ses_empty"


async def test_non_zero_exit_is_reported_and_secrets_are_redacted(monkeypatch):
    stdout = json.dumps(
        {"type": "error", "sessionID": "ses_fail", "error": {"message": "boom api_key=supersecretvalue"}}
    ).encode()
    stderr = b"Authorization: Bearer supersecretvalue"
    process = FakeProcess(stdout=stdout, stderr=stderr, returncode=1)
    provider, _ = offline_provider(monkeypatch, process)

    with pytest.raises(ProviderError) as exc:
        await provider.complete(model=DEFAULT_MODEL, messages=[{"role": "user", "content": "hi"}])

    message = str(exc.value)
    assert "exited with code 1" in message
    assert "supersecretvalue" not in message
    assert exc.value.request_id == "ses_fail"


@pytest.mark.parametrize(
    ("returncode", "kind"),
    [
        (401, ErrorKind.AUTHENTICATION),
        (429, ErrorKind.RATE_LIMIT),
        (500, ErrorKind.SERVER),
        (127, ErrorKind.UNAVAILABLE),
    ],
)
async def test_exit_codes_map_to_provider_error_kinds(monkeypatch, returncode, kind):
    process = FakeProcess(returncode=returncode)
    provider, _ = offline_provider(monkeypatch, process)

    with pytest.raises(ProviderError) as exc:
        await provider.complete(model=DEFAULT_MODEL, messages=[{"role": "user", "content": "hi"}])

    assert exc.value.kind is kind


async def test_timeout_kills_the_process_tree_and_raises_timeout(monkeypatch):
    process = FakeProcess(hang=True)
    provider, _ = offline_provider(
        monkeypatch, process, cli_timeout=0.01, max_retries=0
    )
    killed: list[FakeProcess] = []

    async def fake_kill(target):
        killed.append(target)

    monkeypatch.setattr(provider, "_kill_process_tree", fake_kill)

    with pytest.raises(ProviderError) as exc:
        await provider.complete(model=DEFAULT_MODEL, messages=[{"role": "user", "content": "hi"}])

    assert exc.value.kind is ErrorKind.TIMEOUT
    assert killed == [process]


async def test_missing_executable_raises_unavailable(monkeypatch):
    import autoresearch.providers.llm.opencode_cli as module

    provider = OpenCodeCLIProvider(config())

    monkeypatch.setattr(module, "resolve_executable", lambda configured=None: "opencode")

    async def fail(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(module, "_create_subprocess", fail)

    with pytest.raises(ProviderError) as exc:
        await provider.complete(model=DEFAULT_MODEL, messages=[{"role": "user", "content": "hi"}])

    assert exc.value.kind is ErrorKind.UNAVAILABLE


def test_windows_ps1_command_uses_powershell_without_shell():
    script = r"C:\Users\someone\AppData\Roaming\npm\opencode.ps1"
    command = build_command(
        script,
        model=DEFAULT_MODEL,
        agent=DEFAULT_AGENT,
        message="hi",
        platform="win32",
        powershell=r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    )
    assert command[0].endswith("powershell.exe")
    assert command[1:6] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]
    assert command[6] == script
    assert command[7:10] == ["run", "--format", "json"]
    assert command[10:12] == ["--model", DEFAULT_MODEL]
    assert command[12:14] == ["--agent", DEFAULT_AGENT]
    assert command[-1] == "hi"


def test_configured_bare_executable_resolves_through_path(monkeypatch, tmp_path):
    import autoresearch.providers.llm.opencode_cli as module

    script = tmp_path / "opencode.ps1"
    script.write_text("")
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(module, "_which_in_path", lambda name: str(script) if name == "opencode.ps1" else None)

    assert module.resolve_executable("opencode.ps1") == str(script)


def test_powershell_resolution_uses_machine_wide_system_root_fallback(monkeypatch, tmp_path):
    import autoresearch.providers.llm.opencode_cli as module

    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(module, "_which_in_path", lambda name: None)
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    powershell = tmp_path / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_text("")

    assert module._powershell_executable() == str(powershell)


def test_windows_cmd_command_uses_cmd_exe_without_shell():
    script = r"C:\Users\someone\AppData\Roaming\npm\opencode.cmd"
    command = build_command(script, model=DEFAULT_MODEL, agent=None, message="hi", platform="win32", comspec="cmd.exe")
    assert command[:4] == ["cmd.exe", "/d", "/c", script]
    assert "--agent" not in command


def test_non_windows_executable_is_invoked_directly():
    command = build_command("opencode", model=DEFAULT_MODEL, agent=DEFAULT_AGENT, message="hi", platform="linux")
    assert command[0] == "opencode"
    assert command[1] == "run"


async def test_tool_calls_are_rejected_without_spawning(monkeypatch):
    process = FakeProcess()
    provider, captured = offline_provider(monkeypatch, process)

    with pytest.raises(ProviderError) as exc:
        await provider.complete(
            model=DEFAULT_MODEL,
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "lookup"}}],
        )

    assert exc.value.kind is ErrorKind.UNSUPPORTED
    assert captured == []


def test_structured_instructions_include_schema_and_single_object_rule():
    instructions = structured_instructions(
        {
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "integer"}},
                    "required": ["answer"],
                },
            },
        }
    )
    assert "answer" in instructions
    assert "exactly one valid JSON object" in instructions
    assert "markdown code fences" in instructions
    assert '"required": ["answer"]' in instructions


def test_factory_registers_opencode_cli_without_credentials_or_endpoint():
    registry = build_registry({"providers": {"reasoning": {"kind": "opencode_cli"}}})
    provider = registry.get("reasoning")

    assert isinstance(provider, OpenCodeCLIProvider)
    assert provider.config.kind == "opencode_cli"
    assert provider.config.api_key_env == ""
    assert provider.config.base_url == ""
    assert provider.config.cli_agent == DEFAULT_AGENT
    assert provider.capabilities == frozenset(
        {Capability.TEXT, Capability.STREAMING, Capability.STRUCTURED_OUTPUT}
    )
    assert Capability.TOOL_USE not in provider.capabilities
    assert Capability.EMBEDDINGS not in provider.capabilities
    assert Capability.VISION not in provider.capabilities
    assert Capability.LONG_CONTEXT not in provider.capabilities
    assert Capability.CODE not in provider.capabilities


def test_opencode_cli_configuration_aliases_and_overrides():
    config = provider_config(
        "reasoning",
        {
            "kind": "opencode_cli",
            "executable": r"C:\tools\opencode.ps1",
            "agent": "custom-agent",
            "timeout": 42,
        },
    )
    assert config.cli_executable == r"C:\tools\opencode.ps1"
    assert config.cli_agent == "custom-agent"
    assert config.cli_timeout == 42


def test_opencode_cli_reasoning_is_opt_in_and_unsupported_capabilities_are_rejected():
    default = provider_config("reasoning", {"kind": "opencode_cli"})
    assert Capability.REASONING not in default.capabilities

    opted_in = provider_config(
        "reasoning",
        {"kind": "opencode_cli", "capabilities": ["text_generation", "reasoning"]},
    )
    assert Capability.REASONING in opted_in.capabilities

    with pytest.raises(ValueError):
        provider_config(
            "reasoning",
            {"kind": "opencode_cli", "capabilities": ["text_generation", "tool_use"]},
        )


async def test_prompt_is_always_attached_and_cleaned_up(monkeypatch):
    process = FakeProcess(stdout=text_event("ok").encode())
    provider, captured = offline_provider(monkeypatch, process)
    prompts = capture_prompt_files(monkeypatch)

    await provider.complete(model=DEFAULT_MODEL, messages=[{"role": "user", "content": "hi"}])

    command = captured[0]
    assert command[-1] == PROMPT_ATTACHMENT_MESSAGE
    assert prompts[0][1] == "hi"
    assert not os.path.exists(attachment_arg(command))


async def test_multiline_structured_prompt_never_reaches_argv(monkeypatch):
    process = FakeProcess(stdout=text_event(json.dumps({"answer": 42})).encode())
    provider, captured = offline_provider(monkeypatch, process)
    prompts = capture_prompt_files(monkeypatch)

    await provider.complete(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": "Extract only information supported by the paper."},
            {"role": "user", "content": "Title: Example\n\nReturn the requested fields."},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": {"type": "object", "properties": {"answer": {"type": "integer"}}}},
        },
    )

    command = captured[0]
    prompt = prompts[0][1]
    assert "\n" in prompt and '"' in prompt
    assert attachment_arg(command) == prompts[0][0]
    assert all("\n" not in part and "\r" not in part and '"' not in part for part in command)
    assert command[-1] == PROMPT_ATTACHMENT_MESSAGE


async def test_prompt_file_is_cleaned_when_executable_resolution_fails(monkeypatch):
    import autoresearch.providers.llm.opencode_cli as module

    prompts = capture_prompt_files(monkeypatch)

    def fail(configured=None):
        raise ProviderError("missing executable", ErrorKind.UNAVAILABLE, "reasoning")

    monkeypatch.setattr(module, "resolve_executable", fail)
    provider = OpenCodeCLIProvider(config())

    with pytest.raises(ProviderError) as exc:
        await provider.complete(model=DEFAULT_MODEL, messages=[{"role": "user", "content": "hi"}])

    assert exc.value.kind is ErrorKind.UNAVAILABLE
    assert prompts
    assert not os.path.exists(prompts[0][0])


async def test_prompt_file_is_cleaned_after_non_zero_exit(monkeypatch):
    stdout = json.dumps({"type": "error", "sessionID": "ses_fail", "error": {"message": "boom"}}).encode()
    process = FakeProcess(stdout=stdout, returncode=1)
    provider, captured = offline_provider(monkeypatch, process)
    prompts = capture_prompt_files(monkeypatch)

    with pytest.raises(ProviderError):
        await provider.complete(model=DEFAULT_MODEL, messages=[{"role": "user", "content": "hi"}])

    assert captured
    assert prompts
    assert not os.path.exists(prompts[0][0])
    assert not os.path.exists(attachment_arg(captured[0]))


async def test_stream_emits_one_completed_chunk(monkeypatch):
    process = FakeProcess(stdout=text_event("streamed").encode())
    provider, _ = offline_provider(monkeypatch, process)

    chunks = [chunk async for chunk in provider.stream(model=DEFAULT_MODEL, messages=[{"role": "user", "content": "hi"}])]

    assert chunks == ["streamed"]


WINDOWS_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="cmd.exe argument transport is Windows-specific",
)


def make_argv_probe_shim(tmp_path) -> str:
    """Create a .cmd launcher that reports the argv it actually receives."""
    probe = tmp_path / "argv_probe.py"
    probe.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    shim = tmp_path / "launcher.cmd"
    shim.write_text(
        "@echo off\r\n"
        f'"{sys.executable}" "%~dp0argv_probe.py" %*\r\n',
        encoding="utf-8",
    )
    return str(shim)


async def run_through_cmd(command: list[str]) -> list[str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode(errors="replace")
    return json.loads(stdout.decode())


@WINDOWS_ONLY
async def test_windows_cmd_truncates_a_multiline_inline_argument(tmp_path):
    """The failure mode: cmd.exe keeps only the first line of an inline prompt."""
    shim = make_argv_probe_shim(tmp_path)
    prompt = 'System instructions:\nExtract only information. Return {"answer":42} now.'

    delivered = await run_through_cmd(["cmd.exe", "/d", "/c", shim, "run", "--agent", "plan", prompt])

    assert delivered[-1] == "System instructions:"
    assert "Return" not in delivered[-1]


@WINDOWS_ONLY
async def test_windows_cmd_roundtrip_delivers_full_prompt_only_via_file(monkeypatch, tmp_path):
    """Regression: the built command survives cmd.exe with the prompt in --file."""
    import autoresearch.providers.llm.opencode_cli as module

    shim = make_argv_probe_shim(tmp_path)
    monkeypatch.setattr(module, "resolve_executable", lambda configured=None: shim)

    prompt = (
        "System instructions:\n"
        "Extract only information supported by the supplied seed paper.\n\n"
        'Prompt version: seed-analysis-v1\nReturn {"answer":42} as one JSON object.'
    )
    provider = OpenCodeCLIProvider(config())
    command, prompt_file = provider._build_invocation(DEFAULT_MODEL, prompt)
    try:
        assert prompt_file is not None
        with open(prompt_file, encoding="utf-8") as handle:
            assert handle.read() == prompt
        assert all("\n" not in part and "\r" not in part and '"' not in part for part in command)

        delivered = await run_through_cmd(command)

        assert delivered[0:3] == ["run", "--format", "json"]
        assert delivered[4] == DEFAULT_MODEL
        assert delivered[6] == DEFAULT_AGENT
        assert delivered[7] == "--file"
        assert delivered[8] == prompt_file
        assert delivered[-1] == PROMPT_ATTACHMENT_MESSAGE
        assert prompt not in delivered
    finally:
        if prompt_file:
            with suppress(OSError):
                os.unlink(prompt_file)
