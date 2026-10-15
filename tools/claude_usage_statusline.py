"""Optional Claude statusLine command: save only documented quota fields."""
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exo_state import state_path
from provider_usage import claude_windows, number


def export(data):
    rates = data.get("rate_limits")
    rates = rates if isinstance(rates, dict) else {}
    payload = {"observed_at": time.time(), "rate_limits": {}}
    for key in ("five_hour", "seven_day", "spend_limit"):
        item = rates.get(key)
        if isinstance(item, dict):
            payload["rate_limits"][key] = {field: number(item.get(field)) for field in ("used_percentage", "resets_at")}
    path = state_path("provider-usage", "claude.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            json.dump(payload, stream, allow_nan=False)
        temp.replace(path)
    finally:
        if temp and temp.exists():
            temp.unlink()
    return " | ".join(f"{row['label']}: {row['used_percent']:g}% used" for row in claude_windows(payload)) or "Quota unavailable"


if __name__ == "__main__":
    try:
        data = json.loads(sys.stdin.buffer.read(65536))
        print(export(data))
    except (OSError, ValueError, TypeError, AttributeError):
        print("Quota unavailable")
