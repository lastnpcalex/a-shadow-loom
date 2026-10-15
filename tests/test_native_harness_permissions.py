"""Opt-in model-driven parity: only disposable workspaces and an approval listener.

LOOM_RUN_NATIVE_HARNESS_PERMISSIONS=1 enables existing authenticated providers.
No active-host routes, real project files or provider-wide tool grants.
Antigravity additionally requires LOOM_NATIVE_AGY_TRUST_TEST_HOOKS=1: its
normal launch registers the two Loom hooks for this disposable project only
in the native trust store. Existing project entries must remain untouched.
"""
import asyncio
import json
import os
import pytest

import database as db
import server
from tests.test_live_permission_acceptance import _approval_page
from tests.test_permission_transport_acceptance import permission_listener
from tests.permission_acceptance_metrics import observe, unknown_count


@pytest.mark.skipif(os.environ.get("LOOM_RUN_NATIVE_HARNESS_PERMISSIONS") != "1", reason="Opt-in native model usage")
@pytest.mark.parametrize("harness", ["claude", "claude_openrouter", "antigravity"])
async def test_native_model_permission_contract(harness, tmp_path, monkeypatch, record_property):
    from playwright.async_api import async_playwright
    import claude_client
    import gemini_client
    import codex_client

    if harness == "claude" and os.environ.get("LOOM_RUN_NATIVE_CLAUDE_SUBSCRIPTION_TEST") != "1":
        pytest.skip("Direct Anthropic subscription test requires its own explicit opt-in")
    if harness == "antigravity" and os.environ.get("LOOM_NATIVE_AGY_TRUST_TEST_HOOKS") != "1":
        pytest.skip("Antigravity model test requires explicit disposable-project hook trust opt-in")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if harness == "antigravity":
        (workspace / ".git").mkdir()  # anchor agy's workspace/hooks discovery
    for name in ("LOOM_AGENT_RUN_ID", "LOOM_PARENT_RUN_ID", "LOOM_CLIENT_INSTANCE_ID", "LOOM_BACKSTAGE_PARENT_ID"):
        monkeypatch.delenv(name, raising=False)
    pass
    monkeypatch.setattr(codex_client, "_agent_control_mcp_config", lambda *a, **k: None)
    monkeypatch.setattr(server, "_permission_rules", {})
    monkeypatch.setattr(server, "_pending_hook_permissions", {})
    monkeypatch.setattr(server, "_generation_snapshots", {})
    conv = await db.create_conversation("Native parity fixture", mode=harness, project_dir=str(workspace))
    user = await db.add_message(conv["id"], "user", "fixture")
    draft = await db.add_message(conv["id"], "assistant", "fixture", parent_id=user["id"])
    key = (conv["id"], user["id"], 9920)
    server._generation_snapshots[key] = {"parent_id": user["id"], "draft_msg_id": draft["id"]}
    session = None
    async with permission_listener() as port, async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await _approval_page(browser, conv["id"])
        try:
            for index in range(2):
                audit_start = len(await db.get_permission_policy_audits(conv["id"]))
                prompt = ("Isolated permission acceptance fixture. Use your shell/terminal tool exactly once to execute "
                          "this exact command: echo permission-fixture. No files, discovery, planning, other tools, or subagents. "
                          "Execute it now even if a previous turn did so; testing the new invocation is the task. Then finish briefly.")
                proc = events = collector = None
                seen = set()
                observed = []
                cancel = {"claude": claude_client.cancel_claude, "claude_openrouter": claude_client.cancel_claude,
                          "antigravity": gemini_client.cancel_gemini}[harness]
                try:
                    async with asyncio.timeout(150):
                        kwargs = dict(conv_id=conv["id"], cwd=str(workspace), resume_session_id=session,
                                      fork_session=bool(session), permission_scope="gen:9920")
                        if harness.startswith("claude"):
                            router = harness == "claude_openrouter"
                            proc, events = await claude_client.run_claude(prompt, server_port=port,
                                model="z-ai/glm-5.3-flash" if router else "haiku", use_openrouter=router, effort="low", **kwargs)
                        elif harness == "antigravity":
                            proc, events = await gemini_client.run_gemini(prompt, server_port=port, is_first_turn=False,
                                model=os.environ.get("LOOM_NATIVE_AGY_MODEL", "gemini-3.8-flash-high"), **kwargs)
                        async def collect():
                            nonlocal session
                            async for event in events:
                                # Some CLI versions report identity on result,
                                # rather than on their empty initial handshake.
                                if event.get("session_id"):
                                    session = event["session_id"]
                                if event.get("type") in {"text_delta", "result", "cc_synthetic_error", "tool_result"}:
                                    observed.append(event)
                                assert event.get("type") != "error", event.get("error") or event.get("text")
                                assert not event.get("is_error"), json.dumps(observed)
                        collector = asyncio.create_task(collect())
                        while not collector.done():
                            for rid, pending in list(server._pending_hook_permissions.items()):
                                if rid in seen:
                                    continue
                                seen.add(rid)
                                assert index == 0, "Matching continuation must reuse the scoped rule"
                                assert pending["supports_allow_all"], pending["rule_preview"]
                                await page.evaluate("p => showPermissionPrompt(p)", {**pending, "request_id": rid})
                                await page.get_by_role("button", name="Allow for Branch", exact=True).click()
                            await asyncio.sleep(.05)
                        await collector
                finally:
                    if collector is not None:
                        if not collector.done():
                            collector.cancel()
                        await asyncio.gather(collector, return_exceptions=True)
                    if events is not None:
                        await events.aclose()
                    if proc is not None and proc.returncode is None:
                        await cancel(proc)  # exclusively the fixture-owned child
                audits = (await db.get_permission_policy_audits(conv["id"]))[audit_start:]
                observe(record_property, "claude_code" if harness.startswith("claude") else "antigravity",
                        "native_fork_command" if index else "native_first_command",
                        expected={"prompts": 0 if index else 1, "requests": 1, "unknown": 0, "rule_matches": index},
                        observed={"prompts": len(seen), "requests": len(audits),
                                  "unknown": unknown_count(audits),
                                  "rule_matches": sum(bool(a["action"].get("rule_matched")) for a in audits)})
                if index == 0:
                    assert len(seen) == 1 and session and len(server._permission_rules) == 1, json.dumps(observed)
                    continuation = await db.add_message(conv["id"], "user", "fixture continuation", parent_id=draft["id"])
                    server._generation_snapshots[key] = {"parent_id": continuation["id"]}
            audits = await db.get_permission_policy_audits(conv["id"])
            assert any(a["action"].get("rule_matched") for a in audits)
        finally:
            await browser.close()
