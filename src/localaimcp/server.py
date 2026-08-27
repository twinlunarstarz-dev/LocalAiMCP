from __future__ import annotations

import inspect
from typing import Annotated, Any, Literal, Optional
from urllib.parse import quote

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, create_model

from .client import LocalAIClient
from .config import Settings
from .metadata import field_description, operation_description, operation_search_text, parameter_description
from .spec import Operation, load_spec, operations, safe_identifier

SETTINGS = Settings()
SPEC = load_spec()
OPS = operations(SPEC)
CLIENT = LocalAIClient(SETTINGS)
TOOL_DESCRIPTIONS = {op.tool_name: operation_description(SPEC, op) for op in OPS}

mcp = FastMCP(
    "LocalAI Control Plane",
    instructions=(
        "Use these tools to operate the configured LocalAI server without needing prior LocalAI API knowledge. "
        "Tool names describe the task rather than the HTTP route. If unsure which tool fits a goal, call `find_tools`. "
        "Typed tools are preferred over `raw_request`. Every normal HTTP result has `ok`, `status_code`, and `elapsed_ms`; "
        "JSON bodies are under `data`, text under `text`, SSE chunks under `events`, and binary results include mime/size "
        "plus `base64` and/or `saved_path` depending on configuration. Check `ok` before using a result. "
        "File parameters accept data URIs, base64:<data>, HTTP(S) URLs, or paths under LOCALAI_MCP_FILE_ROOT. "
        "Tools named delete/clear/reset/shutdown/disable/cancel/forget/unpin change server state."
    ),
)

_MODEL_CACHE: dict[str, type[BaseModel]] = {}


def _schema_type(schema: dict[str, Any], name_hint: str) -> Any:
    if "$ref" in schema:
        model_name = schema["$ref"].removeprefix("#/definitions/")
        return _model_for_definition(model_name)
    typ = schema.get("type")
    if typ == "string":
        values = schema.get("enum")
        if values and all(isinstance(v, str) for v in values):
            return Literal.__getitem__(tuple(values))
        return str
    if typ == "integer":
        return int
    if typ == "number":
        return float
    if typ == "boolean":
        return bool
    if typ == "array":
        return list[_schema_type(schema.get("items", {}), name_hint + "Item")]
    if typ == "object" and schema.get("properties"):
        return _model_from_schema(name_hint, schema)
    return dict[str, Any] if typ == "object" else Any


def _model_from_schema(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    required = set(schema.get("required", []))
    if name.endswith("OpenAIRequest"):
        required.discard("file")

    fields: dict[str, tuple[Any, Any]] = {}
    for raw_name, prop in schema.get("properties", {}).items():
        prop = prop or {}
        py_name = safe_identifier(raw_name)
        annotation = _schema_type(prop, name + py_name.title())
        description = field_description(SPEC, raw_name, prop, model_name=name)
        if raw_name in required:
            default = Field(..., description=description, alias=raw_name)
        else:
            annotation = Optional[annotation]
            default = Field(None, description=description, alias=raw_name)
        fields[py_name] = (annotation, default)

    return create_model(
        name.replace(".", "_").replace("-", "_"),
        __config__=ConfigDict(extra="allow", populate_by_name=True),
        **fields,
    )


def _model_for_definition(name: str) -> type[BaseModel]:
    if name in _MODEL_CACHE:
        return _MODEL_CACHE[name]
    schema = SPEC.get("definitions", {}).get(name, {"type": "object"})
    model = _model_from_schema(name, schema)
    _MODEL_CACHE[name] = model
    return model


def _annotated(annotation: Any, description: str) -> Any:
    return Annotated[annotation, Field(description=description)]


def _parameter_specs(op: Operation) -> tuple[list[inspect.Parameter], dict[str, dict[str, Any]]]:
    params: list[inspect.Parameter] = []
    meta: dict[str, dict[str, Any]] = {}
    used: set[str] = set()
    declared_body = False
    multipart = "multipart/form-data" in op.operation.get("consumes", [])

    for p in op.operation.get("parameters", []):
        location = p.get("in")
        raw_name = str(p.get("name", "value"))
        name = safe_identifier(raw_name)
        while name in used:
            name += "_value"
        used.add(name)
        required = bool(p.get("required"))
        schema = p.get("schema") or p

        annotation = str if p.get("type") == "file" else _schema_type(schema, op.tool_name + name.title())
        if location == "body":
            declared_body = True
        default = inspect.Parameter.empty if required else None
        if not required:
            annotation = Optional[annotation]

        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=_annotated(annotation, parameter_description(SPEC, op, p)),
            )
        )
        meta[name] = {
            "raw_name": raw_name,
            "in": location,
            "file": p.get("type") == "file",
            "collection_format": p.get("collectionFormat"),
        }

    if op.method in {"POST", "PUT", "PATCH"} and not multipart and not declared_body and "body" not in used:
        body_description = (
            "JSON object containing model configuration fields to deep-merge into the existing config."
            if op.path == "/api/models/config-json/{name}"
            else "Optional JSON object for LocalAI extension fields not declared by this Swagger operation."
        )
        params.append(
            inspect.Parameter(
                "body",
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=_annotated(Optional[dict[str, Any]], body_description),
            )
        )
        meta["body"] = {"raw_name": "body", "in": "body", "file": False}

    if multipart and "extra_form" not in used:
        params.append(
            inspect.Parameter(
                "extra_form",
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=_annotated(
                    Optional[dict[str, Any]],
                    "Advanced only: additional multipart fields not declared by Swagger. Usually omit this.",
                ),
            )
        )
        meta["extra_form"] = {"raw_name": "extra_form", "in": "extra_form", "file": False}

    return params, meta


def _make_http_callable(op: Operation):
    params, meta = _parameter_specs(op)

    async def invoke(**kwargs: Any) -> dict[str, Any]:
        query: dict[str, Any] = {}
        form: dict[str, Any] = {}
        files: dict[str, Any] = {}
        body: Any = None
        path = op.path
        extra_form = kwargs.pop("extra_form", None)

        for name, value in kwargs.items():
            if value is None:
                continue
            item = meta[name]
            raw = item["raw_name"]
            where = item["in"]
            if where == "path":
                path = path.replace("{" + raw + "}", quote(str(value), safe=""))
            elif where == "query":
                query[raw] = value
            elif where == "formData":
                if item["file"]:
                    files[raw] = value
                elif item.get("collection_format") == "csv" and isinstance(value, (list, tuple)):
                    form[raw] = ",".join(str(v) for v in value)
                else:
                    form[raw] = value
            elif where == "body":
                body = value

        if extra_form:
            form.update(extra_form)

        return await CLIENT.request(
            op.method,
            path,
            query=query,
            body=body,
            form=form,
            file_fields=files,
        )

    invoke.__name__ = op.tool_name
    invoke.__qualname__ = op.tool_name
    invoke.__doc__ = TOOL_DESCRIPTIONS[op.tool_name]
    invoke.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])
    invoke.__annotations__ = {p.name: p.annotation for p in params}
    invoke.__annotations__["return"] = dict[str, Any]
    return invoke


async def _ws_backend_logs(
    model_id: Annotated[str, Field(description="Model ID whose backend process logs should be streamed.")],
    max_messages: Annotated[int, Field(description="Maximum log messages to collect before closing the WebSocket.")] = 50,
    timeout_seconds: Annotated[float, Field(description="Stop waiting after this many seconds without another message.")] = 10,
) -> dict[str, Any]:
    path = "/ws/backend-logs/" + quote(model_id, safe="")
    return await CLIENT.websocket_collect(path, max_messages=max_messages, timeout_seconds=timeout_seconds)


async def _ws_audio_transform(
    session: Annotated[
        dict[str, Any],
        Field(description="Realtime audio transformation session/config object expected by LocalAI before audio frames."),
    ],
    frames_base64: Annotated[
        list[str],
        Field(description="Ordered PCM audio frames encoded as base64 strings; a `base64:` prefix is optional."),
    ],
    max_messages: Annotated[int, Field(description="Maximum transformed messages/frames to collect before closing.")] = 100,
    timeout_seconds: Annotated[float, Field(description="Stop waiting after this many seconds without another message.")] = 10,
) -> dict[str, Any]:
    return await CLIENT.websocket_collect(
        "/audio/transformations/stream",
        initial_json=session,
        binary_frames=frames_base64,
        max_messages=max_messages,
        timeout_seconds=timeout_seconds,
    )


def _register_operation_tools() -> None:
    for op in OPS:
        if op.websocket:
            if op.path.startswith("/ws/backend-logs/"):
                mcp.tool(
                    name=op.tool_name,
                    description=(
                        "Stream backend-process log messages for one loaded model over WebSocket. "
                        "Input `model_id` identifies the model; `max_messages` and `timeout_seconds` bound the read. "
                        "Returns collected messages and count, then closes the connection."
                    ),
                    tags=op.tags,
                )(_ws_backend_logs)
            elif op.path == "/audio/transformations/stream":
                mcp.tool(
                    name=op.tool_name,
                    description=(
                        "Run a bounded realtime audio-transformation WebSocket exchange. "
                        "Send the LocalAI session/config object first, followed by base64 PCM frames in order. "
                        "Returns transformed text/JSON/binary messages collected until the limit or timeout."
                    ),
                    tags=op.tags,
                )(_ws_audio_transform)
            continue

        fn = _make_http_callable(op)
        mcp.tool(name=op.tool_name, description=fn.__doc__, tags=op.tags)(fn)


_register_operation_tools()


@mcp.tool(
    name="find_tools",
    tags={"localai", "help", "discovery"},
    description=(
        "Find the best LocalAI tools for a plain-language goal when you do not know the tool name. "
        "Example queries: 'transcribe audio', 'load a model', 'inspect traces', 'delete a voice profile'. "
        "Returns matching tool names plus their complete usage descriptions; it does not call LocalAI."
    ),
)
async def find_tools(
    query: Annotated[str, Field(description="Plain-language task or capability to search for.")],
    limit: Annotated[int, Field(description="Maximum matches to return, from 1 to 20.")] = 8,
) -> dict[str, Any]:
    terms = [term for term in safe_identifier(query).split("_") if len(term) > 1]
    limit = max(1, min(limit, 20))
    scored: list[tuple[int, Operation]] = []

    for op in OPS:
        haystack = operation_search_text(SPEC, op)
        name_text = op.tool_name.replace("_", " ")
        summary = str(op.summary).lower()
        score = 0
        for term in terms:
            if term in name_text:
                score += 8
            if term in summary:
                score += 5
            if term in haystack:
                score += 2
        if score:
            scored.append((score, op))

    scored.sort(key=lambda item: (-item[0], item[1].tool_name))
    matches = [
        {
            "name": op.tool_name,
            "description": TOOL_DESCRIPTIONS[op.tool_name],
            "tags": sorted(op.tags - {"localai"}),
        }
        for _, op in scored[:limit]
    ]
    return {"query": query, "matches": matches, "count": len(matches)}


@mcp.tool(
    name="server_health",
    tags={"localai", "management", "test"},
    description=(
        "Check whether LocalAI is reachable and whether its system, model-list, and backend-list APIs respond successfully. "
        "Takes no inputs. Runs the three checks concurrently and returns each wrapped response plus an overall `ok` flag."
    ),
)
async def server_health() -> dict[str, Any]:
    import asyncio

    system, models, backends = await asyncio.gather(
        CLIENT.request("GET", "/system"),
        CLIENT.request("GET", "/v1/models"),
        CLIENT.request("GET", "/backends"),
    )
    return {
        "ok": all(x.get("ok") for x in (system, models, backends)),
        "system": system,
        "models": models,
        "backends": backends,
    }


@mcp.tool(
    name="schema_audit",
    tags={"localai", "management", "test"},
    description=(
        "Verify that every operation in the bundled LocalAI Swagger maps to one unique typed MCP tool. "
        "Takes no inputs and does not contact or modify LocalAI. Returns path/operation counts, WebSocket/multipart counts, "
        "and operation counts by tag."
    ),
)
async def schema_audit() -> dict[str, Any]:
    by_tag: dict[str, int] = {}
    for op in OPS:
        for tag in op.operation.get("tags", ["untagged"]):
            by_tag[tag] = by_tag.get(tag, 0) + 1
    names = [op.tool_name for op in OPS]
    return {
        "ok": len(names) == len(set(names)),
        "swagger_paths": len(SPEC.get("paths", {})),
        "swagger_operations": len(OPS),
        "registered_operation_tools": len(OPS),
        "websocket_operations": sum(op.websocket for op in OPS),
        "multipart_operations": sum("multipart/form-data" in op.operation.get("consumes", []) for op in OPS),
        "unique_tool_names": len(set(names)),
        "operations_by_tag": dict(sorted(by_tag.items())),
    }


@mcp.tool(
    name="probe_safe_endpoints",
    tags={"localai", "management", "test"},
    description=(
        "Test LocalAI's zero-argument GET endpoints without intentionally changing server state. "
        "Input `concurrency` controls parallel checks. Skips routes requiring inputs and all mutating methods. "
        "Returns pass/fail status for every probed route."
    ),
)
async def probe_safe_endpoints(
    concurrency: Annotated[int, Field(description="Maximum concurrent probes; clamped to 1-32.")] = 8,
) -> dict[str, Any]:
    import asyncio

    candidates = [
        op
        for op in OPS
        if op.method == "GET"
        and not op.websocket
        and not any(p.get("required") for p in op.operation.get("parameters", []))
    ]
    sem = asyncio.Semaphore(max(1, min(concurrency, 32)))

    async def one(op: Operation) -> dict[str, Any]:
        async with sem:
            result = await CLIENT.request("GET", op.path)
            return {
                "tool": op.tool_name,
                "path": op.path,
                "ok": result.get("ok", False),
                "status_code": result.get("status_code"),
                "error": result.get("network_error") or result.get("error"),
            }

    results = await asyncio.gather(*(one(op) for op in candidates))
    return {
        "ok": all(r["ok"] for r in results),
        "probed": len(results),
        "passed": sum(r["ok"] for r in results),
        "failed": sum(not r["ok"] for r in results),
        "results": results,
    }


@mcp.tool(
    name="raw_request",
    tags={"localai", "raw", "management"},
    description=(
        "Advanced escape hatch for a LocalAI HTTP route that has no typed MCP tool, such as a newer extension absent from "
        "the bundled Swagger. Prefer typed tools when available. Provide an HTTP method, LocalAI path beginning with '/', "
        "optional query/JSON body, headers, and timeout. Returns the same standard MCP response wrapper as typed HTTP tools."
    ),
)
async def raw_request(
    method: Annotated[str, Field(description="HTTP method, e.g. GET, POST, PUT, PATCH, or DELETE.")],
    path: Annotated[str, Field(description="LocalAI path beginning with '/', never a full URL.")],
    query: Annotated[Optional[dict[str, Any]], Field(description="Optional query-string parameters.")] = None,
    body: Annotated[Optional[Any], Field(description="Optional JSON request body.")] = None,
    extra_headers: Annotated[Optional[dict[str, str]], Field(description="Optional per-request headers.")] = None,
    timeout_seconds: Annotated[Optional[float], Field(description="Optional request timeout override in seconds.")] = None,
) -> dict[str, Any]:
    return await CLIENT.request(
        method,
        path,
        query=query,
        body=body,
        extra_headers=extra_headers,
        timeout_seconds=timeout_seconds,
    )


app = mcp.http_app(path=SETTINGS.mcp_path, stateless_http=SETTINGS.stateless_http)


def main() -> None:
    mcp.run(
        transport="http",
        host=SETTINGS.mcp_host,
        port=SETTINGS.mcp_port,
        path=SETTINGS.mcp_path,
        stateless_http=SETTINGS.stateless_http,
    )


if __name__ == "__main__":
    main()
