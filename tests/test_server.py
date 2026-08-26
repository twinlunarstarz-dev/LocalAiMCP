import pytest
from fastmcp import Client

from localaimcp.server import OPS, mcp


@pytest.mark.asyncio
async def test_fastmcp_exposes_complete_tool_surface():
    async with Client(mcp) as client:
        tools = await client.list_tools()

    names = {tool.name for tool in tools}
    assert len(OPS) == 123
    assert len(tools) == 127  # 123 Swagger operations + 4 management/test helpers
    assert len(names) == len(tools)
    assert "localai_v1_chat_completions_post" in names
    assert "localai_v1_audio_transcriptions_post" in names
    assert "localai_audio_transformations_stream_get" in names
    assert "localai_ws_backend_logs_by_modelid_get" in names
    assert "localai_health" in names
    assert "localai_schema_audit" in names
    assert "localai_probe_safe_gets" in names
    assert "localai_raw_request" in names


def test_every_swagger_operation_has_a_description():
    assert all(op.summary.strip() for op in OPS)
