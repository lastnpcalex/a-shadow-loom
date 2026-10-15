"""Pair this computer's default browser with the running EXO server.

Asks the local main server for a one-time pairing code (authenticated with the
machine secret, which only this user account can read) and opens the pairing
page with that code filled in. Sessions last up to 90 days (30 days idle).

    C:\\Python314\\python.exe tools\\pair_browser.py [--port 3000] [--no-open]
    C:\\Python314\\python.exe tools\\pair_browser.py --recovery-codes
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loom_auth  # noqa: E402


def request_code(port: int, *, recovery: bool = False) -> tuple[str, dict]:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # local self-signed certificate
    last_error: Exception | None = None
    for scheme in ("https", "http"):
        path = "recovery-codes" if recovery else "pair-code"
        url = f"{scheme}://127.0.0.1:{port}/api/auth/{path}"
        request = urllib.request.Request(url, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json",
                                                  **loom_auth.relay_headers()})
        try:
            with urllib.request.urlopen(request, timeout=5,
                                        context=context if scheme == "https" else None) as response:
                return scheme, json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # try the other scheme
            last_error = exc
    raise SystemExit(f"Could not reach EXO on port {port}: {last_error}. Is the main server running?")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=int(os.environ.get("LOOM_PORT", "3000")))
    parser.add_argument("--no-open", action="store_true", help="print the link instead of opening it")
    parser.add_argument("--recovery-codes", action="store_true",
                        help="replace recovery codes and print five new one-use codes; save them privately")
    args = parser.parse_args()
    scheme, issued = request_code(args.port, recovery=args.recovery_codes)
    if args.recovery_codes:
        print("Save these privately before traveling. Each signs in one browser once.")
        print("Previous recovery codes are now invalid. Network/portal requirements still apply.")
        for code in issued["codes"]:
            print(code)
        return 0
    link = f"{scheme}://localhost:{args.port}/pair#code={issued['code']}"
    print(f"Pairing code: {issued['code']} (expires in {issued['ttl_seconds'] // 60} minutes)")
    print(f"Pairing link: {link}")
    if not args.no_open:
        webbrowser.open(link)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
