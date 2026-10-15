from pathlib import Path


def test_loom_agent_contract_is_role_neutral():
    from loom_agent_prompt import load_loom_agent_prompt

    text = load_loom_agent_prompt()

    assert "inside Loom" in text
    assert "Do not assume the task is software engineering" in text
    assert "Claude Code backed by a local model" in text
    assert "inside the active Loom instance" in text
    assert "Do not stop, restart, kill, replace, or reconfigure either host" in text
    assert "leave that lifecycle action to the human" in text
    assert "files currently on disk are the canonical workspace state" in text
    assert "never use them as a substitute for reading the live file" in text
    assert "Destructive Git operations are allowed only after Loom presents" in text
    assert "normally end in a focused local commit" in text
    assert "A dirty worktree is not a reason" in text
    assert "Stage only the files or hunks attributable to the current task" in text
    assert "Pushing, force-pushing, rebasing shared history" in text
    assert "renders responses as GitHub-flavored Markdown" in text
    assert "loom-agent-control.attach_to_chat" in text
    assert "loom-agent-control.link_file" in text
    assert "Creating, reading or mentioning a filename does not attach it" in text
    assert "filesystem path as literal, copyable text" in text
    assert "loom-agent-control" in text
    assert "provider-native children remain owned by this provider process" in text
    assert "does not automatically inherit this conversation transcript" in text
    assert "call `wait_agent` and then `get_agent`" in text
    assert "use `profile=\"local\"` only when a local model is intentional" in text
    assert "focused code agent" not in text.lower()


def test_claude_ordinary_sessions_append_loom_contract():
    import claude_client

    appended = claude_client._loom_append_system_prompt("Extra provider note")

    assert appended is not None
    assert "inside Loom" in appended
    assert "Extra provider note" in appended


def test_claude_special_roles_do_not_get_generic_contract():
    import claude_client

    assert claude_client._loom_append_system_prompt(
        None, backstage_parent_id=12
    ) is None
    pass


def test_codex_contract_uses_developer_instructions_without_agents_md(tmp_path):
    import codex_client

    instructions = codex_client._codex_developer_instructions("Project rules")

    assert "Project rules" in instructions
    assert "<loom_agent_contract provider=\"codex\">" in instructions
    assert "<user_task>" not in instructions
    assert not (tmp_path / "AGENTS.md").exists()


def test_codex_special_roles_do_not_get_generic_developer_override():
    import codex_client

    assert codex_client._codex_developer_instructions(
        "Project rules", backstage_parent_id=99
    ) is None
    pass


def test_codex_config_read_developer_instructions_are_preserved():
    import codex_client

    response = {"result": {"config": {"developer_instructions": "Project rules"}}}
    assert codex_client._configured_developer_instructions(response) == "Project rules"
    assert codex_client._configured_developer_instructions({"error": {}}) is None


def test_codex_thread_request_uses_additive_instructions_on_start_and_fork():
    import codex_client

    args = ("C:/workspace", "gpt-5.6-sol", "on-request", "workspace-write")
    method, fresh = codex_client._codex_thread_request(
        *args, developer_instructions="Loom contract"
    )
    assert method == "thread/start"
    assert fresh["developerInstructions"] == "Loom contract"
    assert "baseInstructions" not in fresh

    method, forked = codex_client._codex_thread_request(
        *args,
        resume_session_id="thread-1",
        fork_session=True,
        developer_instructions="Loom contract",
    )
    assert method == "thread/fork"
    assert forked["developerInstructions"] == "Loom contract"
    assert "baseInstructions" not in forked


def test_agy_ordinary_prompt_gets_loom_contract_without_gemini_md(tmp_path):
    import gemini_client

    prompt = gemini_client._prepare_agy_prompt("run the script")

    assert "<loom_agent_contract provider=\"agy\">" in prompt
    assert "<user_task>\nrun the script\n</user_task>" in prompt
    assert not (tmp_path / "GEMINI.md").exists()


def test_agy_special_roles_keep_original_prompt():
    import gemini_client

    assert gemini_client._prepare_agy_prompt(
        "edit cards", backstage_parent_id=99
    ) == "edit cards"
    pass
    assert gemini_client._prepare_agy_prompt(
        "resume task", is_first_turn=False
    ) == "resume task"


def test_goose_ordinary_prompt_gets_loom_host_safety_contract():
    import goose_client

    prompt = goose_client._prepare_goose_prompt("inspect the workspace")

    assert '<loom_agent_contract provider="goose">' in prompt
    assert "inside the active Loom instance" in prompt
    assert "leave that lifecycle action to the human" in prompt
    assert "ExoHome casual chat" in prompt
    assert "new blank conversation branch as a fresh start" in prompt
    assert "not a project you must orient in before answering" in prompt
    assert "An empty or excluded history scope is normal on a fresh start" in prompt
    assert goose_client._prepare_goose_prompt(
        "resume task", is_first_turn=False
    ) == "resume task"


def test_dedicated_roles_identify_loom_and_forbid_host_restart():
    repo = Path(__file__).resolve().parent.parent
    backstage = (repo / "backstage.md").read_text(encoding="utf-8")

    for role_prompt in (backstage,):
        assert "active Loom instance" in role_prompt
        assert "restart" in role_prompt
        assert "human operator" in role_prompt




def test_claude_llama_sessions_append_image_warning():
    import claude_client

    normal = claude_client._loom_append_system_prompt("Extra note", use_llama=False)
    llama = claude_client._loom_append_system_prompt("Extra note", use_llama=True)

    assert "Never use file-reading tools" not in normal
    assert "Never use file-reading tools" in llama


def test_claude_session_injects_model_identity():
    import claude_client

    without = claude_client._loom_append_system_prompt("Extra note")
    with_model = claude_client._loom_append_system_prompt(
        "Extra note", use_umans=True, cc_model="umans-glm-5.2"
    )
    llama_model = claude_client._loom_append_system_prompt(
        "Extra note", use_llama=True, cc_model="qwen-27b"
    )
    api_model = claude_client._loom_append_system_prompt(
        "Extra note", cc_model="claude-opus-4-7"
    )

    # No model arg → no identity block (existing tests' default path unchanged)
    assert "Operating model" not in without
    # Umans: identifies provider + flags cache accounting
    assert "Operating model" in with_model
    assert "`umans-glm-5.2`" in with_model
    assert "Umans AI" in with_model
    # Llama: routes to local llama-server
    assert "`qwen-27b`" in llama_model
    assert "local llama-server" in llama_model
    # API: routes to Anthropic
    assert "`claude-opus-4-7`" in api_model
    assert "Anthropic API" in api_model


def test_claude_special_roles_skip_model_identity():
    import claude_client

    pass
    assert claude_client._loom_append_system_prompt(
        None, backstage_parent_id=5, cc_model="umans-glm-5.2"
    ) is None

