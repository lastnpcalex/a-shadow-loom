"""Test-only tool transport; a fixture endpoint commits before withholding reply."""
import sys

import httpx
from mcp.server.fastmcp import FastMCP


mcp = FastMCP("fault-fixture")


async def request(operation, nonce):
    async with httpx.AsyncClient(verify=False, timeout=240, trust_env=False) as client:
        response = await client.post(sys.argv[1], json={"operation": operation, "nonce": nonce})
        response.raise_for_status()
        return response.text


@mcp.tool()
async def record_action(nonce: str, operation: str = "record") -> str:
    """Commit the requested synthetic action. Non-idempotent: never repeat blindly."""
    if operation != "record":
        raise ValueError("Invalid operation")
    return await request(operation, nonce)


@mcp.tool()
async def inspect_action(nonce: str, operation: str = "inspect") -> str:
    """Read authoritative status/receipt; no mutation. Reconcile an unknown outcome here."""
    if operation != "inspect":
        raise ValueError("Invalid operation")
    return await request(operation, nonce)


if __name__ == "__main__":
    mcp.run(transport="stdio")
