from __future__ import annotations


def test_parse_multi_target_preserves_webhook_url_and_dedupes():
    from cron.delivery_targets import DeliveryIdentity, parse_delivery_targets

    origin = DeliveryIdentity(source_type="cli", session_id="session-1", thread_id="thread-1")
    targets = parse_delivery_targets(
        "origin,webhook:https://example.invalid/hook,local,origin",
        origin=origin,
    )

    assert [(target.target_type, target.adapter_key, target.address) for target in targets] == [
        ("origin", "origin", "session-1"),
        ("webhook", "webhook", "https://example.invalid/hook"),
        ("local", "local", None),
    ]


def test_origin_without_identity_fails_closed():
    from cron.delivery_targets import DeliveryTargetError, parse_delivery_targets

    try:
        parse_delivery_targets("origin", origin=None)
    except DeliveryTargetError as exc:
        assert "origin delivery requires origin identity" in str(exc)
    else:
        raise AssertionError("expected origin without identity to fail")


def test_bare_webhook_uses_env_url(monkeypatch):
    monkeypatch.setenv("AGENT_CRON_WEBHOOK_URL", "https://example.invalid/hook")

    from cron.delivery_targets import parse_delivery_targets

    targets = parse_delivery_targets("webhook", origin=None)

    assert targets[0].address == "https://example.invalid/hook"


def test_bare_webhook_without_env_fails_validation(monkeypatch):
    monkeypatch.delenv("AGENT_CRON_WEBHOOK_URL", raising=False)

    from cron.delivery_registry import default_delivery_registry

    result = default_delivery_registry().validate_targets("webhook", origin=None, job={})

    assert result.ok is False
    assert "webhook delivery requires a URL" in result.error


def test_empty_delivery_target_fails():
    from cron.delivery_targets import DeliveryTargetError, parse_delivery_targets

    try:
        parse_delivery_targets(",", origin=None)
    except DeliveryTargetError as exc:
        assert "delivery target is required" in str(exc)
    else:
        raise AssertionError("expected empty target list to fail")


def test_reserved_platform_target_rejected_without_adapter():
    from cron.delivery_registry import default_delivery_registry

    registry = default_delivery_registry()
    result = registry.validate_targets("slack:C123", origin=None, job={})

    assert result.ok is False
    assert "unsupported delivery target" in result.error


def test_build_delivery_registry_includes_registered_adapter_factory(monkeypatch):
    from cron.delivery_adapters import AdapterValidation, DeliveryResult
    import cron.delivery_registry as delivery_registry

    class FakeSlackAdapter:
        key = "slack"
        active_dispatch = True

        def validate(self, target, job):
            return AdapterValidation(bool(target.address), None if target.address else "slack channel required")

        def deliver(self, event, job, run):
            return DeliveryResult(True)

    delivery_registry.clear_delivery_adapter_factories()
    delivery_registry.register_delivery_adapter_factory(lambda **kwargs: FakeSlackAdapter())
    try:
        registry = delivery_registry.build_delivery_registry()
        result = registry.validate_targets("slack:C123", origin=None, job={})
    finally:
        delivery_registry.clear_delivery_adapter_factories()

    assert "slack" in registry.adapter_keys()
    assert "slack" in registry.active_adapter_keys()
    assert result.ok is True
    assert result.targets[0].adapter_key == "slack"
