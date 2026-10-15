"""Count-only acceptance observations; never persist commands, paths or notes."""
import json


def unknown_count(audits):
    # Human/lifecycle resolution replies need not repeat policy metadata.
    # The persisted policy record is the authority for normalization counts.
    return sum(a.get("action", {}).get("effect", "unknown") == "unknown" or
               a.get("action", {}).get("boundary", "unknown") == "unknown" for a in audits)


def observe(record_property, harness, scenario, *, expected, observed, profile="balanced"):
    record_property("permission_case", json.dumps({
        "harness": harness, "scenario": scenario, "profile": profile,
        "expected": expected, "observed": observed,
    }, sort_keys=True))
    assert observed == expected, f"{harness}/{scenario}: expected {expected}, observed {observed}"
