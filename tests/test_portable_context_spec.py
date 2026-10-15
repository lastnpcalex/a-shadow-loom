"""Checks the generated Lean oracle, NOT production conformance or model fidelity."""
import itertools
import json
from pathlib import Path


CASES = json.loads((Path(__file__).parents[1] / "formal/portable_context/cases.json").read_text())


def test_lean_fixture_contains_every_finite_state_once():
    keys = ("stopped", "native", "replay_fits", "view_fits", "preparation", "result_fits")
    actual = {tuple(row[k] for k in keys) for row in CASES}
    expected = set(itertools.product([False, True], [False, True], [False, True], [False, True],
        ["untried", "succeeded", "failed", "unsupported"], [False, True]))
    assert actual == expected and len(CASES) == len(expected) == 128


def test_spec_never_treats_no_attempt_as_failure():
    for row in CASES:
        if row["action"] == "retrieve":
            assert row["preparation"] in {"failed", "unsupported"}
        if row["stopped"]:
            assert row["action"] == "stop"
        elif row["native"]:
            assert row["action"] == "resume"


def test_spec_retains_preparation_for_oversize_result():
    rows = [r for r in CASES if not any(r[k] for k in ("stopped", "native", "replay_fits", "view_fits", "result_fits"))]
    assert {r["preparation"]: r["action"] for r in rows} == {
        "untried": "prepare", "succeeded": "prepare", "failed": "retrieve", "unsupported": "retrieve"}
