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


def test_build_agent_wires_policy_pre_hook_into_toolbus(monkeypatch):
    import agent_core.builders as builders

    class FakeToolBus:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakePolicy:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    factory_calls = []

    def sentinel_hook(*args, **kwargs):
        return None

    def fake_build_policy_pre_hook(*, policy_tools):
        factory_calls.append(policy_tools)
        return sentinel_hook

    monkeypatch.setattr(builders, "ToolBusMiddleware", FakeToolBus)
    monkeypatch.setattr(builders, "PolicyToolMiddleware", FakePolicy, raising=False)
    monkeypatch.setattr(
        builders,
        "build_policy_pre_hook",
        fake_build_policy_pre_hook,
        raising=False,
    )
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda target: "")
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: None)
    monkeypatch.setattr(builders, "load_project_instruction_blocks", lambda workdir: [])
    monkeypatch.setattr(builders, "create_agent", lambda **kwargs: kwargs)

    agent_config = builders.build_agent()
    middleware = agent_config["middleware"]
    tool_bus_items = [item for item in middleware if isinstance(item, FakeToolBus)]
    policy_items = [item for item in middleware if isinstance(item, FakePolicy)]

    assert len(tool_bus_items) == 1
    assert policy_items == []
    assert tool_bus_items[0].kwargs["specs"]
    assert "terminal" in tool_bus_items[0].kwargs["specs"]
    hooks = tool_bus_items[0].kwargs["hooks"]
    assert factory_calls == [builders.POLICY_REVIEW_TOOLS]
    assert hooks.pre_tool_call == [sentinel_hook]


def test_build_agent_wires_read_only_before_toolbus(monkeypatch):
    import agent_core.builders as builders

    class FakeReadOnlyLimit:
        def __init__(self, *, specs):
            self.specs = specs

    class FakeToolBus:
        def __init__(self, *, specs, hooks):
            self.specs = specs
            self.hooks = hooks

    factory_calls = []

    def sentinel_hook(*args, **kwargs):
        return None

    def fake_build_policy_pre_hook(*, policy_tools):
        factory_calls.append(policy_tools)
        return sentinel_hook

    captured = {}

    def fake_create_agent(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(builders, "create_agent", fake_create_agent)
    monkeypatch.setattr(builders, "ConsecutiveReadOnlyToolLimitMiddleware", FakeReadOnlyLimit)
    monkeypatch.setattr(builders, "ToolBusMiddleware", FakeToolBus)
    monkeypatch.setattr(
        builders,
        "build_policy_pre_hook",
        fake_build_policy_pre_hook,
        raising=False,
    )
    monkeypatch.setattr(builders.memory_store, "load_from_disk", lambda: None)
    monkeypatch.setattr(builders.memory_store, "format_for_system_prompt", lambda target: "")
    monkeypatch.setattr(builders, "install_process_signal_handlers", lambda: None)
    monkeypatch.setattr(builders, "recover_terminal_processes", lambda: None)
    monkeypatch.setattr(builders, "load_project_instruction_blocks", lambda workdir: [])

    builders.build_agent()

    middleware = captured["middleware"]
    read_only_index = next(
        index for index, item in enumerate(middleware)
        if isinstance(item, FakeReadOnlyLimit)
    )
    toolbus_index = next(
        index for index, item in enumerate(middleware)
        if isinstance(item, FakeToolBus)
    )

    assert read_only_index < toolbus_index
    assert middleware[read_only_index].specs is middleware[toolbus_index].specs
    assert factory_calls == [builders.POLICY_REVIEW_TOOLS]
    assert middleware[toolbus_index].hooks.pre_tool_call == [sentinel_hook]
