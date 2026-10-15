"""Zero-turn diagnostics of the final shared monitor. Never print credentials."""
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from provider_usage import monitor


async def main():
    snapshot = await monitor.snapshot()
    print(json.dumps(snapshot, indent=2))
    return all(provider['status'] == 'ok' for provider in snapshot['providers'])


if __name__ == '__main__':
    sys.exit(0 if asyncio.run(main()) else 1)
