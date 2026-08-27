import re

from localaimcp.spec import MAX_TOOL_NAME_LENGTH, load_spec, operations


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


def test_tool_names_are_semantic_compact_and_deterministic():
    ops = operations(load_spec())
    names = {(op.method, op.path): op.tool_name for op in ops}

    assert names[("POST", "/v1/chat/completions")] == "chat"
    assert names[("POST", "/v1/detokenize")] == "detokenize"
    assert names[("POST", "/v1/tokenize")] == "tokenize"
    assert names[("POST", "/v1/audio/transcriptions")] == "transcribe_audio"
    assert names[("GET", "/audio/transformations/stream")] == "stream_audio_transform"
    assert names[("GET", "/ws/backend-logs/{modelId}")] == "stream_backend_logs"

    assert all(len(op.tool_name) <= MAX_TOOL_NAME_LENGTH for op in ops)
    assert all(not op.tool_name.startswith("localai_") for op in ops)
    assert all(not re.search(r"_(get|post|put|patch|delete)$", op.tool_name) for op in ops)
