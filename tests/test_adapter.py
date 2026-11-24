import asyncio
import base64
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api import AstrBotConfig
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image
from astrbot.core.platform.manager import PlatformManager
from astrbot.core.utils.metrics import Metric
from astrbot_plugin_minecraft_adapter import main, mc_platform
from astrbot_plugin_minecraft_adapter.cards import AvatarCache, _card_png, server_icon
from astrbot_plugin_minecraft_adapter.link import LinkError
from astrbot_plugin_minecraft_adapter.server_config import ServerEntry
from PIL import Image as PixelImage


def test_platform_lifecycle_and_message_sessions(monkeypatch):
    monkeypatch.setattr(Metric, "upload", AsyncMock())

    async def check():
        queue = asyncio.Queue()
        platform_config = {"id": "minecraft", "type": "minecraft", "enable": True}
        manager = PlatformManager(
            {"platform": [platform_config], "platform_settings": {}}, queue
        )
        delivery = AsyncMock()
        monkeypatch.setattr(
            mc_platform, "runtime", SimpleNamespace(send_game_reply=delivery)
        )
        data = {"uuid": "player-id", "name": "Alex", "text": "hi"}
        try:
            await manager.load_platform(platform_config)
            platform = manager.get_insts()[0]
            platform.publish("survival", data, ai=True)
            private = queue.get_nowait()
            assert private.is_at_or_wake_command
            await private.send(MessageChain().message("hello"))
            delivery.assert_awaited_once_with("survival", "player-id", "hello")

            await manager.reload(platform_config)
            platform = manager.get_insts()[0]
            assert type(platform) is mc_platform.MinecraftPlatform
            assert len(manager.get_insts()) == 1
            for endpoint, ai in (("creative", True), ("survival", False)):
                platform.publish(endpoint, {**data, "public": True}, ai=ai)
                event = queue.get_nowait()
                assert event.is_at_or_wake_command == ai
                assert event.session_id == f"{endpoint}/"
                await platform.send_by_session(
                    event.session, MessageChain().message(endpoint)
                )
                delivery.assert_awaited_with(endpoint, "", endpoint)
        finally:
            await manager.terminate()
        assert manager.get_insts() == []

    asyncio.run(check())


def test_saved_templates_scope_connections_and_queries(tmp_path, monkeypatch):
    async def check():
        schema_path = Path(main.__file__).with_name("_conf_schema.json")
        path = str(tmp_path / "config.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        config = AstrBotConfig(config_path=path, schema=schema)
        assert config["mc_servers"] == []
        assert "enabled" not in schema
        assert schema["text2image"]["default"] is True
        templates = schema["mc_servers"]["templates"]
        assert set(templates) == {"standalone", "velocity", "native"}
        for template in templates.values():
            assert "text2image" not in template["items"]
            assert template["items"]["enabled"]["default"]
            assert "role" not in template["items"]["server"]["items"]
        assert templates["native"]["items"]["server"]["items"]["rcon_password"][
            "secret"
        ]
        config["enabled"] = False
        session = "onebot:GroupMessage:1"
        config["mc_servers"] = [
            {
                "__template_key": template,
                "server": {"server_id": node, "url": f"ws://localhost:{port}/astrbot"},
                "message": {"target_sessions": [session]},
            }
            for node, port, template in [
                ("survival", 8765, "standalone"),
                ("creative", 8766, "velocity"),
            ]
        ] + [
            {
                "__template_key": "native",
                "server": {
                    "server_id": "vanilla",
                    "host": "127.0.0.1",
                    "query_port": 25566,
                },
                "message": {"target_sessions": [session]},
            },
            {
                "__template_key": "standalone",
                "enabled": False,
                "server": {
                    "server_id": "disabled",
                    "url": "ws://localhost:8767/astrbot",
                },
            },
        ]
        config.save_config()

        started = []
        stopped = []

        async def serve(link):
            started.append(link.endpoint.id)
            try:
                await asyncio.Future()
            finally:
                stopped.append(link.endpoint.id)

        request = AsyncMock(return_value={"online": 2})
        monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _: tmp_path)
        monkeypatch.setattr(main.Link, "serve", serve)
        monkeypatch.setattr(main.Link, "request", request)
        monkeypatch.setattr(main.NativeLink, "serve", serve)
        config = AstrBotConfig(config_path=path, schema=schema)
        plugin = main.MinecraftAdapter(SimpleNamespace(), config)
        event = SimpleNamespace(
            get_platform_name=lambda: "aiocqhttp",
            unified_msg_origin=session,
            get_sender_id=lambda: "user",
            get_self_id=lambda: "bot",
            get_message_str=lambda: "*hello",
        )
        await plugin.initialize()
        try:
            await asyncio.sleep(0)
            assert started == ["survival", "creative", "vanilla"]
            assert [entry.endpoint.role for entry in plugin.entries.values()] == [
                "standalone",
                "velocity",
                "native",
            ]
            assert plugin.entries["vanilla"].endpoint.query_port == 25566
            assert main.ForwardingFilter().filter(event, config)
            event.get_message_str = lambda: "normal chat"
            assert not main.ForwardingFilter().filter(event, config)
            assert json.loads(await plugin.status_tool(event, "@survival")) == {
                "online": 2
            }
            request.assert_awaited_once_with("server.status", None)
            with pytest.raises(LinkError, match="@survival、@creative、@vanilla"):
                await plugin.select(event, "")
            event.unified_msg_origin = "onebot:GroupMessage:2"
            event.get_message_str = lambda: "*hello"
            assert not main.ForwardingFilter().filter(event, config)
            assert (
                json.loads(await plugin.status_tool(event, "@survival"))["error"]
                == "unknown_target"
            )
        finally:
            await plugin.terminate()
        assert stopped == started
        assert mc_platform.runtime is None

    asyncio.run(check())


@pytest.mark.parametrize(
    ("command", "arguments", "operation", "data", "fallback"),
    [
        (
            "status",
            (),
            "server.status",
            {
                "name": "生存服",
                "platform": "Paper",
                "minecraft": "1.21.11",
                "online": 12,
                "capacity": 20,
                "tps": 19.8,
                "mspt": 8.2,
                "memory_percent": 37.54,
                "uptime_seconds": 2 * 86400 + 3 * 3600 + 24 * 60 + 11,
            },
            "内存：37.5%\n已运行：2 天 3 小时 24 分钟",
        ),
        (
            "status",
            (),
            "server.status",
            {
                "name": "Folia",
                "platform": "Folia",
                "minecraft": "1.21.11",
                "online": 0,
                "capacity": 0,
                "memory_mb": 1024,
                "uptime_seconds": 15,
            },
            "内存：—\n已运行：0 分钟",
        ),
        ("players", (), "player.list", {"total": 0, "players": []}, "玩家：暂无"),
        (
            "players",
            (),
            "player.list",
            {
                "total": 2,
                "players": [{"name": "Alex", "ping": 42}, {"name": "Sam", "ping": 0}],
            },
            "Alex（42 ms）、Sam（0 ms）",
        ),
        (
            "players",
            (),
            "player.list",
            {
                "total": 32,
                "players": [
                    {"name": "WWWWWWWWWWWWWWWW"},
                    *({"name": f"Player_{index:02}"} for index in range(1, 32)),
                ],
            },
            "另有 2 人",
        ),
        (
            "player",
            ("Alex",),
            "player.detail",
            {
                "name": "Alex",
                "world": "world_nether",
                "game_mode": "SURVIVAL",
                "health": 18.0,
                "ping": 42,
                "position": [-348.5, 68.0, 1245.3],
            },
            "坐标：-348.5 / 68.0 / 1245.3",
        ),
    ],
)
def test_queries_render_images_and_keep_plain_text(
    command, arguments, operation, data, fallback, monkeypatch
):
    async def check():
        session = "onebot:GroupMessage:1"
        config = {
            "__template_key": "standalone",
            "server": {"server_id": "survival", "url": "ws://127.0.0.1:8765/astrbot"},
            "message": {"target_sessions": [session]},
        }
        plugin = main.MinecraftAdapter(SimpleNamespace(), {})
        plugin.entries["survival"] = ServerEntry.parse(config)
        request = AsyncMock(return_value=data)
        plugin.links["survival"] = SimpleNamespace(request=request)
        event = SimpleNamespace(
            get_platform_name=lambda: "aiocqhttp",
            unified_msg_origin=session,
            chain_result=lambda chain: chain,
            plain_result=lambda text: text,
        )
        handler = getattr(plugin, command)

        result = [reply async for reply in handler(event, *arguments)]
        assert request.await_args.args[0] == operation
        assert request.await_args.args[1]["include_assets"] is True
        if command == "players":
            assert request.await_args.args[1]["include_ping"] is True
        assert isinstance(result[0][0], Image)
        assert result[0][0].file.startswith("base64://iVBORw0KGgo")

        render = AsyncMock(return_value=None)
        monkeypatch.setattr(main, "render_card", render)
        result = [reply async for reply in handler(event, *arguments)]
        assert fallback in result[0]
        render.assert_awaited_once()

        render.reset_mock()
        plugin.config["text2image"] = False
        result = [reply async for reply in handler(event, *arguments)]
        assert fallback in result[0]
        assert not (request.await_args.args[1] or {}).get("include_assets")
        render.assert_not_awaited()

    asyncio.run(check())


def test_minecraft_icon_and_skin_face():
    skin = PixelImage.new("RGBA", (64, 64))
    skin.paste("#3456A0", (8, 8, 16, 16))
    skin.putpixel((40, 8), (220, 30, 10, 255))
    buffer = BytesIO()
    skin.save(buffer, "PNG")
    source = buffer.getvalue()
    avatar = _card_png(source, skin=True)
    assert avatar is not None
    with PixelImage.open(BytesIO(bytes.fromhex(avatar))) as rendered:
        assert rendered.size == (64, 64)
        assert rendered.getpixel((0, 0)) == (220, 30, 10, 255)
        assert rendered.getpixel((63, 63)) == (52, 86, 160, 255)
    assert server_icon("https://example.org/icon.png") is None
    assert server_icon("data:image/png;base64,broken") is None
    assert (
        server_icon("data:image/png;base64," + base64.b64encode(source).decode())
        is not None
    )


def test_avatar_cache_reuses_hashes_and_rejects_urls(monkeypatch):
    async def check():
        cache = AvatarCache()
        download = AsyncMock(return_value="89ab")
        monkeypatch.setattr(cache, "_download", download)
        texture = "a" * 64
        try:
            assert await cache.avatars(
                [texture, None, texture, f"https://example.org/{texture}"]
            ) == [
                "89ab",
                None,
                "89ab",
                None,
            ]
            assert await cache.avatars([texture]) == ["89ab"]
            download.assert_awaited_once()
            assert download.await_args.args[1] == texture
        finally:
            await cache.close()

    asyncio.run(check())


def test_velocity_scope_preserves_child_and_separates_game_sessions():
    async def check():
        session = "onebot:GroupMessage:1"
        context = SimpleNamespace()
        plugin = main.MinecraftAdapter(context, {})
        for node in ("network", "second"):
            plugin.entries[node] = ServerEntry.parse(
                {
                    "__template_key": "velocity",
                    "server": {
                        "server_id": node,
                        "url": "ws://127.0.0.1:8765/astrbot",
                    },
                    "message": {"target_sessions": [session]},
                }
            )

        async def lookup(operation, data=None):
            if operation == "server.list":
                return {"servers": ["lobby", "build"]}
            return {"total": 0, "players": []}

        request = AsyncMock(side_effect=lookup)
        plugin.links["network"] = SimpleNamespace(request=request)
        plugin.links["second"] = SimpleNamespace(request=AsyncMock(side_effect=lookup))
        event = SimpleNamespace(
            get_platform_name=lambda: "aiocqhttp", unified_msg_origin=session
        )
        await plugin.players_tool(event, "@network/lobby")
        request.assert_awaited_once_with("player.list", {"target": "lobby"})

        assert (
            json.loads(await plugin.players_tool(event, "@lobby"))["error"]
            == "ambiguous_target"
        )
        await plugin.players_tool(event, "@network/extra")
        request.assert_awaited_with("player.list", {"target": "extra"})
        await plugin.players_tool(event, "@network")
        request.assert_awaited_with("player.list", None)

        queue = asyncio.Queue()
        platform = mc_platform.MinecraftPlatform({"id": "minecraft"}, {}, queue)
        data = {"name": "Alex", "uuid": "u", "text": "hello"}
        platform.publish("network/lobby", data, ai=True)
        game = queue.get_nowait()
        assert game.session_id == "network/lobby/u"
        assert await plugin.select(game, "") == (plugin.entries["network"], "lobby")
        with pytest.raises(LinkError, match="无法访问"):
            await plugin.select(game, "@network/build")

        await plugin.send_game_reply("network/lobby", "u", "hello")
        request.assert_awaited_with(
            "chat.send",
            {"kind": "ai", "player": "u", "text": "hello", "target": "lobby"},
        )

    asyncio.run(check())
