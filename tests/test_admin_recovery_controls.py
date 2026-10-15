import shutil
import subprocess
from pathlib import Path

import pytest


def test_listenerless_instance_offers_recovery_instead_of_start():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute Admin controls")
    source = (Path(__file__).resolve().parents[1] / "static/admin/admin.js").read_text(encoding="utf-8")
    function = source.split("function _instanceActionsHtml(s) {", 1)[1].split("function _instanceDbHtml", 1)[0]
    script = "function _instanceActionsHtml(s) {" + function + """
const assert = require('node:assert/strict');
for (const state of [
    {name: 'main', status: 'stopping', pid: 76052},
    {name: 'main', status: 'offline', pid: 76052},
    {name: 'main', pids: [76052]},
]) {
    const html = _instanceActionsHtml(state);
    assert.ok(html.includes("'restart'"));
    assert.ok(html.includes("'shutdown'"));
    assert.ok(!html.includes("'start'"));
}
assert.ok(_instanceActionsHtml({name: 'main', status: 'offline'}).includes("'start'"));
"""
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
