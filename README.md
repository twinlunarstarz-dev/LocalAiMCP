# LocalAiMCP

A stateless, asynchronous FastMCP control plane for LocalAI. The bundled LocalAI Swagger contains **114 paths / 123 operations**, and all 123 remain usable through typed, validated callables. To avoid sending roughly 123 operation schemas to the model on every MCP request, only a curated set is advertised directly; everything else is discoverable and executable on demand.

Two Swagger WebSocket operations are implemented as bounded one-call exchanges, multipart routes support file uploads, and binary responses can be saved under `./data/output` and returned inline as base64 when small enough.

## Run

```bash
git clone https://github.com/twinlunarstarz-dev/LocalAiMCP.git
cd LocalAiMCP
cp .env.example .env
# Edit LOCALAI_BASE_URL / LOCALAI_API_KEY if needed.
docker compose up -d --build
```

The MCP endpoint is:

```text
http://localhost:8000/mcp
```

For VS Code/Zoo Code or another Streamable HTTP MCP client, use that URL as the remote MCP server endpoint. The container defaults to `host.docker.internal:8080` for LocalAI and includes the Linux `host-gateway` mapping.

## Curated tool surface

The server does **not** advertise all 123 LocalAI operations by default. The default preset advertises 20 commonly useful operation tools plus five fixed discovery/system helpers.

Default directly exposed operation tools:

```text
# System/model information
get_system_info
get_metrics
get_token_metrics
list_models
list_model_capabilities
get_backend_monitor

# Generation/media
chat
complete_text
generate_image
inpaint_image
generate_sound
generate_video
text_to_speech
text_to_speech_with_voice

# Voice
a list below without this label typo would be confusing, so the actual names are:
list_voice_profiles
create_voice_profile
analyze_voice
verify_speakers

# 3D
generate_3d_asset
remesh_3d_asset
```

The five fixed MCP helpers are:

```text
list_additional_tools
search_additional_tools
execute_additional_tool
server_health
schema_audit
```

Thus the default `tools/list` surface is **25 tools**, rather than about 128. The exact number is configurable.

### Configure which LocalAI operations are directly visible

Set `LOCALAI_MCP_EXPOSED_TOOLS` to a comma-separated list of semantic operation names:

```env
LOCALAI_MCP_EXPOSED_TOOLS=chat,list_models,generate_image,text_to_speech,generate_3d_asset
```

Special values:

```text
*       expose all 123 Swagger operations directly
none    expose no Swagger operations directly; use only the gateway/system helpers
gateway-only  same as none
```

An empty or unset value uses the built-in 20-operation preset. Invalid names fail startup instead of silently disappearing.

Changing direct exposure affects only what MCP clients receive in `tools/list`; it does **not** remove the hidden operation from LocalAiMCP.

## Additional-tool gateway

Less common tools stay in an internal typed registry and are accessed through three small tools.

### `list_additional_tools`

Returns the complete sorted list of hidden tool names and nothing schema-heavy. It is intentionally compact so a model can inspect the whole hidden catalog on demand without permanently carrying those schemas in every request.

### `search_additional_tools`

Searches only hidden tools using a plain-language goal or an exact tool name. Each match returns:

- semantic tool name
- detailed purpose/input/output description
- tags
- complete input JSON schema

Examples:

```text
search_additional_tools(query="detokenize token ids")
search_additional_tools(query="transcribe audio")
search_additional_tools(query="install a backend")
search_additional_tools(query="inspect request traces")
```

### `execute_additional_tool`

Executes a hidden capability by semantic name:

```json
{
  "tool_name": "detokenize",
  "arguments": {
    "request": {
      "model": "my-model",
      "tokens": [1, 42, 9001]
    }
  }
}
```

The `arguments` object is validated against the **same generated Pydantic schema** used by a directly exposed operation. Invalid or unknown fields return a validation error and the expected input schema before any LocalAI request is made. This is not a curl-style dispatcher: the model uses semantic tool names and typed arguments rather than HTTP methods/routes.

Directly exposed operations are intentionally rejected by `execute_additional_tool`; the client should call their normal MCP tool directly.

The previous advanced `raw_request` escape hatch and `probe_safe_endpoints` helper are retained as hidden additional tools, so reducing `tools/list` does not remove those capabilities.

## LLM-oriented descriptions

The registry is designed so a model does not need prior LocalAI API knowledge:

- Tool names describe tasks rather than mirroring HTTP routes or methods.
- Every typed HTTP operation states its purpose, expected inputs, and success output.
- JSON request schemas carry field-level descriptions, including conservative fallback guidance when Swagger only says things like `Request` or leaves a field undocumented.
- Referenced request objects surface useful top-level fields directly in descriptions.
- Response descriptions explain whether data appears under `data`, `text`, `events`, `base64`, or `saved_path`.
- Search returns the complete input schema only when the hidden tool is relevant.
- Wrapper plumbing such as custom headers and per-call timeouts stays off normal typed operations.

For example, hidden tool `detokenize` explains that its request contains:

- `tokens`: integer token IDs to convert back to text
- `model`: LocalAI model name or alias whose tokenizer should be used

and that the JSON response contains `content`, the detokenized text.

## Design

- **FastMCP 3.4.7**, pinned for reproducibility.
- **Streamable HTTP + stateless mode**. Multiple Uvicorn workers are safe because discovery and execution use a process-local immutable registry rather than conversational/session state.
- **Async LocalAI I/O** with `httpx`; independent calls can run concurrently.
- **123 typed Swagger operation callables** with semantic names and generated input validation; only the configured subset is registered directly with FastMCP.
- **On-demand gateway** for hidden operations, preserving full LocalAI functionality without advertising every schema on every request.
- **Multipart support** for audio, images, GLB files, branding assets, and voice profiles. File arguments accept `data:` URIs, `base64:<data>`, HTTP(S) URLs, or files under `/data`.
- **Binary support** for audio/images/GLB responses. Small payloads are returned as base64; binary payloads can also be saved to `/data/output`.
- **SSE-aware response handling** aggregates LocalAI SSE events into a structured result.
- **WebSocket support** for backend-log streaming and realtime audio transforms using bounded exchanges.
- **Bearer auth** via `LOCALAI_API_KEY`; no token is stored in code or returned to MCP clients.

## Response wrapper

Typed HTTP operations return a predictable wrapper:

- `ok`: whether LocalAI returned a successful HTTP status
- `status_code`: LocalAI HTTP status
- `elapsed_ms`: request duration
- `data`: parsed JSON response bodies
- `text`: text responses
- `events`: collected SSE `data:` payloads
- `base64`, `size_bytes`, `mime_type`, `saved_path`: binary response metadata/content when applicable

Always check `ok` before consuming the response body.

## File inputs

For multipart tools, a file argument can be any of:

- `data:<mime>;base64,<payload>`
- `base64:<payload>`
- an `http://` or `https://` URL that the MCP container can fetch
- a local path under `LOCALAI_MCP_FILE_ROOT` (`/data` in Compose)

The Compose file mounts `./data` to `/data`.

## LocalAI streaming behavior

LocalAI request bodies that set `stream=true` are forwarded unchanged. If LocalAI answers with `text/event-stream`, the MCP call collects the SSE `data:` events and returns them when the LocalAI stream ends.

The two Swagger WebSocket routes are mapped specially:

- `stream_backend_logs`: collect backend log messages for a model up to `max_messages`, then close.
- `stream_audio_transform`: send one session/config object plus base64 PCM frames, collect transformed messages up to `max_messages`, then close.

They may be direct or hidden depending on `LOCALAI_MCP_EXPOSED_TOOLS`; hidden WebSocket tools remain executable through `execute_additional_tool`.

## Verification

Repository tests verify:

- exact Swagger coverage: 114 paths / 123 operations
- 123 unique reviewed semantic names
- the default curated exposure count and MCP `tools/list` count
- the full hidden-name catalog
- hidden search returning real descriptions and generated input schemas
- hidden execution validating arguments before network access
- every non-WebSocket operation description explaining inputs and outputs
- referenced request/response schemas surfacing real fields
- `detokenize` exposing useful token/model/content guidance on demand
- WebSocket detection, response wrapping, and binary handling

Run locally with dependencies installed:

```bash
python -m pip install -e '.[test]'
pytest
```

Container validation:

```bash
docker compose config
docker compose build
```

An MCP client should perform the normal MCP `initialize` handshake against `http://localhost:8000/mcp`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LOCALAI_BASE_URL` | `http://host.docker.internal:8080` | LocalAI base URL visible to the container |
| `LOCALAI_API_KEY` | empty | Optional LocalAI bearer token |
| `LOCALAI_MCP_EXPOSED_TOOLS` | built-in 20-tool preset | Comma-separated directly exposed Swagger operation names; `*` for all, `none` for none |
| `LOCALAI_REQUEST_TIMEOUT` | `300` | Overall LocalAI request timeout seconds |
| `LOCALAI_CONNECT_TIMEOUT` | `10` | Connection timeout seconds |
| `LOCALAI_MCP_MAX_UPLOAD_BYTES` | `104857600` | Maximum fetched/uploaded file size |
| `LOCALAI_MCP_MAX_RESPONSE_BYTES` | `104857600` | Maximum buffered LocalAI response size |
| `LOCALAI_MCP_INLINE_BINARY_LIMIT` | `1048576` | Binary bytes allowed inline as base64 |
| `LOCALAI_MCP_SAVE_BINARY` | `true` | Save binary responses to output directory |
| `MCP_PORT` | `8000` | Published host port |
| `MCP_WORKERS` | `2` | Uvicorn worker count |

## Security note

The additional-tool gateway can still execute LocalAI administrative/destructive operations, including model/backend install/delete, task/job controls, trace/log clearing, branding, node budgets, and voice-profile administration. Hiding a tool from `tools/list` reduces context size; it is **not** an authorization boundary. Do not publish port 8000 to an untrusted network without authentication and network access controls in front of it.
