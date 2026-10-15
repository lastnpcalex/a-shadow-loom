"""Destination-owned preparation transport and incremental coordinator.

Tool-free by construction:
- Each provider client is invoked with preparation=True, which forces a
  tool-free transport (empty MCP config, no shell/file/web tools, no
  permission prompts, approval_policy never, read-only sandbox, GOOSE_MODE
  chat with mcpServers: []). The coordinator never calls an ordinary agent
  launcher.
- Isolated cwd under exo_state, never inside a project checkout or the Loom
  repository.
- permission_mode is NEVER 'plan'.
- Real bounds: the runtime-measured context capacity limits each chunk
  request (instructions + rolling prior state + evidence fragments +
  output reserve). There is no universal token cap; the caller's output
  cap is honored and enforced.
- One handoff-wide budget: a single PreparationBudget (calls + elapsed
  time) governs every cut, retry, crash resume and provider call. Call
  claims are atomically incremented in SQLite and NEVER reset on resume.
- Real attestation: an accepted call requires the provider's structured
  model_attestation to say verified with an exact normalized effective
  model. A launch argument is never treated as effective-model evidence.
- Structured state: every accepted chunk is a JSON object matching
  STATE_SCHEMA; free-text rolling state is forbidden.
- Durable and resumable: operations are recorded in preparation_operations
  bound to (conversation, branch revision, destination, protocol version)
  with an exact accepted-boundary cursor; a crash resumes from the next
  fragment and failed calls remain counted.
- Narrow failures: only documented recoverable preparation errors fall
  back to retrieval; cleanup, teardown and configuration failures surface.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import time
import uuid

import database as db
import exo_state
from context_chunking import (
    STATE_SCHEMA,
    EvidenceFragment,
    build_chunk_prompt,
    chunk_prefix_fragments,
)
from continuity_engine import ContextLimit, ProcessTeardownError, SummaryResult, canonical, fingerprint
from preparation_errors import (
    PreparationAttestationError,
    PreparationCapacityError,
    PreparationCleanupError,
    PreparationConfigurationError,
    PreparationInvalidOutput,
    PreparationTransportError,
    PreparationValidationError,
)
from replay_budget import estimate_tokens

# Protocol identity: bump when chunking/prompt/cursor semantics change.
PREPARATION_PROTOCOL_VERSION = "prep-2"

# Worst-case number of provider calls for one complete handoff.
MAX_TOTAL_CALLS = 64
# Handoff wall-clock ceiling (seconds) for all preparation work.
MAX_ELAPSED_SECONDS = 1800.0
# Retries per chunk for transient/validation failures before the operation
# fails closed (retrieval remains the engine's fallback).
RETRIES_PER_CHUNK = 2
# Durable operation records can be disabled for offline tests/offline runs.
import os as _os
DURABILITY = _os.environ.get("LOOM_PREPARATION_DURABILITY", "1") != "0"


@dataclass
class PreparationBudget:
    """One budget for the complete handoff: every cut, retry, crash resume
    and provider call consumes the same record; never reset on resume."""
    maximum_calls: int = MAX_TOTAL_CALLS
    calls_used: int = 0
    maximum_elapsed_seconds: float = MAX_ELAPSED_SECONDS
    started_at: float = field(default_factory=time.monotonic)
    operation_id: str = ""
    exhausted_reason: str | None = None

    def remaining_calls(self) -> int:
        return max(0, self.maximum_calls - self.calls_used)

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def exhausted(self) -> str | None:
        if self.remaining_calls() <= 0:
            return (self.exhausted_reason
                    or f"call budget exhausted ({self.calls_used}/{self.maximum_calls} calls)")
        if self.elapsed() >= self.maximum_elapsed_seconds:
            return (self.exhausted_reason
                    or f"time budget exhausted after {self.elapsed():.0f}s")
        return None

    def visible_failure(self) -> str:
        return (f"Preparation budget exhausted: {self.exhausted()}; "
                "the current request is preserved and retrieval fallback applies")


@dataclass(frozen=True)
class PreparationChunkRequest:
    operation_id: str
    destination: dict
    prior_state: str | None
    fragments: list[EvidenceFragment]
    output_cap: int
    call_budget: int
    timeout_seconds: float = 120.0


@dataclass
class PreparationChunkResult:
    accepted: bool
    result_state: str
    fragments: list[EvidenceFragment]
    effective_model: str
    usage_tokens: int = 0
    terminal_error: str | None = None


def isolated_preparation_cwd(operation_id: str) -> str:
    """Fresh isolated directory outside any project checkout."""
    path = exo_state.state_path("preparation", operation_id)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def cleanup_preparation_cwd(path) -> None:
    """Remove the isolated preparation directory. Cleanup failures are never
    silently ignored: private prompt files must not linger."""
    if not path:
        return
    try:
        import shutil
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PreparationCleanupError(
            f"Preparation working directory {path} could not be removed: {exc}"
        ) from exc


async def verify_process_teardown(proc, harness: str, timeout_seconds: float = 5.0) -> None:
    """Verify child process exit. Fail closed if teardown fails."""
    if proc is None:
        return
    try:
        import server
        teardown = getattr(server, "_teardown_child_provider", None)
        if teardown is not None:
            await teardown(proc, harness)
    except Exception as e:
        try:
            if hasattr(proc, "terminate"):
                proc.terminate()
            if hasattr(proc, "kill"):
                proc.kill()
        except Exception:
            pass
        raise ProcessTeardownError(
            f"Teardown failed for {harness} preparation process: {e}"
        ) from e

    try:
        if hasattr(proc, "poll") and proc.poll() is None:
            if hasattr(proc, "kill"):
                proc.kill()
            if hasattr(proc, "wait"):
                try:
                    await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
                except Exception as wait_err:
                    raise ProcessTeardownError(
                        f"Preparation process {proc.pid} refused to exit: {wait_err}"
                    ) from wait_err
    except ProcessTeardownError:
        raise
    except Exception as e:
        raise ProcessTeardownError(f"Process verification error: {e}") from e


def _strip_harness_prefix(model: str) -> str:
    """Normalize away a single harness route prefix (e.g. goose:<id>)."""
    parts = model.split(":", 1)
    if len(parts) == 2 and parts[0].casefold() in {"goose", "claude", "codex", "gemini", "agy"}:
        return parts[1]
    return model


def _attestation_verified(attestation, requested: str) -> str | None:
    """Return the verified effective model, or None to reject the call.

    The provider's structured attestation must say `verified`, report a
    nonempty effective model itself, and that effective model must be an
    exact normalized match for the launch model in the same attestation.
    Suffix or prefix similarity never counts, and a launch argument is
    never substituted when the provider did not report one."""
    if not isinstance(attestation, dict):
        return None
    status = str(attestation.get("status") or "").strip().casefold()
    effective = str(attestation.get("effective_model") or "").strip()
    launch = str(attestation.get("launch_model") or "").strip()
    if status != "verified" or not effective or not launch:
        return None
    if effective.casefold() != launch.casefold():
        return None
    if requested:
        req = str(requested).strip()
        if launch.casefold() != req.casefold() and not (
            _strip_harness_prefix(launch).casefold() == _strip_harness_prefix(req).casefold()
        ):
            return None
    return effective


def _validate_state_output(text: str) -> dict:
    """Accepted state must be a JSON object with exactly the STATE_SCHEMA keys."""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PreparationValidationError(f"Preparation output is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PreparationValidationError("Preparation output must be a single JSON object")
    keys = set(parsed)
    expected = set(STATE_SCHEMA)
    missing = expected - keys
    extra = keys - expected
    if missing or extra:
        raise PreparationValidationError(
            f"State schema mismatch; missing={sorted(missing)} extra={sorted(extra)}"
        )
    for key, value in parsed.items():
        if not isinstance(value, str):
            raise PreparationValidationError(f"State field {key!r} must be a string")
    return parsed


async def execute_preparation_chunk(
    prompt: str,
    destination: dict,
    isolated_cwd,
    *,
    output_cap: int,
    timeout_seconds: float = 120.0,
    status=None,
) -> tuple[str, dict]:
    """Execute one preparation chunk on the selected destination.

    Launches the provider client with preparation=True — the tool-free
    transport. Returns (output_text, provider_attestation_dict).
    """
    harness = destination.get("harness")
    model = destination.get("model")
    backend = destination.get("backend")

    from config import config
    port = getattr(config, "port", 3000)

    try:
        import server
        claude_client = getattr(server, "claude_client", None)
        codex_client = getattr(server, "codex_client", None)
        gemini_client = getattr(server, "gemini_client", None)
        goose_client = getattr(server, "goose_client", None)
    except (ImportError, AttributeError):
        claude_client = codex_client = gemini_client = goose_client = None

    if claude_client is None:
        import claude_client
    if codex_client is None:
        import codex_client
    if gemini_client is None:
        import gemini_client
    if goose_client is None:
        import goose_client

    if status and callable(status):
        try:
            await status(f"Preparing context on {harness} ({model})...")
        except Exception:
            pass

    proc = None
    attestation = None

    async with asyncio.timeout(timeout_seconds):
        if harness == "codex":
            proc, stream = await codex_client.run_codex(
                prompt, str(isolated_cwd), conv_id=0, server_port=port,
                model=model, permission_mode="default", preparation=True,
            )
        elif harness == "gemini":
            proc, stream = await gemini_client.run_gemini(
                prompt, str(isolated_cwd), conv_id=0, server_port=port,
                model=model, permission_mode="default", preparation=True,
            )
        elif harness == "goose":
            proc, stream = await goose_client.run_goose(
                prompt, conv_id=0, cwd=str(isolated_cwd), model=model,
                loom_port=port, permission_mode="default", builtins=[],
                preparation=True,
            )
        elif harness in {"claude", "local", "openrouter", "umans", "dream-claude", "openrouter-claude"}:
            use_llama = bool(harness == "local" or backend == "llama")
            use_openrouter = bool(harness in {"openrouter", "openrouter-claude"} or backend == "openrouter")
            use_umans = bool(harness == "umans" or backend == "umans")
            use_dream = bool(harness == "dream-claude" or backend == "dream")
            proc, stream = await claude_client.run_claude(
                prompt, str(isolated_cwd), conv_id=0, server_port=port,
                model=model, requested_model=model, permission_mode="default",
                use_llama=use_llama, use_openrouter=use_openrouter,
                use_umans=use_umans, use_dream=use_dream, preparation=True,
            )
        else:
            raise PreparationConfigurationError(
                f"Unsupported destination preparation harness: {harness}")

        full_text = ""
        try:
            async for item in stream:
                item = dict(item or {})
                kind = item.get("type") or "status"
                if kind in {"text_delta", "stream_chunk"}:
                    full_text += item.get("text") or item.get("content") or ""
                elif kind == "result":
                    if not full_text:
                        full_text = str(item.get("result_text") or item.get("text") or "")
                    if isinstance(item.get("model_attestation"), dict):
                        attestation = item["model_attestation"]
                elif kind == "session_info":
                    if isinstance(item.get("model_attestation"), dict):
                        attestation = item["model_attestation"]
        finally:
            await verify_process_teardown(proc, harness)

    output = full_text.strip()
    if not output:
        raise PreparationTransportError("Destination preparation produced empty text")
    if not isinstance(attestation, dict):
        # No provider attestation at all: the launch argument is request
        # evidence, never a substitute for provider-reported evidence.
        attestation = {"status": "unverified", "effective_model": None,
                       "launch_model": None}
    return output, attestation


def _attach_trace(coordinator, older_record_lengths, trace_calls, budget,
                  operation_id, *, reason, trace_attempts=None):
    """Record the bounded-failure trace even when no chunk was accepted, so a
    visible failure always leaves recorded production evidence."""
    task = asyncio.current_task()
    if not task:
        return
    task._context_trace = {
        "selected_route": dict(coordinator.destination),
        "older_record_lengths": older_record_lengths,
        "minimum_accepted_calls": len(trace_calls),
        "call_budget": budget.maximum_calls,
        "calls_used": budget.calls_used,
        "operation_id": operation_id,
        "preparation_calls": trace_calls,
        "preparation_attempts": list(trace_attempts or trace_calls),
        "failure_reason": reason,
    }


class BoundedPreparationCoordinator:
    """Bounded, tool-free chunk preparation with rolling state.

    One handoff-wide budget (shared by every cut of one engine.prepare),
    measured admission, enforced call/time limits, real attestation,
    STATE_SCHEMA validation and durable resumable state.
    """

    def __init__(self, destination: dict, *, status=None, budget: PreparationBudget | None = None,
                 conversation_id: int | None = None):
        self.destination = dict(destination)
        self.status = status
        self.harness = destination["harness"]
        self.model = destination["model"]
        self.backend = destination.get("backend")
        self.conversation_id = conversation_id
        self.identity = ":".join(
            str(x) for x in (self.harness, self.model, self.backend or "") if x
        )
        self.budget = budget or PreparationBudget()

    # -- capacity / admission ------------------------------------------------

    async def _capacity(self) -> int:
        from replay_budget import context_capacity
        cap, _ = await context_capacity(self.model, self.harness, backend=self.backend)
        return int(cap)

    def _destination_label(self, capacity: int) -> str:
        # Resumable identity: harness + normalized model + backend + protocol
        # version + the measured capacity the chunking was computed against.
        return f"{self.harness}:{self.model}:{self.backend or ''}:" \
               f"{PREPARATION_PROTOCOL_VERSION}:cap{capacity}"

    def _admit(self, prompt: str, output_cap: int, capacity: int) -> int:
        """Final admission check for one provider request."""
        request_tokens = estimate_tokens(prompt)
        if request_tokens + output_cap > capacity:
            raise PreparationCapacityError(
                "Preparation request exceeds measured destination capacity "
                f"({request_tokens} request + {output_cap} output > {capacity}); "
                "no inference sent"
            )
        return request_tokens

    # -- durability ----------------------------------------------------------

    async def _claim_call(self, operation_id: str) -> bool:
        """Atomically claim one call slot in SQLite; keeps crash-resumed
        consumption intact and prevents concurrent over-claiming."""
        self.budget.calls_used += 1
        if not DURABILITY:
            return True
        try:
            return await db.claim_preparation_call(
                operation_id, remaining_calls=self.budget.remaining_calls() + 1)
        except Exception:
            return True  # telemetry outage must not corrupt bounded preparation

    async def _start_operation(self, source_revision: str, call_budget: int,
                               capacity: int) -> tuple[str, dict | None]:
        """Create/resume the durable operation; mark any superseded record
        terminal so stale 'running' rows never accumulate."""
        if not DURABILITY:
            return f"prep-{uuid.uuid4().hex[:12]}", None
        try:
            prior = await db.find_resumable_preparation(
                self.conversation_id, source_revision,
                self._destination_label(capacity))
            operation_id = f"prep-{uuid.uuid4().hex[:12]}"
            task = asyncio.current_task()
            owner_run_id = getattr(task, "_agent_run_id", None) if task else None
            carried = 0
            if prior and prior.get("accepted_state"):
                carried = int(prior.get("calls_used") or 0)
                # Budget continues across crash resumes; never reset.
                self.budget.calls_used = max(self.budget.calls_used, carried)
                if prior.get("operation_id"):
                    await db.fail_preparation_operation(
                        prior["operation_id"], terminal_reason="superseded",
                        calls_used=carried)
            await db.create_preparation_operation(
                operation_id, owner_run_id=owner_run_id,
                conversation_id=self.conversation_id,
                source_revision=source_revision,
                destination=self._destination_label(capacity),
                covered_cursor=json.dumps({"boundary": None, "ordinal": -1}),
                call_budget=call_budget,
                calls_used=self.budget.calls_used)
            return operation_id, prior
        except Exception:
            # Durability is best-effort telemetry; a telemetry outage must
            # not corrupt the bounded preparation itself.
            return f"prep-{uuid.uuid4().hex[:12]}", None

    async def _record_advance(self, operation_id: str, boundary: dict,
                              accepted_state: str) -> None:
        if not DURABILITY:
            return
        try:
            await db.advance_preparation_operation(
                operation_id, covered_cursor=json.dumps(boundary),
                accepted_state=accepted_state, calls_used=self.budget.calls_used)
        except Exception:
            pass

    async def _record_terminal(self, operation_id: str, completed: bool,
                               accepted_state: str | None, reason: str | None) -> None:
        if not DURABILITY:
            return
        try:
            if completed:
                await db.complete_preparation_operation(
                    operation_id, accepted_state=accepted_state or "",
                    calls_used=self.budget.calls_used)
            else:
                await db.fail_preparation_operation(
                    operation_id, terminal_reason=reason or "failed",
                    calls_used=self.budget.calls_used)
        except Exception:
            pass

    # -- entry point ---------------------------------------------------------

    async def summarize(self, text: str, max_tokens: int) -> SummaryResult:
        """Prepare compacted continuity state for the selected destination.

        All cuts of one engine.prepare share this coordinator's budget."""
        if not isinstance(max_tokens, int) or max_tokens < 128:
            raise PreparationConfigurationError(
                "Preparation output cap must be a real integer reserve (>=128)")
        exhausted = self.budget.exhausted()
        if exhausted:
            raise PreparationCapacityError(self.budget.visible_failure())
        output_cap = max_tokens

        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list) and all(isinstance(m, dict) for m in parsed):
            records = parsed
        else:
            records = [{"id": 0, "role": "user", "content": text}]

        capacity = await self._capacity()
        source_revision = fingerprint(records)
        isolated_cwd = isolated_preparation_cwd(self.budget.operation_id
                                                or f"prep-{uuid.uuid4().hex[:12]}")

        try:
            return await self._run(records, source_revision, output_cap,
                                   capacity, isolated_cwd)
        finally:
            cleanup_preparation_cwd(isolated_cwd)

    async def _run(self, records, source_revision, output_cap, capacity,
                   isolated_cwd) -> SummaryResult:
        skeleton = build_chunk_prompt(None, [], schema=True)
        instruction_tokens = estimate_tokens(skeleton)
        overhead = instruction_tokens + output_cap + max(256, capacity // 20)
        state_budget_chars = max(0, (capacity - overhead) * 3 // 2)
        evidence_budget_chars = max(0, (capacity - overhead) * 3 - state_budget_chars)
        if evidence_budget_chars < 2000:
            raise PreparationCapacityError(
                "Destination capacity cannot admit instructions, rolling state "
                "and output reserve simultaneously; no inference sent"
            )

        max_frag_chars = min(8000, evidence_budget_chars)
        chunks = chunk_prefix_fragments(
            records, max_chunk_chars=evidence_budget_chars,
            max_fragment_chars=max_frag_chars)
        if not chunks:
            raise PreparationCapacityError("No evidence fragments to prepare")

        operation_id, prior = await self._start_operation(
            source_revision, self.budget.maximum_calls, capacity)
        self.budget.operation_id = operation_id

        # Resume from the exact recorded boundary; never resend paid chunks.
        start_ordinal = -1
        prior_state = None
        prior_state_id = None
        if prior and prior.get("accepted_state"):
            try:
                # The covered cursor IS the exact accepted boundary: chunk
                # ordinal, last fragment extent and the accepted state
                # fingerprint. Corrupt or edited rows never resume.
                cursor = json.loads(prior["covered_cursor"] or "{}")
                ordinal = int(cursor.get("ordinal", -1))
                state_ok = _validate_state_output(prior["accepted_state"])
                if (ordinal >= 0 and state_ok
                        and cursor.get("fingerprint")
                        and cursor["fingerprint"] == fingerprint([prior["accepted_state"]])):
                    start_ordinal = ordinal
                    prior_state = prior["accepted_state"]
                    prior_state_id = "state-resumed"
                    chunks = chunks[ordinal + 1:]
            except (ValueError, TypeError, json.JSONDecodeError,
                    PreparationValidationError):
                start_ordinal, prior_state, prior_state_id = -1, None, None

        trace_calls: list[dict] = []
        # Unlike preparation_calls (accepted rolling-state transitions), this
        # records every paid inference attempt, including rejected retries.
        # Turn identity uses it so a failed wrong/local-model call cannot hide
        # behind a later successful destination response.
        trace_attempts: list[dict] = []
        older_record_lengths = {
            r["id"]: len(str(r.get("content") or "")) for r in records
        }

        for ordinal, chunk_fragments in enumerate(chunks, start=start_ordinal + 1):
            result = None
            terminal_error = None
            terminal_kind: type[Exception] | None = None
            verified_model = None
            for attempt in range(RETRIES_PER_CHUNK + 1):
                exhausted = self.budget.exhausted()
                if exhausted:
                    self.budget.exhausted_reason = exhausted
                    await self._record_terminal(
                        operation_id, False, prior_state, exhausted)
                    _attach_trace(self, older_record_lengths, trace_calls, self.budget,
                                  operation_id, reason=exhausted,
                                  trace_attempts=trace_attempts)
                    raise PreparationCapacityError(self.budget.visible_failure())

                prompt = build_chunk_prompt(prior_state, chunk_fragments)
                request_tokens = self._admit(prompt, output_cap, capacity)

                claimed = await self._claim_call(operation_id)
                if not claimed:
                    self.budget.exhausted_reason = "concurrent claim lost remaining slot"
                    _attach_trace(self, older_record_lengths, trace_calls, self.budget,
                                  operation_id, reason="claim_lost",
                                  trace_attempts=trace_attempts)
                    raise PreparationCapacityError(self.budget.visible_failure())

                attempt_record = {
                    "route": dict(self.destination),
                    "attested_model": None,
                    "accepted": False,
                    "attempt": attempt + 1,
                    "ordinal": ordinal,
                    "request_tokens": request_tokens,
                    "output_reserve": output_cap,
                    "capacity": capacity,
                }
                trace_attempts.append(attempt_record)
                try:
                    output, attested_model = await execute_preparation_chunk(
                        prompt, self.destination, isolated_cwd,
                        output_cap=output_cap, status=self.status,
                        timeout_seconds=min(120.0, max(0.001,
                            self.budget.maximum_elapsed_seconds - self.budget.elapsed())))
                except ProcessTeardownError:
                    raise
                except PreparationConfigurationError:
                    raise
                except (ContextLimit, TimeoutError, OSError, ValueError,
                        RuntimeError, PreparationCapacityError,
                        PreparationTransportError, PreparationValidationError) as exc:
                    terminal_error = str(exc)
                    terminal_kind = type(exc)
                    attempt_record["failure_reason"] = terminal_error
                    continue

                attempt_record["attested_model"] = (
                    attested_model.get("effective_model")
                    if isinstance(attested_model, dict) else None
                )
                verified_model = _attestation_verified(attested_model, self.model)
                if verified_model is None:
                    terminal_error = (
                        f"Model attestation rejected: requested {self.model!r}, "
                        f"provider attestation {attested_model!r}")
                    terminal_kind = PreparationAttestationError
                    attempt_record["failure_reason"] = terminal_error
                    continue

                if estimate_tokens(output) > output_cap:
                    terminal_error = (
                        f"Preparation output {estimate_tokens(output)} tokens "
                        f"exceeds the {output_cap} output reserve")
                    terminal_kind = PreparationCapacityError
                    attempt_record["failure_reason"] = terminal_error
                    continue

                try:
                    result = _validate_state_output(output)
                except PreparationValidationError as exc:
                    terminal_error = str(exc)
                    terminal_kind = PreparationInvalidOutput
                    attempt_record["failure_reason"] = terminal_error
                    continue
                attempt_record["accepted"] = True
                break

            if result is None:
                await self._record_terminal(
                    operation_id, False, prior_state,
                    terminal_error or "chunk rejected")
                _attach_trace(self, older_record_lengths, trace_calls, self.budget,
                              operation_id, reason=terminal_error or "chunk_rejected",
                              trace_attempts=trace_attempts)
                recoverable = (PreparationCapacityError, PreparationTransportError,
                               PreparationValidationError, ContextLimit,
                               TimeoutError, OSError, ValueError)
                kind = terminal_kind if (isinstance(terminal_kind, type)
                                         and issubclass(terminal_kind, recoverable)) \
                    else PreparationTransportError
                raise kind(
                    f"Preparation chunk rejected after retries: {terminal_error}")

            prior_state = json.dumps(result, ensure_ascii=False)
            boundary = {
                "ordinal": ordinal,
                "record_id": chunk_fragments[-1].record_id,
                "start": chunk_fragments[-1].start,
                "end": chunk_fragments[-1].end,
                "fingerprint": fingerprint([prior_state]),
            }
            result_state_id = f"state-{len(trace_calls)}"
            trace_calls.append({
                "route": dict(self.destination),
                "attested_model": verified_model,
                "tools_enabled": False,
                "prior_state_id": prior_state_id,
                "result_state_id": result_state_id,
                "request_tokens": request_tokens,
                "output_reserve": output_cap,
                "capacity": capacity,
                "accepted": True,
                "fragments": [
                    {"record_id": f.record_id, "start": f.start, "end": f.end}
                    for f in chunk_fragments
                ],
            })
            await self._record_advance(operation_id, boundary, prior_state)
            prior_state_id = result_state_id

        if not prior_state:
            await self._record_terminal(
                operation_id, False, None, "no state produced")
            _attach_trace(self, older_record_lengths, trace_calls, self.budget,
                          operation_id, reason="no_state",
                          trace_attempts=trace_attempts)
            raise PreparationTransportError("Preparation produced no continuity state")

        await self._record_terminal(operation_id, True, prior_state, None)

        task = asyncio.current_task()
        if task:
            task._context_trace = {
                "selected_route": dict(self.destination),
                "older_record_lengths": older_record_lengths,
                "minimum_accepted_calls": len(trace_calls),
                "call_budget": self.budget.maximum_calls,
                "calls_used": self.budget.calls_used,
                "operation_id": operation_id,
                "preparation_calls": trace_calls,
                "preparation_attempts": trace_attempts,
            }

        return SummaryResult(prior_state, self.identity)
