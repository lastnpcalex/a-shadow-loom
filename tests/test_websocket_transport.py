"""Real SansIO frames, keepalive and size limits on a test-owned listener."""

import asyncio
import base64
import os
import socket
import struct

import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError


@pytest.fixture
async def transport(request):
    async def echo(scope, receive, send):
        assert scope["type"] == "websocket"
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        while True:
            event = await receive()
            if event["type"] == "websocket.disconnect":
                return
            await send({"type": "websocket.send", **{
                key: event[key] for key in ("text", "bytes") if key in event
            }})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.setblocking(False)
    port = sock.getsockname()[1]
    settings = getattr(request, "param", {})
    listener = uvicorn.Server(uvicorn.Config(
        echo, ws="websockets-sansio", lifespan="off", log_level="error",
        ws_ping_interval=0.05, ws_ping_timeout=0.2,
        timeout_graceful_shutdown=2, **settings,
    ))
    task = asyncio.create_task(listener.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not listener.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        yield port
    finally:
        listener.should_exit = True  # only the listener created above
        await asyncio.wait_for(task, timeout=5)
        sock.close()


async def test_fragmented_text_binary_and_compression_survive_keepalive(transport):
    async with connect(f"ws://127.0.0.1:{transport}/echo") as client:
        assert client.protocol.extensions  # default deflate support is retained
        await client.send(["first ", "second ", "third"])
        assert await client.recv() == "first second third"
        payload = bytes(range(256)) * 4096
        await client.send(payload)
        assert await client.recv() == payload
        # The client automatically answers pings. Staying connected longer than
        # the server's pong deadline exercises successful keepalive, too.
        await asyncio.sleep(0.5)
        await client.send("still connected")
        assert await client.recv() == "still connected"


async def _frame(reader):
    header = await reader.readexactly(2)
    assert not header[1] & 0x80  # server frames must be unmasked
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    return header[0] & 0x0F, await reader.readexactly(length)


async def test_unanswered_server_ping_closes_with_timeout(transport):
    reader, writer = await asyncio.open_connection("127.0.0.1", transport)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write((
            "GET /echo HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{transport}\r\n"
            "Connection: Upgrade\r\nUpgrade: websocket\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        await writer.drain()
        async with asyncio.timeout(3):
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 101 ")
            opcode, payload = await _frame(reader)
            assert opcode == 0x9 and payload  # server initiated ping
            # This raw peer deliberately never sends a pong.
            opcode, payload = await _frame(reader)
            assert opcode == 0x8
            assert struct.unpack("!H", payload[:2])[0] == 1011
            assert payload[2:] == b"keepalive ping timeout"
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize("transport", [{"ws_max_size": 1024}], indirect=True)
async def test_oversized_message_is_rejected(transport):
    async with connect(f"ws://127.0.0.1:{transport}/echo", compression=None) as client:
        await client.send(b"x" * 2048)
        with pytest.raises(ConnectionClosedError) as closed:
            await client.recv()
        assert closed.value.rcvd.code == 1009
