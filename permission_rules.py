"""Session-local, inspectable branch grants. No command execution or fuzzy classifier.

The finite policy proves the conjunction; these functions construct its inputs.
Executable, subcommand, option names and non-path arguments remain literal.
Only explicit workspace file operands may vary. Opaque shell programs ask once.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from permission_commands import command_template

from permission_policy import (
    ActionBoundary, ActionEffect, NormalizedPermissionAction,
    RuleMatchContext, _first_value, _iter_mappings, _path_within, rule_applies,
)


def workspace_identity(root: str) -> str:
    try:
        return os.path.normcase(str(Path(root).resolve())) if root else ""
    except (OSError, ValueError, RuntimeError):
        return ""


def action_rule(action: NormalizedPermissionAction, tool_input) -> dict | None:
    """Return an honest approval preview, or no remembered-rule option."""
    if not action.valid or action.boundary is not ActionBoundary.WORKSPACE:
        return None
    if action.effect in {ActionEffect.READ, ActionEffect.WORKSPACE_WRITE}:
        return {
            "shape": [action.effect.value],
            "description": f"{action.effect.value.replace('_', ' ')} actions on paths inside this workspace",
        }
    if action.effect is ActionEffect.EXECUTE and action.tool_name.lower() in {"task", "agent", "explore", "codebase_investigator"}:
        return {"shape": ["delegate"], "description": "delegate native child tasks; their tool approvals remain separate"}
    if action.effect is not ActionEffect.EXECUTE or not action.command_family:
        return None
    command = action.command_family
    parsed = command_template(command, _first_value(tool_input, ("command_argv",)))
    if parsed is None:
        return None
    tokens, wrapper = parsed
    executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
    cwd = str(_first_value(tool_input, ("cwd", "workdir", "working_directory")) or action.workspace_root)
    if not Path(cwd).is_absolute():
        cwd = str(Path(action.workspace_root) / cwd)
    cwd = workspace_identity(cwd)
    if _path_within(cwd, action.workspace_root) is not True:
        return None
    # Freeze the runner and its entry point. For module runners also freeze
    # the module name. Options/verbs/IDs stay literal at every position.
    frozen = 1 if executable in {"pytest", "rg"} else 2
    if len(tokens) > 2 and tokens[1] == "-m":
        frozen = 3
    shape = []
    for index, token in enumerate(tokens):
        # Do not guess that a slash/domain is a file: git branch names, URLs,
        # regexes and script arguments also contain slashes. Only normalize
        # operands of these known path-taking command forms.
        pytest_operand = (executable == "pytest" or
                          (executable.startswith("python") and tokens[1:3] == ["-m", "pytest"]))
        git_operand = (executable == "git" and len(tokens) > 2
                       and tokens[1] in {"add", "diff", "status"}
                       and "--" in tokens[2:index])
        path_like = "://" not in token and bool(re.search(r"[\\/]|\.[A-Za-z][A-Za-z0-9]{0,7}$", token))
        literal_path_operand = (executable in {
            "get-content", "get-item", "get-childitem", "test-path", "get-filehash",
            "set-content", "add-content",
        } and index > 0 and tokens[index - 1].lower() == "-literalpath")
        eligible = git_operand or literal_path_operand or (pytest_operand and index >= frozen and not tokens[index - 1].startswith("-"))
        if eligible and not token.startswith("-") and path_like:
            target = Path(token)
            if not target.is_absolute():
                target = Path(cwd) / target
            if _path_within(str(target), action.workspace_root) is not True:
                return None
            shape.append({"workspace_file": True})
        else:
            shape.append({"literal": token})
    label = " ".join(t.get("literal", "<workspace-file>") for t in shape)
    if wrapper:
        label = " ".join(wrapper) + " [" + label + "]"
    qualifiers = {}
    for item in _iter_mappings(tool_input):
        for key in ("env", "environment", "env_vars", "shell", "sandbox_permissions", "user"):
            if key in item:
                qualifiers.setdefault(key, []).append(item[key])
    return {
        "shape": ["execute", cwd, shape, qualifiers, wrapper],
        "description": f"{label} (same arguments/options, environment and working directory; only <workspace-file> operands vary)",
    }


def matches(rule: dict, *, conversation_id: int, workspace: str,
            ancestor_ids: set[int], candidate: dict | None) -> bool:
    return rule_applies(RuleMatchContext(
        same_workspace=bool(workspace) and rule.get("workspace") == workspace,
        branch_descendant=(rule.get("conversation_id") == conversation_id
                           and rule.get("branch_id") in ancestor_ids),
        shape_matches=bool(candidate) and rule.get("shape") == candidate.get("shape"),
    ))


def unavailable_reason(action, *, has_branch=True):
    if action.effect is ActionEffect.UNKNOWN or action.boundary is ActionBoundary.UNKNOWN:
        return "Allow for Branch unavailable: the harness did not supply a recognized action and destination. One-time approval only."
    if action.boundary is not ActionBoundary.WORKSPACE:
        return "Allow for Branch unavailable: this request crosses the current workspace boundary. One-time approval only."
    if not has_branch:
        return "Allow for Branch unavailable: no exact owning branch could be established. One-time approval only."
    return "Allow for Branch unavailable: this command cannot be represented by a scoped literal template (for example, a pipeline or script). One-time approval only."


def public_rule(rule: dict) -> dict:
    # Rules are deliberately visible to the human, but never put command
    # arguments/paths into the sanitized policy audit table or server logs.
    return {key: rule[key] for key in
            ("id", "branch_id", "workspace", "description")}
