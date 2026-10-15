"""Exhaustive Python conformance against the Lean-generated access oracle."""
import json
from pathlib import Path

from search_space.policy import Policy, may_read, include_ancestor, tool_allowed, route_decision


def test_search_policy_matches_lean_oracle():
    cases = json.loads((Path(__file__).resolve().parents[1] / "formal/search_space/cases.json").read_text())
    assert len(cases["reads"]) == 48
    for policy, (flags, expected) in cases["reads"]:
        active, assigned, same_source, local = flags
        assert may_read(active=active, assigned=assigned, same_source=same_source, local=local, policy=Policy(policy)) == expected
    assert len(cases["ancestry"]) == 32
    for flags, expected in cases["ancestry"]:
        conv, thread, workspace, parent, unseen = flags
        assert include_ancestor(same_conversation=conv, same_thread=thread, same_workspace=workspace,
                                parent_matches=parent, unseen=unseen) == expected
    for name, expected in cases["tools"]:
        assert tool_allowed(name) == expected
    assert len(cases["routes"]) == 16
    for flags, expected in cases["routes"]:
        nonempty, classified, excluded, sensitive = flags
        assert route_decision(nonempty=nonempty, classified=classified, excluded=excluded, sensitive=sensitive) == expected


def test_history_permission_identity_is_exact_and_read_only():
    from permission_policy import normalize_permission_request, ActionEffect
    from acp_permissions import permission_action
    for tool in ("search_conversations", "read_conversation_context", "get_conversation_details"):
        action = normalize_permission_request(tool_name=f"mcp__loom-agent-control__{tool}", tool_input={"query":"fixture"}, workspace_root=".")
        assert action.effect == ActionEffect.READ
        for extension, expected in (("loom-agent-control","LoomContextRead"),("foreign","Unknown")):
            call = {"title":tool, "kind":"other", "rawInput":{"query":"fixture"},
                    "_meta":{"goose":{"toolCall":{"extensionName":extension,"toolName":tool}}}}
            assert permission_action({"toolCall":call})[0] == expected
