from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import time
from pathlib import Path
from typing import Any

import httpx
import websockets
from pydantic import BaseModel

from .config import Settings


class LocalAIClient:
    """Async, request-scoped LocalAI client. No conversational/session state is retained."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "*/*", "User-Agent": "LocalAiMCP/0.1"}
        if self.settings.localai_api_key:
            headers["Authorization"] = f"Bearer {self.settings.localai_api_key}"
        if extra:
            headers.update({str(k): str(v) for k, v in extra.items()})
        return headers

    def _url(self, path: str) -> str:
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise ValueError("path must be an absolute LocalAI path such as /v1/models")
        return f"{self.settings.localai_base_url}{path}"

    def _timeout(self, value: float | None = None) -> httpx.Timeout:
        return httpx.Timeout(timeout=value or self.settings.request_timeout, connect=self.settings.connect_timeout)

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any = None,
        form: dict[str, Any] | None = None,
        file_fields: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        query = {k: v for k, v in (query or {}).items() if v is not None}
        if isinstance(body, BaseModel):
            body = body.model_dump(mode="json", exclude_none=True, by_alias=True)
        form = {k: self._form_value(v) for k, v in (form or {}).items() if v is not None}
        files: dict[str, tuple[str, bytes, str]] = {}

        limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
        async with httpx.AsyncClient(timeout=self._timeout(timeout_seconds), limits=limits, follow_redirects=True) as http:
            for name, source in (file_fields or {}).items():
                if source is not None:
                    files[name] = await self._load_file(http, source)

            kwargs: dict[str, Any] = {"params": query or None, "headers": self._headers(extra_headers)}
            if files or form:
                kwargs["data"] = form or None
                kwargs["files"] = files or None
            elif body is not None:
                kwargs["json"] = body

            started = time.perf_counter()
            try:
                response = await http.request(method.upper(), self._url(path), **kwargs)
            except httpx.HTTPError as exc:
                return {"ok": False, "network_error": str(exc), "method": method.upper(), "path": path}
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            return await self._response(response, elapsed_ms)

    async def _load_file(self, http: httpx.AsyncClient, source: Any) -> tuple[str, bytes, str]:
        if isinstance(source, BaseModel):
            source = source.model_dump()
        if isinstance(source, dict):
            source = source.get("data") or source.get("url") or source.get("path")
        if not isinstance(source, str) or not source:
            raise ValueError("file input must be a path, URL, data URI, or base64:<data>")

        filename = "upload.bin"
        mime = "application/octet-stream"
        if source.startswith("data:"):
            header, payload = source.split(",", 1)
            mime = header[5:].split(";", 1)[0] or mime
            data = base64.b64decode(payload) if ";base64" in header else payload.encode()
        elif source.startswith("base64:"):
            data = base64.b64decode(source[7:])
        elif source.startswith(("http://", "https://")):
            response = await http.get(source)
            response.raise_for_status()
            data = response.content
            filename = Path(response.url.path).name or filename
            mime = response.headers.get("content-type", mime).split(";", 1)[0]
        else:
            path = Path(source).expanduser().resolve()
            root = self.settings.file_root.expanduser().resolve()
            if root not in path.parents and path != root:
                raise ValueError(f"local file must be under {root}")
            data = await asyncio.to_thread(path.read_bytes)
            filename = path.name
            mime = mimetypes.guess_type(filename)[0] or mime

        if len(data) > self.settings.max_upload_bytes:
            raise ValueError(f"file exceeds {self.settings.max_upload_bytes} byte upload limit")
        return filename, data, mime

    @staticmethod
    def _form_value(value: Any) -> str | list[str]:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, separators=(",", ":"))
        return str(value)

    async def _response(self, response: httpx.Response, elapsed_ms: float) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        common = {"ok": response.is_success, "status_code": response.status_code, "content_type": content_type or None, "elapsed_ms": elapsed_ms}
        useful_headers = {k: v for k, v in response.headers.items() if k.lower() in {"x-total-count", "x-trace-offset", "x-trace-limit", "location", "content-disposition"}}
        if useful_headers:
            common["headers"] = useful_headers
        if response.status_code == 204 or not response.content:
            return common
        if len(response.content) > self.settings.max_response_bytes:
            return {**common, "ok": False, "error": "response exceeds configured byte limit", "size_bytes": len(response.content)}
        if content_type == "text/event-stream":
            events: list[Any] = []
            for line in response.text.splitlines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    events.append("[DONE]")
                else:
                    try:
                        events.append(json.loads(data))
                    except json.JSONDecodeError:
                        events.append(data)
            return {**common, "events": events}
        if content_type == "application/json" or content_type.endswith("+json"):
            try:
                return {**common, "data": response.json()}
            except ValueError:
                pass
        if content_type.startswith("text/") or content_type in {"application/xml", "application/yaml"}:
            return {**common, "text": response.text}
        return {**common, **await self._binary_result(response.content, content_type, response.headers.get("content-disposition"))}

    async def _binary_result(self, data: bytes, mime: str, disposition: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {"size_bytes": len(data), "mime_type": mime or "application/octet-stream"}
        if len(data) <= self.settings.inline_binary_limit:
            result["base64"] = base64.b64encode(data).decode()
        if self.settings.save_binary:
            ext = mimetypes.guess_extension(mime or "") or ".bin"
            name = f"localai-{time.time_ns()}{ext}"
            if disposition and "filename=" in disposition:
                candidate = disposition.split("filename=", 1)[1].strip().strip('"')
                if candidate:
                    name = f"{time.time_ns()}-{Path(candidate).name}"
            self.settings.output_dir.mkdir(parents=True, exist_ok=True)
            path = self.settings.output_dir / name
            await asyncio.to_thread(path.write_bytes, data)
            result["saved_path"] = str(path)
        return result

    async def websocket_collect(
        self,
        path: str,
        *,
        initial_json: dict[str, Any] | None = None,
        binary_frames: list[str] | None = None,
        max_messages: int = 50,
        timeout_seconds: float = 10,
    ) -> dict[str, Any]:
        base = self.settings.localai_base_url
        ws_base = ("wss://" + base[8:]) if base.startswith("https://") else ("ws://" + base[7:] if base.startswith("http://") else base)
        url = ws_base + path
        headers = self._headers()
        received: list[Any] = []
        try:
            async with websockets.connect(url, additional_headers=headers, open_timeout=timeout_seconds, close_timeout=2) as ws:
                if initial_json is not None:
                    await ws.send(json.dumps(initial_json))
                for frame in binary_frames or []:
                    payload = frame[7:] if frame.startswith("base64:") else frame
                    await ws.send(base64.b64decode(payload))
                for _ in range(max(1, max_messages)):
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=timeout_seconds)
                    except asyncio.TimeoutError:
                        break
                    if isinstance(message, bytes):
                        received.append({"binary_base64": base64.b64encode(message).decode(), "size_bytes": len(message)})
                    else:
                        try:
                            received.append(json.loads(message))
                        except json.JSONDecodeError:
                            received.append(message)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "url": url, "messages": received}
        return {"ok": True, "url": url, "message_count": len(received), "messages": received}
