"""Tests for UI model selection preservation and parallel agent model inheritance."""
import pytest
from pathlib import Path
import database as db
import server

REPO = Path(__file__).resolve().parent.parent


def test_edit_user_message_attaches_cc_settings():
    chat_source = (REPO / "static" / "chat.js").read_text(encoding="utf-8")
    edit_fn_idx = chat_source.find("function editMessage(msgId)")
    assert edit_fn_idx != -1
    edit_post_idx = chat_source.find("/api/conversations/${convId}/messages", edit_fn_idx)
    assert edit_post_idx != -1
    edit_block = chat_source[edit_fn_idx:edit_post_idx]
    assert "_attachCCSettings(editMsgData)" in edit_block


def test_update_inline_cc_controls_does_not_fire_unsolicited_put():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    fn_idx = app_source.find("async function updateInlineCCControls(conv)")
    assert fn_idx != -1
    fn_end = app_source.find("async function _populateWeaveModelDropdown", fn_idx)
    fn_body = app_source[fn_idx:fn_end]
    assert "API.put" not in fn_body, "updateInlineCCControls must not fire unsolicited PUTs that mutate conv.cc_model"
    assert "_selectedCCModelForBranch" in fn_body
    assert "_rememberTabPreferredModel" not in fn_body


def test_check_health_does_not_churn_api_model_dropdowns():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    health_idx = app_source.find("async function checkHealth(")
    assert health_idx != -1
    health_end = app_source.find("async function autoDetectLlamaModel", health_idx)
    health_body = app_source[health_idx:health_end]
    assert "loadedGguf !== _ccLocalModelState.loaded" in health_body


def test_render_cc_model_dropdowns_guards_legacy_alias_matching():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    render_idx = app_source.find("function _renderCCModelDropdowns(")
    assert render_idx != -1
    render_end = app_source.find("function setupEventListeners()", render_idx)
    render_body = app_source[render_idx:render_end]
    assert "candidateRoute.model_id" in render_body
    assert "previousMode === 'codex'" in render_body
    assert "previousMode === 'gemini'" in render_body
    assert "_modelsMatch(opt.value, prev)" not in render_body


def test_branch_model_normalization_and_explicit_preservation():
    app_source = (REPO / "static" / "app.js").read_text(encoding="utf-8")
    branch_fn_idx = app_source.find("function _branchLastUsedCCModel(")
    assert branch_fn_idx != -1
    branch_fn_end = app_source.find("function _selectedCCModelForBranch", branch_fn_idx)
    branch_body = app_source[branch_fn_idx:branch_fn_end]
    assert "claude-(opus|sonnet|haiku)-" in branch_body

    sel_fn_idx = app_source.find("function _selectedCCModelForBranch(")
    sel_fn_end = app_source.find("function _rememberExplicitCCModel", sel_fn_idx)
    sel_body = app_source[sel_fn_idx:sel_fn_end]
    assert "!_explicitCCModel.threadId || !threadId || _explicitCCModel.threadId === threadId" in sel_body


@pytest.mark.asyncio
async def test_child_agent_inherits_generation_parent_model(tmp_database):
    conv = await db.create_conversation("Parallel Agent Conv", mode="claude")
    await db.update_conversation_fields(conv["id"], cc_model="sonnet", parallel_agents_enabled=1)
    conv_id = conv["id"]

    # Generation 1 ran on Gemini
    gen_key = (conv_id, 1, 42)
    server._update_gen_snapshot(
        gen_key,
        parent_id=1,
        cc_model="gemini:gemini-3.8-flash-high",
        harness="gemini",
    )

    parent_info = await server._resolve_parent_run_context(conv_id, "gen:42")
    assert parent_info is not None
    assert parent_info["effective_model"] == "gemini:gemini-3.8-flash-high"
    assert parent_info["effective_harness"] == "gemini"

    # Inheritance context must prefer the generation's model over conv.cc_model ("sonnet")
    ctx = server._agent_inheritance_context(conv, parent_info)
    assert ctx["cc_model"] == "gemini:gemini-3.8-flash-high"
    assert ctx["mode"] == "gemini"

    # Resolved child target must be gemini, not claude/sonnet
    run_req = {"requested_harness": "", "requested_model": "", "model_profile": ""}
    harness, model = server._resolve_agent_target(run_req, ctx)
    assert harness == "gemini"
    assert model == "gemini:gemini-3.8-flash-high"


@pytest.mark.asyncio
async def test_parallel_generation_does_not_clobber_conversation_model(tmp_database):
    conv = await db.create_conversation("Parallel Conv", mode="claude")
    await db.update_conversation_fields(conv["id"], cc_model="sonnet", parallel_agents_enabled=1)
    conv_id = conv["id"]

    # Simulating a generation call on Thread 2 with a different model
    # Should not mutate conv["cc_model"] in the database
    updated_conv = await db.get_conversation(conv_id)
    assert updated_conv["cc_model"] == "sonnet"
    assert updated_conv["mode"] == "claude"
