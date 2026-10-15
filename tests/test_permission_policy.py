"""Formal-policy runtime and provider-normalization conformance tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from permission_policy import (
    ActionBoundary,
    ActionEffect,
    NormalizedPermissionAction,
    PermissionProfile,
    PermissionResolution,
    PolicyDecision,
    ResolutionSource,
    RuleMatchContext,
    build_shadow_record,
    decide,
    normalize_permission_request,
    resolve_permission,
    rule_applies,
)


def _action(
    effect: ActionEffect,
    boundary: ActionBoundary = ActionBoundary.WORKSPACE,
    **kwargs,
) -> NormalizedPermissionAction:
    return NormalizedPermissionAction(effect=effect, boundary=boundary, **kwargs)


@pytest.mark.parametrize("profile", list(PermissionProfile))
def test_policy_has_no_automatic_deny(profile):
    for effect in ActionEffect:
        for boundary in ActionBoundary:
            for valid in (False, True):
                for warned in (False, True):
                    decision = decide(
                        profile,
                        _action(
                            effect,
                            boundary,
                            valid=valid,
                            warned=warned,
                        ),
                    )
                    assert decision in {
                        PolicyDecision.AUTO_ALLOW,
                        PolicyDecision.ASK,
                        PolicyDecision.ASK_WITH_WARNING,
                        PolicyDecision.INVALID_REQUEST,
                    }


def test_only_user_source_can_become_user_denial():
    for source in ResolutionSource:
        resolution = resolve_permission(allowed=False, source=source)
        assert (resolution is PermissionResolution.USER_DENIED) == (
            source is ResolutionSource.USER
        )


def test_warning_does_not_turn_an_automatic_allow_into_a_rejection():
    plain = _action(ActionEffect.WORKSPACE_WRITE)
    warned = _action(ActionEffect.WORKSPACE_WRITE, warned=True)
    assert decide(PermissionProfile.BALANCED, plain) is PolicyDecision.AUTO_ALLOW
    assert decide(PermissionProfile.BALANCED, warned) is PolicyDecision.AUTO_ALLOW


def test_unknown_action_asks_in_legacy_profiles():
    action = _action(ActionEffect.UNKNOWN, ActionBoundary.UNKNOWN)
    assert {
        decide(profile, action) for profile in (PermissionProfile.STRICT, PermissionProfile.BALANCED, PermissionProfile.TRUSTED)
    } == {PolicyDecision.ASK}


def test_profile_order_is_monotonic_for_workspace_effects():
    rank = {
        PolicyDecision.INVALID_REQUEST: 0,
        PolicyDecision.ASK_WITH_WARNING: 1,
        PolicyDecision.ASK: 1,
        PolicyDecision.AUTO_ALLOW: 2,
    }
    for effect in ActionEffect:
        for warned in (False, True):
            action = _action(effect, warned=warned)
            strict = rank[decide(PermissionProfile.STRICT, action)]
            balanced = rank[decide(PermissionProfile.BALANCED, action)]
            trusted = rank[decide(PermissionProfile.TRUSTED, action)]
            assert strict <= balanced <= trusted


@pytest.mark.parametrize(
    ("same_workspace", "branch_descendant", "shape_matches", "expected"),
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
def test_branch_rule_requires_all_scope_constraints(
    same_workspace, branch_descendant, shape_matches, expected
):
    assert rule_applies(
        RuleMatchContext(
            same_workspace=same_workspace,
            branch_descendant=branch_descendant,
            shape_matches=shape_matches,
        )
    ) is expected


def test_valid_explicit_rule_can_allow_an_otherwise_asked_action():
    action = _action(
        ActionEffect.EXTERNAL_MUTATION,
        ActionBoundary.EXTERNAL,
        rule_matched=True,
        warned=True,
    )
    assert decide(PermissionProfile.STRICT, action) is PolicyDecision.AUTO_ALLOW


def test_provider_payload_cannot_assert_a_remembered_rule(tmp_path):
    action = normalize_permission_request(
        harness="goose",
        tool_name="shell",
        tool_input={
            "kind": "execute",
            "cwd": str(tmp_path),
            "rule_matched": True,
            "same_workspace": True,
            "branch_descendant": True,
            "shape_matches": True,
        },
        workspace_root=str(tmp_path),
    )
    assert action.rule_matched is False


def test_loom_derives_rule_match_only_from_all_scope_constraints(tmp_path):
    action = normalize_permission_request(
        harness="codex",
        tool_name="Bash",
        tool_input={"command": "python build.py", "cwd": str(tmp_path)},
        workspace_root=str(tmp_path),
        rule_context=RuleMatchContext(
            same_workspace=True,
            branch_descendant=True,
            shape_matches=True,
        ),
    )
    assert action.rule_matched is True
    assert decide(PermissionProfile.STRICT, action) is PolicyDecision.AUTO_ALLOW


def test_provider_payloads_normalize_to_the_same_workspace_execution(tmp_path):
    command = "python scripts/build_deck.py --out review"
    payloads = [
        (
            "codex",
            "Bash",
            {
                "command": command,
                "cwd": str(tmp_path),
                "proposedExecpolicyAmendment": [
                    "python",
                    "scripts/build_deck.py",
                ],
            },
            "item/commandExecution/requestApproval",
        ),
        (
            "goose",
            "shell",
            {
                "toolCall": {
                    "title": "shell",
                    "kind": "execute",
                    "arguments": {"command": command, "cwd": str(tmp_path)},
                }
            },
            "PreToolUse",
        ),
        (
            "hermes",
            "terminal",
            {
                "toolCall": {
                    "title": "terminal",
                    "kind": "execute",
                    "arguments": {"command": command, "cwd": str(tmp_path)},
                }
            },
            "PreToolUse",
        ),
        (
            "claude",
            "Bash",
            {"command": command, "cwd": str(tmp_path)},
            "PreToolUse",
        ),
        (
            "antigravity",
            "Bash",
            {"command": command, "cwd": str(tmp_path)},
            "PreToolUse",
        ),
    ]

    actions = [
        normalize_permission_request(
            harness=harness,
            tool_name=tool_name,
            tool_input=tool_input,
            approval_method=method,
            workspace_root=str(tmp_path),
        )
        for harness, tool_name, tool_input, method in payloads
    ]
    assert {(action.effect, action.boundary) for action in actions} == {
        (ActionEffect.EXECUTE, ActionBoundary.WORKSPACE)
    }
    assert {
        decide(PermissionProfile.BALANCED, action) for action in actions
    } == {PolicyDecision.ASK}


def test_generated_lean_fixture_matches_python_runtime():
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "formal"
        / "permission_policy"
        / "generated"
        / "permission_cases.json"
    )
    assert fixture_path.is_file(), "Lean conformance fixture has not been generated"
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert len(cases) == 2048
    assert len({(c['profile'], c['effect'], c['boundary'], c['valid'], c['warned'], c['rule_matched'], c['destructive']) for c in cases}) == 2048
    for case in cases:
        action = NormalizedPermissionAction(
            effect=ActionEffect(case["effect"]),
            boundary=ActionBoundary(case["boundary"]),
            valid=case["valid"],
            warned=case["warned"],
            rule_matched=case["rule_matched"],
            destructive=case["destructive"],
        )
        assert decide(PermissionProfile(case["profile"]), action).value == case[
            "decision"
        ]


def test_shadow_record_is_provider_independent_and_redacts_paths(tmp_path):
    records = [
        build_shadow_record(
            profile=PermissionProfile.BALANCED,
            tool_name=tool_name,
            tool_input=tool_input,
            approval_method=method,
            harness=harness,
            workspace_root=str(tmp_path),
            warned=False,
            legacy_outcome="ask",
        )
        for harness, tool_name, tool_input, method in (
            (
                "codex",
                "Bash",
                {"command": "python build.py", "cwd": str(tmp_path)},
                "item/commandExecution/requestApproval",
            ),
            (
                "goose",
                "shell",
                {
                    "toolCall": {
                        "kind": "execute",
                        "arguments": {
                            "command": "python build.py",
                            "cwd": str(tmp_path),
                        },
                    }
                },
                "PreToolUse",
            ),
        )
    ]
    assert [record["decision"] for record in records] == ["ask", "ask"]
    assert [record["action"]["effect"] for record in records] == [
        "execute",
        "execute",
    ]
    serialized = json.dumps(records)
    assert str(tmp_path) not in serialized
    assert "python build.py" not in serialized


async def test_permission_policy_audit_persists_only_sanitized_metadata(tmp_database):
    import database as db

    conv = await db.create_conversation("Permission policy audit", mode="codex")
    record = {
        "profile": "balanced",
        "decision": "ask",
        "legacy_outcome": "ask",
        "disagrees": False,
        "action": {
            "effect": "execute",
            "boundary": "workspace",
            "valid": True,
            "warned": False,
            "rule_matched": False,
            "harness": "codex",
            "tool_name": "Bash",
            "workspace_root": "C:/private/workspace",
            "command_family": "secret command arguments",
        },
    }
    await db.save_permission_policy_audit(
        "shadow-request",
        conv["id"],
        record,
        run_id="gen:42",
        permission_scope="gen:42",
    )
    rows = await db.get_permission_policy_audits(conv["id"])
    assert len(rows) == 1
    assert rows[0]["action"] == {
        "boundary": "workspace",
        "effect": "execute",
        "harness": "codex",
        "rule_matched": False,
        "tool_name": "Bash",
        "valid": True,
        "warned": False,
    }
    serialized = json.dumps(rows)
    assert "private/workspace" not in serialized
    assert "secret command arguments" not in serialized
