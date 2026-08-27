from __future__ import annotations

import re
from typing import Any

from .spec import Operation, resolve_ref, safe_identifier

_LOW_INFO = {
    "",
    "request",
    "request body",
    "query params",
    "query parameters",
    "parameters",
    "response",
    "ok",
    "success",
    "message",
    "error",
    "body",
    "value",
}

_FIELD_HINTS: dict[str, str] = {
    "model": "LocalAI model name or alias to use.",
    "models": "Model names or aliases.",
    "content": "Text or content value to process.",
    "text": "Text to process.",
    "input": "Input value to process.",
    "prompt": "Prompt text or prompt payload to process.",
    "messages": "Conversation messages, typically objects containing role and content.",
    "tokens": "Token IDs.",
    "stream": "Whether to request streaming output.",
    "temperature": "Sampling temperature; higher values generally increase randomness.",
    "max_tokens": "Maximum number of output tokens to generate.",
    "max_completion_tokens": "Maximum number of output tokens to generate.",
    "top_p": "Nucleus-sampling probability mass.",
    "top_k": "Top-k limit or number of highest-ranked results.",
    "stop": "Stop sequence or sequences.",
    "seed": "Random seed for reproducibility when supported.",
    "n": "Number of results to generate.",
    "voice": "Voice name or profile to use.",
    "speed": "Speech speed multiplier when supported.",
    "language": "Language code or language hint.",
    "response_format": "Requested output format.",
    "format": "Requested response format.",
    "image": "Input image accepted by this operation, commonly a URL, base64 string, or data URI.",
    "audio": "Input audio accepted by this operation, commonly a URL, base64 string, data URI, or PCM samples.",
    "file": "Input file.",
    "name": "Name of the target resource.",
    "id": "Identifier of the target resource.",
    "status": "Status value or status filter.",
    "limit": "Maximum number of results to return.",
    "offset": "Number of results to skip before returning results.",
    "page": "Page number to return.",
    "size": "Requested size or dimensions.",
    "width": "Output width in pixels.",
    "height": "Output height in pixels.",
    "fps": "Frames per second.",
    "duration": "Requested duration.",
    "duration_seconds": "Requested duration in seconds.",
    "threshold": "Score or confidence threshold.",
    "tools": "Tools/functions the model may call.",
    "tool_choice": "Controls whether and which tool/function the model may call.",
    "parallel_tool_calls": "Whether multiple tool calls may be emitted in parallel.",
    "reasoning": "Reasoning configuration.",
}

_BODY_FIELD_FOCUS: dict[tuple[str, str], tuple[str, ...]] = {
    ("POST", "/v1/chat/completions"): (
        "model", "messages", "temperature", "max_tokens", "stream", "tools", "tool_choice"
    ),
    ("POST", "/v1/mcp/chat/completions"): (
        "model", "messages", "temperature", "max_tokens", "stream", "tools", "tool_choice"
    ),
    ("POST", "/v1/completions"): (
        "model", "prompt", "temperature", "max_tokens", "stream", "stop"
    ),
    ("POST", "/v1/embeddings"): ("model", "input", "encoding_format", "dimensions"),
    ("POST", "/v1/edits"): ("model", "input", "instruction", "temperature", "top_p"),
    ("POST", "/v1/images/generations"): (
        "model", "prompt", "negative_prompt", "n", "size", "width", "height", "seed", "response_format"
    ),
    ("POST", "/v1/images/edits"): (
        "model", "prompt", "image", "mask", "n", "size", "response_format"
    ),
    ("POST", "/v1/images/variations"): (
        "model", "image", "n", "size", "response_format"
    ),
    ("POST", "/v1/responses"): (
        "model", "input", "instructions", "tools", "tool_choice", "stream", "temperature", "max_output_tokens"
    ),
    ("POST", "/v1/moderations"): ("model", "input"),
    ("POST", "/v1/rerank"): ("model", "query", "documents", "top_n", "return_documents"),
    ("POST", "/v1/tokenize"): ("content", "model"),
    ("POST", "/v1/detokenize"): ("tokens", "model"),
}

_OPERATION_PURPOSE_OVERRIDES: dict[tuple[str, str], str] = {
    ("POST", "/v1/tokenize"): "Convert text into tokenizer token IDs using the selected model.",
    ("POST", "/v1/detokenize"): "Convert tokenizer token IDs back into text using the selected model.",
    ("POST", "/v1/chat/completions"): "Generate an assistant reply from chat messages using an OpenAI-compatible chat request.",
    ("POST", "/v1/mcp/chat/completions"): "Generate a chat reply and let LocalAI automatically execute configured MCP tools during the turn.",
    ("POST", "/v1/completions"): "Generate text completion output from a prompt.",
    ("POST", "/v1/embeddings"): "Create vector embeddings for text or other model-supported input.",
    ("POST", "/v1/audio/transcriptions"): "Transcribe an uploaded audio file into text, with optional language and timestamp controls.",
    ("POST", "/v1/audio/speech"): "Generate speech audio from input text using the selected TTS model and voice.",
    ("POST", "/tts"): "Generate WAV speech audio from input text using the legacy LocalAI TTS route.",
    ("POST", "/v1/images/generations"): "Generate one or more images from a text prompt.",
    ("POST", "/v1/images/edits"): "Edit an input image from a text prompt, optionally using a mask.",
    ("POST", "/v1/images/variations"): "Create variations of an input image.",
    ("POST", "/v1/rerank"): "Rank candidate documents or phrases by relevance to a query.",
    ("POST", "/v1/moderations"): "Classify input text for potentially harmful content.",
    ("POST", "/3d/generations"): "Generate a 3D asset from a conditioning image.",
    ("POST", "/3d/remesh"): "Remesh an existing GLB into a watertight 3D asset suitable for printing or downstream use.",
}

_DESTRUCTIVE_WORDS = {
    "cancel",
    "clear",
    "delete",
    "disable",
    "forget",
    "remove",
    "reset",
    "shutdown",
    "terminate",
    "unpin",
}


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def meaningful_description(value: Any, *, field_name: str | None = None) -> bool:
    text = clean_text(value)
    if text.lower().strip(" .:") in _LOW_INFO:
        return False
    if field_name and text.lower().strip(" .:") in {
        field_name.lower(),
        field_name.replace("_", " ").lower(),
    }:
        return False
    return bool(text)


def schema_name(schema: dict[str, Any]) -> str | None:
    ref = schema.get("$ref")
    if not ref:
        return None
    return ref.removeprefix("#/definitions/").split(".")[-1]


def schema_type_label(spec: dict[str, Any], schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return schema_name(schema) or "object"
    typ = schema.get("type")
    if typ == "array":
        return f"array[{schema_type_label(spec, schema.get('items', {}))}]"
    if typ == "object":
        return "object"
    if typ == "file":
        return "file"
    return str(typ or "any")


def _constraint_suffix(schema: dict[str, Any]) -> str:
    pieces: list[str] = []
    enum = schema.get("enum")
    if enum:
        pieces.append("allowed: " + ", ".join(repr(x) for x in enum[:8]))
    if "default" in schema:
        pieces.append(f"default: {schema['default']!r}")
    if "minimum" in schema:
        pieces.append(f"min: {schema['minimum']}")
    if "maximum" in schema:
        pieces.append(f"max: {schema['maximum']}")
    return f" ({'; '.join(pieces)})" if pieces else ""


def field_description(
    spec: dict[str, Any],
    field_name: str,
    schema: dict[str, Any],
    *,
    model_name: str | None = None,
) -> str:
    raw = clean_text(schema.get("description"))
    if meaningful_description(raw, field_name=field_name):
        base = raw
    else:
        base = _FIELD_HINTS.get(field_name)
        if not base:
            human = field_name.replace("_", " ")
            base = f"{human[:1].upper() + human[1:]} value."
    return base.rstrip(".") + _constraint_suffix(schema) + "."


def _resolved_schema(spec: dict[str, Any], schema: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    ref = schema.get("$ref")
    if ref:
        return resolve_ref(spec, ref), schema_name(schema)
    return schema, None


def _schema_fields(
    spec: dict[str, Any],
    schema: dict[str, Any],
    *,
    focus: tuple[str, ...] | None = None,
    limit: int = 8,
) -> tuple[list[str], int]:
    resolved, model_name = _resolved_schema(spec, schema)
    properties = resolved.get("properties", {}) if isinstance(resolved, dict) else {}
    if not isinstance(properties, dict):
        return [], 0

    required = set(resolved.get("required", []))
    names = list(properties)
    if focus:
        focused = [name for name in focus if name in properties]
        names = focused + [name for name in names if name not in focused]
    else:
        names.sort(key=lambda n: (n not in required, not meaningful_description((properties[n] or {}).get("description"), field_name=n)))

    selected = names[:limit]
    result = []
    for name in selected:
        prop = properties.get(name) or {}
        required_mark = "required" if name in required else "optional"
        type_label = schema_type_label(spec, prop)
        desc = field_description(spec, name, prop, model_name=model_name)
        result.append(f"`{name}` ({type_label}, {required_mark}) — {desc}")
    return result, len(names)


def parameter_description(spec: dict[str, Any], op: Operation, parameter: dict[str, Any]) -> str:
    raw_name = str(parameter.get("name", "value"))
    raw_desc = clean_text(parameter.get("description"))
    location = parameter.get("in")
    schema = parameter.get("schema") or parameter

    if location == "body":
        focus = _BODY_FIELD_FOCUS.get((op.method, op.path))
        fields, total = _schema_fields(spec, schema, focus=focus, limit=8)
        model = schema_name(schema)
        lead = raw_desc if meaningful_description(raw_desc, field_name=raw_name) else (
            f"{model} request object" if model else "JSON request object"
        )
        if fields:
            extra = " Other optional fields are available in the input schema." if total > len(fields) else ""
            return f"{lead}. Key fields: {'; '.join(fields)}{extra}"
        return lead + "."

    if parameter.get("type") == "file":
        base = raw_desc if meaningful_description(raw_desc, field_name=raw_name) else "File to upload"
        return (
            base.rstrip(".")
            + ". Supply a data URI, base64:<data>, HTTP(S) URL, or a path under LOCALAI_MCP_FILE_ROOT."
        )

    if meaningful_description(raw_desc, field_name=raw_name):
        base = raw_desc
    else:
        base = field_description(spec, raw_name, schema)

    location_hint = {
        "path": "Target selector",
        "query": "Optional filter/control" if not parameter.get("required") else "Request control",
        "formData": "Multipart field",
    }.get(str(location))
    if location_hint and not base.lower().startswith(location_hint.lower()):
        return f"{location_hint}. {base}"
    return base


def _purpose(op: Operation) -> str:
    override = _OPERATION_PURPOSE_OVERRIDES.get((op.method, op.path))
    if override:
        return override

    summary = clean_text(op.operation.get("summary"))
    detail = clean_text(op.operation.get("description"))
    purpose = summary if meaningful_description(summary) else detail
    if not purpose:
        purpose = f"Use this LocalAI operation for {op.path}."
    if detail and meaningful_description(detail) and detail.lower() != purpose.lower():
        first_sentence = re.split(r"(?<=[.!?])\s+", detail, maxsplit=1)[0]
        if first_sentence and first_sentence.lower() not in purpose.lower():
            purpose = f"{purpose.rstrip('.')} — {first_sentence.rstrip('.')}"
    return purpose.rstrip(".") + "."


def _input_summary(spec: dict[str, Any], op: Operation) -> str:
    parameters = op.operation.get("parameters", [])
    if not parameters:
        return "Inputs: none."

    parts: list[str] = []
    for p in parameters:
        name = safe_identifier(str(p.get("name", "value")))
        location = p.get("in")
        if location == "body":
            schema = p.get("schema") or {}
            focus = _BODY_FIELD_FOCUS.get((op.method, op.path))
            fields, total = _schema_fields(spec, schema, focus=focus, limit=7)
            model = schema_name(schema)
            if fields:
                compact = "; ".join(fields)
                suffix = "; additional optional fields are available in the schema" if total > len(fields) else ""
                parts.append(f"`{name}` {model or 'object'}: {compact}{suffix}")
            else:
                parts.append(f"`{name}`: {parameter_description(spec, op, p)}")
        else:
            required = "required" if p.get("required") else "optional"
            desc = parameter_description(spec, op, p)
            parts.append(f"`{name}` ({required}): {desc}")

    return "Inputs: " + " | ".join(parts) + "."


def _success_response(op: Operation) -> dict[str, Any] | None:
    responses = op.operation.get("responses", {})
    candidates: list[tuple[int, dict[str, Any]]] = []
    for code, response in responses.items():
        try:
            numeric = int(code)
        except (TypeError, ValueError):
            continue
        if 200 <= numeric < 300 and isinstance(response, dict):
            candidates.append((numeric, response))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item[0])[0][1]


def _response_schema_summary(spec: dict[str, Any], schema: dict[str, Any]) -> str:
    resolved, model_name = _resolved_schema(spec, schema)
    typ = schema.get("type") or resolved.get("type")
    if typ == "array":
        item = schema.get("items", {})
        return f"an array of {schema_type_label(spec, item)} values"
    if typ == "file":
        return "binary file data"

    properties = resolved.get("properties", {}) if isinstance(resolved, dict) else {}
    if isinstance(properties, dict) and properties:
        fields, total = _schema_fields(spec, schema, limit=5)
        if fields:
            suffix = "; more fields may be present" if total > len(fields) else ""
            return f"{model_name or 'object'} with " + "; ".join(fields) + suffix

    return model_name or schema_type_label(spec, schema)


def _output_summary(spec: dict[str, Any], op: Operation) -> str:
    response = _success_response(op)
    produces = [str(x).lower() for x in op.operation.get("produces", [])]
    schema = (response or {}).get("schema") or {}

    if "text/event-stream" in produces:
        return "Returns: collected server-sent event payloads in the MCP wrapper's `events` field."
    binary_mimes = [
        p for p in produces
        if not (p.startswith("application/json") or p.endswith("+json") or p.startswith("text/"))
    ]
    if binary_mimes or schema.get("type") == "file":
        mime = binary_mimes[0] if binary_mimes else "binary data"
        return (
            f"Returns: {mime}. The MCP wrapper includes size/mime metadata and may include `base64` "
            "and/or `saved_path` depending on configured binary limits."
        )

    if response is None:
        return "Returns: LocalAI status metadata in the standard MCP wrapper."

    if not schema:
        description = clean_text(response.get("description"))
        if description and description.lower() not in {"ok", "response"}:
            return f"Returns: {description.rstrip('.')}."
        return "Returns: success status; the endpoint may not include a response body."

    summary = _response_schema_summary(spec, schema)
    return f"Returns: JSON in the MCP wrapper's `data` field: {summary}."


def operation_description(spec: dict[str, Any], op: Operation) -> str:
    parts = [_purpose(op), _input_summary(spec, op), _output_summary(spec, op)]

    lowered = f"{op.tool_name} {op.summary}".lower()
    if op.method == "DELETE" or any(word in lowered for word in _DESTRUCTIVE_WORDS):
        parts.append("Caution: this changes or removes server state.")

    text = " ".join(clean_text(part) for part in parts if part)
    if len(text) > 1400:
        text = text[:1390].rsplit(" ", 1)[0] + "…"
    return text


def operation_search_text(spec: dict[str, Any], op: Operation) -> str:
    tags = " ".join(sorted(op.tags))
    return " ".join(
        [
            op.tool_name.replace("_", " "),
            clean_text(op.summary),
            clean_text(op.operation.get("description")),
            tags,
            op.path,
        ]
    ).lower()
