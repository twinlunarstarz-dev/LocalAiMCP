from __future__ import annotations

from typing import Annotated, Any, Optional

from fastmcp import FastMCP
from pydantic import Field, ValidationError

from .metadata import operation_search_text
from .registry import (
    CLIENT,
    EXPOSED_TOOL_NAMES,
    OPS,
    OP_BY_NAME,
    SETTINGS,
    SPEC,
    TOOL_CALLABLES,
    TOOL_INPUT_SCHEMAS,
    argument_model,
    execute_operation,
    tool_description,
)
from .spec import Operation, safe_identifier

mcp = FastMCP(
    "LocalAI Control Plane",
    instructions=(
        "A curated set of common LocalAI tools is directly visible to reduce tool-schema context. For anything else, "
        "call `search_additional_tools` or `list_additional_tools`, then `execute_additional_tool` with the returned name "
        "and arguments. Hidden tools use the same typed validation as direct tools. Normal HTTP results include `ok`, "
        "`status_code`, and `elapsed_ms`; JSON is under `data`, text under `text`, SSE under `events`, and binary results "
        "include mime/size plus `base64` and/or `saved_path`. File parameters accept data URIs, `base64:<data>`, HTTP(S) "
        "URLs, or paths under LOCALAI_MCP_FILE_ROOT. Delete/clear/reset/shutdown/cancel/forget operations change server state."
    ),
)


def _register_exposed_operations() -> None:
    for op in OPS:
        if op.tool_name in EXPOSED_TOOL_NAMES:
            mcp.tool(name=op.tool_name, description=tool_description(op.tool_name), tags=op.tags)(
                TOOL_CALLABLES[op.tool_name]
            )


_register_exposed_operations()


async def _probe_safe_endpoints(
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


async def _raw_request(
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


_ADDITIONAL_HELPERS: dict[str, tuple[Any, str, set[str]]] = {
    "probe_safe_endpoints": (
        _probe_safe_endpoints,
        "Test LocalAI's zero-argument GET endpoints without intentionally changing state. `concurrency` controls parallel "
        "checks. Returns pass/fail status for every probed route.",
        {"management", "test"},
    ),
    "raw_request": (
        _raw_request,
        "Advanced escape hatch for a LocalAI HTTP route absent from the bundled Swagger. Provide method, LocalAI path, and "
        "optional query/body/headers/timeout. Prefer a typed tool whenever one exists.",
        {"raw", "management"},
    ),
}

for _name, (_fn, _description, _tags) in _ADDITIONAL_HELPERS.items():
    TOOL_INPUT_SCHEMAS[_name] = argument_model(_name, _fn).model_json_schema()

ADDITIONAL_TOOL_NAMES = frozenset((set(OP_BY_NAME) - set(EXPOSED_TOOL_NAMES)) | set(_ADDITIONAL_HELPERS))


def _additional_description(name: str) -> str:
    helper = _ADDITIONAL_HELPERS.get(name)
    return helper[1] if helper else tool_description(name)


def _additional_tags(name: str) -> list[str]:
    helper = _ADDITIONAL_HELPERS.get(name)
    return sorted(helper[2]) if helper else sorted(OP_BY_NAME[name].tags - {"localai"})


def _search_score(name: str, terms: list[str]) -> int:
    if name in OP_BY_NAME:
        op = OP_BY_NAME[name]
        haystack = operation_search_text(SPEC, op)
        summary = str(op.summary).lower()
    else:
        haystack = _additional_description(name).lower()
        summary = haystack
    name_text = name.replace("_", " ")
    score = 0
    for term in terms:
        if term in name_text:
            score += 8
        if term in summary:
            score += 5
        if term in haystack:
            score += 2
    return score


@mcp.tool(
    name="list_additional_tools",
    tags={"localai", "help", "discovery"},
    description=(
        "List every LocalAI capability hidden from the always-visible MCP surface. Takes no inputs and returns tool names "
        "only to stay compact. Use `search_additional_tools` with a name or goal to get its description and input schema."
    ),
)
async def list_additional_tools() -> dict[str, Any]:
    names = sorted(ADDITIONAL_TOOL_NAMES)
    return {"count": len(names), "tools": names}


@mcp.tool(
    name="search_additional_tools",
    tags={"localai", "help", "discovery"},
    description=(
        "Search hidden LocalAI capabilities by plain-language goal or exact name. Returns matching names, detailed usage "
        "descriptions, tags, and complete input schemas for `execute_additional_tool`; it does not call LocalAI."
    ),
)
async def search_additional_tools(
    query: Annotated[str, Field(description="Plain-language task, capability, or exact hidden tool name.")],
    limit: Annotated[int, Field(description="Maximum matches to return, from 1 to 20.")] = 5,
) -> dict[str, Any]:
    terms = [term for term in safe_identifier(query).split("_") if len(term) > 1]
    limit = max(1, min(limit, 20))
    scored = [(_search_score(name, terms), name) for name in ADDITIONAL_TOOL_NAMES]
    scored = [item for item in scored if item[0] > 0]
    scored.sort(key=lambda item: (-item[0], item[1]))
    matches = [
        {
            "name": name,
            "description": _additional_description(name),
            "tags": _additional_tags(name),
            "input_schema": TOOL_INPUT_SCHEMAS[name],
        }
        for _, name in scored[:limit]
    ]
    return {"query": query, "matches": matches, "count": len(matches)}


async def _execute_helper(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    fn = _ADDITIONAL_HELPERS[name][0]
    model = argument_model(name, fn)
    try:
        validated = model.model_validate(arguments)
    except ValidationError as exc:
        return {
            "ok": False,
            "error": "Arguments did not match the additional tool schema.",
            "tool_name": name,
            "validation_errors": exc.errors(include_url=False),
            "input_schema": TOOL_INPUT_SCHEMAS[name],
        }
    kwargs = {field: getattr(validated, field) for field in model.model_fields if getattr(validated, field) is not None}
    try:
        return await fn(**kwargs)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "tool_name": name}


@mcp.tool(
    name="execute_additional_tool",
    tags={"localai", "gateway"},
    description=(
        "Execute one LocalAI capability hidden from the always-visible surface. First use `search_additional_tools` or "
        "`list_additional_tools`. Pass the exact hidden `tool_name` and an `arguments` JSON object matching its discovered "
        "input schema. Arguments are typed and validated before any LocalAI request. Destructive hidden tools stay destructive."
    ),
)
async def execute_additional_tool(
    tool_name: Annotated[str, Field(description="Exact hidden tool name returned by additional-tool discovery.")],
    arguments: Annotated[
        dict[str, Any],
        Field(description="Arguments matching the hidden tool's input schema; use an empty object for a no-input tool."),
    ],
) -> dict[str, Any]:
    if tool_name not in ADDITIONAL_TOOL_NAMES:
        if tool_name in EXPOSED_TOOL_NAMES:
            return {"ok": False, "error": f"`{tool_name}` is directly exposed; call that MCP tool directly."}
        return {
            "ok": False,
            "error": f"Unknown additional tool `{tool_name}`.",
            "hint": "Call list_additional_tools or search_additional_tools first.",
        }
    if tool_name in _ADDITIONAL_HELPERS:
        return await _execute_helper(tool_name, arguments)
    return await execute_operation(tool_name, arguments)


@mcp.tool(
    name="server_health",
    tags={"localai", "management", "test"},
    description=(
        "Check whether LocalAI is reachable and whether its system, model-list, and backend-list APIs respond successfully. "
        "Takes no inputs. Runs the three checks concurrently and returns each response plus an overall `ok` flag."
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
        "Audit the bundled Swagger and curated exposure split without contacting LocalAI. Returns total operations, direct "
        "and additional operation counts, WebSocket/multipart counts, and operation counts by tag."
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
        "unique_operation_names": len(set(names)),
        "exposed_operation_tools": len(EXPOSED_TOOL_NAMES),
        "additional_operation_tools": len(set(OP_BY_NAME) - set(EXPOSED_TOOL_NAMES)),
        "additional_helper_tools": len(_ADDITIONAL_HELPERS),
        "websocket_operations": sum(op.websocket for op in OPS),
        "multipart_operations": sum("multipart/form-data" in op.operation.get("consumes", []) for op in OPS),
        "operations_by_tag": dict(sorted(by_tag.items())),
    }


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
