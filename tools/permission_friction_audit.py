"""Read-only aggregate permission audit. Never report prompts, tool titles or paths.

Run: python tools/permission_friction_audit.py --db loom.db --hours 24
Old rows can contain raw tool titles; this query deliberately never selects them.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import time


# Keep recognized bridge provenance visible without guessing which harness
# produced an old shared-hook row. Values outside this finite set stay hidden.
_AUDIT_HARNESSES = {
    "codex_app_server", "codex", "claude", "claude_code", "antigravity", "gemini",
    "goose", "goose_acp", "hermes", "hermes_acp", "loom_workspace",
    "loom_permission_hook", "loom-workspace", "dream", "local", "umans", "openrouter",
}


def summarize(db_path, *, since=0):
    connection = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        # Group in SQLite so a long-lived database does not become a large
        # Python allocation. Only finite categories cross the output boundary.
        rows = connection.execute("""SELECT harness, profile, policy_decision,
            json_extract(action_json, '$.effect'), json_extract(action_json, '$.boundary'),
            json_extract(action_json, '$.rule_matched'), count(*), min(created_at), max(created_at)
            FROM permission_policy_audits WHERE created_at >= ? GROUP BY 1,2,3,4,5,6""", (since,)).fetchall()
    finally:
        connection.close()
    groups = Counter()
    start, end = None, None
    for harness, profile, decision, effect, boundary, matched, count, first, last in rows:
        harness = harness if harness in _AUDIT_HARNESSES else "unknown"
        profile = profile if profile in {"strict", "balanced", "trusted", "trust_review"} else "unknown"
        decision = decision if decision in {"auto_allow", "ask", "ask_with_warning", "invalid_request"} else "unknown"
        if effect == "unknown" or boundary == "unknown":
            reason = "unrecognized_action_or_boundary"
        elif boundary in {"outside_workspace", "external"}:
            reason = "outside_workspace_or_external"
        elif matched:
            reason = "remembered_rule"
        elif effect == "execute":
            reason = "workspace_command"
        elif effect == "workspace_write":
            reason = "workspace_write"
        elif effect == "read":
            reason = "workspace_read"
        else:
            reason = "other_or_sensitive"
        groups[harness, profile, decision, reason] += count
        start = first if start is None else min(start, first)
        end = last if end is None else max(end, last)
    return {"first_at": start, "last_at": end, "total": sum(groups.values()), "groups": [
        dict(harness=k[0], profile=k[1], decision=k[2], reason=k[3], count=count)
        for k, count in sorted(groups.items())]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--hours", type=float, default=24)
    args = parser.parse_args()
    print(json.dumps(summarize(args.db, since=time.time() - args.hours * 3600), indent=2))
