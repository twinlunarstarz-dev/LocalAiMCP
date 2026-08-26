from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    localai_base_url: str = os.getenv("LOCALAI_BASE_URL", "http://host.docker.internal:8080").rstrip("/")
    localai_api_key: str | None = os.getenv("LOCALAI_API_KEY") or None
    request_timeout: float = float(os.getenv("LOCALAI_REQUEST_TIMEOUT", "300"))
    connect_timeout: float = float(os.getenv("LOCALAI_CONNECT_TIMEOUT", "10"))
    max_upload_bytes: int = int(os.getenv("LOCALAI_MCP_MAX_UPLOAD_BYTES", str(100 * 1024 * 1024)))
    max_response_bytes: int = int(os.getenv("LOCALAI_MCP_MAX_RESPONSE_BYTES", str(100 * 1024 * 1024)))
    inline_binary_limit: int = int(os.getenv("LOCALAI_MCP_INLINE_BINARY_LIMIT", str(1024 * 1024)))
    save_binary: bool = _bool("LOCALAI_MCP_SAVE_BINARY", True)
    output_dir: Path = Path(os.getenv("LOCALAI_MCP_OUTPUT_DIR", "/data/output"))
    file_root: Path = Path(os.getenv("LOCALAI_MCP_FILE_ROOT", "/data"))
    mcp_host: str = os.getenv("MCP_HOST", "0.0.0.0")
    mcp_port: int = int(os.getenv("MCP_PORT", "8000"))
    mcp_path: str = os.getenv("MCP_PATH", "/mcp")
    stateless_http: bool = _bool("MCP_STATELESS_HTTP", True)
