import base64

import httpx
import pytest

from localaimcp.client import LocalAIClient
from localaimcp.config import Settings


@pytest.mark.asyncio
async def test_response_json_wrapper(tmp_path):
    settings = Settings(output_dir=tmp_path, save_binary=True)
    client = LocalAIClient(settings)
    response = httpx.Response(200, json={"ok": "yes"}, headers={"content-type": "application/json"})
    result = await client._response(response, 1.2)
    assert result["ok"] is True
    assert result["data"] == {"ok": "yes"}


@pytest.mark.asyncio
async def test_binary_is_saved_and_inlined(tmp_path):
    settings = Settings(output_dir=tmp_path, inline_binary_limit=1024, save_binary=True)
    client = LocalAIClient(settings)
    response = httpx.Response(200, content=b"abc", headers={"content-type": "audio/x-wav"})
    result = await client._response(response, 1.2)
    assert result["base64"] == base64.b64encode(b"abc").decode()
    assert (tmp_path / result["saved_path"].split("/")[-1]).exists()
