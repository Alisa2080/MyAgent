def test_langchain_stub_toolmessage_identity_is_stable(monkeypatch):
    import sys

    import conftest

    monkeypatch.setattr(conftest, "_is_real_package_installed", lambda name: False)

    conftest._install_langchain_stubs()
    first = sys.modules["langchain_core.messages"].ToolMessage

    conftest._install_langchain_stubs(monkeypatch)
    second = sys.modules["langchain_core.messages"].ToolMessage

    assert second is first


def test_fake_tool_marks_runtime_as_injected_arg():
    import conftest

    @conftest._fake_tool("example")
    def example(value: str, *, runtime):
        return value

    assert example.name == "example"
    assert example.func is example
    assert "runtime" in example._injected_args_keys
    assert "runtime" not in example.args
    assert "value" in example.args
