# LocalAiMCP

A stateless, asynchronous FastMCP control plane for LocalAI. The server exposes every operation in the bundled LocalAI Swagger as an MCP tool, including inference, model/backend management, monitoring, agent jobs, config, routing/PII, media, voice/face, node/P2P, and WebSocket operations.

The bundled Swagger currently contains **114 paths / 123 operations**. Two WebSocket operations are implemented as bounded one-call exchanges, 10 multipart routes support file uploads, and binary responses can be saved under `./data/output` and returned inline as base64 when small enough.

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

For VS Code/Zoo Code or another Streamable HTTP MCP client, use that URL as the remote MCP server endpoint. The container defaults to `host.docker.internal:8080` for LocalAI and includes the Linux `host-gateway` mapping. If LocalAI runs elsewhere, set `LOCALAI_BASE_URL` to a URL reachable **from the container**.

## LLM-oriented tool design

The tool surface is intentionally designed so a model does not need prior LocalAI API knowledge:

- Tool names describe the action rather than mirroring HTTP routes or methods.
- Every typed HTTP tool description states its purpose, expected inputs, and success output.
- JSON request schemas carry field-level descriptions, including fallback descriptions when Swagger only says things like `Request` or `query params`.
- Referenced request objects surface their useful top-level fields directly in the tool description.
- Response descriptions explain whether data appears under `data`, `text`, `events`, `base64`, or `saved_path` in the MCP response wrapper.
- `find_tools` accepts a plain-language goal and returns the most relevant LocalAI tools and their full usage descriptions.
- Wrapper plumbing such as custom headers and per-call timeouts is kept off normal typed tools; use `raw_request` only for advanced/undocumented routes.

Examples of simplified names:

```text
chat
complete_text
embed
generate_image
transcribe_audio
text_to_speech
tokenize
detokenize
list_models
stream_backend_logs
```

For example, `detokenize` explains that its `request` object contains:

- `tokens`: integer token IDs to convert back to text
- `model`: LocalAI model name or alias whose tokenizer should be used

and that the JSON result contains `content`, the detokenized text.

If a model knows the goal but not the tool name, it can call:

```text
find_tools(query="load a model")
find_tools(query="transcribe audio")
find_tools(query="inspect backend traces")
```

## Design

- **FastMCP 3.4.7**, pinned to the stable version used by this project.
- **Streamable HTTP + stateless mode**. Docker uses multiple Uvicorn workers by default (`MCP_WORKERS=2`), safe because MCP session state is disabled.
- **Async LocalAI I/O** with `httpx`; independent calls can run concurrently.
- **One typed MCP tool per Swagger operation** with semantic names, detailed descriptions, typed path/query/form parameters, nested request-body schemas, and LocalAI tags.
- **Multipart support** for audio, images, GLB files, branding assets, and voice profiles. File arguments accept `data:` URIs, `base64:<data>`, HTTP(S) URLs, or files under `/data`.
- **Binary support** for audio/images/GLB responses. Small payloads are returned as base64; binary payloads are also saved to `/data/output` by default (host path `./data/output`).
- **SSE-aware response handling** aggregates LocalAI SSE events into a structured result.
- **WebSocket support** for backend-log streaming and realtime audio transforms using bounded exchanges so the MCP server remains stateless.
- **Bearer auth** via `LOCALAI_API_KEY`; no token is stored in code or returned to MCP clients.
- **Raw escape hatch** (`raw_request`) for LocalAI extensions or newly added routes not yet in the bundled Swagger.

## Built-in management/discovery tools

`find_tools` searches all typed LocalAI tools by a plain-language goal and returns matching names and complete descriptions. It does not contact or modify LocalAI.

`server_health` concurrently checks `/system`, `/v1/models`, and `/backends`.

`schema_audit` verifies that all bundled Swagger operations have unique MCP tool mappings and reports counts by tag, multipart type, and WebSocket type.

`probe_safe_endpoints` concurrently probes only zero-argument GET endpoints. It avoids destructive/mutating calls and GET routes that require IDs or parameters.

`raw_request` sends an arbitrary method/path/body/query request to the configured LocalAI base URL. It refuses full URLs for the request path, so calls remain scoped to the configured LocalAI server. Prefer a typed tool whenever one exists.

## Response wrapper

Typed HTTP tools return a predictable wrapper:

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

The Compose file mounts `./data` to `/data`. Put host files there when path-based upload is easiest.

## LocalAI streaming behavior

LocalAI request bodies that set `stream=true` are forwarded unchanged. If LocalAI answers with `text/event-stream`, the MCP tool collects the SSE `data:` events and returns them when the LocalAI stream ends. This keeps a tool call compatible with ordinary MCP clients while preserving streamed chunks in order.

The two Swagger WebSocket routes are mapped specially:

- `stream_backend_logs`: collect backend log messages for a model up to `max_messages`, then close.
- `stream_audio_transform`: send one session/config JSON object plus base64 PCM frames, collect transformed messages up to `max_messages`, then close.

## Verification

The repository tests assert:

- exact Swagger coverage: 114 paths / 123 operations
- 123 unique semantic names, no route/method suffix naming
- every non-WebSocket typed tool explains inputs and outputs
- every referenced request body surfaces actual request fields in its tool description
- the `detokenize` MCP schema exposes `tokens` and `model` with useful field descriptions
- FastMCP can enumerate the complete tool surface in-memory
- WebSocket detection, JSON response wrapping, and binary handling

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

An MCP client should perform the normal MCP `initialize` handshake against `http://localhost:8000/mcp`. Once connected, call `schema_audit`, then `server_health`, then `probe_safe_endpoints` for a non-destructive live check.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LOCALAI_BASE_URL` | `http://host.docker.internal:8080` | LocalAI base URL visible to the container |
| `LOCALAI_API_KEY` | empty | Optional LocalAI bearer token |
| `LOCALAI_REQUEST_TIMEOUT` | `300` | Overall LocalAI request timeout seconds |
| `LOCALAI_CONNECT_TIMEOUT` | `10` | Connection timeout seconds |
| `LOCALAI_MCP_MAX_UPLOAD_BYTES` | `104857600` | Maximum fetched/uploaded file size |
| `LOCALAI_MCP_MAX_RESPONSE_BYTES` | `104857600` | Maximum buffered LocalAI response size |
| `LOCALAI_MCP_INLINE_BINARY_LIMIT` | `1048576` | Binary bytes allowed inline as base64 |
| `LOCALAI_MCP_SAVE_BINARY` | `true` | Save binary responses to output directory |
| `MCP_PORT` | `8000` | Published host port |
| `MCP_WORKERS` | `2` | Uvicorn worker count |

## Security note

This MCP exposes LocalAI administrative/destructive endpoints, including model/backend install/delete, task/job controls, trace/log clearing, branding, node budgets, and voice-profile administration. Do not publish port 8000 to an untrusted network without placing authentication and network access controls in front of it.
