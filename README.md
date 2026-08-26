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

## Design

- **FastMCP 3.4.7**, pinned to the latest stable 3.x release at implementation time.
- **Streamable HTTP + stateless mode**. Docker uses multiple Uvicorn workers by default (`MCP_WORKERS=2`), safe because MCP session state is disabled.
- **Async LocalAI I/O** with `httpx`; independent calls can run concurrently.
- **One MCP tool per Swagger operation** with deterministic names, concise endpoint descriptions, typed path/query/form parameters, nested request-body schemas, and LocalAI tags.
- **Multipart support** for audio, images, GLB files, branding assets, and voice profiles. File arguments accept `data:` URIs, `base64:<data>`, HTTP(S) URLs, or files under `/data`.
- **Binary support** for audio/images/GLB responses. Small payloads are returned as base64; binary payloads are also saved to `/data/output` by default (host path `./data/output`).
- **SSE-aware response handling** aggregates LocalAI SSE events into a structured result.
- **WebSocket support** for backend-log streaming and realtime audio transforms using bounded exchanges so the MCP server remains stateless.
- **Bearer auth** via `LOCALAI_API_KEY`; no token is stored in code or returned to MCP clients.
- **Raw escape hatch** (`localai_raw_request`) for LocalAI extensions or newly added routes not yet in the bundled Swagger.

Tool names are route/method based, for example:

```text
localai_v1_chat_completions_post
localai_v1_models_get
localai_api_agent_jobs_get
localai_backend_load_post
localai_backends_apply_post
```

## Built-in management/test tools

`localai_health` concurrently checks `/system`, `/v1/models`, and `/backends`.

`localai_schema_audit` verifies that all bundled Swagger operations have unique MCP tool mappings and reports counts by tag, multipart type, and WebSocket type.

`localai_probe_safe_gets` concurrently probes only zero-argument GET endpoints. It avoids destructive/mutating calls and GET routes that require IDs or parameters.

`localai_raw_request` sends an arbitrary method/path/body/query request to the configured LocalAI base URL. It refuses full URLs for the request path, so calls remain scoped to the configured LocalAI server.

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

- `/ws/backend-logs/{modelId}`: collect up to `max_messages` log messages, then close.
- `/audio/transformations/stream`: send one `session.update` JSON object plus a list of base64 PCM frames, collect up to `max_messages` output frames/messages, then close.

## Verification

The repository includes tests that assert exact Swagger coverage (114 paths, 123 operations), unique tool naming, WebSocket detection, JSON response wrapping, and binary handling.

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

An MCP client should perform the normal MCP `initialize` handshake against `http://localhost:8000/mcp`. Once connected, call `localai_schema_audit`, then `localai_health`, then `localai_probe_safe_gets` for a non-destructive live check.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `LOCALAI_BASE_URL` | `http://host.docker.internal:8080` | LocalAI base URL visible to the container |
| `LOCALAI_API_KEY` | empty | Optional LocalAI bearer token |
| `LOCALAI_REQUEST_TIMEOUT` | `300` | Per-call overall LocalAI timeout seconds |
| `LOCALAI_CONNECT_TIMEOUT` | `10` | Connection timeout seconds |
| `LOCALAI_MCP_MAX_UPLOAD_BYTES` | `104857600` | Maximum fetched/uploaded file size |
| `LOCALAI_MCP_MAX_RESPONSE_BYTES` | `104857600` | Maximum buffered LocalAI response size |
| `LOCALAI_MCP_INLINE_BINARY_LIMIT` | `1048576` | Binary bytes allowed inline as base64 |
| `LOCALAI_MCP_SAVE_BINARY` | `true` | Save binary responses to output directory |
| `MCP_PORT` | `8000` | Published host port |
| `MCP_WORKERS` | `2` | Uvicorn worker count |

## Security note

This MCP exposes LocalAI administrative/destructive endpoints, including model/backend install/delete, task/job controls, trace/log clearing, branding, node budgets, and voice-profile administration. Do not publish port 8000 to an untrusted network without placing authentication and network access controls in front of it.
