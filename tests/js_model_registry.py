"""Execute actual frontend classifiers under Node without a browser/host."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parent.parent


def frontend_function(filename, name):
    source = (ROOT / "static" / filename).read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    end = source.index("\n}", start) + 2
    return source[start:end]


def evaluate(expression, data=None, extra=""):
    import model_registry
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for executing frontend classifiers")
    functions = {
        "app.js": ["_isAnthropicValue", "_isOpusValue", "_isCodexValue", "_isUmansValue",
                   "_isGooseValue", "_isOpenRouterValue", "_isOpenRouterModel", "_isDreamEngineModel",
                   "_gooseRouteModelValue", "_supportsEffortValue", "_allowsXhighEffortValue", "_allowsMaxEffortValue"],
        "chat.js": ["_agentKindForModel"],
    }
    script = (ROOT / "static/model-registry.js").read_text(encoding="utf-8")
    script += "\nconst payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));\nEXOModelRegistry.install(payload);\n"
    script += "\n".join(frontend_function(f, name) for f, names in functions.items() for name in names)
    script += "\n" + extra + "\nprocess.stdout.write(JSON.stringify(" + expression + "));"
    result = subprocess.run([node, "-e", script], input=json.dumps(data if data is not None else model_registry.REGISTRY.public()),
                            capture_output=True, text=True, encoding="utf-8", timeout=15, check=True)
    return json.loads(result.stdout)
