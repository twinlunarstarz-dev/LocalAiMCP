from localaimcp.config import DEFAULT_EXPOSED_TOOLS, _tool_list


def test_default_exposed_tool_preset_is_small_and_unique(monkeypatch):
    monkeypatch.delenv("TEST_EXPOSED_TOOLS", raising=False)
    assert len(DEFAULT_EXPOSED_TOOLS) == 20
    assert len(set(DEFAULT_EXPOSED_TOOLS)) == 20
    assert _tool_list("TEST_EXPOSED_TOOLS", DEFAULT_EXPOSED_TOOLS) == DEFAULT_EXPOSED_TOOLS


def test_exposed_tool_list_supports_all_none_and_custom(monkeypatch):
    monkeypatch.setenv("TEST_EXPOSED_TOOLS", "*")
    assert _tool_list("TEST_EXPOSED_TOOLS", DEFAULT_EXPOSED_TOOLS) == ("*",)

    monkeypatch.setenv("TEST_EXPOSED_TOOLS", "none")
    assert _tool_list("TEST_EXPOSED_TOOLS", DEFAULT_EXPOSED_TOOLS) == ()

    monkeypatch.setenv("TEST_EXPOSED_TOOLS", "chat, list_models, chat")
    assert _tool_list("TEST_EXPOSED_TOOLS", DEFAULT_EXPOSED_TOOLS) == ("chat", "list_models")

    monkeypatch.setenv("TEST_EXPOSED_TOOLS", "")
    assert _tool_list("TEST_EXPOSED_TOOLS", DEFAULT_EXPOSED_TOOLS) == DEFAULT_EXPOSED_TOOLS
