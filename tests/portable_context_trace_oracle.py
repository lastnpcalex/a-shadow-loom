"""Acceptance trace assertions. Feed observations from the real adapter boundary.

The recorder is an implementation deliverable: planner self-reported booleans
alone are insufficient. This oracle proves structure, never summary semantics.
"""


def assert_incremental_handoff(trace):
    route = trace["selected_route"]
    lengths = trace["older_record_lengths"]
    records = list(lengths)
    record_index, offset = 0, 0
    state = None
    calls = trace["preparation_calls"]
    assert len(calls) >= trace["minimum_accepted_calls"] > 0, "Missing incremental preparation"
    assert len(calls) <= trace["call_budget"], "Preparation exceeded call budget"
    for call in calls:
        assert call["route"] == route, "Unselected preparation route"
        assert call["tools_enabled"] is False, "Preparation could execute tools"
        assert call["prior_state_id"] == state, "Lost rolling state"
        assert 0 < call["request_tokens"]
        assert call["request_tokens"] + call["output_reserve"] <= call["capacity"], "Oversized preparation request"
        assert call["accepted"] is True, "Rejected result advanced coverage"
        assert call["fragments"], "No-progress preparation"
        for fragment in call["fragments"]:
            assert record_index < len(records), "Duplicated historical coverage"
            rid = records[record_index]
            assert fragment["record_id"] == rid, "Skipped/reordered historical record"
            assert fragment["start"] == offset, "Gap/overlap in giant record"
            assert offset < fragment["end"] <= lengths[rid], "Invalid fragment extent"
            offset = fragment["end"]
            if offset == lengths[rid]:
                record_index += 1
                offset = 0
        assert call["result_state_id"] and call["result_state_id"] != state, "State failed to advance"
        state = call["result_state_id"]
    assert record_index == len(records) and offset == 0, "Unprocessed historical evidence"
    launch = trace["working_launch"]
    assert launch["route"] == route and launch["state_id"] == state
    assert launch["recent_records"] == trace["expected_recent_records"], "Recent tail changed"
    assert launch["current_request"] == trace["expected_current_request"], "Current request changed"
    assert launch["request_tokens"] + launch["output_reserve"] <= launch["capacity"], "Oversized working launch"
    assert launch["completed"] is True and launch["native_session_id"], "Working turn did not complete"
    assert trace["followup_source_session"] == launch["native_session_id"], "Next turn did not resume"
    assert trace["retrieval_fallback"] is False, "Retrieval substituted for normal compaction"
    assert trace["unselected_inference_calls"] == 0, "Unexpected inference dependency"
