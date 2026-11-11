# Provider configuration

AutoResearch stores provider *names* in TOML and reads secrets from the
environment. A provider table contains:

| Key | Meaning |
|---|---|
| `kind` | `openai_compatible`, `anthropic`, `gemini`, `opencode_cli`, or `external_embeddings` |
| `base_url` | API endpoint (not used by `opencode_cli`) |
| `api_key_env` | Environment variable that holds the key |
| `capabilities` | Optional capability override |
| `concurrency` / `max_concurrency` | In-flight request control |
| `requests_per_second` | Client-side rate limit |
| `max_retries` | Retry limit for throttle/transient errors |
| `read_timeout` / `total_timeout` | Deadlines; `0` selects finite defaults |
| `structured_output_mode` | `json_schema` (default) or `json_object` |
| `cli_executable` / `cli_agent` / `cli_timeout` | OpenCode CLI options |

Three model roles are required for the default pipeline:

```toml
[models.literature_extraction]
provider = "reasoning"
model = "<model-id>"

[models.research_reasoning]
provider = "reasoning"
model = "<model-id>"

[models.scientific_critic]
provider = "reasoning"
model = "<model-id>"
```

An optional embeddings role enables embedding-based review ranking; without
it the ranker uses deterministic lexical similarity.

## OpenAI-compatible example

```toml
[providers.reasoning]
kind = "openai_compatible"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
structured_output_mode = "json_schema"
```

## DeepSeek example

```toml
[providers.reasoning]
kind = "openai_compatible"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
structured_output_mode = "json_object"
```

## Anthropic example

```toml
[providers.reasoning]
kind = "anthropic"
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"
```

## Gemini example

```toml
[providers.reasoning]
kind = "gemini"
base_url = "https://generativelanguage.googleapis.com"
api_key_env = "GEMINI_API_KEY"
```

## OpenCode CLI example

The CLI must already be installed and authenticated. No API key or HTTP
endpoint is required.

```toml
[providers.opencode]
kind = "opencode_cli"
cli_executable = "opencode"
cli_agent = "autoresearch-llm"
cli_timeout = 600
```

## Credential resolution order

For an HTTP provider, the adapter resolves a key in this order:

1. the environment variable named by `api_key_env`;
2. a matching active API credential in the OpenCode credential store, for a
   recognized official endpoint;
3. for Anthropic-protocol providers only, ambient `ANTHROPIC_API_KEY` /
   `ANTHROPIC_AUTH_TOKEN` or the local Claude connector settings.

Nothing is copied into project state. OAuth and subscription tokens are not
repurposed as API keys.

## Validation and failure behavior

- Unknown provider kinds, missing `base_url`, inline secret fields, and
  missing role assignments fail before a request is sent.
- A resumed project must have its own `.autoresearch/config.toml`; a missing
  or invalid file fails closed rather than silently switching to an
  auto-detected session provider.
- `autoresearch skill doctor` reports role assignment and local credential
  availability. It does not perform a live model request.

## Tested surface

The release tests use fake providers and offline fixtures. They verify
configuration parsing, endpoint restrictions, credential-error handling, and
request/response translation where a transport fake is used. They do not
verify live vendor accounts, model availability, or billing behavior.
