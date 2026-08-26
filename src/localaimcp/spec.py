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


def tool_name(method: str, path: str) -> str:
    parts = [safe_identifier(p) for p in path.strip("/").split("/") if p]
    name = "localai_" + "_".join(parts + [method.lower()])
    if len(name) <= 96:
        return name
    return name[:70].rstrip("_") + "_" + name[-25:].lstrip("_")


def is_websocket_operation(operation: dict[str, Any], path: str) -> bool:
    text = " ".join(str(operation.get(k, "")) for k in ("summary", "description")).lower()
    return path.startswith("/ws/") or "websocket" in text


def operations(spec: dict[str, Any] | None = None) -> list[Operation]:
    spec = spec or load_spec()
    result: list[Operation] = []
    for path, path_item in spec.get("paths", {}).items():
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            result.append(Operation(method=method.upper(), path=path, operation=operation, tool_name=tool_name(method, path), websocket=is_websocket_operation(operation, path)))
    return result


def resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    if not ref.startswith("#/definitions/"):
        return {}
    return spec.get("definitions", {}).get(ref.removeprefix("#/definitions/"), {})
