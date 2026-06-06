# Cron Concurrency CLI Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add claim-time cron concurrency policies and CLI management commands for runs, logs, deliveries, dry-run planning, and delivery retry.

**Architecture:** Extend the existing SQLite cron state machine so jobs carry `concurrency_key` and `concurrency_policy`, runs persist queue/skip/replace decisions, and scheduler ticks promote queued runs before claiming new due work. CLI commands read and mutate the same SQLite store through focused helper methods, keeping runner execution free of concurrency policy decisions.

**Tech Stack:** Python 3.11+, SQLite via `sqlite3`, existing `cron.state_store.StateStore`, `cron.scheduler`, `cron.jobs`, `agent_cli.main`, `agent_cli.cron_commands`, pytest.

---

## Scope Check

This plan covers one cohesive feature: persistent cron concurrency policy plus the CLI surfaces needed to inspect and operate that state. The scheduler/state-store work and CLI work are separate tasks, but they share the same SQLite model and are testable together.

## File Structure

- Modify `cron/jobs.py`: normalize and validate `concurrency_key` and `concurrency_policy`; pass fields through create/update and legacy job reads.
- Modify `cron/state_store.py`: add SQLite columns, row conversion, claim/promote/dry-run/list/retry methods, and run-state transitions.
- Modify `cron/scheduler.py`: call stale recovery, queued promotion, due claim, and execute only claimed runs.
- Modify `agent_tools/public/cronjob.py`: expose concurrency fields in the public cron job tool schema.
- Modify `agent_cli/main.py`: add new cron subcommands and flags.
- Modify `agent_cli/cron_commands.py`: render runs/logs/deliveries/status, dry-run plans, and retry delivery events.
- Modify tests:
  - `tests/test_cron_jobs.py`
  - `tests/test_cron_state_store.py`
  - `tests/test_cron_scheduler.py`
  - `tests/test_cronjob_tool.py`
  - `tests/test_agent_cli_cron_commands.py`

## Task 1: Job Schema Normalization

**Files:**
- Modify: `cron/jobs.py`
- Modify: `agent_tools/public/cronjob.py`
- Test: `tests/test_cron_jobs.py`
- Test: `tests/test_cronjob_tool.py`

- [ ] **Step 1: Write failing tests for job defaults and validation**

Add tests to `tests/test_cron_jobs.py`:

```python
def test_create_job_defaults_concurrency(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job

    job = create_job(prompt="hello", schedule="every 5m")

    assert job["concurrency_key"] == f"job:{job['id']}"
    assert job["concurrency_policy"] == "queue_one"


def test_create_job_accepts_explicit_concurrency(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job

    job = create_job(
        prompt="hello",
        schedule="every 5m",
        concurrency_key="repo:/work/project",
        concurrency_policy="skip_if_running",
    )

    assert job["concurrency_key"] == "repo:/work/project"
    assert job["concurrency_policy"] == "skip_if_running"


def test_create_job_rejects_unknown_concurrency_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job

    with pytest.raises(ValueError, match="Unsupported concurrency_policy"):
        create_job(
            prompt="hello",
            schedule="every 5m",
            concurrency_policy="serialize",
        )


def test_update_job_normalizes_empty_concurrency_key(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job

    job = create_job(prompt="hello", schedule="every 5m", concurrency_key="repo:a")
    updated = update_job(job["id"], {"concurrency_key": ""})

    assert updated is not None
    assert updated["concurrency_key"] == f"job:{job['id']}"
```

Add tests to `tests/test_cronjob_tool.py`:

```python
def test_cronjob_tool_create_accepts_concurrency_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from agent_tools.public.cronjob import run_cronjob_action

    result = run_cronjob_action(
        "create",
        schedule="every 5m",
        prompt="hello",
        concurrency_key="account:alpha",
        concurrency_policy="queue_all",
    )

    assert result["success"] is True
    assert result["job"]["concurrency_key"] == "account:alpha"
    assert result["job"]["concurrency_policy"] == "queue_all"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_cron_jobs.py::test_create_job_defaults_concurrency tests/test_cron_jobs.py::test_create_job_accepts_explicit_concurrency tests/test_cron_jobs.py::test_create_job_rejects_unknown_concurrency_policy tests/test_cron_jobs.py::test_update_job_normalizes_empty_concurrency_key tests/test_cronjob_tool.py::test_cronjob_tool_create_accepts_concurrency_fields -q
```

Expected: failures because `concurrency_policy` is not implemented and public tool schema does not pass the fields.

- [ ] **Step 3: Implement normalization in `cron/jobs.py`**

Add constants and helpers near the existing normalization helpers:

```python
CONCURRENCY_POLICIES = {
    "queue_one",
    "queue_all",
    "replace_running",
    "skip_if_running",
}
DEFAULT_CONCURRENCY_POLICY = "queue_one"


def normalize_concurrency_policy(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return DEFAULT_CONCURRENCY_POLICY
    normalized = str(value).strip()
    if normalized not in CONCURRENCY_POLICIES:
        raise ValueError(f"Unsupported concurrency_policy: {normalized}")
    return normalized


def normalize_concurrency_key(value: Any, job_id: str) -> str:
    normalized = str(value).strip() if value is not None else ""
    return normalized or f"job:{job_id}"
```

Update `create_job(...)` signature:

```python
def create_job(
    prompt: str,
    schedule: str,
    name: str | None = None,
    repeat: int | None = None,
    deliver: str | None = None,
    origin: dict[str, Any] | None = None,
    skill: str | None = None,
    skills: list[str] | str | None = None,
    model: str | None = None,
    provider: str | None = None,
    base_url: str | None = None,
    script: str | None = None,
    context_from: str | list[str] | None = None,
    enabled_toolsets: list[str] | str | None = None,
    workdir: str | None = None,
    idle_timeout_seconds: int | str | None = None,
    max_runtime_seconds: int | str | None = None,
    concurrency_key: str | None = None,
    concurrency_policy: str | None = None,
) -> dict[str, Any]:
```

Inside `create_job`, assign `job_id` before building the job dict:

```python
job_id = uuid.uuid4().hex[:12]
```

Use `job_id` in the job dict and add fields:

```python
"id": job_id,
"concurrency_key": normalize_concurrency_key(concurrency_key, job_id),
"concurrency_policy": normalize_concurrency_policy(concurrency_policy),
```

Update `_normalize_updates(...)`:

```python
    if "concurrency_policy" in normalized_updates:
        normalized_updates["concurrency_policy"] = normalize_concurrency_policy(
            normalized_updates["concurrency_policy"]
        )
    if "concurrency_key" in normalized_updates:
        job_id = str((existing_job or {}).get("id") or "")
        if not job_id:
            raise ValueError("concurrency_key update requires an existing job id")
        normalized_updates["concurrency_key"] = normalize_concurrency_key(
            normalized_updates["concurrency_key"],
            job_id,
        )
```

Ensure any job row returned from `get_job()` / `list_jobs()` has defaults. If there is already a row-normalization function, add:

```python
    job["concurrency_key"] = normalize_concurrency_key(
        job.get("concurrency_key"),
        str(job["id"]),
    )
    try:
        job["concurrency_policy"] = normalize_concurrency_policy(
            job.get("concurrency_policy")
        )
    except ValueError:
        job["concurrency_policy"] = DEFAULT_CONCURRENCY_POLICY
```

- [ ] **Step 4: Expose fields in `agent_tools/public/cronjob.py`**

Where create/update arguments are read and passed into `cron.jobs.create_job` / `update_job`, include:

```python
"concurrency_key": kwargs.get("concurrency_key"),
"concurrency_policy": kwargs.get("concurrency_policy"),
```

Where the tool description schema lists create/update fields, add descriptions:

```python
"concurrency_key": {
    "type": "string",
    "description": "Optional key used to serialize related cron runs. Defaults to job:<job_id>.",
},
"concurrency_policy": {
    "type": "string",
    "enum": ["queue_one", "queue_all", "replace_running", "skip_if_running"],
    "description": "How to handle a due run when another run with the same concurrency key is active.",
},
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_cron_jobs.py::test_create_job_defaults_concurrency tests/test_cron_jobs.py::test_create_job_accepts_explicit_concurrency tests/test_cron_jobs.py::test_create_job_rejects_unknown_concurrency_policy tests/test_cron_jobs.py::test_update_job_normalizes_empty_concurrency_key tests/test_cronjob_tool.py::test_cronjob_tool_create_accepts_concurrency_fields -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```bash
git add cron/jobs.py agent_tools/public/cronjob.py tests/test_cron_jobs.py tests/test_cronjob_tool.py
git commit -m "feat: add cron concurrency job schema"
```

## Task 2: State Store Schema and Claim Decisions

**Files:**
- Modify: `cron/state_store.py`
- Test: `tests/test_cron_state_store.py`

- [ ] **Step 1: Write failing state-store tests**

Add helper functions near existing cron state store tests:

```python
def _due_job(store, *, job_id, key="key:one", policy="queue_one", next_run_at="2026-05-29T10:00:00+00:00"):
    from cron.jobs import create_job, update_job

    job = create_job(
        prompt=f"job {job_id}",
        schedule="every 5m",
        concurrency_key=key,
        concurrency_policy=policy,
    )
    return update_job(
        job["id"],
        {
            "next_run_at": next_run_at,
            "state": "scheduled",
            "enabled": True,
        },
    )
```

Add tests:

```python
def test_claim_due_jobs_queue_one_creates_single_queued_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    first = _due_job(store, job_id="first", key="repo:a", policy="queue_one")
    second = _due_job(store, job_id="second", key="repo:a", policy="queue_one")

    claimed = store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)
    assert len(claimed) == 1
    assert claimed[0]["run"]["status"] == "claimed"

    claimed_again = store.claim_due_jobs(now_text="2026-05-29T10:05:00+00:00", limit=10)
    assert claimed_again == []

    runs = store.list_runs(limit=20)
    queued = [run for run in runs if run["status"] == "queued"]
    assert len(queued) == 1
    assert queued[0]["concurrency_key"] == "repo:a"
    assert queued[0]["concurrency_policy"] == "queue_one"


def test_claim_due_jobs_queue_all_records_each_due_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    _due_job(store, job_id="first", key="repo:a", policy="queue_all")
    _due_job(store, job_id="second", key="repo:a", policy="queue_all")

    store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)
    store.claim_due_jobs(now_text="2026-05-29T10:05:00+00:00", limit=10)

    queued = [run for run in store.list_runs(limit=20) if run["status"] == "queued"]
    assert len(queued) == 2


def test_claim_due_jobs_skip_if_running_records_skipped_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    _due_job(store, job_id="first", key="repo:a", policy="skip_if_running")
    _due_job(store, job_id="second", key="repo:a", policy="skip_if_running")

    claimed = store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)
    assert len(claimed) == 1

    skipped = [run for run in store.list_runs(limit=20) if run["status"] == "skipped"]
    assert len(skipped) == 1
    assert skipped[0]["exit_reason"] == "concurrency_skip"


def test_claim_due_jobs_replace_running_abandons_active_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    _due_job(store, job_id="first", key="repo:a", policy="replace_running")
    _due_job(store, job_id="second", key="repo:a", policy="replace_running")

    claimed = store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)

    runs = store.list_runs(limit=20)
    abandoned = [run for run in runs if run["status"] == "abandoned"]
    active = [run for run in runs if run["status"] == "claimed"]

    assert len(claimed) == 2
    assert len(abandoned) == 1
    assert abandoned[0]["exit_reason"] == "replaced_by_newer_run"
    assert abandoned[0]["replaced_by_run_id"] == active[-1]["id"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_cron_state_store.py::test_claim_due_jobs_queue_one_creates_single_queued_run tests/test_cron_state_store.py::test_claim_due_jobs_queue_all_records_each_due_run tests/test_cron_state_store.py::test_claim_due_jobs_skip_if_running_records_skipped_run tests/test_cron_state_store.py::test_claim_due_jobs_replace_running_abandons_active_run -q
```

Expected: failures due missing columns/methods/claim behavior.

- [ ] **Step 3: Add columns and row mapping**

In `cron/state_store.py`, update `CREATE TABLE jobs` to include:

```sql
concurrency_policy TEXT,
```

near `concurrency_key TEXT`.

Update `CREATE TABLE runs` to include:

```sql
concurrency_key TEXT,
concurrency_policy TEXT,
replaced_by_run_id TEXT,
```

Add migration columns through the existing `_ensure_columns` call or column migration list:

```python
("jobs", "concurrency_policy", "TEXT"),
("runs", "concurrency_key", "TEXT"),
("runs", "concurrency_policy", "TEXT"),
("runs", "replaced_by_run_id", "TEXT"),
```

In `_row_to_job`, default:

```python
from cron.jobs import DEFAULT_CONCURRENCY_POLICY, normalize_concurrency_key, normalize_concurrency_policy

job["concurrency_key"] = normalize_concurrency_key(
    job.get("concurrency_key"),
    str(job["id"]),
)
try:
    job["concurrency_policy"] = normalize_concurrency_policy(
        job.get("concurrency_policy")
    )
except ValueError:
    job["concurrency_policy"] = DEFAULT_CONCURRENCY_POLICY
```

In `_job_to_row_values`, persist `concurrency_policy`.

- [ ] **Step 4: Add focused store helpers**

Add methods to `StateStore`:

```python
ACTIVE_RUN_STATUSES = {"queued", "claimed", "running"}
OCCUPYING_RUN_STATUSES = {"claimed", "running"}


def list_runs(
    self,
    *,
    job_id: str | None = None,
    run_id: str | None = None,
    statuses: set[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if job_id is not None:
        clauses.append("job_id = ?")
        params.append(job_id)
    if run_id is not None:
        clauses.append("id = ?")
        params.append(run_id)
    if statuses:
        status_marks = ", ".join("?" for _ in statuses)
        clauses.append(f"status IN ({status_marks})")
        params.extend(sorted(statuses))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    with self._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM runs
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [self._row_to_run(row) for row in rows]
```

Add private helpers used by claim logic:

```python
def _active_runs_for_key(
    self,
    conn: sqlite3.Connection,
    concurrency_key: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM runs
        WHERE concurrency_key = ?
          AND status IN ('queued', 'claimed', 'running')
        ORDER BY scheduled_for, created_at, id
        """,
        (concurrency_key,),
    ).fetchall()
    return [dict(row) for row in rows]


def _insert_run(
    self,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    job: dict[str, Any],
    status: str,
    now_text: str,
    lease_expires_at: str | None,
    exit_reason: str | None = None,
    replaced_by_run_id: str | None = None,
) -> dict[str, Any]:
    previous_attempts = conn.execute(
        "SELECT COUNT(*) AS count FROM runs WHERE job_id = ?",
        (job["id"],),
    ).fetchone()["count"]
    conn.execute(
        """
        INSERT INTO runs (
            id, job_id, scheduled_for, claimed_at, lease_expires_at,
            started_at, finished_at, attempt, status, exit_reason,
            output_path, final_response, error, delivery_status,
            heartbeat_at, last_activity_at, last_activity_desc, current_tool,
            concurrency_key, concurrency_policy, replaced_by_run_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, NULL, NULL, NULL,
                  ?, ?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            job["id"],
            job["next_run_at"],
            now_text,
            lease_expires_at,
            now_text if status in {"skipped", "abandoned"} else None,
            int(previous_attempts) + 1,
            status,
            exit_reason,
            now_text,
            now_text,
            status,
            job["concurrency_key"],
            job["concurrency_policy"],
            replaced_by_run_id,
            now_text,
            now_text,
        ),
    )
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return self._row_to_run(row)
```

Use the real runs schema columns already present in this repo. If column order differs, keep the same field names and adapt only the SQL column list.

- [ ] **Step 5: Rewrite `claim_due_jobs` concurrency branch**

Inside `claim_due_jobs`, after `_skip_missed_job_if_needed`, compute:

```python
from cron.jobs import normalize_concurrency_key, normalize_concurrency_policy

job["concurrency_key"] = normalize_concurrency_key(
    job.get("concurrency_key"),
    str(job["id"]),
)
try:
    job["concurrency_policy"] = normalize_concurrency_policy(
        job.get("concurrency_policy")
    )
except ValueError:
    job["concurrency_policy"] = "queue_one"
```

Apply policy in the transaction:

```python
active_runs = self._active_runs_for_key(conn, job["concurrency_key"])
occupying = [run for run in active_runs if run["status"] in {"claimed", "running"}]
queued = [run for run in active_runs if run["status"] == "queued"]
policy = job["concurrency_policy"]
run_id = uuid.uuid4().hex
now_actual = utc_now().isoformat()
lease_expires_at = (now_dt + timedelta(seconds=self.lease_seconds)).isoformat()

if policy == "queue_one" and occupying:
    if not queued:
        self._insert_run(
            conn,
            run_id=run_id,
            job=job,
            status="queued",
            now_text=now_actual,
            lease_expires_at=None,
        )
    self._advance_job_after_claim(conn, job, now_actual)
    continue

if policy == "queue_all" and occupying:
    self._insert_run(
        conn,
        run_id=run_id,
        job=job,
        status="queued",
        now_text=now_actual,
        lease_expires_at=None,
    )
    self._advance_job_after_claim(conn, job, now_actual)
    continue

if policy == "skip_if_running" and active_runs:
    self._insert_run(
        conn,
        run_id=run_id,
        job=job,
        status="skipped",
        now_text=now_actual,
        lease_expires_at=None,
        exit_reason="concurrency_skip",
    )
    self._advance_job_after_claim(conn, job, now_actual)
    continue

if policy == "replace_running" and active_runs:
    for active in active_runs:
        conn.execute(
            """
            UPDATE runs
            SET status = 'abandoned',
                finished_at = ?,
                exit_reason = 'replaced_by_newer_run',
                replaced_by_run_id = ?,
                updated_at = ?
            WHERE id = ? AND status IN ('queued', 'claimed', 'running')
            """,
            (now_actual, run_id, now_actual, active["id"]),
        )
```

Then create the default claimed run:

```python
run = self._insert_run(
    conn,
    run_id=run_id,
    job=job,
    status="claimed",
    now_text=now_actual,
    lease_expires_at=lease_expires_at,
)
conn.execute(
    """
    UPDATE jobs
    SET state = 'running',
        lease_run_id = ?,
        lease_expires_at = ?,
        updated_at = ?
    WHERE id = ? AND state = 'scheduled'
    """,
    (run_id, lease_expires_at, now_actual, job["id"]),
)
self._advance_job_after_claim(conn, job, now_actual)
claimed_job = self._row_to_job(conn.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone())
claimed.append({"job": claimed_job, "run": run})
```

If no `_advance_job_after_claim` exists, extract existing completion/next-run update code into:

```python
def _advance_job_after_claim(
    self,
    conn: sqlite3.Connection,
    job: dict[str, Any],
    now_text: str,
) -> None:
    from cron.jobs import compute_next_run

    repeat = dict(job.get("repeat") or {"times": None, "completed": 0})
    next_run_at = compute_next_run(job["schedule"], job.get("next_run_at"))
    conn.execute(
        """
        UPDATE jobs
        SET next_run_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (next_run_at, now_text, job["id"]),
    )
```

When implementing, preserve current repeat/completed semantics from existing `complete_run()` and `claim_due_jobs()` rather than replacing them blindly.

- [ ] **Step 6: Run state-store tests**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_cron_state_store.py -q
```

Expected: state-store tests pass. If existing tests expected no queued/skipped records, update assertions to the new explicit state-machine behavior.

- [ ] **Step 7: Commit**

```bash
git add cron/state_store.py tests/test_cron_state_store.py
git commit -m "feat: add cron claim concurrency policies"
```

## Task 3: Queued Run Promotion and Scheduler Tick Flow

**Files:**
- Modify: `cron/state_store.py`
- Modify: `cron/scheduler.py`
- Test: `tests/test_cron_state_store.py`
- Test: `tests/test_cron_scheduler.py`

- [ ] **Step 1: Write failing queued promotion tests**

Add to `tests/test_cron_state_store.py`:

```python
def test_promote_queued_runs_claims_one_per_key(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    _due_job(store, job_id="a1", key="repo:a", policy="queue_all")
    _due_job(store, job_id="a2", key="repo:a", policy="queue_all")
    _due_job(store, job_id="b1", key="repo:b", policy="queue_all")

    store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)
    first_claimed = store.list_runs(statuses={"claimed"}, limit=10)[0]
    store.complete_run(
        first_claimed["id"],
        success=True,
        output_path=None,
        final_response="ok",
        error=None,
        next_run_at=None,
        completed=False,
    )

    promoted = store.promote_queued_runs(
        now_text="2026-05-29T10:01:00+00:00",
        limit=10,
    )

    promoted_keys = [item["run"]["concurrency_key"] for item in promoted]
    assert promoted_keys.count("repo:a") == 1
    assert promoted_keys.count("repo:b") == 1


def test_promote_queued_runs_skips_key_with_running_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    _due_job(store, job_id="a1", key="repo:a", policy="queue_all")
    _due_job(store, job_id="a2", key="repo:a", policy="queue_all")

    store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)
    promoted = store.promote_queued_runs(
        now_text="2026-05-29T10:01:00+00:00",
        limit=10,
    )

    assert promoted == []
```

Add to `tests/test_cron_scheduler.py`:

```python
def test_tick_promotes_queued_before_claiming_due_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.scheduler import tick
    from cron.state_store import StateStore
    from cron.contracts import JobRunResult

    first = create_job(
        prompt="first",
        schedule="every 5m",
        concurrency_key="repo:a",
        concurrency_policy="queue_all",
    )
    second = create_job(
        prompt="second",
        schedule="every 5m",
        concurrency_key="repo:a",
        concurrency_policy="queue_all",
    )
    update_job(first["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})
    update_job(second["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})

    results = []

    def runner(job):
        results.append(job["id"])
        return JobRunResult(success=True, output_doc="ok", final_response="ok")

    tick(now_text="2026-05-29T10:00:00+00:00", job_runner=runner)
    tick(now_text="2026-05-29T10:01:00+00:00", job_runner=runner)

    assert len(results) == 2
    runs = StateStore().list_runs(statuses={"succeeded"}, limit=10)
    assert len(runs) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_cron_state_store.py::test_promote_queued_runs_claims_one_per_key tests/test_cron_state_store.py::test_promote_queued_runs_skips_key_with_running_run tests/test_cron_scheduler.py::test_tick_promotes_queued_before_claiming_due_jobs -q
```

Expected: failures because `promote_queued_runs` and scheduler integration do not exist.

- [ ] **Step 3: Implement `StateStore.promote_queued_runs`**

Add:

```python
def promote_queued_runs(
    self,
    *,
    now_text: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    now_dt = _parse_time(now_text)
    if now_dt is None or int(limit) <= 0:
        return []
    promoted: list[dict[str, Any]] = []
    promoted_keys: set[str] = set()
    with self._connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT * FROM runs
            WHERE status = 'queued'
            ORDER BY scheduled_for, created_at, id
            """
        ).fetchall()
        for row in rows:
            if len(promoted) >= int(limit):
                break
            run = dict(row)
            key = str(run.get("concurrency_key") or "")
            if not key or key in promoted_keys:
                continue
            occupying = conn.execute(
                """
                SELECT id FROM runs
                WHERE concurrency_key = ?
                  AND status IN ('claimed', 'running')
                LIMIT 1
                """,
                (key,),
            ).fetchone()
            if occupying is not None:
                continue
            lease_expires_at = (now_dt + timedelta(seconds=self.lease_seconds)).isoformat()
            conn.execute(
                """
                UPDATE runs
                SET status = 'claimed',
                    claimed_at = ?,
                    lease_expires_at = ?,
                    heartbeat_at = ?,
                    last_activity_at = ?,
                    last_activity_desc = 'claimed',
                    updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now_text, lease_expires_at, now_text, now_text, now_text, run["id"]),
            )
            conn.execute(
                """
                UPDATE jobs
                SET state = 'running',
                    lease_run_id = ?,
                    lease_expires_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (run["id"], lease_expires_at, now_text, run["job_id"]),
            )
            updated_run = self._row_to_run(
                conn.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone()
            )
            job = self._row_to_job(
                conn.execute("SELECT * FROM jobs WHERE id = ?", (run["job_id"],)).fetchone()
            )
            promoted.append({"job": job, "run": updated_run})
            promoted_keys.add(key)
    return promoted
```

- [ ] **Step 4: Integrate promotion in `cron/scheduler.py`**

In `tick(...)`, after stale recovery and before `claim_due_jobs`, add:

```python
promoted = store.promote_queued_runs(now_text=now_text, limit=limit)
claimed = promoted + store.claim_due_jobs(now_text=now_text, limit=max(0, limit - len(promoted)))
```

If current `tick` computes `run_at` as a datetime instead of accepting `now_text`, keep its existing public signature and derive:

```python
run_at = now if now is not None else datetime.now(timezone.utc)
now_text = run_at.isoformat()
```

Ensure `_run_parallel_claimed()` receives only `claimed` status runs from `promoted + claim_due_jobs`.

- [ ] **Step 5: Run scheduler tests**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_cron_scheduler.py tests/test_cron_state_store.py -q
```

Expected: all selected suites pass.

- [ ] **Step 6: Commit**

```bash
git add cron/state_store.py cron/scheduler.py tests/test_cron_state_store.py tests/test_cron_scheduler.py
git commit -m "feat: promote queued cron runs"
```

## Task 4: Dry-Run Planning

**Files:**
- Modify: `cron/state_store.py`
- Modify: `agent_cli/cron_commands.py`
- Modify: `agent_cli/main.py`
- Test: `tests/test_cron_state_store.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing dry-run tests**

Add to `tests/test_cron_state_store.py`:

```python
def test_plan_job_claim_reports_queue_decision_without_writing(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.state_store import StateStore

    store = StateStore()
    job = _due_job(store, job_id="first", key="repo:a", policy="queue_one")
    store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)

    plan = store.plan_job_claim(
        str(job["id"]),
        now_text="2026-05-29T10:05:00+00:00",
    )

    assert plan["decision"] == "would_queue"
    assert plan["concurrency_key"] == "repo:a"
    assert len(store.runs_for_job(job["id"])) == 1
```

Add to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_run_dry_run_does_not_create_run(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore
    from agent_cli.cron_commands import run_cron_job

    job = create_job(prompt="hello", schedule="every 5m")
    update_job(job["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})

    result = run_cron_job(job_id=job["id"], dry_run=True, now_text="2026-05-29T10:00:00+00:00")

    assert result.exit_code == 0
    assert "would_claim" in result.text
    assert StateStore().runs_for_job(job["id"]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_cron_state_store.py::test_plan_job_claim_reports_queue_decision_without_writing tests/test_agent_cli_cron_commands.py::test_cron_run_dry_run_does_not_create_run -q
```

Expected: failures because dry-run planning is missing.

- [ ] **Step 3: Implement `StateStore.plan_job_claim`**

Add:

```python
def plan_job_claim(self, job_id: str, *, now_text: str) -> dict[str, Any]:
    now_dt = _parse_time(now_text)
    job = self.get_job(job_id)
    if now_dt is None:
        return {"job_id": job_id, "decision": "invalid_now", "error": f"Invalid now_text: {now_text}"}
    if not job.get("enabled", True):
        return {"job_id": job_id, "decision": "disabled", "job": job}
    due_at = _parse_time(job.get("next_run_at"))
    if due_at is None or due_at > now_dt:
        return {
            "job_id": job_id,
            "job": job,
            "decision": "not_due",
            "next_run_at": job.get("next_run_at"),
            "concurrency_key": job.get("concurrency_key"),
            "concurrency_policy": job.get("concurrency_policy"),
        }
    with self._connect() as conn:
        active = self._active_runs_for_key(conn, str(job["concurrency_key"]))
    policy = str(job["concurrency_policy"])
    occupying = [run for run in active if run["status"] in {"claimed", "running"}]
    queued = [run for run in active if run["status"] == "queued"]
    decision = "would_claim"
    if policy == "queue_one" and occupying:
        decision = "would_skip_duplicate_queue" if queued else "would_queue"
    elif policy == "queue_all" and occupying:
        decision = "would_queue"
    elif policy == "skip_if_running" and active:
        decision = "would_skip"
    elif policy == "replace_running" and active:
        decision = "would_replace"
    return {
        "job_id": job_id,
        "job": job,
        "decision": decision,
        "next_run_at": job.get("next_run_at"),
        "concurrency_key": job.get("concurrency_key"),
        "concurrency_policy": policy,
        "active_run_count": len(active),
        "active_runs": active[:5],
    }
```

- [ ] **Step 4: Add CLI dry-run command function**

In `agent_cli/cron_commands.py`, add:

```python
def run_cron_job(
    *,
    job_id: str,
    dry_run: bool = False,
    now_text: str | None = None,
) -> CronCommandResult:
    if not dry_run:
        return simple_job_action("run", job_id=job_id)
    from cron.state_store import StateStore, utc_now

    store = StateStore()
    try:
        plan = store.plan_job_claim(job_id, now_text=now_text or utc_now().isoformat())
    except KeyError:
        return CronCommandResult(f"Cron job not found: {job_id}.", exit_code=2)

    job = plan.get("job") or {}
    lines = [
        f"Dry run for cron job {job_id}",
        f"Decision: {plan['decision']}",
        f"Enabled: {job.get('enabled', '-')}",
        f"Next run: {plan.get('next_run_at') or '-'}",
        f"Concurrency: {plan.get('concurrency_key') or '-'} ({plan.get('concurrency_policy') or '-'})",
        f"Active same-key runs: {plan.get('active_run_count', 0)}",
    ]
    return CronCommandResult("\n".join(lines))
```

In `agent_cli/main.py`, change run parser creation:

```python
for name in ("pause", "resume"):
    parser_for_action = cron_subparsers.add_parser(name, parents=[public_options])
    parser_for_action.add_argument("job_id")

cron_run = cron_subparsers.add_parser("run", parents=[public_options])
cron_run.add_argument("job_id")
cron_run.add_argument("--dry-run", action="store_true")
```

Change dispatch:

```python
if subcommand == "run":
    return cron_commands.run_cron_job(
        job_id=args.job_id,
        dry_run=bool(getattr(args, "dry_run", False)),
    )
if subcommand in {"pause", "resume"}:
    return cron_commands.simple_job_action(subcommand, job_id=args.job_id)
```

- [ ] **Step 5: Run dry-run tests**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_cron_state_store.py::test_plan_job_claim_reports_queue_decision_without_writing tests/test_agent_cli_cron_commands.py::test_cron_run_dry_run_does_not_create_run -q
```

Expected: tests pass.

- [ ] **Step 6: Commit**

```bash
git add cron/state_store.py agent_cli/cron_commands.py agent_cli/main.py tests/test_cron_state_store.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: add cron dry run planning"
```

## Task 5: Runs, Logs, Deliveries, and Retry CLI

**Files:**
- Modify: `cron/state_store.py`
- Modify: `cron/delivery_store.py`
- Modify: `agent_cli/cron_commands.py`
- Modify: `agent_cli/main.py`
- Test: `tests/test_agent_cli_cron_commands.py`
- Test: `tests/test_cron_state_store.py`

- [ ] **Step 1: Write failing CLI tests**

Add to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_runs_lists_recent_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore
    from agent_cli.cron_commands import list_cron_runs

    job = create_job(prompt="hello", schedule="every 5m")
    update_job(job["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})
    store = StateStore()
    claimed = store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=1)[0]
    store.complete_run(
        claimed["run"]["id"],
        success=True,
        output_path="/tmp/out.md",
        final_response="done",
        error=None,
        next_run_at=None,
        completed=False,
    )

    result = list_cron_runs(job_id=job["id"], limit=10)

    assert result.exit_code == 0
    assert claimed["run"]["id"][:8] in result.text
    assert "succeeded" in result.text


def test_cron_logs_shows_latest_error_and_output(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore
    from agent_cli.cron_commands import cron_logs

    job = create_job(prompt="hello", schedule="every 5m", name="Daily")
    update_job(job["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})
    store = StateStore()
    claimed = store.claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=1)[0]
    store.complete_run(
        claimed["run"]["id"],
        success=False,
        output_path="/tmp/out.md",
        final_response="",
        error="boom",
        next_run_at=None,
        completed=False,
    )

    result = cron_logs(job_id=job["id"], limit=5)

    assert result.exit_code == 0
    assert "Daily" in result.text
    assert "boom" in result.text
    assert "/tmp/out.md" in result.text


def test_retry_delivery_resets_failed_event(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.delivery_store import DeliveryStore
    from agent_cli.cron_commands import retry_delivery

    store = DeliveryStore()
    event = store.enqueue(
        job_id="job-1",
        run_id="run-1",
        job_name="Job",
        run_at="2026-05-29T10:00:00+00:00",
        target="webhook:https://example.invalid",
        target_type="webhook",
        adapter_key="webhook",
        payload={"ok": True},
        status="failed",
    )
    store.update_event(event["id"], status="failed", last_error="HTTP 500")

    result = retry_delivery(event["id"])
    updated = store.get(event["id"])

    assert result.exit_code == 0
    assert updated["status"] == "pending"
    assert updated["last_error"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_agent_cli_cron_commands.py::test_cron_runs_lists_recent_runs tests/test_agent_cli_cron_commands.py::test_cron_logs_shows_latest_error_and_output tests/test_agent_cli_cron_commands.py::test_retry_delivery_resets_failed_event -q
```

Expected: failures because command functions are missing.

- [ ] **Step 3: Add store methods for delivery listing and retry**

In `cron/state_store.py`, add:

```python
def list_delivery_events(
    self,
    *,
    job_id: str | None = None,
    run_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if job_id is not None:
        clauses.append("job_id = ?")
        params.append(job_id)
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, int(limit)))
    with self._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM delivery_events
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def retry_delivery_event(self, event_id: str) -> dict[str, Any]:
    event = self.get_delivery_event(event_id)
    if event["status"] not in {"failed", "dead"}:
        raise ValueError(f"delivery event is not retryable: {event['status']}")
    return self.update_delivery_event(
        event_id,
        status="pending",
        last_error=None,
        next_attempt_at=utc_now().isoformat(),
    )
```

In `cron/delivery_store.py`, add wrappers:

```python
def list_events(
    self,
    *,
    job_id: str | None = None,
    run_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return self._store.list_delivery_events(job_id=job_id, run_id=run_id, limit=limit)


def retry(self, event_id: str) -> dict[str, Any]:
    return self._store.retry_delivery_event(event_id)
```

- [ ] **Step 4: Add CLI renderers**

In `agent_cli/cron_commands.py`, add:

```python
def _short(value: Any, length: int = 8) -> str:
    text = str(value or "-")
    return text[:length] if text != "-" else "-"


def _preview(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text or "-"
    return text[: limit - 3] + "..."


def list_cron_runs(job_id: str | None = None, *, limit: int = 20) -> CronCommandResult:
    from cron.state_store import StateStore

    store = StateStore()
    rows = store.list_runs(job_id=job_id, limit=limit)
    if not rows:
        return CronCommandResult("No cron runs.")
    lines = ["Cron Runs:"]
    for run in rows:
        lines.append(
            "  "
            f"{_short(run['id'])} "
            f"job={_short(run['job_id'])} "
            f"status={run['status']} "
            f"scheduled={run.get('scheduled_for') or '-'} "
            f"exit={run.get('exit_reason') or '-'} "
            f"delivery={run.get('delivery_status') or '-'}"
        )
        activity = run.get("last_activity_desc") or run.get("current_tool")
        if run["status"] in {"queued", "claimed", "running"} and activity:
            lines.append(f"    activity={activity} heartbeat={run.get('heartbeat_at') or '-'}")
    return CronCommandResult("\n".join(lines))


def cron_logs(job_id: str, *, limit: int = 10) -> CronCommandResult:
    from cron.state_store import StateStore

    store = StateStore()
    try:
        job = store.get_job(job_id)
    except KeyError:
        return CronCommandResult(f"Cron job not found: {job_id}.", exit_code=2)
    runs = store.list_runs(job_id=job_id, limit=limit)
    lines = [
        f"Cron Logs: {job.get('name') or job_id}",
        f"Job: {job_id}",
        f"Next run: {job.get('next_run_at') or '-'}",
    ]
    if not runs:
        lines.append("No runs.")
        return CronCommandResult("\n".join(lines))
    lines.append("Recent runs:")
    for run in runs:
        lines.append(
            f"  {_short(run['id'])} status={run['status']} "
            f"scheduled={run.get('scheduled_for') or '-'} "
            f"output={run.get('output_path') or '-'}"
        )
        if run.get("error"):
            lines.append(f"    error={_preview(run['error'])}")
        elif run.get("final_response"):
            lines.append(f"    final={_preview(run['final_response'])}")
    return CronCommandResult("\n".join(lines))


def list_deliveries(selector: str | None = None, *, limit: int = 20) -> CronCommandResult:
    from cron.state_store import StateStore

    store = StateStore()
    job_id = None
    run_id = None
    filter_label = "all"
    if selector:
        try:
            store.get_job(selector)
            job_id = selector
            filter_label = f"job={selector}"
        except KeyError:
            try:
                store.get_run(selector)
                run_id = selector
                filter_label = f"run={selector}"
            except KeyError:
                return CronCommandResult(f"No cron job or run found for: {selector}.", exit_code=2)
    events = store.list_delivery_events(job_id=job_id, run_id=run_id, limit=limit)
    if not events:
        return CronCommandResult(f"No delivery events ({filter_label}).")
    lines = [f"Delivery Events ({filter_label}):"]
    for event in events:
        lines.append(
            "  "
            f"{_short(event['id'])} "
            f"job={_short(event.get('job_id'))} "
            f"run={_short(event.get('run_id'))} "
            f"target={event.get('target') or '-'} "
            f"adapter={event.get('adapter_key') or '-'} "
            f"status={event.get('status') or '-'} "
            f"attempts={event.get('attempt_count') or 0} "
            f"next={event.get('next_attempt_at') or '-'}"
        )
        if event.get("last_error"):
            lines.append(f"    error={_preview(event['last_error'])}")
    return CronCommandResult("\n".join(lines))


def retry_delivery(event_id: str) -> CronCommandResult:
    from cron.delivery_store import DeliveryStore

    store = DeliveryStore()
    try:
        before = store.get(event_id)
    except KeyError:
        return CronCommandResult(f"Delivery event not found: {event_id}.", exit_code=2)
    try:
        after = store.retry(event_id)
    except ValueError as exc:
        return CronCommandResult(str(exc), exit_code=2)
    except Exception as exc:
        return CronCommandResult(f"Failed to retry delivery event: {exc}", exit_code=1)
    return CronCommandResult(
        f"Delivery event {event_id} reset: {before['status']} -> {after['status']}."
    )
```

- [ ] **Step 5: Wire CLI parsers and dispatch**

In `agent_cli/main.py`, add parsers:

```python
cron_runs = cron_subparsers.add_parser("runs", parents=[public_options])
cron_runs.add_argument("job_id", nargs="?")
cron_runs.add_argument("--limit", type=int, default=20)

cron_logs = cron_subparsers.add_parser("logs", parents=[public_options])
cron_logs.add_argument("job_id")
cron_logs.add_argument("--limit", type=int, default=10)

cron_deliveries = cron_subparsers.add_parser("deliveries", parents=[public_options])
cron_deliveries.add_argument("selector", nargs="?")
cron_deliveries.add_argument("--limit", type=int, default=20)

cron_retry_delivery = cron_subparsers.add_parser("retry-delivery", parents=[public_options])
cron_retry_delivery.add_argument("event_id")
```

Add dispatch:

```python
if subcommand == "runs":
    return cron_commands.list_cron_runs(
        job_id=getattr(args, "job_id", None),
        limit=getattr(args, "limit", 20),
    )
if subcommand == "logs":
    return cron_commands.cron_logs(
        job_id=args.job_id,
        limit=getattr(args, "limit", 10),
    )
if subcommand == "deliveries":
    return cron_commands.list_deliveries(
        selector=getattr(args, "selector", None),
        limit=getattr(args, "limit", 20),
    )
if subcommand == "retry-delivery":
    return cron_commands.retry_delivery(args.event_id)
```

- [ ] **Step 6: Run CLI tests**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_agent_cli_cron_commands.py tests/test_cron_state_store.py -q
```

Expected: tests pass.

- [ ] **Step 7: Commit**

```bash
git add cron/state_store.py cron/delivery_store.py agent_cli/cron_commands.py agent_cli/main.py tests/test_agent_cli_cron_commands.py tests/test_cron_state_store.py
git commit -m "feat: add cron run and delivery management commands"
```

## Task 6: Status Enhancements

**Files:**
- Modify: `cron/state_store.py`
- Modify: `agent_cli/cron_commands.py`
- Test: `tests/test_agent_cli_cron_commands.py`

- [ ] **Step 1: Write failing status test**

Add to `tests/test_agent_cli_cron_commands.py`:

```python
def test_cron_status_shows_running_queued_and_next_due(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_HOME", str(tmp_path))

    from cron.jobs import create_job, update_job
    from cron.state_store import StateStore
    from agent_cli.cron_commands import cron_status

    first = create_job(
        prompt="first",
        schedule="every 5m",
        concurrency_key="repo:a",
        concurrency_policy="queue_one",
    )
    second = create_job(
        prompt="second",
        schedule="every 5m",
        concurrency_key="repo:a",
        concurrency_policy="queue_one",
    )
    update_job(first["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})
    update_job(second["id"], {"next_run_at": "2026-05-29T10:00:00+00:00"})
    StateStore().claim_due_jobs(now_text="2026-05-29T10:00:00+00:00", limit=10)

    result = cron_status()

    assert result.exit_code == 0
    assert "Running:" in result.text
    assert "Queued:" in result.text
    assert "Next due:" in result.text
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_agent_cli_cron_commands.py::test_cron_status_shows_running_queued_and_next_due -q
```

Expected: failure because status output lacks the new sections.

- [ ] **Step 3: Add store status summary helper**

In `cron/state_store.py`, add:

```python
def cron_status_summary(self, *, top_n: int = 5) -> dict[str, Any]:
    running = self.list_runs(statuses={"claimed", "running"}, limit=top_n)
    queued = self.list_runs(statuses={"queued"}, limit=top_n)
    stale = self.list_stale_running_runs(limit=top_n)
    failed = self.latest_failed_run()
    with self._connect() as conn:
        next_due = conn.execute(
            """
            SELECT id, name, next_run_at FROM jobs
            WHERE enabled = 1
              AND state = 'scheduled'
              AND next_run_at IS NOT NULL
            ORDER BY next_run_at ASC
            LIMIT 1
            """
        ).fetchone()
    return {
        "running": running,
        "queued": queued,
        "stale": stale,
        "latest_failed_run": failed,
        "next_due": dict(next_due) if next_due else None,
    }
```

- [ ] **Step 4: Enhance `cron_status` renderer**

In `agent_cli/cron_commands.py`, inside `cron_status()`, after existing service status lines, append:

```python
from cron.state_store import StateStore

summary = StateStore().cron_status_summary(top_n=5)
next_due = summary.get("next_due")
lines.append(f"Next due: {next_due['id']} at {next_due['next_run_at']}" if next_due else "Next due: -")
lines.append(f"Running: {len(summary['running'])}")
for run in summary["running"]:
    lines.append(
        f"  {_short(run['id'])} job={_short(run['job_id'])} "
        f"activity={run.get('last_activity_desc') or '-'}"
    )
lines.append(f"Queued: {len(summary['queued'])}")
for run in summary["queued"]:
    lines.append(f"  {_short(run['id'])} job={_short(run['job_id'])} scheduled={run.get('scheduled_for') or '-'}")
lines.append(f"Stale: {len(summary['stale'])}")
if summary.get("latest_failed_run"):
    failed = summary["latest_failed_run"]
    lines.append(f"Latest failed run: {_short(failed['id'])} error={_preview(failed.get('error'))}")
```

Keep existing leader/service status output intact.

- [ ] **Step 5: Run status tests**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_agent_cli_cron_commands.py::test_cron_status_shows_running_queued_and_next_due -q
```

Expected: test passes.

- [ ] **Step 6: Commit**

```bash
git add cron/state_store.py agent_cli/cron_commands.py tests/test_agent_cli_cron_commands.py
git commit -m "feat: enrich cron status diagnostics"
```

## Task 7: Full Regression and Review Prep

**Files:**
- Review: all files changed in Tasks 1-6

- [ ] **Step 1: Run formatting and whitespace checks**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 2: Run focused cron suite**

Run:

```bash
PYTHONPATH=$PWD pytest tests/test_cron_state_store.py tests/test_cron_jobs.py tests/test_cronjob_tool.py tests/test_cron_scheduler.py tests/test_cron_runner.py tests/test_cron_runner_subprocess.py tests/test_agent_cli_cron_commands.py tests/test_cron_import_health.py tests/test_cron_service.py tests/test_cron_service_state.py tests/test_cron_leader.py -q
```

Expected: all selected cron tests pass.

- [ ] **Step 3: Inspect public CLI parsing**

Run:

```bash
PYTHONPATH=$PWD python -m agent_cli.main cron --help
```

Expected: help includes `runs`, `logs`, `deliveries`, `retry-delivery`, `run`.

Run:

```bash
PYTHONPATH=$PWD python -m agent_cli.main cron run example --dry-run
```

Expected: command exits with code 2 and prints `Cron job not found: example.` or equivalent not-found text without traceback.

- [ ] **Step 4: Final commit if any verification fixes were needed**

If Step 1-3 required edits:

```bash
git add cron agent_cli agent_tools tests
git commit -m "fix: stabilize cron concurrency management"
```

If no edits were needed, do not create an empty commit.

- [ ] **Step 5: Request code review**

Use `superpowers:requesting-code-review` after verification passes. Ask reviewers to focus on:

- SQLite transaction safety in `claim_due_jobs` and `promote_queued_runs`
- Schedule advancement semantics for `queue_one` duplicate queued cases
- Late completion behavior after `replace_running`
- CLI commands creating no side effects in dry-run/list/logs paths
- Delivery retry preserving attempt history

## Self-Review

Spec coverage:

- Concurrency schema: Task 1.
- Runs schema and claim-time policy decisions: Task 2.
- Queued promotion and scheduler ordering: Task 3.
- Dry-run CLI: Task 4.
- Runs/logs/deliveries/retry CLI: Task 5.
- Status enrichment: Task 6.
- Regression and review: Task 7.

Placeholder scan:

- No unresolved marker text or unspecified "add tests" steps remain.
- Each code-changing step names exact files and includes code or concrete command examples.

Type consistency:

- Plan consistently uses `concurrency_key`, `concurrency_policy`, `replaced_by_run_id`, `queued`, `claimed`, `running`, `skipped`, and `abandoned`.
- CLI function names are introduced before parser dispatch uses them.
- Store methods are introduced before command renderers call them.
