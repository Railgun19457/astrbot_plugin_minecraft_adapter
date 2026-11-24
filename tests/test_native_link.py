import asyncio

import pytest
from astrbot_plugin_minecraft_adapter.link import LinkError
from astrbot_plugin_minecraft_adapter.native_link import (
    NativeEndpoint,
    NativeLink,
    _packet,
    _read_packet,
)


def test_rcon_preserves_multipart_output_and_does_not_resend_after_disconnect():
    async def check():
        commands = []

        async def console(reader, writer):
            try:
                assert await _read_packet(reader) == (1, 3, b"fixture-password")
                writer.write(_packet(1, 0, "") + _packet(1, 2, ""))
                await writer.drain()
                request_id, kind, command = await _read_packet(reader)
                assert (request_id, kind) == (2, 2)
                commands.append(command)
                if command != b"list":
                    return
                writer.write(_packet(2, 0, "第一行\n"))
                await writer.drain()
                assert await _read_packet(reader) == (3, 0, b"")
                writer.write(_packet(2, 0, "第二行") + _packet(3, 0, "end"))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(console, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            link = NativeLink(
                NativeEndpoint("test", "127.0.0.1", 25565, 0, port, "fixture-password")
            )
            result = await link.request("command.run", {"command": "list"})
            assert result == {
                "accepted": True,
                "output": ["第一行", "第二行"],
                "source": "rcon",
            }
            with pytest.raises(LinkError) as error:
                await link.request("command.run", {"command": "time query daytime"})
            assert error.value.code == "command_unknown"
            assert commands == [b"list", b"time query daytime"]

    asyncio.run(check())
