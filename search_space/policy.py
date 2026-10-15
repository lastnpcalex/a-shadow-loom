"""Small access algebra mirrored by formal/search_space; normalization is separate."""
from enum import IntEnum


class Policy(IntEnum):
    EXCLUDED = 0
    CLOUD = 1
    SENSITIVE = 2


def may_read(*, active: bool, assigned: bool, same_source: bool,
             policy: Policy, local: bool) -> bool:
    return bool(active and assigned and same_source and policy != Policy.EXCLUDED
                and (policy == Policy.CLOUD or local))


def search_route(policies: list[str]) -> str:
    decision = route_decision(nonempty=bool(policies),
                              classified=all(p in {"excluded", "cloud", "sensitive"} for p in policies),
                              excluded="excluded" in policies, sensitive="sensitive" in policies)
    if not decision:
        raise ValueError("Every selected workspace must be classified and searchable")
    return "cloud" if decision == 2 else "local"


def route_decision(*, nonempty: bool, classified: bool, excluded: bool, sensitive: bool) -> int:
    return 0 if not nonempty or not classified or excluded else 1 if sensitive else 2


READ_TOOLS = frozenset({"search_conversations", "read_conversation_context", "get_conversation_details"})


def tool_allowed(name: str) -> bool:
    return name in READ_TOOLS


def include_ancestor(*, same_conversation: bool, same_thread: bool,
                     same_workspace: bool, parent_matches: bool, unseen: bool) -> bool:
    return bool(same_conversation and same_thread and same_workspace and parent_matches and unseen)
