import pytest
from fastmcp import Client

from localaimcp.server import OPS, SPEC, TOOL_DESCRIPTIONS, mcp
from localaimcp.spec import resolve_ref, safe_identifier


@pytest.mark.asyncio
async def test_fastmcp_exposes_complete_tool_surface():
    async with Client(mcp) as client:
        tools = await client.list_tools()

    names = {tool.name for tool in tools}
    assert len(OPS) == 123
    assert len(tools) == 128  # 123 Swagger operations + 5 MCP management/discovery helpers
    assert len(names) == len(tools)
    assert "chat" in names
    assert "transcribe_audio" in names
    assert "detokenize" in names
    assert "tokenize" in names
    assert "stream_audio_transform" in names
    assert "stream_backend_logs" in names
    assert "find_tools" in names
    assert "server_health" in names
    assert "schema_audit" in names
    assert "probe_safe_endpoints" in names
    assert "raw_request" in names


@pytest.mark.asyncio
async def test_detokenize_is_self_describing_to_an_mcp_client():
    async with Client(mcp) as client:
        tools = await client.list_tools()

    tool = next(tool for tool in tools if tool.name == "detokenize")
    description = tool.description or ""
    assert "token IDs" in description
    assert "`tokens`" in description
    assert "`model`" in description
    assert "`content`" in description

    dumped = tool.model_dump(by_alias=True)
    schema_text = str(dumped.get("inputSchema") or dumped.get("input_schema") or "").lower()
    assert "tokens" in schema_text
    assert "token ids to convert back to text" in schema_text
    assert "localai model name or alias" in schema_text


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


def test_generated_http_tools_do_not_expose_wrapper_plumbing_by_default():
    # Wrapper-level controls belong on raw_request, not on every normal LocalAI tool.
    for tool_name in ("chat", "detokenize", "list_models"):
        op = next(op for op in OPS if op.tool_name == tool_name)
        if op.method == "GET" and not op.operation.get("parameters"):
            continue
        params = {safe_identifier(str(p.get("name", "value"))) for p in op.operation.get("parameters", [])}
        assert "extra_headers" not in params
        assert "timeout_seconds" not in params
