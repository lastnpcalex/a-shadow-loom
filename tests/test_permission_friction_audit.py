import json
import sqlite3

from tools.permission_friction_audit import summarize


def test_audit_sanitizes_legacy_titles_and_does_not_write(tmp_path):
    path = tmp_path / "audit.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE permission_policy_audits (harness, profile, policy_decision, action_json, tool_name, created_at)")
        connection.execute("INSERT INTO permission_policy_audits VALUES (?, ?, ?, ?, ?, ?)",
            ("goose_acp", "balanced", "ask", json.dumps({"effect": "unknown", "boundary": "unknown"}), "PRIVATE_FIXTURE_TITLE", 20))
        connection.execute("INSERT INTO permission_policy_audits VALUES (?, ?, ?, ?, ?, ?)",
            ("PRIVATE_FIXTURE_HARNESS", "PRIVATE_FIXTURE_PROFILE", "PRIVATE_FIXTURE_RESULT", "{}", "secret", 5))
    before = path.read_bytes()
    result = summarize(path)
    assert result["total"] == 2
    assert "PRIVATE" not in json.dumps(result) and "secret" not in json.dumps(result)
    assert path.read_bytes() == before
    assert summarize(path, since=10)["total"] == 1


def test_audit_preserves_known_bridge_provenance_without_guessing_harness(tmp_path):
    from tools.permission_friction_audit import _AUDIT_HARNESSES
    from permission_policy import NormalizedPermissionAction, ActionEffect, ActionBoundary
    path = tmp_path / 'bridge-audit.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE permission_policy_audits (harness, profile, policy_decision, action_json, created_at)')
        for harness in _AUDIT_HARNESSES:
            action = NormalizedPermissionAction(ActionEffect.EXECUTE, ActionBoundary.WORKSPACE, harness=harness)
            assert action.audit_dict()['harness'] == harness
            connection.execute('INSERT INTO permission_policy_audits VALUES (?, ?, ?, ?, ?)',
                               (harness, 'balanced', 'ask', json.dumps(action.audit_dict()), 30))
    before = path.read_bytes()
    result = summarize(path)
    assert {g['harness'] for g in result['groups']} == _AUDIT_HARNESSES
    assert next(g for g in result['groups'] if g['harness'] == 'loom_permission_hook')['count'] == 1
    assert path.read_bytes() == before


def test_opt_in_review_profile_is_preserved_in_count_only_audit(tmp_path):
    path = tmp_path / 'review-audit.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE permission_policy_audits (harness, profile, policy_decision, action_json, created_at)')
        connection.execute('INSERT INTO permission_policy_audits VALUES (?, ?, ?, ?, ?)',
            ('goose_acp', 'trust_review', 'ask_with_warning',
             json.dumps({'effect': 'execute', 'boundary': 'workspace', 'destructive': True}), 30))
    result = summarize(path)
    assert result['groups'][0]['profile'] == 'trust_review'
