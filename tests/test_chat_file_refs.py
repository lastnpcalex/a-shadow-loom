"""File-path link chips and reply-image selection in static/chat.js.

The functions are extracted from the shipped source and run under Node, so the
test exercises exactly what the browser loads.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _function_source(name: str) -> str:
    source = (ROOT / "static" / "chat.js").read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    end = source.index("\n}", start) + 2
    return source[start:end]


def _run(expression: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for chat.js unit checks")
    script = "\n".join([_function_source("localFileRef"), _function_source("replyImageEntries"),
                        f"process.stdout.write(JSON.stringify({expression}));"])
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15, check=True)
    return json.loads(result.stdout)


# Link-target styles observed in real assistant messages (Claude Code, agy,
# Codex, local models), plus web links that must stay links.
@pytest.mark.parametrize("href,expected", [
    ("C:/Users/x/proj/server.py:4265", {"path": "C:/Users/x/proj/server.py", "line": ":4265"}),
    ("file:///G:/2Dto3d/skills/server.py", {"path": "G:/2Dto3d/skills/server.py", "line": ""}),
    ("/mnt/c/Users/x/proj/server.py:4627", {"path": "C:/Users/x/proj/server.py", "line": ":4627"}),
    ("/G:/Work%20Agents/tools/bridge.py", {"path": "G:/Work Agents/tools/bridge.py", "line": ""}),
    ("<C:\\Users\\x\\server.py:9620>", {"path": "C:\\Users\\x\\server.py", "line": ":9620"}),
    ("file://C:\\Prompts\\guide.md#79-80", {"path": "C:\\Prompts\\guide.md", "line": ":79-80"}),
    ("static/chat.js#L12-L20", {"path": "static/chat.js", "line": ":12-20"}),
    ("./haiku-test.md", {"path": "./haiku-test.md", "line": ""}),
    ("https://github.com/org/repo", None),
    ("mailto:a@example.com", None),
    ("#section", None),
    ("README", None),
])
def test_local_file_ref(href, expected):
    assert _run(f"localFileRef({json.dumps(href)})") == expected


def test_reply_images_come_only_from_reply_text_inside_the_project():
    text = ("Saved the screenshot to shots/a.png and `C:\\Work Agents\\proj\\out\\b.png`, "
            "plus [c](C:/Work Agents/proj/c.webp). Outside: C:/Other/d.png and ../x.png. "
            "Listed icon.png twice: icon.png")
    entries = _run(f"replyImageEntries({json.dumps(text)}, 'C:\\\\Work Agents\\\\proj', 5)")
    assert [e["name"] for e in entries] == ["b.png", "c.webp", "a.png", "icon.png"]
    assert entries[0]["url"] == "/api/conversations/5/file?path=out%2Fb.png"


def test_reply_prose_is_never_treated_as_a_path():
    entries = _run("replyImageEntries('I made a nice chart for you in chart.png today', 'C:/p', 1)")
    assert [e["url"] for e in entries] == ["/api/conversations/1/file?path=chart.png"]
