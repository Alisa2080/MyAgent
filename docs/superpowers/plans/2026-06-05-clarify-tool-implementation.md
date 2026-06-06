# Clarify Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a LangGraph-native `clarify` tool that lets the parent agent ask interactive clarification questions in CLI and gateway/IM sessions without blocking cron jobs.

**Architecture:** Implement `clarify` as a normal LangChain public tool and route its interaction through the existing LangGraph interrupt/resume path. CLI gets a clarify-specific input collector; gateway stores one pending clarify per session and treats the next inbound message as the answer. Cron never receives the tool.

**Tech Stack:** Python, LangChain `@tool`, LangGraph `Command(resume=...)`, project `ToolMessage` helpers, SQLite-backed gateway session store, pytest.

---

## File Structure

- Create `agent_tools/public/clarify.py`: public LangChain tool facade, input schema, validation, and structured result helpers.
- Modify `agent_tools/public/__init__.py`: export `clarify` without changing lazy cron import behavior.
- Replace `agent_tools/clarify_tool.py`: compatibility shim that re-exports the public tool instead of using Herme `tools.registry`.
- Modify `agent_core/delegation.py`: add `clarify` to parent `BASE_TOOLS`, keep it out of `READ_ONLY_TOOLS`.
- Modify `agent_core/builders.py`: add `clarify` to `HUMAN_INTERRUPT_ON`.
- Modify `agent_cli/approval.py`: add clarify rendering and answer collection while preserving existing approval behavior.
- Modify `gateway/session_store.py`: persist one pending clarify interrupt per gateway session.
- Modify `gateway/dispatch.py`: detect clarify interrupts, send questions, store pending state, resume from next inbound message.
- Modify `cron/runner.py`: no production change expected unless tests expose accidental inclusion; `build_cron_tools` should remain without `clarify`.
- Add or modify tests in `tests/test_public_toolmessage_results.py`, `tests/test_agent_tools_public_imports.py`, `tests/test_cron_runner.py`, `tests/test_permissions_human_loop.py`, `tests/test_agent_cli_interrupts.py`, `tests/test_gateway_session_store.py`, and `tests/test_gateway_dispatch.py`.

---

### Task 1: Public Clarify Tool

**Files:**
- Create: `agent_tools/public/clarify.py`
- Modify: `agent_tools/public/__init__.py`
- Modify: `agent_tools/clarify_tool.py`
- Test: `tests/test_public_toolmessage_results.py`
- Test: `tests/test_agent_tools_public_imports.py`

- [ ] **Step 1: Write failing tests for public tool behavior**

Add these tests to `tests/test_public_toolmessage_results.py`:

```python
def test_clarify_tool_returns_structured_payload():
    from types import SimpleNamespace

    from agent_tools.public.clarify import clarify

    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="clarify-thread"),
        tool_call_id="call-clarify",
    )

    result = clarify.func(
        question="Which implementation path should I take?",
        choices=["Small", "Complete"],
        runtime=runtime,
    )

    _assert_tool_result(result, "clarify", True)
    artifact = result.artifact
    assert artifact["data"]["question"] == "Which implementation path should I take?"
    assert artifact["data"]["choices"] == ["Small", "Complete"]
    assert result.content == "Clarification requested."


def test_clarify_tool_rejects_empty_question():
    from types import SimpleNamespace

    from agent_tools.public.clarify import clarify

    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="clarify-thread"),
        tool_call_id="call-clarify",
    )

    result = clarify.func(question="   ", choices=None, runtime=runtime)

    _assert_tool_result(result, "clarify", False)
    assert result.artifact["code"] == "invalid_input"
    assert "question is required" in result.artifact["message"].lower()


def test_clarify_tool_rejects_too_many_choices():
    from types import SimpleNamespace

    from agent_tools.public.clarify import clarify

    runtime = SimpleNamespace(
        execution_info=SimpleNamespace(thread_id="clarify-thread"),
        tool_call_id="call-clarify",
    )

    result = clarify.func(
        question="Pick one",
        choices=["a", "b", "c", "d", "e"],
        runtime=runtime,
    )

    _assert_tool_result(result, "clarify", False)
    assert result.artifact["code"] == "invalid_input"
    assert "at most 4" in result.artifact["message"]
```

Add this test to `tests/test_agent_tools_public_imports.py`:

```python
def test_public_package_exports_clarify():
    from agent_tools.public import __all__, clarify

    assert "clarify" in __all__
    assert getattr(clarify, "name", None) == "clarify"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_public_toolmessage_results.py::test_clarify_tool_returns_structured_payload tests/test_public_toolmessage_results.py::test_clarify_tool_rejects_empty_question tests/test_public_toolmessage_results.py::test_clarify_tool_rejects_too_many_choices tests/test_agent_tools_public_imports.py::test_public_package_exports_clarify -q
```

Expected: FAIL because `agent_tools.public.clarify` does not exist or `clarify` is not exported.

- [ ] **Step 3: Implement the public tool**

Create `agent_tools/public/clarify.py`:

```python
from __future__ import annotations

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from pydantic import BaseModel, Field

from agent_core.session_context import origin_identity_from_runtime
from agent_tools.shared.tool_result import tool_failure, tool_success


MAX_CHOICES = 4


class ClarifyInput(BaseModel):
    question: str = Field(description="Question to ask the user for clarification.")
    choices: list[str] | None = Field(
        default=None,
        max_length=MAX_CHOICES,
        description=(
            "Optional list of 1 to 4 suggested answers. The UI may add an "
            "Other option that lets the user type a custom answer."
        ),
    )


def _clean_question(question: str) -> str:
    return str(question or "").strip()


def _clean_choices(choices: list[str] | None) -> list[str] | None:
    if choices is None:
        return None
    cleaned = [str(choice).strip() for choice in choices if str(choice).strip()]
    return cleaned or None


def _interactive_source(runtime: ToolRuntime | None) -> str | None:
    identity = origin_identity_from_runtime(runtime)
    if identity is None:
        return "cli"
    source_type = identity.get("source_type")
    if source_type in {"cli", "gateway", "web"}:
        return source_type
    return None


@tool("clarify", args_schema=ClarifyInput)
def clarify(
    question: str,
    runtime: ToolRuntime,
    choices: list[str] | None = None,
) -> ToolMessage:
    """Ask the user a clarification question before proceeding.

    Use this when the task is ambiguous, when the agent needs the user's
    preference, or when there are meaningful technical trade-offs. Do not use
    this for dangerous terminal command yes/no approval; terminal policy tools
    handle command approval separately.
    """
    cleaned_question = _clean_question(question)
    if not cleaned_question:
        return tool_failure(
            "clarify",
            "question is required.",
            code="invalid_input",
            runtime=runtime,
        )

    cleaned_choices = _clean_choices(choices)
    if cleaned_choices is not None and len(cleaned_choices) > MAX_CHOICES:
        return tool_failure(
            "clarify",
            f"choices supports at most {MAX_CHOICES} non-empty items.",
            code="invalid_input",
            data={"max_choices": MAX_CHOICES},
            runtime=runtime,
        )

    source_type = _interactive_source(runtime)
    if source_type is None:
        return tool_failure(
            "clarify",
            "clarify is unavailable in this non-interactive execution context.",
            code="interactive_unavailable",
            runtime=runtime,
        )

    return tool_success(
        "clarify",
        message="Clarification requested.",
        data={
            "question": cleaned_question,
            "choices": cleaned_choices,
            "source_type": source_type,
        },
        runtime=runtime,
        content="Clarification requested.",
    )
```

Modify `agent_tools/public/__init__.py`:

```python
from agent_tools.public.clarify import clarify
from agent_tools.public.files import file_info, list_directory, patch, read_file, search_files, write_file
from agent_tools.public.memory import memory_manage
from agent_tools.public.skills import skill_manage, skill_view, skills_list
from agent_tools.public.terminal import process, terminal
from agent_tools.public.web import web_fetch, web_search
```

Add `"clarify"` to `__all__`.

Replace `agent_tools/clarify_tool.py` with:

```python
"""Compatibility shim. New code should import from agent_tools.public.clarify."""

import sys
from importlib import import_module

_impl = import_module("agent_tools.public.clarify")

sys.modules[__name__] = _impl
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_public_toolmessage_results.py::test_clarify_tool_returns_structured_payload tests/test_public_toolmessage_results.py::test_clarify_tool_rejects_empty_question tests/test_public_toolmessage_results.py::test_clarify_tool_rejects_too_many_choices tests/test_agent_tools_public_imports.py::test_public_package_exports_clarify -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_tools/public/clarify.py agent_tools/public/__init__.py agent_tools/clarify_tool.py tests/test_public_toolmessage_results.py tests/test_agent_tools_public_imports.py
git commit -m "feat: add public clarify tool"
```

---

### Task 2: Register Clarify With Parent Agent And HITL

**Files:**
- Modify: `agent_core/delegation.py`
- Modify: `agent_core/builders.py`
- Test: `tests/test_cronjob_tool.py`
- Test: `tests/test_permissions_human_loop.py`

- [ ] **Step 1: Write failing tests for registration**

Add this test near existing build-agent tests in `tests/test_cronjob_tool.py`:

```python
def test_build_agent_includes_clarify_by_default(monkeypatch):
    import agent_core.builders as builders

    _patch_build_agent_side_effects(monkeypatch, builders)

    result = builders.build_agent()

    names = [getattr(tool, "name", "") for tool in result["tools"]]
    assert "clarify" in names
```

Add this test near read-only/delegation assertions in `tests/test_cron_runner.py`:

```python
def test_build_cron_tools_excludes_clarify():
    import cron.runner as runner

    names = [getattr(tool, "name", "") for tool in runner.build_cron_tools(["terminal", "file_write", "delegation"])]

    assert "clarify" not in names
```

Add this test to `tests/test_permissions_human_loop.py`:

```python
def test_clarify_uses_human_interrupt(monkeypatch):
    from langchain_core.messages import AIMessage

    import agent_core.human_loop as human_loop

    seen_payloads = []

    def fake_interrupt(payload):
        seen_payloads.append(payload)
        return {"type": "respond", "message": "Use the complete option."}

    monkeypatch.setattr(human_loop, "interrupt", fake_interrupt)

    middleware = human_loop.FlexibleHumanInTheLoopMiddleware(
        interrupt_on={
            "clarify": {
                "allowed_decisions": ["respond"],
                "description": "Answer this clarification question.",
                "kind": "clarify",
            }
        },
        policy_tools=set(),
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "clarify",
                        "args": {
                            "question": "Which path?",
                            "choices": ["Small", "Complete"],
                        },
                        "id": "call-clarify",
                    }
                ],
            )
        ]
    }

    result = middleware.after_model(state, runtime=_runtime("thread-clarify"))

    assert seen_payloads
    action_requests = seen_payloads[0]["action_requests"]
    review_configs = seen_payloads[0]["review_configs"]
    assert action_requests[0]["name"] == "clarify"
    assert action_requests[0]["args"]["question"] == "Which path?"
    assert review_configs[0]["allowed_decisions"] == ["respond"]
    assert result is not None
```

If `tests/test_permissions_human_loop.py` does not already expose `_runtime`, use its existing local runtime helper pattern rather than creating a second incompatible helper.

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_cronjob_tool.py::test_build_agent_includes_clarify_by_default tests/test_cron_runner.py::test_build_cron_tools_excludes_clarify tests/test_permissions_human_loop.py::test_clarify_uses_human_interrupt -q
```

Expected: first and HITL tests fail because `clarify` is not registered yet. Cron exclusion may already pass.

- [ ] **Step 3: Register clarify**

Modify `agent_core/delegation.py`:

```python
from agent_tools.public.clarify import clarify
from agent_tools.public.files import file_info, list_directory, patch, read_file, search_files, write_file
from agent_tools.public.skills import skill_manage, skill_view, skills_list
from agent_tools.public.terminal import process, terminal
from agent_tools.public.web import web_fetch, web_search
```

Update `BASE_TOOLS`:

```python
BASE_TOOLS = [
    *READ_ONLY_TOOLS,
    write_file,
    patch,
    terminal,
    process,
    skill_manage,
    clarify,
]
```

Modify `agent_core/builders.py`:

```python
HUMAN_INTERRUPT_ON = {
    "memory_manage": {
        "allowed_decisions": ["approve", "edit", "reject", "respond"],
        "description": "Review this durable memory change before it is written to disk.",
    },
    "skill_manage": {
        "allowed_decisions": ["approve", "edit", "reject", "respond"],
        "description": "Review this procedural skill change before it is written to disk.",
    },
    "clarify": {
        "allowed_decisions": ["respond"],
        "description": "Answer this clarification question.",
        "kind": "clarify",
    },
}
```

No change should be made to `cron/runner.py` unless `clarify` is accidentally imported into `READ_ONLY_TOOLS`.

- [ ] **Step 4: Run tests to verify they pass**

Run:

```bash
pytest tests/test_cronjob_tool.py::test_build_agent_includes_clarify_by_default tests/test_cron_runner.py::test_build_cron_tools_excludes_clarify tests/test_permissions_human_loop.py::test_clarify_uses_human_interrupt -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_core/delegation.py agent_core/builders.py tests/test_cronjob_tool.py tests/test_cron_runner.py tests/test_permissions_human_loop.py
git commit -m "feat: route clarify through human interrupt"
```

---

### Task 3: CLI Clarify Input Collection

**Files:**
- Modify: `agent_cli/approval.py`
- Test: `tests/test_agent_cli_interrupts.py`

- [ ] **Step 1: Write failing CLI tests**

Add these tests to `tests/test_agent_cli_interrupts.py`:

```python
def test_collect_approval_decisions_maps_clarify_choice_number():
    from agent_cli.approval import ApprovalRequest, collect_approval_decisions

    request = ApprovalRequest(
        {
            "name": "clarify",
            "args": {
                "question": "Which path?",
                "choices": ["Small", "Complete"],
            },
        },
        {"kind": "clarify", "allowed_decisions": ["respond"]},
    )

    resume = collect_approval_decisions(
        [request],
        input_func=lambda prompt: "2",
        print_func=lambda text="": None,
    )

    assert resume == {"decisions": [{"type": "respond", "message": "Complete"}]}


def test_collect_approval_decisions_accepts_clarify_other_text():
    from agent_cli.approval import ApprovalRequest, collect_approval_decisions

    request = ApprovalRequest(
        {
            "name": "clarify",
            "args": {
                "question": "Which path?",
                "choices": ["Small", "Complete"],
            },
        },
        {"kind": "clarify", "allowed_decisions": ["respond"]},
    )

    resume = collect_approval_decisions(
        [request],
        input_func=lambda prompt: "Use a staged rollout",
        print_func=lambda text="": None,
    )

    assert resume == {"decisions": [{"type": "respond", "message": "Use a staged rollout"}]}


def test_collect_approval_decisions_accepts_open_ended_clarify_answer():
    from agent_cli.approval import ApprovalRequest, collect_approval_decisions

    request = ApprovalRequest(
        {
            "name": "clarify",
            "args": {
                "question": "What should the title be?",
            },
        },
        {"kind": "clarify", "allowed_decisions": ["respond"]},
    )

    resume = collect_approval_decisions(
        [request],
        input_func=lambda prompt: "Clarify Tool",
        print_func=lambda text="": None,
    )

    assert resume == {"decisions": [{"type": "respond", "message": "Clarify Tool"}]}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_agent_cli_interrupts.py::test_collect_approval_decisions_maps_clarify_choice_number tests/test_agent_cli_interrupts.py::test_collect_approval_decisions_accepts_clarify_other_text tests/test_agent_cli_interrupts.py::test_collect_approval_decisions_accepts_open_ended_clarify_answer -q
```

Expected: FAIL because current collection treats clarify as approval.

- [ ] **Step 3: Implement clarify collection helpers**

Modify `agent_cli/approval.py` by adding helpers above `collect_approval_decisions`:

```python
def _is_clarify_request(request: ApprovalRequest) -> bool:
    return request.tool_name == "clarify" or request.review_config.get("kind") == "clarify"


def _clarify_choices(request: ApprovalRequest) -> list[str]:
    args = request.args if isinstance(request.args, dict) else {}
    raw_choices = args.get("choices")
    if not isinstance(raw_choices, list):
        return []
    return [str(choice).strip() for choice in raw_choices if str(choice).strip()]


def _clarify_question(request: ApprovalRequest) -> str:
    args = request.args if isinstance(request.args, dict) else {}
    question = str(args.get("question") or "").strip()
    return question or "Clarification needed."


def _render_clarify_request(index: int, total: int, request: ApprovalRequest) -> str:
    lines = [f"[{index}/{total}] clarify", _clarify_question(request), ""]
    choices = _clarify_choices(request)
    for choice_index, choice in enumerate(choices, start=1):
        lines.append(f"{choice_index}. {choice}")
    if choices:
        lines.append(f"{len(choices) + 1}. Other (type your answer)")
        lines.append("")
    return "\n".join(lines)


def _clarify_decision_from_answer(request: ApprovalRequest, answer: str) -> dict[str, str]:
    text = str(answer or "").strip()
    choices = _clarify_choices(request)
    if choices and text.isdigit():
        selected = int(text)
        if 1 <= selected <= len(choices):
            text = choices[selected - 1]
        elif selected == len(choices) + 1:
            text = ""
    if not text:
        text = "No clarification answer provided."
    return {"type": "respond", "message": text}
```

At the start of the `while index < total:` loop in `collect_approval_decisions`, before `_render_request`, add:

```python
        if _is_clarify_request(request):
            print_func(_render_clarify_request(index + 1, total, request))
            try:
                answer = input_func("Answer: ")
            except EOFError:
                decisions.extend(
                    {"type": "reject", "message": eof_message}
                    for _ in range(total - index)
                )
                break
            decisions.append(_clarify_decision_from_answer(request, answer))
            index += 1
            continue
```

Keep the existing approval branch unchanged after this clarify branch.

- [ ] **Step 4: Run CLI tests**

Run:

```bash
pytest tests/test_agent_cli_interrupts.py::test_collect_approval_decisions_maps_clarify_choice_number tests/test_agent_cli_interrupts.py::test_collect_approval_decisions_accepts_clarify_other_text tests/test_agent_cli_interrupts.py::test_collect_approval_decisions_accepts_open_ended_clarify_answer tests/test_agent_cli_interrupts.py::test_collect_approval_decisions_supports_approve_reject_respond_edit -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent_cli/approval.py tests/test_agent_cli_interrupts.py
git commit -m "feat: collect clarify answers in cli"
```

---

### Task 4: Gateway Pending Clarify State

**Files:**
- Modify: `gateway/session_store.py`
- Test: `tests/test_gateway_session_store.py`

- [ ] **Step 1: Write failing state-store tests**

Add this test to `tests/test_gateway_session_store.py`:

```python
def test_gateway_session_store_pending_clarify_lifecycle(tmp_path):
    from gateway.session_store import GatewaySessionStore

    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    session = store.get_or_create_session(
        platform="feishu",
        chat_id="oc_123",
        thread_id=None,
        sender_id="ou_1",
        sender_name="Miku",
    )
    payload = {
        "action_request": {
            "name": "clarify",
            "args": {
                "question": "Which path?",
                "choices": ["Small", "Complete"],
            },
        },
        "review_config": {"kind": "clarify"},
    }

    store.set_pending_interrupt(session.session_id, kind="clarify", payload=payload)
    pending = store.get_pending_interrupt(session.session_id)

    assert pending is not None
    assert pending.kind == "clarify"
    assert pending.payload["action_request"]["args"]["question"] == "Which path?"

    store.clear_pending_interrupt(session.session_id)
    assert store.get_pending_interrupt(session.session_id) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
pytest tests/test_gateway_session_store.py::test_gateway_session_store_pending_clarify_lifecycle -q
```

Expected: FAIL because pending interrupt methods and dataclass do not exist.

- [ ] **Step 3: Implement pending interrupt persistence**

Modify `gateway/session_store.py`.

Add this table to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS gateway_pending_interrupts (
  session_id TEXT PRIMARY KEY REFERENCES gateway_sessions(session_id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,
  payload    TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

Add dataclass after `GatewayMessage`:

```python
@dataclass(frozen=True)
class GatewayPendingInterrupt:
    session_id: str
    kind: str
    payload: dict[str, Any]
    created_at: str
    updated_at: str
```

Add methods to `GatewaySessionStore`:

```python
    @staticmethod
    def _pending_interrupt_from_row(row: sqlite3.Row) -> GatewayPendingInterrupt:
        return GatewayPendingInterrupt(
            session_id=row["session_id"],
            kind=row["kind"],
            payload=json.loads(row["payload"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def set_pending_interrupt(self, session_id: str, *, kind: str, payload: dict[str, Any]) -> None:
        now = self.now()
        payload_json = json.dumps(payload, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_pending_interrupts
                    (session_id, kind, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    kind = excluded.kind,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (session_id, kind, payload_json, now, now),
            )

    def get_pending_interrupt(self, session_id: str) -> GatewayPendingInterrupt | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT session_id, kind, payload, created_at, updated_at
                FROM gateway_pending_interrupts
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._pending_interrupt_from_row(row) if row is not None else None

    def clear_pending_interrupt(self, session_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM gateway_pending_interrupts WHERE session_id = ?",
                (session_id,),
            )
```

- [ ] **Step 4: Run state-store tests**

Run:

```bash
pytest tests/test_gateway_session_store.py::test_gateway_session_store_pending_clarify_lifecycle -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gateway/session_store.py tests/test_gateway_session_store.py
git commit -m "feat: persist gateway pending interrupts"
```

---

### Task 5: Gateway Clarify Dispatch And Resume

**Files:**
- Modify: `gateway/dispatch.py`
- Test: `tests/test_gateway_dispatch.py`

- [ ] **Step 1: Write failing gateway tests**

Add these helpers and tests to `tests/test_gateway_dispatch.py`:

```python
def _clarify_interrupt_result():
    return {
        "__interrupt__": [
            {
                "value": {
                    "action_requests": [
                        {
                            "name": "clarify",
                            "args": {
                                "question": "Which path?",
                                "choices": ["Small", "Complete"],
                            },
                        }
                    ],
                    "review_configs": [
                        {
                            "kind": "clarify",
                            "allowed_decisions": ["respond"],
                        }
                    ],
                }
            }
        ]
    }


def test_gateway_dispatch_sends_clarify_and_records_pending(tmp_path):
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    adapter = FakeAdapter()
    registry = GatewayRegistry()
    registry.register(adapter)
    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    dispatcher = GatewayDispatcher(
        store=store,
        registry=registry,
        runner=lambda event, session, origin: _clarify_interrupt_result(),
    )

    result = dispatcher.dispatch(
        InboundEvent(
            platform="feishu",
            event_id="evt-clarify",
            event_type="message",
            chat_id="oc_123",
            text="build it",
            timestamp="2026-06-05T00:00:00+00:00",
            raw={},
        )
    )

    assert result.ok is True
    assert adapter.sent
    assert "Which path?" in adapter.sent[0][1].text
    assert "1. Small" in adapter.sent[0][1].text
    assert "3. Other" in adapter.sent[0][1].text
    pending = store.get_pending_interrupt(result.session_id)
    assert pending is not None
    assert pending.kind == "clarify"
    messages = store.list_messages(result.session_id)
    assert [message.direction for message in messages] == ["inbound", "outbound"]


def test_gateway_dispatch_next_message_answers_pending_clarify(tmp_path):
    from gateway.dispatch import GatewayDispatcher
    from gateway.registry import GatewayRegistry
    from gateway.session_store import GatewaySessionStore

    adapter = FakeAdapter()
    registry = GatewayRegistry()
    registry.register(adapter)
    store = GatewaySessionStore(tmp_path / "gateway.sqlite")
    calls = []

    def runner(event, session, origin):
        calls.append(event.text)
        if len(calls) == 1:
            return _clarify_interrupt_result()
        return "resumed with answer"

    dispatcher = GatewayDispatcher(store=store, registry=registry, runner=runner)
    first = dispatcher.dispatch(
        InboundEvent(
            platform="feishu",
            event_id="evt-1",
            event_type="message",
            chat_id="oc_123",
            text="build it",
            timestamp="2026-06-05T00:00:00+00:00",
            raw={},
        )
    )
    second = dispatcher.dispatch(
        InboundEvent(
            platform="feishu",
            event_id="evt-2",
            event_type="message",
            chat_id="oc_123",
            text="2",
            timestamp="2026-06-05T00:01:00+00:00",
            raw={},
        )
    )

    assert first.ok is True
    assert second.ok is True
    assert adapter.sent[-1][1].text == "resumed with answer"
    assert store.get_pending_interrupt(first.session_id) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
pytest tests/test_gateway_dispatch.py::test_gateway_dispatch_sends_clarify_and_records_pending tests/test_gateway_dispatch.py::test_gateway_dispatch_next_message_answers_pending_clarify -q
```

Expected: FAIL because dispatch does not understand interrupt results or pending state.

- [ ] **Step 3: Implement clarify detection and formatting**

Modify `gateway/dispatch.py`.

Add imports:

```python
from langgraph.types import Command

from agent_cli.interrupts import extract_interrupt_review_requests, has_interrupt
```

Change `GatewayRunner` return type:

```python
GatewayRunner = Callable[[InboundEvent, GatewaySession, dict[str, Any]], Any]
```

Add helper functions after `_assistant_text_from_result`:

```python
def _clarify_request_from_result(result: Any):
    if not has_interrupt(result):
        return None
    for request in extract_interrupt_review_requests(result):
        if request.tool_name == "clarify" or request.review_config.get("kind") == "clarify":
            return request
    return None


def _clarify_choices_from_payload(payload: dict[str, Any]) -> list[str]:
    action = payload.get("action_request") or {}
    args = action.get("args") if isinstance(action, dict) else {}
    choices = args.get("choices") if isinstance(args, dict) else None
    if not isinstance(choices, list):
        return []
    return [str(choice).strip() for choice in choices if str(choice).strip()]


def _clarify_answer_from_text(payload: dict[str, Any], text: str) -> str:
    answer = str(text or "").strip()
    choices = _clarify_choices_from_payload(payload)
    if choices and answer.isdigit():
        selected = int(answer)
        if 1 <= selected <= len(choices):
            return choices[selected - 1]
        if selected == len(choices) + 1:
            return ""
    return answer


def _clarify_payload(request: Any) -> dict[str, Any]:
    return {
        "action_request": request.action_request,
        "review_config": request.review_config,
    }


def _format_clarify_message(payload: dict[str, Any]) -> str:
    action = payload.get("action_request") or {}
    args = action.get("args") if isinstance(action, dict) else {}
    question = str(args.get("question") or "Clarification needed.").strip()
    choices = _clarify_choices_from_payload(payload)
    lines = [question]
    if choices:
        lines.append("")
        for index, choice in enumerate(choices, start=1):
            lines.append(f"{index}. {choice}")
        lines.append(f"{len(choices) + 1}. Other (type your answer)")
    return "\n".join(lines)
```

- [ ] **Step 4: Preserve raw runner results**

Modify `run_gateway_agent_turn` to return the raw result, not just assistant text:

```python
    result = invoke_agent_with_terminal_notifications(
        agent,
        {"messages": [{"role": "user", "content": event.text}]},
        gateway_agent_config(origin),
    )
    return result
```

The dispatcher will call `_assistant_text_from_result` when it needs text.

- [ ] **Step 5: Add resume path to dispatcher**

At the start of `GatewayDispatcher.dispatch`, after `session = self.store.get_or_create_session(...)` and duplicate begin succeeds, record inbound as today, then branch:

```python
            pending = self.store.get_pending_interrupt(session.session_id)
            if pending is not None and pending.kind == "clarify":
                answer = _clarify_answer_from_text(pending.payload, event.text)
                response_text = self.runner(
                    event,
                    session,
                    {
                        "source_type": "gateway",
                        "platform": event.platform,
                        "chat_id": event.chat_id,
                        "thread_id": event.thread_id,
                        "sender_id": event.sender_id,
                        "display_name": event.sender_name,
                        "session_id": session.session_id,
                        "resume": {
                            "decisions": [
                                {
                                    "type": "respond",
                                    "message": answer,
                                }
                            ]
                        },
                    },
                )
                response_text = _assistant_text_from_result(response_text)
                if not response_text:
                    response_text = _EMPTY_AGENT_RESPONSE_TEXT
                self._send_and_record_response(event, session, response_text)
                self.store.clear_pending_interrupt(session.session_id)
                self.store.complete_event(event.platform, event.event_id)
                return DispatchResult(True, session.session_id)
```

To keep dispatch readable, extract the existing send/record response block into a method:

```python
    def _send_and_record_response(self, event: InboundEvent, session: GatewaySession, response_text: str) -> DispatchResult | None:
        adapter = self.registry.get(event.platform)
        if adapter is None:
            return DispatchResult(False, session.session_id, error=f"unsupported gateway platform: {event.platform}")

        target = PlatformMessageTarget(
            platform=event.platform,
            target_type="chat_id",
            target_id=event.chat_id,
            thread_id=event.thread_id,
        )
        send_result = adapter.send_text(
            target,
            OutboundMessage(text=response_text, metadata={"session_id": session.session_id}),
        )
        if not send_result.ok:
            return DispatchResult(False, session.session_id, error=send_result.error)

        self.store.record_message(
            session.session_id,
            direction="outbound",
            platform=event.platform,
            event_id=None,
            text=response_text,
            raw={"send_result": "ok"},
        )
        return None
```

In the normal path, after `runner_result = self.runner(event, session, origin)`, add:

```python
            clarify_request = _clarify_request_from_result(runner_result)
            if clarify_request is not None:
                payload = _clarify_payload(clarify_request)
                response_text = _format_clarify_message(payload)
                send_error = self._send_and_record_response(event, session, response_text)
                if send_error is not None:
                    self.store.release_event(event.platform, event.event_id)
                    return send_error
                self.store.set_pending_interrupt(session.session_id, kind="clarify", payload=payload)
                self.store.complete_event(event.platform, event.event_id)
                return DispatchResult(True, session.session_id)

            response_text = _assistant_text_from_result(runner_result)
```

When send fails in pending-answer path, do not clear pending state.

- [ ] **Step 6: Teach default gateway runner to resume**

Modify `run_gateway_agent_turn`:

```python
    input_data: Any
    if "resume" in origin:
        input_data = Command(resume=origin["resume"])
    else:
        input_data = {"messages": [{"role": "user", "content": event.text}]}

    result = invoke_agent_with_terminal_notifications(
        agent,
        input_data,
        gateway_agent_config(origin),
    )
    return result
```

- [ ] **Step 7: Run gateway tests**

Run:

```bash
pytest tests/test_gateway_dispatch.py::test_gateway_dispatch_sends_clarify_and_records_pending tests/test_gateway_dispatch.py::test_gateway_dispatch_next_message_answers_pending_clarify tests/test_gateway_dispatch.py::test_gateway_dispatch_records_messages_and_sends_response tests/test_gateway_dispatch.py::test_default_gateway_runner_passes_origin_config_to_agent -q
```

Expected: PASS. If `test_default_gateway_runner_passes_origin_config_to_agent` expected `"ok"` from `run_gateway_agent_turn`, update it to call `_assistant_text_from_result(result)` or assert the raw result contains the assistant message.

- [ ] **Step 8: Commit**

```bash
git add gateway/dispatch.py tests/test_gateway_dispatch.py
git commit -m "feat: resume gateway clarify answers"
```

---

### Task 6: End-To-End Regression Sweep

**Files:**
- Verify only unless failures require scoped fixes.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
pytest tests/test_public_toolmessage_results.py tests/test_agent_tools_public_imports.py tests/test_permissions_human_loop.py tests/test_agent_cli_interrupts.py tests/test_gateway_session_store.py tests/test_gateway_dispatch.py tests/test_cron_runner.py tests/test_cronjob_tool.py -q
```

Expected: PASS.

- [ ] **Step 2: Run import and structure tests**

Run:

```bash
pytest tests/test_agent_tools_structure.py tests/test_agent_cli_builders.py tests/test_policy_tool_middleware.py -q
```

Expected: PASS.

- [ ] **Step 3: Inspect git status**

Run:

```bash
git status --short
```

Expected: only files intentionally changed by this plan are modified or staged. Existing unrelated dirty files from before this work may still appear; do not revert them.

- [ ] **Step 4: Commit final fixes if needed**

If Step 1 or Step 2 required small fixes, commit them:

```bash
git add <changed-files>
git commit -m "test: cover clarify integration"
```

If no fixes were needed, skip this commit.

---

## Self-Review

- Spec coverage: public tool, parent registration, CLI, gateway/IM, pending IM answer behavior, cron exclusion, error handling, and tests are all mapped to tasks.
- Red-flag scan: no deferred-content steps; each code-changing step names files and includes concrete snippets.
- Type consistency: gateway pending state uses `GatewayPendingInterrupt`, `kind`, and `payload` consistently; CLI and gateway both resume with `{"decisions": [{"type": "respond", "message": answer}]}`; public tool uses `choices` consistently.
