from localaimcp.spec import load_spec, operations, tool_name


def test_full_swagger_operation_coverage():
    spec = load_spec()
    ops = operations(spec)
    assert len(spec["paths"]) == 114
    assert len(ops) == 123
    assert len({op.tool_name for op in ops}) == 123


def test_websocket_detection():
    ops = operations(load_spec())
    ws = {(op.method, op.path) for op in ops if op.websocket}
    assert ws == {
        ("GET", "/audio/transformations/stream"),
        ("GET", "/ws/backend-logs/{modelId}"),
    }


def test_tool_names_are_deterministic():
    assert tool_name("post", "/v1/chat/completions") == "localai_v1_chat_completions_post"
    assert tool_name("get", "/api/agent/jobs/{id}") == "localai_api_agent_jobs_by_id_get"
