from __future__ import annotations

import base64
import gzip
import json
import keyword
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

_TOOL_NAME_OVERRIDES: dict[tuple[str, str], str] = {
    ("POST", "/3d/generations"): "generate_3d_asset",
    ("POST", "/3d/remesh"): "remesh_3d_asset",
    ("GET", "/audio/transformations/stream"): "stream_audio_transform",
    ("POST", "/tts"): "text_to_speech_legacy",
    ("GET", "/system"): "get_system_info",
    ("GET", "/tokenMetrics"): "get_token_metrics",
    ("POST", "/v1/audio/classification"): "classify_audio",
    ("POST", "/v1/audio/diarization"): "diarize_audio",
    ("POST", "/v1/audio/speech"): "text_to_speech",
    ("POST", "/v1/audio/transcriptions"): "transcribe_audio",
    ("POST", "/v1/chat/completions"): "chat",
    ("POST", "/v1/completions"): "complete_text",
    ("POST", "/v1/depth"): "estimate_depth",
    ("POST", "/v1/detection"): "detect_objects",
    ("POST", "/v1/detokenize"): "detokenize",
    ("POST", "/v1/edits"): "edit_text",
    ("POST", "/v1/embeddings"): "embed",
    ("POST", "/v1/images/edits"): "edit_image",
    ("POST", "/v1/images/generations"): "generate_image",
    ("POST", "/v1/images/variations"): "create_image_variation",
    ("POST", "/v1/mcp/chat/completions"): "chat_with_mcp_tools",
    ("GET", "/v1/models"): "list_models",
    ("GET", "/v1/models/capabilities"): "list_model_capabilities",
    ("POST", "/v1/moderations"): "moderate_text",
    ("POST", "/v1/rerank"): "rerank",
    ("POST", "/v1/responses"): "create_response",
    ("GET", "/v1/responses/{id}"): "get_response",
    ("POST", "/v1/tokenize"): "tokenize",
    ("GET", "/ws/backend-logs/{modelId}"): "stream_backend_logs",
}

_VERB_NORMALIZATION = {
    "analyzes": "analyze",
    "applies": "apply",
    "cancels": "cancel",
    "classifies": "classify",
    "creates": "create",
    "deletes": "delete",
    "detects": "detect",
    "estimates": "estimate",
    "executes": "execute",
    "extracts": "extract",
    "generates": "generate",
    "identifies": "identify",
    "lists": "list",
    "loads": "load",
    "removes": "remove",
    "reports": "get",
    "resets": "reset",
    "returns": "get",
    "reranks": "rerank",
    "runs": "run",
    "shows": "get",
    "streams": "stream",
    "toggles": "toggle",
    "updates": "update",
    "uploads": "upload",
}


@dataclass(frozen=True, slots=True)
class Operation:
    method: str
    path: str
    operation: dict[str, Any]
    tool_name: str
    websocket: bool

    @property
    def summary(self) -> str:
        return self.operation.get("summary") or f"{self.method} {self.path}"

    @property
    def tags(self) -> set[str]:
        return {str(x).lower() for x in self.operation.get("tags", [])} | {"localai"}


def load_spec() -> dict[str, Any]:
    payload = files("localaimcp").joinpath("swagger.json.gz.b64").read_text().strip()
    return json.loads(gzip.decompress(base64.b64decode(payload)))


def safe_identifier(value: str) -> str:
    value = value.replace("{", "by_").replace("}", "")
    value = value.replace("-", "_")
    value = re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_").lower()
    value = re.sub(r"_+", "_", value)
    if not value:
        value = "value"
    if value[0].isdigit():
        value = f"v_{value}"
    if keyword.iskeyword(value):
        value += "_value"
    return value


def _summary_tool_name(summary: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", summary or "")
    text = re.sub(r"\bLocalAI\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bOpenAI\b", " ", text, flags=re.IGNORECASE)
    words = safe_identifier(text).split("_")
    words = [w for w in words if w not in {"a", "an", "the", "api", "endpoint"}]
    if words:
        words[0] = _VERB_NORMALIZATION.get(words[0], words[0])
    if len(words) > 9:
        words = words[:9]
    return "_".join(words).strip("_") or "call_localai"


def _path_qualifier(path: str) -> str:
    parts = []
    for raw in path.strip("/").split("/"):
        if raw in {"api", "v1"} or raw.startswith("{"):
            continue
        parts.append(safe_identifier(raw))
    return "_".join(parts[-3:]) or "route"


def _unique_name(base: str, method: str, path: str, used: set[str]) -> str:
    if base not in used:
        return base

    qualifier = _path_qualifier(path)
    candidate = base if qualifier in base else f"{base}_{qualifier}"
    if candidate not in used:
        return candidate

    action = {
        "GET": "read",
        "POST": "run",
        "PUT": "update",
        "PATCH": "patch",
        "DELETE": "remove",
        "HEAD": "head",
        "OPTIONS": "options",
    }.get(method, method.lower())
    candidate = f"{base}_{action}"
    index = 2
    while candidate in used:
        candidate = f"{base}_{action}_{index}"
        index += 1
    return candidate


def tool_name(method: str, path: str, operation: dict[str, Any]) -> str:
    method = method.upper()
    override = _TOOL_NAME_OVERRIDES.get((method, path))
    if override:
        return override
    return _summary_tool_name(operation.get("summary") or operation.get("description") or f"{method} {path}")


def is_websocket_operation(operation: dict[str, Any], path: str) -> bool:
    text = " ".join(str(operation.get(k, "")) for k in ("summary", "description")).lower()
    return path.startswith("/ws/") or "websocket" in text


def operations(spec: dict[str, Any] | None = None) -> list[Operation]:
    spec = spec or load_spec()
    result: list[Operation] = []
    used_names: set[str] = set()
    for path, path_item in spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            http_method = method.upper()
            base_name = tool_name(http_method, path, operation)
            name = _unique_name(base_name, http_method, path, used_names)
            used_names.add(name)
            result.append(
                Operation(
                    method=http_method,
                    path=path,
                    operation=operation,
                    tool_name=name,
                    websocket=is_websocket_operation(operation, path),
                )
            )
    return result


def resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/definitions/"):
        return {}
    return spec.get("definitions", {}).get(ref.removeprefix("#/definitions/"), {})
