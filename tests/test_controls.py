import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.api import AstrBotConfig
from astrbot_plugin_minecraft_adapter import main
from astrbot_plugin_minecraft_adapter.link import LinkError
from astrbot_plugin_minecraft_adapter.server_config import ServerEntry

UMO = "onebot:GroupMessage:1"


def session():
    return SimpleNamespace(
        unified_msg_origin=UMO,
        get_platform_name=lambda: "aiocqhttp",
        get_sender_id=lambda: "member",
        get_sender_name=lambda: "Member",
        get_self_id=lambda: "bot",
        get_message_str=lambda: "*hello",
        is_admin=lambda: False,
        plain_result=lambda text: text,
        message_obj=SimpleNamespace(message_id="123"),
        bot=SimpleNamespace(call_action=AsyncMock()),
    )


def entry(name="survival", *, commands=None, **message):
    return ServerEntry.parse(
        {
            "__template_key": "standalone",
            "server": {"server_id": name, "url": "ws://127.0.0.1:8765/astrbot"},
            "message": {"target_sessions": [UMO], **message},
            "cmd": commands or {},
        }
    )


def test_manual_model_and_admin_command_lists():
    async def check():
        config = {
            "llm_tools": {
                "enabled_tools": ["command"],
                "cmd_list": ["time", "whitelist"],
            }
        }
        plugin = main.MinecraftAdapter(SimpleNamespace(), config)
        rules = {
            "enabled": True,
            "cmd_list": ["list", "whitelist"],
            "admin_commands": ["whitelist"],
        }
        plugin.entries["survival"] = entry(commands=rules)
        rpc = AsyncMock(return_value={"accepted": True, "output": ["ok"]})
        plugin.links["survival"] = SimpleNamespace(request=rpc)
        event = session()
        assert [reply async for reply in plugin.command(event, "list")] == ["ok"]
        assert "允许执行" in await anext(plugin.command(event, "time query day"))
        assert "仅管理员" in await anext(
            plugin.command(event, "minecraft:whitelist list")
        )
        event.is_admin = Mock(
            side_effect=AssertionError(
                "Model must not inspect the caller's administrator role"
            )
        )
        assert json.loads(await plugin.command_tool(event, "whitelist list"))[
            "accepted"
        ]
        assert (
            json.loads(await plugin.command_tool(event, "list"))["error"]
            == "command_denied"
        )
        assert json.loads(await plugin.command_tool(event, "time query day"))[
            "accepted"
        ]

        event.is_admin = lambda: True
        assert await anext(plugin.command(event, "whitelist list")) == "ok"
        event.is_admin = lambda: False
        plugin.entries["survival"] = entry(commands={**rules, "admin_umos": [UMO]})
        assert await anext(plugin.command(event, "whitelist list")) == "ok"

        plugin.entries["survival"] = entry(
            commands={
                "enabled": True,
                "cmd_white_black_list": "black",
                "cmd_list": ["stop"],
            }
        )
        rpc.reset_mock()
        assert "允许执行" in await anext(plugin.command(event, "/minecraft:stop"))
        rpc.assert_not_awaited()
        assert await anext(plugin.command(event, "time query day")) == "ok"

        rpc.side_effect = LinkError("timeout", "timeout")
        assert (
            json.loads(await plugin.command_tool(event, "time query day"))["error"]
            == "command_unknown"
        )
        event.unified_msg_origin = "onebot:GroupMessage:2"
        assert (
            json.loads(await plugin.command_tool(event, "time query day"))["error"]
            == "no_target"
        )

    asyncio.run(check())


def test_custom_command_validates_placeholders_for_all_members():
    async def check():
        plugin = main.MinecraftAdapter(SimpleNamespace(), {})
        plugin.entries["survival"] = entry(
            commands={
                "enabled": True,
                "custom_commands": [
                    {
                        "id": "reward",
                        "command": "give {sender} {item} {count}",
                        "parameters": [
                            {
                                "name": "item",
                                "__template_key": "choice",
                                "choices": ["minecraft:apple"],
                            },
                            {
                                "name": "count",
                                "__template_key": "integer",
                                "min": 1,
                                "max": 2,
                            },
                        ],
                    }
                ],
            }
        )
        rpc = AsyncMock(return_value={"accepted": True, "output": ["ok"]})
        plugin.links["survival"] = SimpleNamespace(request=rpc)
        event = session()
        assert "/mc action @survival reward" in await anext(plugin.list_actions(event))
        assert "游戏账号" in await anext(
            plugin.action(event, "reward item=minecraft:apple count=1")
        )
        key = plugin.binding_key("survival", UMO, "member")
        plugin.bindings[key] = {"name": "Alex"}
        assert "整数" in await anext(
            plugin.action(event, "reward item=minecraft:apple count=9")
        )
        rpc.assert_not_awaited()
        assert (
            await anext(plugin.action(event, "reward item=minecraft:apple count=2"))
            == "ok"
        )
        rpc.assert_awaited_once_with(
            "command.run", {"command": "give Alex minecraft:apple 2"}
        )

    asyncio.run(check())


def test_saved_shortcut_list_accepts_positional_arguments_and_embedded_placeholders(
    tmp_path,
):
    schema_path = Path(main.__file__).with_name("_conf_schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    server_fields = schema["mc_servers"]["templates"]["standalone"]["items"]
    assert server_fields["cmd"]["items"]["custom_commands"]["type"] == "list"

    path = str(tmp_path / "config.json")
    config = AstrBotConfig(config_path=path, schema=schema)
    head = "head <&player&><<>>give {sender} minecraft:player_head[minecraft:profile='<&player&>'] 1"
    warp = "传送 <&x&> <&y&> <&z&><<>>tp {sender} {x} <&y&> {z}"
    config["mc_servers"] = [
        {
            "__template_key": "standalone",
            "server": {"server_id": "survival", "url": "ws://127.0.0.1:8765/astrbot"},
            "message": {"target_sessions": [UMO]},
            "cmd": {"enabled": True, "custom_commands": [head, warp]},
        }
    ]
    config.save_config()
    saved = AstrBotConfig(config_path=path, schema=schema)["mc_servers"][0]
    assert saved["cmd"]["custom_commands"] == [head, warp]

    async def check():
        plugin = main.MinecraftAdapter(SimpleNamespace(), {})
        first = ServerEntry.parse(saved)
        second = entry(
            "creative", commands={"enabled": True, "custom_commands": [head]}
        )
        plugin.entries.update(survival=first, creative=second)
        request = AsyncMock(return_value={"accepted": True, "output": ["ok"]})
        other = AsyncMock()
        plugin.links.update(
            survival=SimpleNamespace(request=request),
            creative=SimpleNamespace(request=other),
        )
        event = session()
        event.get_message_str = lambda: "head"
        usage = await anext(plugin.on_message(event))
        assert "自定义指令用法" in usage and "head <&player&>" in usage
        event.get_message_str = lambda: "HEAD   Steve"
        ambiguous = await anext(plugin.on_message(event))
        assert (
            "@survival HEAD   Steve" in ambiguous
            and "@creative HEAD   Steve" in ambiguous
        )
        request.assert_not_awaited()
        other.assert_not_awaited()
        event.get_message_str = lambda: "@survival head Steve"
        assert "验证游戏账号" in await anext(plugin.on_message(event))
        plugin.bindings[plugin.binding_key("survival", UMO, "member")] = {
            "name": "Alex"
        }
        assert await anext(plugin.on_message(event)) == "[survival] ok"
        request.assert_awaited_once_with(
            "command.run",
            {
                "command": "give Alex minecraft:player_head[minecraft:profile='Steve'] 1",
            },
        )
        event.get_message_str = lambda: "传送 ~1.5 65 -6.5"
        await anext(plugin.on_message(event))
        request.assert_awaited_with("command.run", {"command": "tp Alex ~1.5 65 -6.5"})
        request.reset_mock()
        event.get_message_str = lambda: "@survival head Steve']"
        assert "需要一个参数" in await anext(plugin.on_message(event))
        request.assert_not_awaited()
        other.assert_not_awaited()

    asyncio.run(check())


def test_structured_action_can_also_use_a_parameterized_trigger():
    command = entry(
        commands={
            "custom_commands": [
                {
                    "id": "clock",
                    "trigger": "时间",
                    "command": "time query {clock}",
                    "parameters": [
                        {
                            "name": "clock",
                            "__template_key": "choice",
                            "choices": ["daytime"],
                        }
                    ],
                }
            ]
        }
    ).custom_commands["clock"]
    values = command.match(["时间", "daytime"])
    assert command.render(command.validate(values)) == "time query daytime"


def test_server_number_replays_once_for_requesting_sender():
    async def check():
        plugin = main.MinecraftAdapter(SimpleNamespace(), {})
        rules = {"enabled": True, "cmd_list": ["list"]}
        plugin.entries.update(
            survival=entry(commands=rules), creative=entry("creative", commands=rules)
        )
        survival = AsyncMock()
        creative = AsyncMock(return_value={"accepted": True, "output": ["ok"]})
        plugin.links.update(
            survival=SimpleNamespace(request=survival),
            creative=SimpleNamespace(request=creative),
        )
        event = session()
        prompt = await anext(plugin.command(event, "list"))
        assert "1. @survival" in prompt and "2. @creative" in prompt
        stranger = session()
        stranger.get_sender_id = lambda: "stranger"
        stranger.get_message_str = lambda: "2"
        assert [reply async for reply in plugin.on_message(stranger)] == []
        creative.assert_not_awaited()
        event.get_message_str = lambda: "2"
        assert [reply async for reply in plugin.on_message(event)] == ["ok"]
        creative.assert_awaited_once_with("command.run", {"command": "list"})
        survival.assert_not_awaited()
        assert [reply async for reply in plugin.on_message(event)] == []

    asyncio.run(check())


def test_velocity_backend_command_routes_without_model_admin_check():
    async def check():
        plugin = main.MinecraftAdapter(
            SimpleNamespace(),
            {"llm_tools": {"enabled_tools": ["command"], "cmd_list": ["list"]}},
        )
        plugin.entries["network"] = ServerEntry.parse(
            {
                "__template_key": "velocity",
                "server": {
                    "server_id": "network",
                    "url": "ws://127.0.0.1:8765/astrbot",
                },
                "message": {"target_sessions": [UMO]},
                "cmd": {
                    "enabled": True,
                    "admin_commands": ["list"],
                    "custom_commands": ["time-now<<>>time query daytime"],
                },
            }
        )
        request = AsyncMock(return_value={"accepted": True, "output": ["ok"]})
        plugin.links["network"] = SimpleNamespace(request=request)
        event = session()
        assert "仅管理员" in await anext(plugin.command(event, "@network/lobby list"))
        request.assert_not_awaited()
        assert json.loads(await plugin.command_tool(event, "list", "@network/lobby"))[
            "accepted"
        ]
        request.assert_awaited_once_with(
            "command.run", {"target": "lobby", "command": "list"}
        )
        event.get_message_str = lambda: "@network/lobby time-now"
        assert await anext(plugin.on_message(event)) == "[network] ok"
        request.assert_awaited_with(
            "command.run", {"target": "lobby", "command": "time query daytime"}
        )

    asyncio.run(check())


def test_tool_registration_is_opt_in_and_uses_the_real_event(tmp_path, monkeypatch):
    async def check():
        registry = SimpleNamespace(func_list=[])
        registry.remove_func = Mock(
            side_effect=lambda name: registry.func_list.__setitem__(
                slice(None), [tool for tool in registry.func_list if tool.name != name]
            )
        )
        context = SimpleNamespace(
            add_llm_tools=lambda *tools: registry.func_list.extend(tools),
            provider_manager=SimpleNamespace(llm_tools=registry),
        )
        monkeypatch.setattr(main.StarTools, "get_data_dir", lambda _: tmp_path)
        plugin = main.MinecraftAdapter(
            context, {"llm_tools": {"enabled_tools": ["targets", "status"]}}
        )
        await plugin.initialize()
        try:
            assert [tool.name for tool in registry.func_list] == [
                "minecraft_targets",
                "minecraft_status",
                "minecraft_players",
            ]
            tool_context = SimpleNamespace(context=SimpleNamespace(event=session()))
            assert json.loads(await registry.func_list[0].call(tool_context)) == {
                "targets": []
            }
        finally:
            await plugin.terminate()
        assert not registry.func_list

    asyncio.run(check())


def test_chat_format_and_success_reaction():
    async def check():
        context = SimpleNamespace(get_platform=lambda _: None)
        plugin = main.MinecraftAdapter(context, {})
        plugin.send_to_session = AsyncMock()
        first = entry(forward_chat_format="{player_id} · {server} · {message}")
        second = entry("creative")
        plugin.entries.update(survival=first, creative=second)
        await plugin.on_server_event(
            first, "chat.message", {"uuid": "uuid", "name": "Alex", "text": "{player}"}
        )
        plugin.send_to_session.assert_awaited_once_with(
            UMO, "uuid · survival · {player}"
        )
        event = session()
        rpc = AsyncMock(return_value={"delivered": 1})
        plugin.links.update(
            survival=SimpleNamespace(request=rpc), creative=SimpleNamespace(request=rpc)
        )
        assert [reply async for reply in plugin.on_message(event)] == []
        event.bot.call_action.assert_awaited_once_with(
            "set_msg_emoji_like", message_id=123, emoji_id="124"
        )
        event.bot.call_action.reset_mock()
        rpc.return_value = {"delivered": 0}
        assert [reply async for reply in plugin.on_message(event)] == []
        event.bot.call_action.assert_not_awaited()
        rpc.return_value = {"delivered": 1}
        plugin.entries = {"survival": entry(mark_option="none")}
        assert [reply async for reply in plugin.on_message(event)] == []
        event.bot.call_action.assert_not_awaited()

    asyncio.run(check())


def test_public_preset_cannot_overlap_management_commands():
    with pytest.raises(ValueError, match="admin_commands 冲突"):
        entry(
            commands={
                "admin_commands": ["whitelist"],
                "custom_commands": [
                    {"id": "public", "command": "minecraft:whitelist list"}
                ],
            }
        )
