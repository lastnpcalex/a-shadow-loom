# Workspace-owned EXO extensions

EXO can manage local services without adding a bespoke card or lifecycle route
to the EXO repository. The admin dashboard's **Extensions** tab stores a
reference to a manifest; the manifest and implementation remain in the Git
workspace that owns them.

The external registry defaults to:

- Windows: `%LOCALAPPDATA%\A Shadow Loom\extensions`
- Linux: `$XDG_STATE_HOME/a-shadow-loom/extensions`
- Override: `EXO_STATE_DIR`

No manifest, command, log, PID record, or extension checkout is copied into the
EXO Git tree.

## Manifest

```json
{
  "schema_version": 1,
  "id": "example.service",
  "name": "Example Service",
  "kind": "service",
  "workspace_root": ".",
  "cwd": ".",
  "start": {"command": ["python", "server.py"]},
  "health": {"url": "http://127.0.0.1:8123/health"},
  "update": {"command": ["git", "pull", "--ff-only"]},
  "environment": {"EXAMPLE_MODE": "production"}
}
```

Paths are resolved relative to the manifest. `cwd` must remain inside
`workspace_root`. Commands must be argument arrays, not shell strings. Health
URLs are restricted to loopback HTTP(S), preventing the admin endpoint from
becoming an arbitrary network probe.

Start, Stop, Update, and Unregister are explicit user actions. EXO persists the
PID and process creation time outside the repo, so an admin reload cannot mistake
PID reuse for the registered process. Stop terminates the verified process tree;
Update is refused while it is running.

`kind: "mcp"` currently describes/manages an MCP service but does not
automatically attach it to provider sessions. Provider-neutral MCP attachment is
tracked in [SYSTEM_REVIEW_BACKLOG.md](SYSTEM_REVIEW_BACKLOG.md); until that lands,
the owning harness still needs its normal MCP registration.

## Integration boundary and discovery

The registry manages service processes; registration does not grant an extension
automatic access to CLI agents or attach its MCP tools to them. Keep application
UI, document workflows, and setup logic in the owning workspace, on its own
service origin. Companion files such as `exo.portal.json` are not loaded by EXO.

Inspect the running instance's `/docs` and `/openapi.json` before integrating.
Existing conversation APIs and `/api/conversations/{conv_id}/agent-runs` provide
conversation and durable-agent workflows; these are not an extension-scoped
authorization boundary. An integration must explicitly implement its permitted
models, workspace scope, permissions, cancellation, and result handling. Do not
introduce a separate in-host task runner or edit the EXO checkout as an implicit
part of an extension task; host changes require their own user-authorized scope.
