"""Mutation tests of the acceptance checker, not synthetic native success."""
import copy

import pytest

from tests.portable_context_trace_oracle import assert_incremental_handoff


def trace_fixture():
    route = {"harness": "fixture", "model": "selected", "backend": "cloud"}
    tail = [{"id": 3, "role": "assistant", "content": "Recent result"}]
    calls = []
    for index, fragments in enumerate([
        [{"record_id": 1, "start": 0, "end": 5000}],
        [{"record_id": 1, "start": 5000, "end": 9000}, {"record_id": 2, "start": 0, "end": 200}],
    ]):
        calls.append({"route": copy.deepcopy(route), "tools_enabled": False,
            "prior_state_id": None if index == 0 else "state-0", "result_state_id": f"state-{index}",
            "request_tokens": 5500, "output_reserve": 1000, "capacity": 8000,
            "accepted": True, "fragments": fragments})
    return {"selected_route": route, "older_record_lengths": {1: 9000, 2: 200},
        "minimum_accepted_calls": 2, "call_budget": 6, "preparation_calls": calls,
        "expected_recent_records": tail, "expected_current_request": "Continue",
        "working_launch": {"route": copy.deepcopy(route), "state_id": "state-1",
            "recent_records": copy.deepcopy(tail), "current_request": "Continue",
            "request_tokens": 3000, "output_reserve": 1000, "capacity": 8000,
            "completed": True, "native_session_id": "new-session"},
        "followup_source_session": "new-session", "retrieval_fallback": False,
        "unselected_inference_calls": 0}


def test_checker_accepts_structurally_complete_fixture():
    assert_incremental_handoff(trace_fixture())


@pytest.mark.parametrize("mutation", ["no_calls", "gap", "duplicate", "wrong_model", "tools",
    "over_budget", "lost_state", "truncated_tail", "changed_user", "retrieval", "local", "no_followup"])
def test_checker_rejects_false_success(mutation):
    trace = trace_fixture()
    calls = trace["preparation_calls"]
    if mutation == "no_calls": trace["preparation_calls"] = []
    elif mutation == "gap": calls[1]["fragments"][0]["start"] += 1
    elif mutation == "duplicate": calls[1]["fragments"] = copy.deepcopy(calls[0]["fragments"])
    elif mutation == "wrong_model": calls[0]["route"]["model"] = "other"
    elif mutation == "tools": calls[0]["tools_enabled"] = True
    elif mutation == "over_budget": calls[0]["request_tokens"] = 7500
    elif mutation == "lost_state": calls[1]["prior_state_id"] = None
    elif mutation == "truncated_tail": trace["working_launch"]["recent_records"] = []
    elif mutation == "changed_user": trace["working_launch"]["current_request"] = "Different task"
    elif mutation == "retrieval": trace["retrieval_fallback"] = True
    elif mutation == "local": trace["unselected_inference_calls"] = 1
    elif mutation == "no_followup": trace["followup_source_session"] = "unrelated"
    with pytest.raises(AssertionError):
        assert_incremental_handoff(trace)
