"""Trust/review is opt-in; fixtures classify requests but NEVER execute them."""
import asyncio
from itertools import product
import json

import pytest

import database as db
import permission_policy as policy
import permission_rules
from permission_risk import destructive_command_reason
import server
from tests.test_permission_workflows import HARNESSES, wire
from tests.test_permission_enforcement import isolated_permissions, wait_pending
from tests.test_live_permission_acceptance import _answer


@pytest.mark.parametrize('command', [
    'rm -rf generated', 'rm -- generated/*', 'Remove-Item -LiteralPath stale.txt',
    'del /s old', 'rmdir /s old', 'Clear-Content -LiteralPath old.txt', 'format D:',
    'diskpart /s cleanup.txt', 'mkfs.ext4 /dev/fixture', 'robocopy from to /MIR',
    'Copy-Item -Force from.txt to.txt', 'git reset --hard HEAD', 'git clean -fd',
    'git -C fixture reset --hard', 'git -c color.ui=false clean -ffdx',
    'git restore -- source.py', 'git checkout -- source.py', 'git switch --discard-changes main',
    'git stash drop', 'git stash clear', 'git branch -D old',
    'git push --force-with-lease origin main', 'git push origin +main', 'git push origin :old',
    'git push origin --delete old', 'echo ready && git reset --hard',
    'powershell.exe -NoProfile -Command "Remove-Item -Recurse generated"',
    'bash -lc "git reset --hard"', 'cmd.exe /c "rmdir /s generated"',
    'echo ready\nrm -rf generated', 'echo ready\r\nRemove-Item old.txt', 'rm -- --help',
])
def test_known_destructive_operations(command):
    assert destructive_command_reason(command)


@pytest.mark.parametrize('command', [
    'Get-ChildItem -Recurse .', 'tree .', 'ls -R', 'Format-List', 'Format-Table',
    'git status --short', 'git add -- source.py', 'git commit -m "fix git reset --hard behavior"',
    'Write-Output "rm -rf generated"', 'echo ";" git reset --hard',
    'Get-Content -LiteralPath "git reset --hard.txt"', 'git clean -ndf',
    'git push --force --dry-run origin main', 'Remove-Item -WhatIf generated',
    'robocopy from to /MIR /L', 'rm --help', 'node opaque-program.js',
    'python -c "print(123)"', 'powershell -EncodedCommand Zml4dHVyZQ==',
    'Write-Output "first\nrm -rf generated"',
])
def test_general_warnings_descriptions_dry_runs_and_opaque_code_are_not_evidence(command):
    assert not destructive_command_reason(command)


def test_argv_keeps_quoted_description_separate_from_shell_program():
    assert not destructive_command_reason(None, ['echo', 'git reset --hard'])
    assert destructive_command_reason(None, ['powershell.exe', '-NoProfile', '-Command', 'git reset --hard'])
    assert not destructive_command_reason('"unterminated')


def test_exhaustive_profile_contract_and_legacy_nonregression():
    for effect, boundary, valid, warned, matched, destructive in product(
        policy.ActionEffect, policy.ActionBoundary, (False, True), (False, True), (False, True), (False, True)
    ):
        action = policy.NormalizedPermissionAction(effect, boundary, valid=valid, warned=warned,
                                                   rule_matched=matched, destructive=destructive)
        expected = (policy.PolicyDecision.INVALID_REQUEST if not valid else
                    policy.PolicyDecision.ASK_WITH_WARNING if destructive else policy.PolicyDecision.AUTO_ALLOW)
        assert policy.decide(policy.PermissionProfile.TRUST_REVIEW, action) is expected
        # Frozen pre-feature policy: all 768 original combinations keep their
        # old result, even when the new destructive evidence bit is set.
        for profile in (policy.PermissionProfile.STRICT, policy.PermissionProfile.BALANCED, policy.PermissionProfile.TRUSTED):
            ask = policy.PolicyDecision.ASK_WITH_WARNING if warned else policy.PolicyDecision.ASK
            if not valid:
                legacy = policy.PolicyDecision.INVALID_REQUEST
            elif matched:
                legacy = policy.PolicyDecision.AUTO_ALLOW
            elif boundary is not policy.ActionBoundary.WORKSPACE or effect not in {
                policy.ActionEffect.READ, policy.ActionEffect.WORKSPACE_WRITE, policy.ActionEffect.EXECUTE
            }:
                legacy = ask
            elif effect is policy.ActionEffect.READ or (effect is policy.ActionEffect.WORKSPACE_WRITE and profile is not policy.PermissionProfile.STRICT) or (effect is policy.ActionEffect.EXECUTE and profile is policy.PermissionProfile.TRUSTED):
                legacy = policy.PolicyDecision.AUTO_ALLOW
            else:
                legacy = ask
            assert policy.decide(profile, action) is legacy


@pytest.mark.parametrize('name,args,method,expected', [
    ('Edit', {'changes': {'old.txt': {'type': 'delete'}}}, 'item/fileChange/requestApproval', True),
    ('Edit', {'changes': [{'path': 'old.txt', 'kind': {'type': 'delete'}}]}, 'item/fileChange/requestApproval', True),
    ('Edit', {'changes': {'old.txt': {'type': 'update'}}}, 'item/fileChange/requestApproval', False),
    ('delete_file', {'path': 'old.txt'}, '', True),
    ('apply_patch', {'patch': '*** Begin Patch\n*** Delete File: old.txt\n*** End Patch'}, '', True),
    ('Write', {'file_path': 'notes.txt', 'content': '*** Begin Patch\n*** Delete File: old.txt\n*** End Patch'}, '', False),
    ('Read', {'path': 'safe.txt', 'destructive': True}, '', False),
    ('Bash', {'command': 'git reset --hard', 'destructive': False, 'risk_level': 'safe'}, '', True),
    ('Bash', {'command': 'git reset --hard', 'command_argv': ['git', 'status']}, '', True),
    ('Bash', {'command': 'git reset --hard', 'additionalPermissions': {'filesystem': {}}}, '', True),
    ('Bash', {'command': 'git reset --hard', 'networkApprovalContext': {'host': 'example.invalid'}}, 'item/commandExecution/requestApproval', True),
    ('apply_patch', {'command': '*** Begin Patch\r\n*** Delete File: old.txt\r\n*** End Patch'}, '', True),
    ('Unknown', {'title': 'git reset --hard', 'result': {'tool_name': 'delete_file'}}, '', False),
])
def test_only_structural_operation_evidence_counts(tmp_path, name, args, method, expected):
    action = policy.normalize_permission_request(tool_name=name, tool_input=args, approval_method=method, workspace_root=str(tmp_path))
    assert action.destructive is expected
    assert bool(action.destructive_reason) is expected
    assert 'destructive_reason' not in action.audit_dict()


def test_native_argv_display_is_not_reinterpreted_as_a_shell_program(tmp_path):
    from permission_commands import display_argv
    argv = ['echo', ';', 'git', 'reset', '--hard']
    action = policy.normalize_permission_request(tool_name='Bash',
        tool_input={'command': display_argv(argv), 'command_argv': argv}, workspace_root=str(tmp_path))
    assert not action.destructive


@pytest.mark.parametrize('harness', HARNESSES)
async def test_opt_in_profile_across_real_bridge_normalizers(harness, tmp_path, client):
    conv = await db.create_conversation('Trust review fixture', mode='goose', project_dir=str(tmp_path))
    assert conv['permission_profile'] == 'trusted'
    assert (await client.put(f"/api/conversations/{conv['id']}", json={'permission_profile': 'trust_review'})).status_code == 200
    assert (await db.get_conversation(conv['id']))['permission_profile'] == 'trust_review'
    root = await db.add_message(conv['id'], 'user', 'fixture')
    draft = await db.add_message(conv['id'], 'assistant', 'fixture', parent_id=root['id'])
    server._generation_snapshots[(conv['id'], root['id'], 9955)] = {'parent_id': root['id'], 'draft_msg_id': draft['id']}
    async def request(kind, **kwargs):
        return await server.handle_cc_permission({**wire(harness, kind, tmp_path, **kwargs),
            'loom_conv_id': conv['id'], 'permission_scope': 'gen:9955'})
    for kind, kwargs in [('read', {}), ('write', {}), ('edit', {}), ('unknown', {}),
                         ('command', {'command': 'node opaque.js'}),
                         ('command', {'command': 'Get-ChildItem -Recurse .'}),
                         ('read', {'path': str(tmp_path.parent / 'outside.txt')}),
                         ('write', {'path': str(tmp_path.parent / 'outside.txt')})]:
        result = await asyncio.wait_for(request(kind, **kwargs), 2)
        assert result['allow'] and result['decision_source'] == 'loom_policy'
        assert not result['always'] and not server._pending_hook_permissions
    for name in ('WebFetch', 'SensitiveRead', 'external_mutation'):
        result = await server.handle_cc_permission({'loom_conv_id': conv['id'], 'tool_name': name,
            'tool_input': {'url': 'https://example.invalid/fixture'}, 'source': harness})
        assert result['allow'] and result['decision_source'] == 'loom_policy'
    # A matching branch grant created under an older profile cannot override
    # one-time destructive review. This never executes the fixture command.
    args = {'command': 'git reset --hard HEAD', 'cwd': str(tmp_path)}
    action = policy.normalize_permission_request(tool_name='Bash', tool_input=args, workspace_root=str(tmp_path))
    candidate = permission_rules.action_rule(action, args)
    server._permission_rules['old-rule'] = {**candidate, 'id': 'old-rule', 'conversation_id': conv['id'],
        'branch_id': draft['id'], 'workspace': permission_rules.workspace_identity(str(tmp_path))}
    for allow in (True, False):
        task = asyncio.create_task(request('command', command='git reset --hard HEAD'))
        try:
            rid, pending = await wait_pending(task)
            assert pending['risk_level'] == 'destructive'
            assert pending['policy']['action']['destructive'] is True
            assert pending['policy']['action']['rule_matched'] is True
            assert not pending['supports_allow_all'] and pending['rule_candidate'] is None
            assert 'one-time' in pending['rule_preview']
            await _answer(conv['id'], {'request_id': rid, 'allow': allow, 'always': True, 'feedback': 'Synthetic review note'})
            result = await asyncio.wait_for(task, 2)
            assert result['allow'] is allow and result['decision_source'] == 'user'
            assert result['feedback'] == 'Synthetic review note' and not result.get('always', False)
            assert len(server._permission_rules) == 1
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    audits = await db.get_permission_policy_audits(conv['id'])
    assert sum(bool(row['action']['destructive']) for row in audits) == 2
    assert 'git reset' not in json.dumps(audits) and str(tmp_path) not in json.dumps(audits)


async def test_new_profile_does_not_bypass_child_ownership_or_lifecycle(tmp_path):
    conv = await db.create_conversation('Owner fixture', mode='goose', project_dir=str(tmp_path))
    await db.update_conversation_fields(conv['id'], permission_profile='trust_review')
    run = await db.create_agent_run('review-child', conv['id'], 'fixture', project_dir=str(tmp_path))
    await db.update_agent_run(run['run_id'], status='cancelled')
    for owner in ('review-child', 'missing-child'):
        result = await server.handle_cc_permission({'loom_conv_id': conv['id'], 'agent_run_id': owner,
            'tool_name': 'Bash', 'tool_input': {'command': 'node task.js'}})
        assert not result['allow'] and result['decision_source'] == 'loom_lifecycle'


async def test_destructive_review_browser_card_resolves_exact_waiter(tmp_path):
    playwright = pytest.importorskip('playwright.async_api')
    from tests.test_live_permission_acceptance import _approval_page
    conv = await db.create_conversation('Review card fixture', mode='goose', project_dir=str(tmp_path))
    await db.update_conversation_fields(conv['id'], permission_profile='trust_review')
    async with playwright.async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await _approval_page(browser, conv['id'])
            for allow in (True, False):
                task = asyncio.create_task(server.handle_cc_permission({
                    'loom_conv_id': conv['id'], **wire('goose_acp', 'command', tmp_path, command='git reset --hard HEAD')}))
                try:
                    rid, pending = await wait_pending(task)
                    payload = {k: pending[k] for k in ('tool_name', 'input_summary', 'supports_allow_all',
                                                      'rule_preview', 'risk_level', 'risk_reason')}
                    await page.evaluate('p => showPermissionPrompt(p)', {**payload, 'request_id': rid})
                    card = page.locator(f'.permission-prompt[data-request-id="{rid}"]')
                    assert await card.get_by_role('button', name='Allow for Branch', exact=True).count() == 0
                    assert 'one-time' in await card.inner_text()
                    await card.locator('.permission-feedback-input').fill('Synthetic browser review note')
                    action = 'allow' if allow else 'deny'
                    await card.locator(f'[data-perm-action="{action}"]').click()
                    result = await asyncio.wait_for(task, 2)
                    assert result['allow'] is allow and result['decision_source'] == 'user'
                    assert result['feedback'] == 'Synthetic browser review note'
                    assert not server._permission_rules
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        finally:
            await browser.close()
