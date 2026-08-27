import re

from localaimcp.spec import MAX_TOOL_NAME_LENGTH, load_spec, operations
from localaimcp.tool_names import TOOL_NAME_OVERRIDES


def test_full_swagger_operation_coverage():
    spec = load_spec()
    ops = operations(spec)
    assert len(spec["paths"]) == 114
    assert len(ops) == 123
    assert len({op.tool_name for op in ops}) == 123


def test_full_swagger_metadata_is_preserved():
    spec = load_spec()
    assert len(spec["definitions"]) == 166

    detokenize = spec["paths"]["/v1/detokenize"]["post"]
    assert detokenize["parameters"][0]["schema"]["$ref"] == "#/definitions/schema.DetokenizeRequest"
    assert detokenize["responses"]["200"]["schema"]["$ref"] == "#/definitions/schema.DetokenizeResponse"

    request = spec["definitions"]["schema.DetokenizeRequest"]
    response = spec["definitions"]["schema.DetokenizeResponse"]
    assert "tokens" in request["properties"]
    assert "model" in request["properties"]
    assert "content" in response["properties"]


def test_websocket_detection():
    ops = operations(load_spec())
    ws = {(op.method, op.path) for op in ops if op.websocket}
    assert ws == {
        ("GET", "/audio/transformations/stream"),
        ("GET", "/ws/backend-logs/{modelId}"),
    }


def test_every_bundled_operation_has_an_explicit_reviewed_name():
    spec = load_spec()
    swagger_operations = {
        (method.upper(), path)
        for path, path_item in spec["paths"].items()
        for method, operation in path_item.items()
        if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}
        and isinstance(operation, dict)
    }
    assert len(TOOL_NAME_OVERRIDES) == 123
    assert set(TOOL_NAME_OVERRIDES) == swagger_operations
    assert len(set(TOOL_NAME_OVERRIDES.values())) == 123
    assert max(map(len, TOOL_NAME_OVERRIDES.values())) <= 32


def test_tool_names_are_semantic_compact_and_deterministic():
    ops = operations(load_spec())
    names = {(op.method, op.path): op.tool_name for op in ops}

    assert names[("POST", "/v1/chat/completions")] == "chat"
    assert names[("POST", "/v1/detokenize")] == "detokenize"
    assert names[("POST", "/v1/tokenize")] == "tokenize"
    assert names[("POST", "/v1/audio/transcriptions")] == "transcribe_audio"
    assert names[("POST", "/api/pii/redact")] == "redact_pii"
    assert names[("POST", "/api/router/decide")] == "route_prompt"
    assert names[("PUT", "/api/nodes/{id}/vram-budget")] == "set_node_vram_budget"
    assert names[("POST", "/v1/voice/verify")] == "verify_speakers"
    assert names[("POST", "/video")] == "generate_video"
    assert names[("GET", "/audio/transformations/stream")] == "stream_audio_transform"
    assert names[("GET", "/ws/backend-logs/{modelId}")] == "stream_backend_logs"

    assert all(len(op.tool_name) <= MAX_TOOL_NAME_LENGTH for op in ops)
    assert all(not op.tool_name.startswith("localai_") for op in ops)
    assert all(not re.search(r"_(get|post|put|patch|delete)$", op.tool_name) for op in ops)
