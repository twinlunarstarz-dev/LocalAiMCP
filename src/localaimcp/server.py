from __future__ import annotations

import inspect
from typing import Annotated, Any, Literal, Optional
from urllib.parse import quote

from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, create_model

from .client import LocalAIClient
from .config import Settings
from .spec import Operation, load_spec, operations, safe_identifier

SETTINGS = Settings()
SPEC = load_spec()
OPS = operations(SPEC)
CLIENT = LocalAIClient(SETTINGS)
mcp = FastMCP(
    "LocalAI Control Plane",
    instructions=(
        "Manage, test, and use the configured LocalAI server. Tools map to LocalAI REST/WebSocket endpoints. "
        "Binary inputs accept data URIs, base64:<data>, HTTP(S) URLs, or files under LOCALAI_MCP_FILE_ROOT."
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
    # LocalAI reuses OpenAIRequest across many endpoints even though its generated
    # Swagger marks `file` required. Requiring it for chat/images/embeddings would
    # make valid calls impossible, so only the operation-level body stays required.
    if name.endswith("OpenAIRequest"):
        required.discard("file")
    fields: dict[str, tuple[Any, Any]] = {}
    for raw_name, prop in schema.get("properties", {}).items():
        py_name = safe_identifier(raw_name)
        annotation = _schema_type(prop or {}, name + py_name.title())
        description = (prop or {}).get("description") or raw_name
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
        description = p.get("description") or raw_name
        schema = p.get("schema") or p
        if p.get("type") == "file":
            annotation = str
            description += " File source: data URI, base64:<data>, HTTP(S) URL, or path under the configured file root."
        else:
            annotation = _schema_type(schema, op.tool_name + name.title())
        if location == "body":
            declared_body = True
        default = inspect.Parameter.empty if required else None
        if not required:
            annotation = Optional[annotation]
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=_annotated(annotation, description),
            )
        )
        meta[name] = {"raw_name": raw_name, "in": location, "file": p.get("type") == "file"}

    # Preserve functionality when the generated Swagger omits a JSON body (notably
    # PATCH /api/models/config-json/{name}) and for LocalAI extension fields.
    if op.method in {"POST", "PUT", "PATCH"} and not multipart and not declared_body and "body" not in used:
        params.append(
            inspect.Parameter(
                "body",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=None,
                annotation=_annotated(Optional[dict[str, Any]], "Optional JSON body for undocumented or extension fields."),
            )
        )
        meta["body"] = {"raw_name": "body", "in": "body", "file": False}

    if multipart and "extra_form" not in used:
        params.append(
            inspect.Parameter(
                "extra_form",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=None,
                annotation=_annotated(Optional[dict[str, Any]], "Additional multipart form fields, including backend-specific params."),
            )
        )
        meta["extra_form"] = {"raw_name": "extra_form", "in": "extra_form", "file": False}
    params.append(
        inspect.Parameter(
            "extra_headers",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=None,
            annotation=_annotated(Optional[dict[str, str]], "Optional headers merged into this LocalAI request."),
        )
    )
    meta["extra_headers"] = {"raw_name": "extra_headers", "in": "control", "file": False}
    params.append(
        inspect.Parameter(
            "timeout_seconds",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=None,
            annotation=_annotated(Optional[float], "Override the LocalAI request timeout for this call."),
        )
    )
    meta["timeout_seconds"] = {"raw_name": "timeout_seconds", "in": "control", "file": False}
    return params, meta


def _make_http_callable(op: Operation):
    params, meta = _parameter_specs(op)

    async def invoke(**kwargs: Any) -> dict[str, Any]:
        query: dict[str, Any] = {}
        form: dict[str, Any] = {}
        files: dict[str, Any] = {}
        body: Any = None
        path = op.path
        extra_headers = kwargs.pop("extra_headers", None)
        timeout_seconds = kwargs.pop("timeout_seconds", None)
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
            extra_headers=extra_headers,
            timeout_seconds=timeout_seconds,
        )

    invoke.__name__ = op.tool_name
    invoke.__qualname__ = op.tool_name
    invoke.__doc__ = f"{op.summary} LocalAI {op.method} {op.path}."
    invoke.__signature__ = inspect.Signature(params, return_annotation=dict[str, Any])
    return invoke


async def _ws_backend_logs(model_id: str, max_messages: int = 50, timeout_seconds: float = 10) -> dict[str, Any]:
    path = "/ws/backend-logs/" + quote(model_id, safe="")
    return await CLIENT.websocket_collect(path, max_messages=max_messages, timeout_seconds=timeout_seconds)


async def _ws_audio_transform(
    session: dict[str, Any],
    frames_base64: list[str],
    max_messages: int = 100,
    timeout_seconds: float = 10,
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
                    description="Stream LocalAI backend logs over WebSocket, collect a bounded batch, then close.",
                    tags=op.tags,
                )(_ws_backend_logs)
            elif op.path == "/audio/transformations/stream":
                mcp.tool(
                    name=op.tool_name,
                    description="Run a bounded bidirectional LocalAI realtime audio-transform WebSocket exchange. Frames are base64 PCM.",
                    tags=op.tags,
                )(_ws_audio_transform)
            continue
        fn = _make_http_callable(op)
        mcp.tool(name=op.tool_name, description=fn.__doc__, tags=op.tags)(fn)


_register_operation_tools()


@mcp.tool(tags={"localai", "management", "test"})
async def localai_health() -> dict[str, Any]:
    """Check core LocalAI system, model, and backend endpoints concurrently."""
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


@mcp.tool(tags={"localai", "management", "test"})
async def localai_schema_audit() -> dict[str, Any]:
    """Audit MCP coverage of the bundled LocalAI Swagger without changing LocalAI state."""
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


@mcp.tool(tags={"localai", "management", "test"})
async def localai_probe_safe_gets(concurrency: int = 8) -> dict[str, Any]:
    """Probe zero-argument GET endpoints concurrently; skip required-input and mutating routes."""
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


@mcp.tool(tags={"localai", "raw", "management"})
async def localai_raw_request(
    method: Annotated[str, Field(description="HTTP method, e.g. GET, POST, PUT, PATCH, DELETE.")],
    path: Annotated[str, Field(description="LocalAI path beginning with '/', never a full URL.")],
    query: Annotated[Optional[dict[str, Any]], Field(description="Optional query parameters.")] = None,
    body: Annotated[Optional[Any], Field(description="Optional JSON body.")] = None,
    extra_headers: Annotated[Optional[dict[str, str]], Field(description="Optional per-request headers.")] = None,
    timeout_seconds: Annotated[Optional[float], Field(description="Optional request timeout override.")] = None,
) -> dict[str, Any]:
    """Call any LocalAI HTTP route, including extensions not present in the bundled Swagger."""
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
