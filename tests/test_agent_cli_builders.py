def _patch_build_agent_side_effects(monkeypatch, builders):
    monkeypatch.setattr(
        builders,
        "create_agent",
        lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(
        builders.memory_store,
        "format_for_system_prompt",
        lambda name: "",
    )
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: 0)
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)


def test_build_agent_forwards_checkpointer(monkeypatch):
    import agent_core.builders as builders

    _patch_build_agent_side_effects(monkeypatch, builders)

    result = builders.build_agent(checkpointer="checkpoint")

    assert result["checkpointer"] == "checkpoint"


def test_build_agent_uses_tool_catalog_build_tools(monkeypatch):
    import agent_core.builders as builders

    fake_tool = object()
    calls = []

    def fake_build_tools(**kwargs):
        calls.append(kwargs)
        return [fake_tool]

    monkeypatch.setattr(builders, "build_tools", fake_build_tools)
    monkeypatch.setattr(builders, "create_agent", lambda **kwargs: kwargs)
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda target: "")
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: None)
    monkeypatch.setattr(builders, "load_project_instruction_blocks", lambda workdir: [])

    agent_config = builders.build_agent(include_cron_tools=True, checkpointer="cp")

    assert calls == [
        {
            "enabled_toolsets": None,
            "include_cron_tools": True,
            "runtime_profile": None,
        }
    ]
    assert agent_config["tools"] == [fake_tool]
    assert agent_config["checkpointer"] == "cp"


def test_build_agent_includes_tool_bus_before_policy(monkeypatch):
    import agent_core.builders as builders

    class FakeToolBus:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakePolicy:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(builders, "ToolBusMiddleware", FakeToolBus)
    monkeypatch.setattr(builders, "PolicyToolMiddleware", FakePolicy)
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda target: "")
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: None)
    monkeypatch.setattr(builders, "load_project_instruction_blocks", lambda workdir: [])
    monkeypatch.setattr(builders, "create_agent", lambda **kwargs: kwargs)

    agent_config = builders.build_agent()
    middleware = agent_config["middleware"]
    tool_bus_index = next(i for i, item in enumerate(middleware) if isinstance(item, FakeToolBus))
    policy_index = next(i for i, item in enumerate(middleware) if isinstance(item, FakePolicy))

    assert tool_bus_index < policy_index
    assert middleware[tool_bus_index].kwargs["specs"]
    assert "terminal" in middleware[tool_bus_index].kwargs["specs"]
