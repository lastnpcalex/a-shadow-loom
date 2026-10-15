"""Pure coordination reducers, mirrored by formal/coordination.

No I/O, text classification, provider names or database access belongs here.
Callers establish identity/exit evidence and execute returned effects. These
functions govern decisions; conformance tests are not proofs of Python or I/O.
"""
from dataclasses import dataclass, replace

OUTCOMES = ("pending", "completed", "failed", "cancelled", "interrupted")
QUEUES = ("waiting", "paused", "dispatched")


@dataclass(frozen=True)
class State:
    outcome: str = "pending"
    exited: bool = False
    leased: bool = True
    queue: str = "waiting"
    authorized: bool = False
    diagnostic: bool = False

    def __post_init__(self):
        if self.outcome not in OUTCOMES or self.queue not in QUEUES:
            raise ValueError("Invalid coordination state")

    def as_list(self):
        return [self.outcome, self.exited, self.leased, self.queue, self.authorized, self.diagnostic]


def _terminal(state: State, outcome: str) -> State:
    if outcome not in OUTCOMES:
        raise ValueError("Invalid coordination outcome")
    if state.outcome != "pending" or outcome == "pending":
        return state
    queue = state.queue if outcome == "completed" or state.queue == "dispatched" else "paused"
    return replace(state, outcome=outcome, queue=queue)


def step(state: State, event: str, value=None) -> tuple[State, str]:
    if event == "tool_data":
        return state, "none"
    if event == "diagnostic_error":
        return replace(state, diagnostic=True), "none"
    if event == "terminal":
        return _terminal(state, value), "none"
    if event == "stop":
        return _terminal(state, "interrupted" if value else "cancelled"), "none"
    if event == "process_exited":
        return replace(_terminal(state, "failed"), exited=True), "none"
    if event == "release":
        if state.exited and state.leased and state.outcome != "pending":
            return replace(state, leased=False), "release_lease"
        return state, "none"
    if event == "resume_queue":
        return (replace(state, queue="waiting", authorized=True), "none") if state.queue == "paused" else (state, "none")
    if event == "dispatch":
        if state.leased or not state.exited or state.queue != "waiting":
            return state, "none"
        if state.outcome == "completed" or state.authorized:
            return replace(state, queue="dispatched"), "launch_queued"
        return replace(state, queue="paused"), "pause_queue"
    raise ValueError("Invalid coordination event")


def failed(state: State) -> bool:
    return state.outcome == "failed" or (state.outcome == "pending" and state.diagnostic)


def view_step(landed: bool, same_identity: bool, incoming_landed: bool) -> tuple[bool, bool]:
    if not same_identity or (landed and not incoming_landed):
        return landed, False
    return incoming_landed, True


# Boundary projections: legacy names remain in SQLite; the reducer sees a
# small typed outcome, never arbitrary prose or a provider's display title.
def outcome_for_lifecycle(value: str) -> str:
    value = {"cancel": "cancelled", "interrupt": "interrupted", "partial_failed": "failed"}.get(value, value)
    return value if value in OUTCOMES else "failed"


def result_failed(previous_error: bool, confirmed: bool, is_error: bool) -> bool:
    state = State(diagnostic=bool(previous_error))
    if confirmed:
        state, _ = step(state, "terminal", "failed" if is_error else "completed")
    elif is_error:
        state, _ = step(state, "diagnostic_error")
    return failed(state)


def queue_effect(*, held: bool, outcome: str, paused: bool, authorized: bool) -> str:
    state = State(outcome_for_lifecycle(outcome), not held, held,
                  "paused" if paused else "waiting", authorized)
    return step(state, "dispatch")[1]


def finish_effects(outcome: str, *, exited: bool, leased: bool) -> tuple[str, str]:
    state, _ = step(State(leased=leased), "terminal", outcome_for_lifecycle(outcome))
    if exited:
        state, _ = step(state, "process_exited")
    state, release = step(state, "release")
    return release, step(state, "dispatch")[1]
