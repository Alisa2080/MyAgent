import json

import pytest


def test_policy_decision_data_is_copied_and_immutable():
    from agent_core.permissions.models import PolicyDecision

    source = {"path": "/tmp/report.txt", "tags": ["outside"]}
    decision = PolicyDecision.review(
        "writes_outside_workspace",
        risk_tags=("writes_outside_workspace",),
        data=source,
    )
    source["path"] = "/tmp/changed.txt"
    source["tags"].append("changed")

    assert decision.data["path"] == "/tmp/report.txt"
    assert decision.data["tags"] == ("outside",)
    with pytest.raises(TypeError, match="immutable"):
        decision.data["path"] = "/tmp/mutated.txt"


def test_policy_decision_data_blocks_in_place_merge():
    from agent_core.permissions.models import PolicyDecision

    decision = PolicyDecision.allow("allowed", data={"path": "/tmp/report.txt"})

    with pytest.raises(TypeError, match="immutable"):
        decision.data |= {"extra": True}
    assert "extra" not in decision.data


def test_policy_decision_risk_tags_are_normalized_to_tuple():
    from agent_core.permissions.models import PolicyDecision

    risk_tags = ["network_access"]
    decision = PolicyDecision.allow("allowed", risk_tags=risk_tags)
    risk_tags.append("mutated")

    assert decision.risk_tags == ("network_access",)


def test_policy_decision_data_remains_json_serializable():
    from agent_core.permissions.models import PolicyDecision

    decision = PolicyDecision.deny(
        "sensitive_path",
        risk_tags=("sensitive_path",),
        data={"path": "/root/.ssh/id_rsa"},
    )

    assert json.loads(json.dumps(decision.data)) == {"path": "/root/.ssh/id_rsa"}
