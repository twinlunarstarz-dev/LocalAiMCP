import pytest
from fastmcp import Client

from localaimcp.config import DEFAULT_EXPOSED_TOOLS
from localaimcp.metadata import _success_response
from localaimcp.registry import OPS, SPEC, TOOL_DESCRIPTIONS, TOOL_INPUT_SCHEMAS
from localaimcp.server import (
    ADDITIONAL_TOOL_NAMES,
    EXPOSED_TOOL_NAMES,
    execute_additional_tool,
    list_additional_tools,
    mcp,
    search_additional_tools,
)
from localaimcp.spec import resolve_ref, safe_identifier


@pytest.mark.asyncio
async def test_fastmcp_exposes_curated_tool_surface():
    async with Client(mcp) as client:
        tools = await client.list_tools()

    names = {tool.name for tool in tools}
    assert len(OPS) == 123
    assert len(DEFAULT_EXPOSED_TOOLS) == 20
    assert len(EXPOSED_TOOL_NAMES) == 20
    assert len(tools) == 25  # 20 configured LocalAI operations + 5 fixed gateway/system helpers
    assert len(names) == len(tools)

    # Representative always-visible system, generation, voice, and 3D tools.
    for name in {
        "get_system_info",
        "get_metrics",
        "list_models",
        "chat",
        "generate_image",
        "generate_video",
        "text_to_speech",
        "analyze_voice",
        "verify_speakers",
        "generate_3d_asset",
        "remesh_3d_asset",
    }:
        assert name in names

    # Less common operations stay out of tools/list but remain discoverable/executable.
    assert "detokenize" not in names
    assert "transcribe_audio" not in names
    assert "stream_backend_logs" not in names
    assert "raw_request" not in names
    assert "probe_safe_endpoints" not in names

    for name in {
        "list_additional_tools",
        "search_additional_tools",
        "execute_additional_tool",
        "server_health",
        "schema_audit",
    }:
        assert name in names


@pytest.mark.asyncio
async def test_list_additional_tools_returns_complete_hidden_name_list():
    result = await list_additional_tools()
    assert result["count"] == len(ADDITIONAL_TOOL_NAMES)
    assert result["count"] == (123 - len(EXPOSED_TOOL_NAMES)) + 2  # raw_request + safe probe
    assert result["tools"] == sorted(result["tools"])
    assert all(isinstance(name, str) for name in result["tools"])
    assert "detokenize" in result["tools"]
    assert "transcribe_audio" in result["tools"]
    assert "raw_request" in result["tools"]
    assert "probe_safe_endpoints" in result["tools"]
    assert "chat" not in result["tools"]


@pytest.mark.asyncio
async def test_hidden_detokenize_is_self_describing_on_demand():
    detok_op = next(op for op in OPS if op.tool_name == "detokenize")
    assert _success_response(detok_op) is not None, detok_op.operation.get("responses")

    result = await search_additional_tools("detokenize token ids", limit=5)
    match = next(item for item in result["matches"] if item["name"] == "detokenize")
    description = match["description"]
    assert "token IDs" in description
    assert "`tokens`" in description
    assert "`model`" in description
    assert "`content`" in description

    schema_text = str(match["input_schema"]).lower()
    assert "tokens" in schema_text
    assert "token ids to convert back to text" in schema_text
    assert "localai model name or alias" in schema_text


@pytest.mark.asyncio
async def test_execute_additional_tool_validates_before_network_call():
    # get_agent_job requires an id. Supplying an unknown field must fail validation
    # locally, before the dispatcher can make a LocalAI request.
    result = await execute_additional_tool("get_agent_job", {"bogus": 1})
    assert result["ok"] is False
    assert "schema" in result["error"].lower()
    assert result["tool_name"] == "get_agent_job"
    assert result["validation_errors"]
    assert "input_schema" in result

    direct = await execute_additional_tool("chat", {})
    assert direct["ok"] is False
    assert "directly exposed" in direct["error"]

    unknown = await execute_additional_tool("does_not_exist", {})
    assert unknown["ok"] is False
    assert "Unknown additional tool" in unknown["error"]


def test_every_http_tool_description_explains_inputs_and_output():
    for op in OPS:
        if op.websocket:
            continue
        description = TOOL_DESCRIPTIONS[op.tool_name]
        assert len(description) >= 80, (op.tool_name, description)
        assert "Inputs:" in description, (op.tool_name, description)
        assert "Returns:" in description, (op.tool_name, description)
        assert f"{op.method} {op.path}" not in description

        for parameter in op.operation.get("parameters", []):
            name = safe_identifier(str(parameter.get("name", "value")))
            assert f"`{name}`" in description, (op.tool_name, name, description)


def test_referenced_request_bodies_surface_real_fields_in_description():
    checked = 0
    for op in OPS:
        if op.websocket:
            continue
        body_params = [p for p in op.operation.get("parameters", []) if p.get("in") == "body"]
        for parameter in body_params:
            schema = parameter.get("schema") or {}
            ref = schema.get("$ref")
            if not ref:
                continue
            definition = resolve_ref(SPEC, ref)
            properties = definition.get("properties", {})
            if not properties:
                continue
            description = TOOL_DESCRIPTIONS[op.tool_name]
            assert any(f"`{field}`" in description for field in properties), (op.tool_name, ref, description)
            checked += 1

    assert checked >= 40


def test_referenced_success_responses_surface_real_fields_in_description():
    checked = 0
    for op in OPS:
        if op.websocket:
            continue
        response = _success_response(op)
        if not response:
            continue
        schema = response.get("schema") or {}
        ref = schema.get("$ref")
        if not ref:
            continue
        definition = resolve_ref(SPEC, ref)
        properties = definition.get("properties", {})
        if not properties:
            continue
        description = TOOL_DESCRIPTIONS[op.tool_name]
        assert any(f"`{field}`" in description for field in properties), (op.tool_name, ref, description)
        checked += 1

    assert checked >= 30


def test_generated_operation_schemas_do_not_expose_wrapper_plumbing():
    for tool_name in ("chat", "detokenize", "list_models"):
        schema_text = str(TOOL_INPUT_SCHEMAS[tool_name]).lower()
        assert "extra_headers" not in schema_text
        assert "timeout_seconds" not in schema_text
