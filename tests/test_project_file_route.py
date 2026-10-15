"""The project-file route serves images and plan previews only, inside the project."""

import pytest

import database as db


@pytest.fixture
async def project(tmp_path):
    root = tmp_path / "proj"
    (root / ".claude" / "plans").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "shots").mkdir()
    (root / "shots" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "task.md").write_text("# task", encoding="utf-8")
    (root / ".claude" / "plans" / "plan.md").write_text("# plan", encoding="utf-8")
    (root / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    (root / ".git" / "config").write_text("[core]", encoding="utf-8")
    (root / "config.json").write_text('{"key": "x"}', encoding="utf-8")
    (root / "notes.py").write_text("print(1)", encoding="utf-8")
    sibling = tmp_path / "proj-other"
    sibling.mkdir()
    (sibling / "leak.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    conv = await db.create_conversation("File route fixture", mode="claude", project_dir=str(root))
    return conv["id"]


@pytest.mark.parametrize("path", ["shots/a.png", "task.md", ".claude/plans/plan.md"])
async def test_images_and_plan_previews_are_served(client, project, path):
    response = await client.get(f"/api/conversations/{project}/file", params={"path": path})
    assert response.status_code == 200
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", [
    ".env", ".git/config", ".claude/settings.json", "config.json", "notes.py",
    "../proj-other/leak.png",
])
async def test_secrets_code_and_sibling_prefix_are_refused(client, project, path):
    response = await client.get(f"/api/conversations/{project}/file", params={"path": path})
    assert response.status_code == 403
