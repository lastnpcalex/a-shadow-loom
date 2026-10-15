"""Durable Loom-managed child-agent supervisor.

The supervisor owns child runs independently of the parent model turn.  A
provider adapter is injected by the server, so Codex/Gemini/Goose can share
the same lifecycle and event persistence even when their native protocols
differ.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import os
from pathlib import Path, PurePath
import time
import uuid

import database as db
import agent_access
import psutil
from agent_events import ChildEventWriter

log = logging.getLogger(__name__)

Launcher = Callable[[dict, Callable[[str, dict], Awaitable[dict]]], Awaitable[dict | None]]
EventSink = Callable[[dict, dict], Awaitable[None]]


class AgentTeardownError(RuntimeError):
    """Provider exit is unconfirmed; its workspace scope must remain occupied."""


def _normalized_run_scope(run: dict) -> tuple[str, tuple[str, ...]]:
    workspace = os.path.normcase(os.path.abspath(str(run.get("project_dir") or ".")))
    raw_scope = run.get("path_scope") or []
    normalized = tuple(
        os.path.normcase(os.path.normpath(str(item).strip().replace("/", os.sep)))
        for item in raw_scope
        if str(item).strip()
    )
    return workspace, normalized


def _scopes_overlap(left: dict, right: dict) -> bool:
    """Treat an empty/dot scope as the whole checkout; serialize overlaps."""
    if left.get("access") is not None or right.get("access") is not None:
        return agent_access.conflicts(left, right)
    left_workspace, left_scope = _normalized_run_scope(left)
    right_workspace, right_scope = _normalized_run_scope(right)
    if left_workspace != right_workspace:
        return False
    if not left_scope or not right_scope or "." in left_scope or "." in right_scope:
        return True
    for left_path in left_scope:
        left_parts = PurePath(left_path).parts
        for right_path in right_scope:
            right_parts = PurePath(right_path).parts
            common = min(len(left_parts), len(right_parts))
            if left_parts[:common] == right_parts[:common]:
                return True
    return False


def _scope_contains(parent: dict, child: dict) -> bool:
    """A descendant may narrow a reservation, but cannot broaden it."""
    parent_workspace, parent_paths = _normalized_run_scope(parent)
    child_workspace, child_paths = _normalized_run_scope(child)
    if parent_workspace != child_workspace:
        return False
    if parent.get("access") is not None or child.get("access") is not None:
        return agent_access.narrows(parent, child)
    if not parent_paths or "." in parent_paths:
        return True
    if not child_paths or "." in child_paths:
        return False
    return all(any(
        PurePath(child_path).parts[:len(PurePath(parent_path).parts)] == PurePath(parent_path).parts
        for parent_path in parent_paths
    ) for child_path in child_paths)


class AgentSupervisor:
    """Queue and run child agents without tying them to a parent turn."""

    def __init__(self, launcher: Launcher | None = None, event_sink: EventSink | None = None, batch_sink=None, target_resolver=None):
        self.launcher = launcher
        self.event_sink = event_sink
        self.batch_sink = batch_sink
        self.target_resolver = target_resolver
        self.max_concurrent = max(1, int(os.getenv("LOOM_AGENT_MAX_CONCURRENT", "4")))
        self.max_local_concurrent = max(1, int(os.getenv("LOOM_AGENT_MAX_LOCAL_CONCURRENT", "1")))
        # Coordinating ancestors remain alive while children execute. Bound
        # nesting as well as independent leaves to bound total process count.
        self.max_depth = max(0, int(os.getenv("LOOM_AGENT_MAX_DEPTH", "4")))
        self._loop_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._runs: dict[str, asyncio.Task] = {}
        self._stopping = False

    async def _ancestors(self, run: dict, rows: dict | None = None) -> list[dict]:
        rows = rows if rows is not None else {}
        ancestors = []
        seen = {str(run["run_id"])}
        child = run
        parent_id = str(run.get("parent_run_id") or "").removeprefix("agent:")
        while parent_id and not parent_id.startswith("gen:"):
            if parent_id in seen:
                raise ValueError("Child delegation contains an ancestry cycle")
            seen.add(parent_id)
            parent = rows.get(parent_id) or await db.get_agent_run(parent_id)
            if not parent or int(parent["conversation_id"]) != int(run["conversation_id"]):
                raise ValueError("Child parent does not belong to this conversation")
            if "project_dir" in child and not _scope_contains(parent, child):
                raise ValueError("Nested child scope must stay within its parent's workspace and paths")
            rows[parent_id] = parent
            ancestors.append(parent)
            if len(ancestors) > self.max_depth:
                raise ValueError(f"Child delegation exceeds nesting limit ({self.max_depth})")
            parent_id = str(parent.get("parent_run_id") or "").removeprefix("agent:")
            child = parent
        return ancestors

    async def start(self) -> None:
        if self._loop_task and not self._loop_task.done():
            return
        self._stopping = False
        # A prior process may have died mid-tool. Replaying such a task can
        # duplicate filesystem or external side effects, so require an
        # explicit resume instead of silently launching it again.
        for run in await db.list_agent_runs(
            statuses=["launching", "running", "cancel_requested", "needs_relaunch"]
        ):
            if run.get("pid") and psutil.pid_exists(int(run["pid"])):
                # A restart is not evidence that the provider died. Keep this
                # scope reserved until its process can be inspected/cleaned up.
                await db.update_agent_run(
                    run["run_id"], status="cancel_requested",
                    error="Provider exit requires verification after Loom restart; scope retained.",
                )
                continue
            if run.get("pid"):
                await db.update_agent_run(run["run_id"], pid=None)
            if run.get("cancel_requested") or run.get("status") == "cancel_requested":
                await db.update_agent_run(
                    run["run_id"], status="cancelled", completed_at=time.time()
                )
            else:
                await db.update_agent_run(
                    run["run_id"],
                    status="paused",
                    error=(
                        "Loom restarted before this child completed. Resume "
                        "recovers its own recorded evidence; uncertain tool outcomes must be reconciled."
                    ),
                )
        self._loop_task = asyncio.create_task(self._dispatch_loop(), name="loom-agent-supervisor")
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        tasks = list(self._runs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()

    async def spawn(
        self,
        conversation_id: int,
        task: str,
        *,
        parent_run_id: str | None = None,
        parent_message_id: int | None = None,
        harness: str | None = None,
        model: str | None = None,
        profile: str | None = None,
        project_dir: str | None = None,
        path_scope: list[str] | None = None,
        client_instance_id: str | None = None,
        defer_start: bool = False,
        image_request: dict | None = None,
        access: dict | None = None,
    ) -> dict:
        if not str(task or "").strip():
            raise ValueError("task is required")
        run_id = str(uuid.uuid4())
        parent_run_id = str(parent_run_id or "").removeprefix("agent:") or None
        ancestors = await self._ancestors({
            "run_id": run_id, "conversation_id": conversation_id, "parent_run_id": parent_run_id,
        })
        if ancestors:
            parent = ancestors[0]
            if parent["status"] not in {"launching", "running"}:
                raise ValueError("Only an active child can delegate new nested work")
            project_dir = project_dir or parent.get("project_dir")
            path_scope = path_scope or parent.get("path_scope") or []
            parent_message_id = parent_message_id or parent.get("parent_message_id")
            client_instance_id = parent.get("client_instance_id") or client_instance_id
            if access is None and parent.get("access") is not None:
                access = parent["access"]
            if not harness and not profile:
                harness = parent.get("effective_harness") or parent.get("requested_harness")
                model = model or parent.get("effective_model") or parent.get("requested_model")
        workspace = Path(project_dir or ".").resolve()
        access = agent_access.validate(workspace, access)
        if access and access.get("sensitive"):
            candidate = {"requested_harness": harness, "requested_model": model, "model_profile": profile}
            conversation = await db.get_conversation(conversation_id)
            effective = self.target_resolver(candidate, conversation)[0] if self.target_resolver else (
                "local" if profile == "local" else harness or (conversation or {}).get("mode"))
            if effective != "local":
                raise ValueError("Sensitive children require local inference")
        normalized_scope = []
        if not isinstance(path_scope or [], list):
            raise ValueError("path_scope must be a list of paths")
        for item in path_scope or []:
            if not isinstance(item, str):
                raise ValueError("path_scope must contain only paths")
            resolved = (workspace / item).resolve()
            if resolved != workspace and workspace not in resolved.parents:
                raise ValueError("path_scope escapes project_dir")
            normalized_scope.append(str(resolved.relative_to(workspace)))
        if ancestors and not _scope_contains(ancestors[0], {
            "project_dir": str(workspace), "path_scope": normalized_scope, "access": access,
        }):
            raise ValueError("Nested child scope must stay within its parent's workspace and paths")
        run = await db.create_agent_run(
            run_id,
            conversation_id,
            task.strip(),
            parent_run_id=parent_run_id,
            parent_message_id=parent_message_id,
            requested_harness=harness,
            requested_model=model,
            model_profile=profile,
            project_dir=str(workspace),
            path_scope=normalized_scope,
            image_request=image_request,
            access=access,
            client_instance_id=client_instance_id,
        )
        if not defer_start:
            self._wake.set()
        return run

    def wake(self) -> None:
        self._wake.set()

    async def cancel(self, run_id: str) -> dict | None:
        run = await db.request_agent_cancel(run_id)
        active = self._runs.get(str(run_id))
        if active:
            active.cancel()
            # Do not return the intermediate cancel_requested row after the
            # terminal WebSocket event has already been emitted.  Waiting for
            # the run wrapper also guarantees provider teardown finishes
            # before callers see cancellation as complete.
            await asyncio.gather(active, return_exceptions=True)
            run = await db.get_agent_run(run_id)
        elif run and not run.get("pid"):
            run = await db.update_agent_run(
                run_id,
                status="cancelled",
                completed_at=time.time(),
                cancel_requested=1,
            )
        self._wake.set()
        return run

    async def resume(self, run_id: str) -> dict | None:
        run = await db.request_agent_resume(run_id)
        if run and run.get("status") == "queued":
            self._wake.set()
        return run

    async def _queue_wait(self, run, reason):
        if run.get("queue_reason") == reason:
            return
        event = await db.append_agent_event(run["run_id"], "agent_waiting", {"queue_reason": reason},
                                            run_state={"queue_reason": reason})
        run["queue_reason"] = reason
        if self.event_sink:
            try:
                await self.event_sink(run, event)
            except Exception:
                log.exception("Child queue reason delivery failed")

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                queued = await db.list_agent_runs(statuses=["queued"])
                active_rows = await db.list_agent_runs(
                    statuses=["launching", "running", "cancel_requested"]
                )
                rows = {r["run_id"]: r for r in [*queued, *active_rows]}
                ancestry = {}
                for run in [*active_rows, *queued]:
                    try:
                        ancestry[run["run_id"]] = {
                            parent["run_id"] for parent in await self._ancestors(run, rows)
                        }
                    except ValueError as exc:
                        ancestry[run["run_id"]] = set()
                        if run["status"] == "queued":
                            event = await db.append_agent_event(
                                run["run_id"], "agent_failed", {"status": "failed", "error": str(exc)},
                                run_state={"status": "failed", "error": str(exc), "completed_at": time.time()},
                            )
                            run["status"] = "failed"
                            if self.event_sink:
                                try:
                                    await self.event_sink(run, event)
                                except Exception:
                                    log.exception("Invalid child %s event delivery failed", run["run_id"])
                local_runs = set()
                for row in [*active_rows, *queued]:
                    conv = await db.get_conversation(row['conversation_id'])
                    if self.target_resolver:
                        try:
                            harness, model = self.target_resolver(row, conv)
                        except ValueError:
                            continue  # Launch validation reports invalid selectors.
                    else:
                        harness = row.get('effective_harness') or row.get('requested_harness') or (conv or {}).get('mode')
                        model = row.get('effective_model') or row.get('requested_model') or (conv or {}).get('cc_model') or ''
                    local = row.get('model_profile') == 'local' or harness in {'local', 'dream-claude', 'dream_claude'}
                    if harness == 'goose':
                        from goose_client import split_goose_model
                        local = local or split_goose_model(model)[0] in {'llama', 'dream'}
                    if local:
                        local_runs.add(row['run_id'])
                selected: list[dict] = []
                for run in sorted(queued, key=lambda row: row["created_at"]):
                    if run["run_id"] in self._runs or run["status"] != "queued":
                        continue
                    occupied = [*active_rows, *selected]
                    def conflicts(other):
                        if not _scopes_overlap(run, other):
                            return False
                        # Only a real ancestor/descendant can share a family
                        # reservation. Siblings and failed-stop owners cannot.
                        related = (other["run_id"] in ancestry[run["run_id"]]
                                   or run["run_id"] in ancestry[other["run_id"]])
                        return not related or other["status"] == "cancel_requested"
                    if any(conflicts(other) for other in occupied):
                        await self._queue_wait(run, "workspace_conflict")
                        continue
                    # Local adapters share machine inference resources even
                    # across disjoint folders. Cloud scopes retain parallelism.
                    if run['run_id'] in local_runs and sum(
                        other['run_id'] in local_runs
                        and (other['run_id'] not in ancestry[run['run_id']]
                             or other['status'] == 'cancel_requested')
                        for other in occupied
                    ) >= self.max_local_concurrent:
                        await self._queue_wait(run, "inference_capacity")
                        continue
                    trial = [*occupied, run]
                    coordinating = set().union(*(ancestry[r["run_id"]] for r in trial))
                    leaves = sum(
                        r["run_id"] not in coordinating or r["status"] == "cancel_requested"
                        for r in trial
                    )
                    if not agent_access.admitted(False, leaves <= self.max_concurrent, True):
                        await self._queue_wait(run, "worker_capacity")
                        # A later descendant may fit even when an independent
                        # job cannot; do not break at a full leaf budget.
                        continue
                    selected.append(run)
                for run in selected:
                    await db.update_agent_run(run["run_id"], status="launching", queue_reason=None)
                    task = asyncio.create_task(self._run_one(run), name=f"loom-agent-{run['run_id'][:8]}")
                    self._runs[run["run_id"]] = task
                    task.add_done_callback(lambda _task, rid=run["run_id"]: self._runs.pop(rid, None))
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Agent supervisor dispatch loop failed")
                await asyncio.sleep(1)

    async def _run_one(self, run: dict) -> None:
        run_id = run["run_id"]
        started = time.time()
        await db.update_agent_run(run_id, status="running", started_at=started, heartbeat_at=started)

        writer = ChildEventWriter(run, self.event_sink, self.batch_sink)
        emit = writer.emit

        try:
            latest = await db.get_agent_run(run_id)
            if latest and latest.get("cancel_requested"):
                await emit(
                    "agent_cancelled", {"status": "cancelled"},
                    run_state={"status": "cancelled", "completed_at": time.time()},
                )
                return
            await emit("agent_started", {"task": run["task"], "started_at": started})
            if not self.launcher:
                raise RuntimeError("No agent launcher is configured")
            if run.get("image_request") is not None:
                from image_generation import IMAGE_RUN_TIMEOUT_SEC
                try:
                    result = await asyncio.wait_for(
                        self.launcher(run, emit), timeout=IMAGE_RUN_TIMEOUT_SEC,
                    )
                except TimeoutError as exc:
                    raise RuntimeError("Image generation exceeded its 10-minute run limit") from exc
            else:
                result = await self.launcher(run, emit)
            latest = await db.get_agent_run(run_id)
            if latest and latest.get("cancel_requested"):
                status = "cancelled"
            elif (result or {}).get("error"):
                status = "failed"
            else:
                status = "completed"
            await emit(
                f"agent_{status}",
                {"status": status, "result": result or {}, "error": (result or {}).get("error"),
                 "pid": (latest or {}).get("pid")},
                run_state={"status": status, "result": result or {},
                           "error": (result or {}).get("error"), "completed_at": time.time()},
            )
        except AgentTeardownError as exc:
            # Do not emit a terminal event or unblock overlapping children.
            await emit(
                "agent_stop_failed", {"status": "cancel_requested", "error": str(exc)},
                run_state={"status": "cancel_requested", "error": str(exc), "cancel_requested": 1},
            )
        except asyncio.CancelledError:
            status = "paused" if self._stopping else "cancelled"
            latest = await db.get_agent_run(run_id)
            await emit(
                f"agent_{status}", {"status": status, "pid": (latest or {}).get("pid"),
                                   "cancel_requested": 0 if self._stopping else 1},
                run_state={
                    "status": status, "cancel_requested": 0 if self._stopping else 1,
                    "completed_at": None if self._stopping else time.time(),
                    "error": ("Loom stopped before this child completed. Resume recovers its recorded "
                              "evidence and requires reconciliation of uncertain tool outcomes.") if self._stopping else None,
                },
            )
        except Exception as exc:
            log.exception("Agent run %s failed", run_id)
            latest = await db.get_agent_run(run_id)
            await emit(
                "agent_failed", {"status": "failed", "error": str(exc), "pid": (latest or {}).get("pid")},
                run_state={"status": "failed", "error": str(exc), "completed_at": time.time()},
            )
        finally:
            await writer.close()
