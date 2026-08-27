from __future__ import annotations

import inspect
from typing import Annotated, Any, Literal, Optional
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from .client import LocalAIClient
from .config import Settings
from .metadata import field_description, operation_description, parameter_description
from .spec import Operation, load_spec, operations, safe_identifier

SETTINGS = Settings()
SPEC = load_spec()
OPS = operations(SPEC)
CLIENT = LocalAIClient(SETTINGS)
TOOL_DESCRIPTIONS = {op.tool_name: operation_description(SPEC, op) for op in OPS}
OP_BY_NAME = {op.tool_name: op for op in OPS}

if SETTINGS.exposed_tools == ("*",):
    EXPOSED_TOOL_NAMES = frozenset(OP_BY_NAME)
else:
    unknown = sorted(set(SETTINGS.exposed_tools) - set(OP_BY_NAME))
    if unknown:
        raise ValueError("LOCALAI_MCP_EXPOSED_TOOLS contains unknown tool names: " + ", ".join(unknown))
    EXPOSED_TOOL_NAMES = frozenset(SETTINGS.exposed_tools)

_MODEL_CACHE: dict[str, type[BaseModel]] = {}
_ARGUMENT_MODEL_CACHE: dict[str, type[BaseModel]] = {}
TOOL_CALLABLES: dict[str, Any] = {}
TOOL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {}


def _schema_type(schema: dict[str, Any], name_hint: str) -> Any:
    if "$ref" in schema:
        return _model_for_definition(schema["$ref"].removeprefix("#/definitions/"))
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
    if name not in _MODEL_CACHE:
        schema = SPEC.get("definitions", {}).get(name, {"type": "object"})
        _MODEL_CACHE[name] = _model_from_schema(name, schema)
    return _MODEL_CACHE[name]


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
        description = (
            "JSON object containing model configuration fields to deep-merge into the existing config."
            if op.path == "/api/models/config-json/{name}"
            else "Optional JSON object for LocalAI extension fields not declared by this Swagger operation."
        )
        params.append(
            inspect.Parameter(
                "body",
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=_annotated(Optional[dict[str, Any]], description),
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
            raw, where = item["raw_name"], item["in"]
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
        return await CLIENT.request(op.method, path, query=query, body=body, form=form, file_fields=files)

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
        Field(description="Realtime audio transformation session/config object expected before audio frames."),
    ],
    frames_base64: Annotated[
        list[str],
        Field(description="Ordered PCM audio frames encoded as base64 strings; `base64:` prefix is optional."),
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


WS_DESCRIPTIONS = {
    "stream_backend_logs": (
        "Stream backend-process log messages for one loaded model over WebSocket. Input `model_id` identifies the model; "
        "`max_messages` and `timeout_seconds` bound the read. Returns collected messages and count, then closes."
    ),
    "stream_audio_transform": (
        "Run a bounded realtime audio-transformation WebSocket exchange. Send a session/config object then base64 PCM frames. "
        "Returns transformed text/JSON/binary messages collected until the limit or timeout."
    ),
}


def tool_description(name: str) -> str:
    return WS_DESCRIPTIONS.get(name, TOOL_DESCRIPTIONS[name])


def _operation_callable(op: Operation):
    if not op.websocket:
        return _make_http_callable(op)
    if op.path.startswith("/ws/backend-logs/"):
        fn = _ws_backend_logs
    elif op.path == "/audio/transformations/stream":
        fn = _ws_audio_transform
    else:
        raise RuntimeError(f"No WebSocket adapter for {op.path}")
    fn.__name__ = op.tool_name
    fn.__qualname__ = op.tool_name
    return fn


def argument_model(tool_name: str, fn: Any | None = None) -> type[BaseModel]:
    cached = _ARGUMENT_MODEL_CACHE.get(tool_name)
    if cached is not None:
        return cached
    fn = fn or TOOL_CALLABLES[tool_name]
    fields: dict[str, tuple[Any, Any]] = {}
    for parameter in inspect.signature(fn).parameters.values():
        annotation = parameter.annotation if parameter.annotation is not inspect.Parameter.empty else Any
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter.name] = (annotation, default)
    model = create_model(
        tool_name.title().replace("_", "") + "Arguments",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    _ARGUMENT_MODEL_CACHE[tool_name] = model
    return model


for _op in OPS:
    _fn = _operation_callable(_op)
    TOOL_CALLABLES[_op.tool_name] = _fn
    TOOL_INPUT_SCHEMAS[_op.tool_name] = argument_model(_op.tool_name, _fn).model_json_schema()


async def execute_operation(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    model = argument_model(tool_name)
    try:
        validated = model.model_validate(arguments)
    except ValidationError as exc:
        return {
            "ok": False,
            "error": "Arguments did not match the tool schema.",
            "tool_name": tool_name,
            "validation_errors": exc.errors(include_url=False),
            "input_schema": TOOL_INPUT_SCHEMAS[tool_name],
        }
    kwargs = {name: getattr(validated, name) for name in model.model_fields if getattr(validated, name) is not None}
    try:
        return await TOOL_CALLABLES[tool_name](**kwargs)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc), "tool_name": tool_name}
