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


def test_reserved_platform_target_rejected_without_adapter():
    from cron.delivery_registry import default_delivery_registry

    registry = default_delivery_registry()
    result = registry.validate_targets("slack:C123", origin=None, job={})

    assert result.ok is False
    assert "unsupported delivery target" in result.error
