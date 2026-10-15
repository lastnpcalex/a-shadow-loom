"""Canvas sharing uses this device's verified Tailscale DNS identity."""
import json
import re
import shutil
import subprocess
from urllib.parse import urlunsplit


def canvas_tailnet_url(slug: str, scheme: str, port: int | None) -> str:
    executable = shutil.which('tailscale')
    if not executable:
        raise ValueError('Tailscale CLI is unavailable; cannot resolve the Canvas tailnet link')
    try:
        result = subprocess.run([executable, 'status', '--json'], capture_output=True,
                                text=True, timeout=3, check=True)
        hostname = json.loads(result.stdout).get('Self', {}).get('DNSName', '').rstrip('.')
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError) as error:
        raise ValueError('Could not resolve this device’s Tailscale DNS name') from error
    if not re.fullmatch(r'[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+\.ts\.net', hostname):
        raise ValueError('This device has no valid Tailscale DNS name')
    authority = hostname if port is None else f'{hostname}:{port}'
    return urlunsplit((scheme, authority, '/' + slug, '', ''))
