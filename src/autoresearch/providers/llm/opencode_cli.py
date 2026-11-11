"""OpenCode CLI adapter (``opencode run --format json``).

The CLI is a text-only transport: there is no HTTP endpoint, no API key, and
no native JSON-Schema or tool-calling contract. Structured output is therefore
prompt-enforced and validated locally; token usage and cost are never invented
because the CLI does not report them.

Prompt text is always delivered through OpenCode's ``--file`` attachment, never
as an inline command-line argument. On Windows the CLI launcher is usually a
``.cmd`` shim that ``_wrap_executable`` starts through ``cmd.exe``; cmd.exe ends
the command at the first newline and re-parses quotes, so multi-line prompts and
JSON schemas would be truncated or mangled before OpenCode ever saw them.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from ..capabilities import Capability
from ..config import ProviderConfig
from ..errors import ErrorKind, ProviderError, kind_for_status
from ..redaction import redact_text
from .base import CompletionResult, HealthResult, ModelInfo

DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"
DEFAULT_AGENT = "autoresearch-llm"
DEFAULT_TIMEOUT = 600.0

# The CLI can only be trusted for plain text, one-shot streaming, prompt-
# enforced structured output, and reasoning when the caller explicitly
# configures it. Tool use, vision, embeddings, long-context guarantees, and
# code-specific behaviour are deliberately not advertised.
OPENCODE_CLI_DEFAULT_CAPABILITIES = frozenset(
    {Capability.TEXT, Capability.STREAMING, Capability.STRUCTURED_OUTPUT}
)
OPENCODE_CLI_ALLOWED_CAPABILITIES = frozenset(
    {Capability.TEXT, Capability.STREAMING, Capability.STRUCTURED_OUTPUT, Capability.REASONING}
)

# Prompt bytes never travel through argv: every request writes a temporary
# prompt file and passes a fixed single-line message alongside --file. This is
# independent of prompt length and applies on every platform. See the module
# docstring for why inline prompts are unsafe on Windows.
PROMPT_ATTACHMENT_MESSAGE = "The complete task instructions are attached in the file. Follow them exactly."


@dataclass(frozen=True)
class ParsedEvents:
    """Normalized result of one JSONL event stream."""

    text: str
    session_id: str | None
    errors: tuple[str, ...]
    malformed: int
    event_count: int


def parse_jsonl_events(output: str) -> ParsedEvents:
    """Parse newline-delimited OpenCode events.

    Malformed lines are counted and skipped: the CLI can interleave logs or
    partial writes, and a single bad line must not discard otherwise valid
    text. A non-JSON line is only fatal when no text event was produced.
    """
    texts: list[str] = []
    errors: list[str] = []
    session_id: str | None = None
    malformed = 0
    event_count = 0
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        event_count += 1
        candidate_session = event.get("sessionID")
        if session_id is None and isinstance(candidate_session, str) and candidate_session.strip():
            session_id = candidate_session
        if event.get("type") == "error":
            message = _event_error_message(event)
            if message:
                errors.append(message)
        part = event.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            if part.get("type") == "text" or event.get("type") == "text":
                texts.append(part["text"])
    return ParsedEvents("".join(texts), session_id, tuple(errors), malformed, event_count)


def _event_error_message(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        for key in ("message", "name", "type"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(error, str) and error.strip():
        return error.strip()
    message = event.get("message")
    return message.strip() if isinstance(message, str) and message.strip() else ""


def build_command(
    executable: str,
    *,
    model: str,
    agent: str | None,
    message: str,
    prompt_file: str | None = None,
    platform: str | None = None,
    powershell: str | None = None,
    comspec: str | None = None,
) -> list[str]:
    """Build a shell-free argv for ``opencode run``.

    Windows script launchers are not native executables. ``.ps1`` files are
    run through PowerShell and ``.cmd``/``.bat`` files through cmd.exe; both
    paths keep ``shell=True`` out of the subprocess call.
    """
    args = ["run", "--format", "json", "--model", model]
    if agent:
        args.extend(["--agent", agent])
    if prompt_file:
        args.extend(["--file", prompt_file])
    args.append(message)
    return _wrap_executable(executable, args, platform=platform, powershell=powershell, comspec=comspec)


def _wrap_executable(
    executable: str,
    args: list[str],
    *,
    platform: str | None = None,
    powershell: str | None = None,
    comspec: str | None = None,
) -> list[str]:
    resolved_platform = platform or sys.platform
    lower = executable.lower()
    if resolved_platform.startswith("win"):
        if lower.endswith(".ps1"):
            return [powershell or _powershell_executable(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", executable, *args]
        if lower.endswith((".cmd", ".bat")):
            return [comspec or os.environ.get("COMSPEC") or "cmd.exe", "/d", "/c", executable, *args]
    return [executable, *args]


def _powershell_executable() -> str:
    for name in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
        found = shutil.which(name) or _which_in_path(name)
        if found:
            return found
    # Windows fallback. SystemRoot is machine-wide, not a hardcoded user name.
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    candidate = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(candidate):
        return candidate
    return "powershell.exe"


def resolve_executable(configured: str | None = None) -> str:
    """Resolve the CLI path from config or PATH without a hardcoded user name."""
    if configured and configured.strip():
        candidate = os.path.expandvars(os.path.expanduser(configured.strip()))
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
        found = shutil.which(candidate) or _which_in_path(candidate)
        if found:
            return found
        if os.path.sep in candidate or (os.path.altsep and os.path.altsep in candidate):
            return os.path.abspath(candidate)
        return candidate
    if sys.platform.startswith("win"):
        # shutil.which ignores .ps1 unless PATHEXT includes it; the verified
        # Windows launcher is opencode.ps1, so search for it explicitly.
        ps1 = _which_in_path("opencode.ps1")
        if ps1:
            return ps1
    for name in ("opencode", "opencode.cmd", "opencode.exe"):
        found = shutil.which(name)
        if found:
            return found
    raise ProviderError(
        "OpenCode CLI executable was not found. Install OpenCode or set 'cli_executable' on the provider.",
        ErrorKind.UNAVAILABLE,
        "opencode_cli",
    )


def _which_in_path(filename: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        directory = directory.strip().strip('"')
        if not directory:
            continue
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    """Flatten chat messages into one CLI prompt without losing roles."""
    rendered: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user").lower()
        content = _message_text(message.get("content"))
        if not content:
            continue
        if role == "system":
            rendered.append(f"System instructions:\n{content}")
        elif role == "assistant":
            rendered.append(f"Assistant:\n{content}")
        elif role == "tool":
            rendered.append(f"Tool result:\n{content}")
        else:
            rendered.append(content)
    if len(rendered) == 1 and str(messages[0].get("role") or "user").lower() == "user":
        return rendered[0]
    return "\n\n".join(rendered)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(part.get("content"), str):
                    parts.append(part["content"])
        return "\n".join(parts)
    return "" if content is None else str(content)


def structured_instructions(response_format: dict[str, Any]) -> str:
    """Return the prompt-enforced JSON contract for a structured request."""
    schema_document = response_format.get("json_schema")
    schema: Any = None
    name: str | None = None
    if isinstance(schema_document, dict):
        schema = schema_document.get("schema")
        candidate_name = schema_document.get("name")
        name = str(candidate_name) if candidate_name else None
    if schema is None:
        schema = response_format.get("schema")
    if schema is None:
        schema = {"type": "object"}
    lines = []
    if name:
        lines.append(f"Structured output name: {name}.")
    lines.extend(
        [
            "Respond with exactly one valid JSON object and nothing else.",
            "Do not wrap the object in markdown code fences and do not add commentary.",
            "The object must conform to this JSON Schema:",
            json.dumps(schema, ensure_ascii=False, sort_keys=True),
        ]
    )
    return "\n".join(lines)


def _validate_structured_text(text: str, provider_name: str, request_id: str | None) -> None:
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 2:
            candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProviderError(
            "OpenCode CLI returned malformed JSON for a structured request.",
            ErrorKind.SCHEMA,
            provider_name,
            request_id=request_id,
        ) from exc
    if not isinstance(value, dict):
        raise ProviderError(
            "Structured provider output must be a single JSON object.",
            ErrorKind.SCHEMA,
            provider_name,
            request_id=request_id,
        )


# Windows NTSTATUS-style process failures arrive as unsigned 32-bit exit codes
# from ``subprocess`` on some path/host combinations and as signed values on
# others. Normalize both before classification.
_WINDOWS_FAIL_FAST = {0xC0000409, 0xC0000409 - (1 << 32)}
_QUOTA_MARKERS = (
    "free usage limit",
    "usage limit reached",
    "quotaexceeded",
    "quota exceeded",
    "subscribe to opencode go",
    "insufficient quota",
)
_PAGING_MARKERS = (
    "paging file",
    "os error 1455",
    "failed to start http client thread",
)
_TRANSIENT_MARKERS = (
    "connection refused",
    "connection reset",
    "network is unreachable",
    "temporarily unavailable",
    "service unavailable",
)
_TIMEOUT_MARKERS = ("timed out", "timeout", "deadline exceeded")


def _kind_for_exit(returncode: int, *, stderr: str = "") -> ErrorKind:
    """Classify a CLI exit code and, when needed, its diagnostic stderr.

    Exit 1 is deliberately ambiguous for the OpenCode CLI: the same code can
    mean quota exhaustion, a Windows commit-limit failure, or an ordinary
    provider error.  Stderr carries the discriminator, so it is part of the
    classification rather than only the message.
    """
    lower = (stderr or "").lower()
    if any(marker in lower for marker in _QUOTA_MARKERS):
        return ErrorKind.QUOTA
    if any(marker in lower for marker in _PAGING_MARKERS):
        return ErrorKind.UNAVAILABLE
    if any(marker in lower for marker in _TIMEOUT_MARKERS):
        return ErrorKind.TIMEOUT
    if any(marker in lower for marker in _TRANSIENT_MARKERS):
        return ErrorKind.UNAVAILABLE
    if re.search(r"\b429\b", lower):
        return ErrorKind.RATE_LIMIT
    if re.search(r"\b403\b", lower):
        return ErrorKind.AUTHENTICATION
    if re.search(r"\b400\b", lower):
        return ErrorKind.INVALID_REQUEST

    if returncode in _WINDOWS_FAIL_FAST:
        # STATUS_STACK_BUFFER_OVERRUN / fail-fast. The child process crashed;
        # a fresh process is the appropriate bounded recovery.
        return ErrorKind.UNAVAILABLE
    if returncode in (401, 403):
        return ErrorKind.AUTHENTICATION
    if returncode == 429:
        return ErrorKind.RATE_LIMIT
    if returncode in (124, 408, 504):
        return ErrorKind.TIMEOUT
    if returncode in (126, 127):
        return ErrorKind.UNAVAILABLE
    if 500 <= returncode <= 599:
        return ErrorKind.SERVER
    return kind_for_status(returncode) if returncode in (400, 404, 409, 413, 422) else ErrorKind.UNKNOWN


def _bounded_detail(value: str, limit: int = 300) -> str:
    return redact_text(value.strip())[:limit]


class OpenCodeCLIProvider:
    """LLMProvider implementation backed by a fresh ``opencode run`` process."""

    def __init__(self, config: ProviderConfig) -> None:
        unsupported = frozenset(config.capabilities) - OPENCODE_CLI_ALLOWED_CAPABILITIES
        if unsupported:
            names = ", ".join(sorted(item.value for item in unsupported))
            raise ValueError(f"OpenCode CLI provider cannot advertise: {names}.")
        self.config = config
        self.name = config.name
        self.capabilities = config.capabilities
        # The CLI is a separate process per call. Enforce the configured
        # concurrency INSIDE the provider so callers cannot accidentally run
        # several children behind a nominal concurrency limit (in particular
        # the two legs of an ensemble review).
        ceiling = int(config.max_concurrency or config.concurrency or 1)
        self._gate = asyncio.Semaphore(max(1, ceiling))

    def _executable(self) -> str:
        try:
            return resolve_executable(self.config.cli_executable)
        except ProviderError as exc:
            raise ProviderError(exc.message, exc.kind, self.name, details=exc.details) from exc

    def _timeout(self) -> float:
        return float(self.config.cli_timeout or DEFAULT_TIMEOUT)

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> CompletionResult:
        if tools:
            raise ProviderError(
                "OpenCode CLI provider does not support tool calls.",
                ErrorKind.UNSUPPORTED,
                self.name,
            )
        # temperature/max_tokens are accepted for interface compatibility, but
        # the CLI exposes no equivalent controls: they are neither forwarded
        # nor reported as if they had been enforced.
        effective_model = model or DEFAULT_MODEL
        prompt = _messages_to_prompt(messages)
        if response_format:
            instructions = structured_instructions(response_format)
            prompt = f"{prompt}\n\n{instructions}" if prompt else instructions
        if not prompt.strip():
            raise ProviderError("OpenCode CLI request contained no prompt text.", ErrorKind.INVALID_REQUEST, self.name)

        attempts = max(1, int(getattr(self.config, "max_retries", 0) or 0) + 1)
        last_error: ProviderError | None = None
        for attempt in range(attempts):
            try:
                return await self._complete_once(
                    model=effective_model,
                    prompt=prompt,
                    response_format=response_format,
                )
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable or attempt >= attempts - 1:
                    raise
                delay = exc.retry_after
                if delay is None:
                    delay = min(8.0, 0.5 * (2**attempt))
                await asyncio.sleep(max(0.0, float(delay)))
        assert last_error is not None
        raise last_error

    async def _complete_once(
        self,
        *,
        model: str,
        prompt: str,
        response_format: dict[str, Any] | None,
    ) -> CompletionResult:
        """One bounded CLI attempt; the caller owns retry policy."""
        async with self._gate:
            command, prompt_file = self._build_invocation(model, prompt)
            try:
                stdout, stderr, returncode = await self._run(command)
            finally:
                if prompt_file:
                    with suppress(OSError):
                        os.unlink(prompt_file)

        parsed = parse_jsonl_events(stdout)
        if returncode != 0:
            raise self._process_error(returncode, parsed, stderr)
        if not parsed.text.strip():
            detail = "; ".join(parsed.errors[:3])
            message = "OpenCode CLI returned no text output."
            if detail:
                message = f"{message} {_bounded_detail(detail)}"
            raise ProviderError(
                message,
                ErrorKind.SCHEMA,
                self.name,
                request_id=parsed.session_id,
                details={"event_count": parsed.event_count, "malformed_events": parsed.malformed},
            )
        if response_format:
            _validate_structured_text(parsed.text, self.name, parsed.session_id)
        return CompletionResult(
            text=parsed.text,
            model=model,
            prompt_tokens=None,
            completion_tokens=None,
            request_id=parsed.session_id,
            estimated_cost=None,
            currency=None,
            raw={
                "tool_calls": [],
                "finish_reason": "stop",
                "event_count": parsed.event_count,
                "malformed_events": parsed.malformed,
                "session_id": parsed.session_id,
            },
        )

    def _build_invocation(self, model: str, prompt: str) -> tuple[list[str], str | None]:
        # Always attach the prompt. Leaving it in argv is unsafe wherever the
        # launcher is a cmd.exe shim: newlines terminate the command and quotes
        # are re-parsed, so the model would receive only a fragment of the
        # request. The fixed message below is single-line and contains no quotes.
        prompt_file = _write_prompt_file(prompt)
        try:
            command = build_command(
                self._executable(),
                model=model,
                agent=self.config.cli_agent or DEFAULT_AGENT,
                message=PROMPT_ATTACHMENT_MESSAGE,
                prompt_file=prompt_file,
            )
        except Exception:
            with suppress(OSError):
                os.unlink(prompt_file)
            raise
        return command, prompt_file

    def _process_error(self, returncode: int, parsed: ParsedEvents, stderr: str) -> ProviderError:
        details: list[str] = []
        details.extend(parsed.errors[:3])
        if stderr.strip():
            details.append(stderr.strip())
        detail_text = " ".join(_bounded_detail(item) for item in details if item.strip())
        message = f"OpenCode CLI exited with code {returncode}."
        if detail_text:
            message = f"{message} {detail_text}"
        return ProviderError(
            message,
            _kind_for_exit(returncode, stderr=stderr),
            self.name,
            request_id=parsed.session_id,
            details={
                "exit_code": returncode,
                "event_count": parsed.event_count,
                "malformed_events": parsed.malformed,
                # Keep a bounded, redacted stderr copy for post-mortem
                # diagnosis even when the message is truncated.
                "stderr": _bounded_detail(stderr, 1200),
            },
        )

    async def _run(self, command: list[str]) -> tuple[str, str, int]:
        try:
            process = await _create_subprocess(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ProviderError(
                f"OpenCode CLI executable not found: {command[0]}",
                ErrorKind.UNAVAILABLE,
                self.name,
            ) from exc
        except OSError as exc:
            raise ProviderError(
                f"Could not start the OpenCode CLI: {_bounded_detail(str(exc))}",
                ErrorKind.UNAVAILABLE,
                self.name,
            ) from exc

        timeout = self._timeout()
        communicate = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communicate), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            await self._kill_process_tree(process)
            if not communicate.done():
                communicate.cancel()
            with suppress(BaseException):
                await communicate
            raise ProviderError(
                f"OpenCode CLI timed out after {timeout:g} seconds.",
                ErrorKind.TIMEOUT,
                self.name,
            ) from None
        except asyncio.CancelledError:
            await self._kill_process_tree(process)
            if not communicate.done():
                communicate.cancel()
            with suppress(BaseException):
                await communicate
            raise
        except Exception as exc:
            await self._kill_process_tree(process)
            if not communicate.done():
                communicate.cancel()
            with suppress(BaseException):
                await communicate
            raise ProviderError(
                f"OpenCode CLI communication failed: {_bounded_detail(str(exc))}",
                ErrorKind.UNAVAILABLE,
                self.name,
            ) from exc
        return _decode(stdout), _decode(stderr), process.returncode if process.returncode is not None else 0

    async def _kill_process_tree(self, process: Any) -> None:
        pid = getattr(process, "pid", None)
        killed = False
        if sys.platform.startswith("win") and pid:
            try:
                killer = await _create_subprocess(
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(pid),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=5)
                killed = True
            except Exception:
                killed = False
        if not killed:
            with suppress(ProcessLookupError, AttributeError):
                process.kill()
        with suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        async def iterator() -> AsyncIterator[str]:
            result = await self.complete(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            yield result.text

        return iterator()

    async def health_check(self) -> HealthResult:
        started = time.perf_counter()
        try:
            self._executable()
        except ProviderError as exc:
            return HealthResult(False, exc.kind.value, str(exc), (time.perf_counter() - started) * 1000)
        return HealthResult(
            True,
            "ready",
            "OpenCode CLI executable is available.",
            (time.perf_counter() - started) * 1000,
        )

    async def model_info(self, model: str) -> ModelInfo:
        return ModelInfo(self.name, model or DEFAULT_MODEL, self.capabilities)

    async def close(self) -> None:
        return None


def _write_prompt_file(prompt: str) -> str:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False)
    try:
        handle.write(prompt)
        return handle.name
    finally:
        handle.close()


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def _create_subprocess(*args: Any, **kwargs: Any) -> Any:
    """Indirection so tests can replace process creation without a live CLI."""
    return await asyncio.create_subprocess_exec(*args, **kwargs)
