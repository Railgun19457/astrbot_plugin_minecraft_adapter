"""AstrBot entry point and Minecraft session routing."""

import asyncio
import base64
import json
import os
import secrets
import tempfile
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from time import monotonic

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

from . import mc_platform
from .actions import PLAYER_NAME, CommandList, CustomCommand, command_root, matches_root
from .cards import AvatarCache, render_card, server_icon
from .link import IdentityStore, Link, LinkError, node_id
from .model_tools import build_tools
from .native_link import NativeEndpoint, NativeLink
from .server_config import ServerEntry, identifiers, object_section


class ForwardingFilter(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        runtime = mc_platform.runtime
        if runtime is None:
            return False
        matches, usages = runtime.shortcuts(event)
        return (
            bool(matches or usages)
            or runtime.numbered_choice(event) is not None
            or next(runtime.forwarding(event), None) is not None
        )


class MinecraftAdapter(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.entries: dict[str, ServerEntry] = {}
        self.links: dict[str, Link | NativeLink] = {}
        self.tasks: list[asyncio.Task] = []
        self.avatars = AvatarCache()
        self.bindings: dict[str, dict] = {}
        self.pending_bindings: dict[str, tuple[float, str, str, str, str]] = {}
        self.pending_choices: dict[
            tuple[str, str], tuple[float, tuple[str, ...], str, str]
        ] = {}
        self.binding_path: Path
        tools = object_section(config, "llm_tools")
        self.selected_tools = identifiers(tools, "enabled_tools")
        self.model_commands = CommandList.parse(tools, "llm_tools")
        self.registered_tools = []

    async def initialize(self) -> None:
        client = node_id(self.config.get("client_id", "astrbot-main"))
        directory = StarTools.get_data_dir("astrbot_plugin_minecraft_adapter") / "v3_2"
        identity = None
        self.binding_path = directory / "bindings.json"
        if self.binding_path.exists():
            self.bindings = json.loads(self.binding_path.read_text(encoding="utf-8"))
        for index, row in enumerate(self.config.get("mc_servers", []), 1):
            if isinstance(row, dict) and not row.get("enabled", True):
                continue
            try:
                if not isinstance(row, dict):
                    raise TypeError("服务器模板必须是对象")
                entry = ServerEntry.parse(row)
                endpoint = entry.endpoint
                if endpoint.id in self.entries:
                    raise ValueError(f"服务器 ID 重复：{endpoint.id}")
                handler = partial(self.on_server_event, entry)
                if isinstance(endpoint, NativeEndpoint):
                    link = NativeLink(
                        endpoint, handler if entry.player_events else None
                    )
                else:
                    if identity is None:
                        identity = IdentityStore(directory)
                    link = Link(
                        endpoint,
                        client,
                        identity,
                        handler,
                        partial(
                            self.clear_pairing,
                            row["server"],
                            endpoint.pairing_id,
                            endpoint.pairing_code,
                        ),
                    )
            except (TypeError, ValueError) as error:
                logger.error(f"Minecraft 第 {index} 项配置无效：{error}")
                continue
            self.entries[endpoint.id] = entry
            self.links[endpoint.id] = link
            self.tasks.append(
                asyncio.create_task(link.serve(), name=f"minecraft:{endpoint.id}")
            )
        mc_platform.runtime = self
        if self.selected_tools:
            available = build_tools(self)
            self.registered_tools = [
                tool
                for key, tools in available.items()
                if key in self.selected_tools
                for tool in tools
            ]
            if self.registered_tools:
                self.context.add_llm_tools(*self.registered_tools)

    async def terminate(self) -> None:
        if mc_platform.runtime is self:
            mc_platform.runtime = None
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        await self.avatars.close()
        if self.registered_tools:
            registry = self.context.provider_manager.llm_tools
            for tool in self.registered_tools:
                if any(current is tool for current in registry.func_list):
                    registry.remove_func(tool.name)
            self.registered_tools.clear()

    def clear_pairing(self, server: dict, pair_id: str, code: str) -> None:
        if not pair_id or (server.get("pairing_id"), server.get("pairing_code")) != (
            pair_id,
            code,
        ):
            return
        server["pairing_id"] = server["pairing_code"] = ""
        try:
            self.config.save_config()
        except OSError:
            logger.exception("Minecraft 已保存节点信任，清理配置中的配对字段失败")

    async def send_to_session(self, session: str, text: str) -> None:
        try:
            if not await self.context.send_message(
                session, MessageChain().message(text)
            ):
                logger.warning(f"Minecraft 找不到目标会话 {session}")
        except Exception:  # noqa: BLE001 - Platform delivery failures are isolated per session.
            logger.exception(f"Minecraft 向会话 {session} 发送消息失败")

    async def on_server_event(
        self, entry: ServerEntry, operation: str, data: dict
    ) -> None:
        endpoint = entry.endpoint.id
        backend = data.get("server", "") if entry.endpoint.role == "velocity" else ""
        if entry.endpoint.role == "velocity" and not backend:
            return
        route = f"{endpoint}/{backend}" if backend else endpoint
        if operation == "player.bind":
            await self.confirm_binding(endpoint, data)
            return
        platform = self.context.get_platform("minecraft")
        publish = getattr(platform, "publish", None)
        if operation == "ai.request" and entry.ai:
            if callable(publish):
                publish(route, data, ai=True)
            else:
                await self.send_game_reply(
                    route, data["uuid"], "AstrBot 的 Minecraft 平台尚未启用。"
                )
            return
        if operation == "chat.message" and data["text"].strip():
            if callable(publish):
                publish(route, data, ai=False)
            if not entry.bridge:
                return
            text = entry.chat_format.format(
                server=route,
                server_id=endpoint,
                backend=backend,
                player=data["name"],
                player_name=data["name"],
                player_id=data["uuid"],
                uuid=data["uuid"],
                message=data["text"],
            )
        elif entry.player_events and operation in {
            "player.join",
            "player.leave",
            "player.switch",
        }:
            if operation == "player.switch":
                text = f"[{route}] {data['name']} 切换到了此子服"
            else:
                action = "加入" if operation == "player.join" else "离开"
                text = f"[{route}] {data['name']} {action}了服务器"
        else:
            return
        await asyncio.gather(
            *(self.send_to_session(session, text) for session in entry.sessions)
        )

    async def send_game_reply(self, endpoint: str, recipient: str, text: str) -> None:
        server, _, backend = endpoint.partition("/")
        link = self.links.get(server)
        if link is None:
            return
        try:
            request = {"kind": "ai", "player": recipient, "text": text[:1500]}
            if backend:
                request["target"] = backend
            await link.request("chat.send", request)
        except LinkError as error:
            logger.warning(f"Minecraft {endpoint} 回复游戏会话失败：{error.code}")

    def visible(self, event: AstrMessageEvent) -> list[ServerEntry]:
        if event.get_platform_name() == "minecraft":
            endpoint = event.session_id.split("/", 1)[0]
            entry = self.entries.get(endpoint)
            return [entry] if entry else []
        return [
            entry
            for entry in self.entries.values()
            if event.unified_msg_origin in entry.sessions
        ]

    async def proxy_backends(self, entry: ServerEntry) -> list[str]:
        try:
            data = await self.links[entry.endpoint.id].request("server.list")
            return data["servers"]
        except LinkError:
            return []

    async def select(
        self,
        event: AstrMessageEvent,
        target: str,
        continuation: tuple[str, str] | None = None,
    ) -> tuple[ServerEntry, str]:
        entries = self.visible(event)
        game = event.get_platform_name() == "minecraft"
        parts = event.session_id.split("/") if game else []
        current = parts[1] if len(parts) == 3 else ""
        if target:
            if continuation is not None:
                self.pending_choices.pop(
                    (event.unified_msg_origin, event.get_sender_id()), None
                )
            name, separator, backend = target.removeprefix("@").partition("/")
            for entry in entries:
                if entry.endpoint.id != name:
                    continue
                if not separator:
                    return entry, current
                if (
                    entry.endpoint.role == "velocity"
                    and backend
                    and (not game or backend == current)
                ):
                    try:
                        return entry, node_id(backend)
                    except ValueError:
                        break
            if not separator and name:
                proxies = [
                    entry for entry in entries if entry.endpoint.role == "velocity"
                ]
                if game:
                    matches = proxies if name == current else []
                else:
                    names = await asyncio.gather(
                        *(self.proxy_backends(entry) for entry in proxies)
                    )
                    matches = [
                        entry
                        for entry, backends in zip(proxies, names, strict=True)
                        if name in backends
                    ]
                if len(matches) == 1:
                    return matches[0], name
                if matches:
                    choices = tuple(f"@{entry.endpoint.id}/{name}" for entry in matches)
                    raise self.target_choice(event, choices, continuation)
            raise LinkError("unknown_target", "当前会话无法访问该服务器")
        if not entries:
            raise LinkError("no_target", "当前会话未绑定 Minecraft 服务器")
        if len(entries) > 1:
            choices = tuple(f"@{entry.endpoint.id}" for entry in entries)
            raise self.target_choice(event, choices, continuation)
        return entries[0], current

    def target_choice(
        self,
        event: AstrMessageEvent,
        choices: tuple[str, ...],
        continuation: tuple[str, str] | None,
    ) -> LinkError:
        if continuation is None:
            return LinkError("ambiguous_target", "请选择服务器 " + "、".join(choices))
        now = monotonic()
        self.pending_choices = {
            key: value for key, value in self.pending_choices.items() if value[0] > now
        }
        key = (event.unified_msg_origin, event.get_sender_id())
        self.pending_choices[key] = (now + 60, choices, *continuation)
        return LinkError(
            "ambiguous_target",
            "请在 60 秒内回复服务器编号\n"
            + "\n".join(
                f"{index}. {choice}" for index, choice in enumerate(choices, 1)
            ),
        )

    def numbered_choice(self, event: AstrMessageEvent) -> tuple[str, str, str] | None:
        key = (event.unified_msg_origin, event.get_sender_id())
        pending = self.pending_choices.get(key)
        if pending is None:
            return None
        expires, choices, handler, arguments = pending
        if monotonic() >= expires:
            self.pending_choices.pop(key, None)
            return None
        number = event.get_message_str().strip()
        if (
            number.isascii()
            and number.isdecimal()
            and len(number) <= len(str(len(choices)))
        ):
            index = int(number) - 1
            if 0 <= index < len(choices):
                return handler, choices[index], arguments
        return None

    async def query(
        self, entry: ServerEntry, backend: str, operation: str, data: dict | None = None
    ) -> dict:
        if entry.endpoint.role == "velocity" and backend:
            data = {**(data or {}), "target": backend}
        return await self.links[entry.endpoint.id].request(operation, data)

    @staticmethod
    def split_target(arguments: str) -> tuple[str, str]:
        first, _, rest = arguments.strip().partition(" ")
        return (
            (first, rest.strip()) if first.startswith("@") else ("", arguments.strip())
        )

    @staticmethod
    def can_command(event: AstrMessageEvent, entry: ServerEntry) -> bool:
        return entry.command_enabled and event.unified_msg_origin in entry.sessions

    @staticmethod
    def is_command_admin(event: AstrMessageEvent, entry: ServerEntry) -> bool:
        return event.unified_msg_origin in entry.admin_umos or event.is_admin()

    def binding_key(self, endpoint: str, session: str, sender: str) -> str:
        return json.dumps([endpoint, session, sender], ensure_ascii=False)

    def write_bindings(self, bindings: dict) -> None:
        descriptor, filename = tempfile.mkstemp(
            prefix=".bindings-", dir=self.binding_path.parent
        )
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(bindings, stream, ensure_ascii=False, indent=2)
            temporary.replace(self.binding_path)
            self.bindings = bindings
        finally:
            temporary.unlink(missing_ok=True)

    async def confirm_binding(self, endpoint: str, data: dict) -> None:
        code = data["code"]
        route = f"{endpoint}/{data['server']}" if data.get("server") else endpoint
        pending = self.pending_bindings.get(code)
        if pending is None or pending[0] <= monotonic():
            self.pending_bindings.pop(code, None)
            await self.send_game_reply(route, data["uuid"], "验证码无效或已过期。")
            return
        _, expected_endpoint, expected_player, session, sender = pending
        if expected_endpoint != endpoint or expected_player != data["uuid"]:
            await self.send_game_reply(
                route, data["uuid"], "验证码与当前游戏账号不匹配。"
            )
            return
        key = self.binding_key(endpoint, session, sender)
        self.write_bindings(
            {**self.bindings, key: {"uuid": data["uuid"], "name": data["name"]}}
        )
        del self.pending_bindings[code]
        await self.send_to_session(
            session, f"[{endpoint}] 已验证游戏账号 {data['name']}。"
        )

    async def execute(
        self,
        event: AstrMessageEvent,
        entry: ServerEntry,
        command: str,
        backend: str = "",
        *,
        source: str = "manual",
    ) -> dict:
        if source == "model":
            if "command" not in self.selected_tools:
                raise LinkError("command_denied", "当前会话未开放模型指令")
        elif not self.can_command(event, entry):
            raise LinkError("command_denied", "当前会话未开启远程指令")
        if not isinstance(command, str) or not command or len(command) > 256:
            raise LinkError("bad_request", "请输入 1 至 256 字符的服务器指令")
        root = command_root(command)
        if source == "model":
            if not self.model_commands.allows(root):
                raise LinkError("command_denied", "此指令不在模型允许执行的范围内")
        elif matches_root(entry.admin_commands, root):
            if not self.is_command_admin(event, entry):
                raise LinkError("admin_required", "此指令仅管理员可执行")
        elif source == "manual" and not entry.commands.allows(root):
            raise LinkError("command_denied", "此指令不在当前会话允许执行的范围内")
        try:
            return await self.query(entry, backend, "command.run", {"command": command})
        except LinkError as error:
            if error.code in {"timeout", "disconnected", "command_unknown"}:
                raise LinkError(
                    "command_unknown", "指令结果未知，请先确认游戏服状态。"
                ) from error
            raise

    async def run_action(
        self,
        event: AstrMessageEvent,
        entry: ServerEntry,
        action: CustomCommand,
        backend: str = "",
        *,
        parameters: dict | None = None,
    ) -> dict:
        if not self.can_command(event, entry):
            raise LinkError("command_denied", "当前会话未开启远程指令")
        values = action.validate({} if parameters is None else parameters)
        if action.needs_binding:
            if isinstance(entry.endpoint, NativeEndpoint):
                raise LinkError(
                    "unsupported", "使用 {sender} 的指令需要服务端适配器验证游戏账号"
                )
            player = self.bindings.get(
                self.binding_key(
                    entry.endpoint.id, event.unified_msg_origin, event.get_sender_id()
                )
            )
            if not player or not PLAYER_NAME.fullmatch(player["name"]):
                raise LinkError("binding_required", "请先使用 /mc bind 验证游戏账号")
            values["sender"] = player["name"]
        for name, parameter in action.parameters.items():
            if parameter.kind == "player":
                online = await self.query(
                    entry,
                    backend,
                    "player.detail",
                    {"player": values[name], "online_only": True},
                )
                canonical = online.get("name", "")
                if (
                    online.get("offline")
                    or not isinstance(canonical, str)
                    or not PLAYER_NAME.fullmatch(canonical)
                ):
                    raise LinkError("invalid_parameters", f"{name} 的游戏账号格式无效")
                values[name] = canonical
        return await self.execute(
            event, entry, action.render(values), backend, source="preset"
        )

    @staticmethod
    def command_result(data: dict) -> str:
        output = "\n".join(data["output"])
        if data["accepted"]:
            return output or "指令已执行。"
        return f"服务器未接受该指令。\n{output}" if output else "服务器未接受该指令。"

    @staticmethod
    def is_external_message(event: AstrMessageEvent) -> bool:
        return (
            event.get_platform_name() != "minecraft"
            and event.get_sender_id() != event.get_self_id()
        )

    def shortcuts(
        self, event: AstrMessageEvent
    ) -> tuple[list[tuple[ServerEntry, str, CustomCommand, dict[str, str]]], list[str]]:
        matches = []
        usages = []
        if not self.is_external_message(event):
            return matches, usages
        target, body = self.split_target(event.get_message_str())
        endpoint_name, separator, backend = target.removeprefix("@").partition("/")
        if separator:
            try:
                node_id(backend)
            except ValueError:
                return matches, usages
        words = body.split()
        if not words:
            return matches, usages
        for entry in self.visible(event):
            endpoint = entry.endpoint.id
            if (
                (target and endpoint_name != endpoint)
                or (backend and entry.endpoint.role != "velocity")
                or not self.can_command(event, entry)
            ):
                continue
            for action in entry.custom_commands.values():
                if (
                    not action.trigger_tokens
                    or action.trigger_tokens[0].casefold() != words[0].casefold()
                ):
                    continue
                values = action.match(words)
                if values is None:
                    usages.append(f"{target or '@' + endpoint} {action.trigger}")
                else:
                    matches.append((entry, backend, action, values))
        return matches, usages

    def forwarding(self, event: AstrMessageEvent) -> Iterator[tuple[ServerEntry, str]]:
        if not self.is_external_message(event):
            return
        text = event.get_message_str().strip()
        for entry in self.visible(event):
            if entry.bridge and entry.prefix and text.startswith(entry.prefix):
                content = text[len(entry.prefix) :].strip()
                if content:
                    yield entry, content

    @filter.custom_filter(ForwardingFilter)
    async def on_message(self, event: AstrMessageEvent):
        if choice := self.numbered_choice(event):
            self.pending_choices.pop(
                (event.unified_msg_origin, event.get_sender_id()), None
            )
            handler, target, arguments = choice
            async for reply in getattr(self, handler)(
                event, f"{target} {arguments}".strip()
            ):
                yield reply
            return
        matches, usages = self.shortcuts(event)
        if len(matches) > 1:
            targets = {entry.endpoint.id for entry, _, _, _ in matches}
            if len(targets) == len(matches):
                suggestions = [
                    f"@{entry.endpoint.id} {event.get_message_str().strip()}"
                    for entry, _, _, _ in matches
                ]
            else:
                suggestions = [
                    f"/mc action @{entry.endpoint.id}{'/' + backend if backend else ''} {action.id} "
                    + " ".join(f"{key}={value}" for key, value in values.items())
                    for entry, backend, action, values in matches
                ]
            yield event.plain_result(
                "请选择一条自定义指令：\n" + "\n".join(suggestions)
            )
            return
        if matches:
            entry, backend, action, values = matches[0]
            try:
                result = await self.run_action(
                    event, entry, action, backend, parameters=values
                )
                reply = self.command_result(result)
            except LinkError as error:
                reply = str(error)
            yield event.plain_result(f"[{entry.endpoint.id}] {reply}")
            return
        if usages:
            yield event.plain_result("自定义指令用法：\n" + "\n".join(usages))
            return

        mark = False
        for entry, content in self.forwarding(event):
            endpoint = entry.endpoint.id
            try:
                sent = await self.query(
                    entry,
                    "",
                    "chat.send",
                    {
                        "kind": "bridge",
                        "player": "",
                        "text": content[:1500],
                        "platform": event.get_platform_name()[:40],
                        "sender": event.get_sender_name()[:80],
                    },
                )
                mark |= entry.mark_option == "emoji" and sent["delivered"] > 0
            except LinkError as error:
                yield event.plain_result(f"[{endpoint}] {error}")
        if mark:
            await self.mark_forwarded(event)

    @staticmethod
    async def mark_forwarded(event: AstrMessageEvent) -> None:
        try:
            if event.get_platform_name() == "aiocqhttp":
                await event.bot.call_action(
                    "set_msg_emoji_like",
                    message_id=int(event.message_obj.message_id),
                    emoji_id="124",
                )
            elif (
                getattr(type(event), "react", AstrMessageEvent.react)
                is not AstrMessageEvent.react
            ):
                await event.react("OK" if event.get_platform_name() == "lark" else "👌")
        except Exception as error:  # noqa: BLE001 - Reactions are optional platform effects.
            logger.debug(f"Minecraft 消息已转发，表情回应失败：{error}")

    @filter.command_group("mc")
    def mc(self):
        pass

    @mc.command("help")
    async def help(self, event: AstrMessageEvent):
        entries = self.visible(event)
        if not entries:
            yield event.plain_result("当前会话未绑定 Minecraft 服务器。")
            return
        lines = [
            "/mc status [@服务器] · 查看状态",
            "/mc list [@服务器] · 查看在线玩家",
            "/mc player [@服务器] <玩家> · 查看玩家详情",
        ]
        if event.get_platform_name() != "minecraft" and any(
            entry.endpoint.role != "native" for entry in entries
        ):
            lines.extend(
                (
                    "/mc bind [@服务器] <玩家> · 验证游戏账号",
                    "/mc unbind [@服务器] · 解除绑定",
                )
            )
        if any(
            entry.endpoint.role != "native" and self.is_command_admin(event, entry)
            for entry in entries
        ):
            lines.append("/mc logs [@服务器] [行数] [关键词] · 查阅服务器日志")
        for entry in entries:
            if self.can_command(event, entry):
                if (
                    entry.commands.mode == "black"
                    or entry.commands.roots
                    or (entry.admin_commands and self.is_command_admin(event, entry))
                ):
                    lines.append(
                        f"/mc cmd @{entry.endpoint.id} <指令> · 执行控制台指令"
                    )
                if entry.custom_commands:
                    lines.append(
                        f"/mc actions @{entry.endpoint.id} · 查看并使用预设动作"
                    )
                lines.extend(
                    f"{action.trigger} · {entry.endpoint.id}"
                    for action in entry.custom_commands.values()
                    if action.trigger
                )
        yield event.plain_result("\n".join(lines))

    def uses_cards(self, event: AstrMessageEvent) -> bool:
        return (
            self.config.get("text2image", True)
            and event.get_platform_name() != "minecraft"
        )

    @staticmethod
    def format_uptime(seconds: int) -> str:
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        parts = [
            f"{value} {unit}"
            for value, unit in (
                (days, "天"),
                (hours, "小时"),
                (remainder // 60, "分钟"),
            )
            if value
        ]
        return " ".join(parts) or "0 分钟"

    async def display(
        self,
        event: AstrMessageEvent,
        card: dict,
        rows: list[tuple[str, str]],
    ):
        if self.uses_cards(event):
            image = await render_card(card)
            if image is not None:
                return event.chain_result([Image.fromBytes(image)])
        return event.plain_result(
            "\n".join([card["title"], *(f"{label}：{value}" for label, value in rows)])
        )

    @mc.command("status")
    async def status(self, event: AstrMessageEvent, target: str = ""):
        try:
            entry, backend = await self.select(event, target, ("status", ""))
            assets = {"include_assets": True} if self.uses_cards(event) else None
            data = await self.query(entry, backend, "server.status", assets)
            version = f"{data['platform']} {data.get('minecraft', '')}".strip()
            availability = (
                ("可达" if data["reachable"] else "探测不可达")
                if "reachable" in data
                else ""
            )
            if data.get("adapter_error") or data.get("adapter_connected") is False:
                availability = f"{availability} · 后端适配器不可用".strip(" ·")
            capacity = data.get("capacity")
            memory = data.get("memory_percent")
            memory_value, memory_unit = (
                (f"{memory:.1f}", "%") if memory is not None else ("—", "")
            )
            metrics = []
            text_metrics = []
            if "uptime_seconds" in data:
                uptime = self.format_uptime(data["uptime_seconds"])
                metrics.extend(
                    [
                        ("memory", "内存", memory_value, memory_unit),
                        ("time", "已运行", uptime, ""),
                    ]
                )
                text_metrics.extend(
                    [("内存", memory_value + memory_unit), ("已运行", uptime)]
                )
            if "tps" in data and "mspt" in data:
                tps, mspt = f"{data['tps']:.2f}", f"{data['mspt']:.2f}"
                label = (
                    "全局区 " if data.get("metrics_scope") == "global_region" else ""
                )
                metrics[:0] = [
                    ("pulse", label + "TPS", tps, ""),
                    ("speed", label + "MSPT", mspt, "ms"),
                ]
                text_metrics.insert(0, (label + "TPS / MSPT", f"{tps} / {mspt}"))
            elif "mspt" in data:
                mspt = f"{data['mspt']:.2f}"
                metrics.insert(0, ("speed", "MSPT", mspt, "ms"))
                text_metrics.insert(0, ("MSPT", f"{mspt} ms"))
            children = [
                (
                    node["server_id"],
                    f"{node['online']} 人 · {'可达' if node['reachable'] else '探测不可达'}",
                )
                for node in data.get("servers", [])
            ]
            card = {
                "layout": "status",
                "title": data["name"],
                "version": version,
                "online": data["online"],
                "capacity": capacity,
                "metrics": metrics,
                "icon": server_icon(data.get("icon")) if assets else None,
                "availability": availability,
                "motd": data.get("motd", ""),
                "servers": children,
                "proxy_count": bool(backend) and data.get("source") != "adapter",
            }
            count = (
                str(data["online"])
                if card["proxy_count"] or capacity is None
                else f"{data['online']} / {capacity}"
            )
            rows = [
                ("版本", version),
                ("代理在线" if card["proxy_count"] else "在线", count),
                *text_metrics,
            ]
            if availability:
                rows.append(("探测", availability))
            if "proxy_online" in data:
                rows.append(("代理在线", str(data["proxy_online"])))
            if data.get("motd"):
                rows.append(("MOTD", data["motd"]))
            if data.get("reported_online") is not None and capacity is not None:
                rows.append(("子服报告", f"{data['reported_online']} / {capacity}"))
                card["reported_players"] = f"{data['reported_online']} / {capacity}"
            rows.extend(children)
            yield await self.display(event, card, rows)
        except LinkError as error:
            yield event.plain_result(str(error))

    @mc.command("list")
    async def players(self, event: AstrMessageEvent, target: str = ""):
        try:
            entry, backend = await self.select(event, target, ("players", ""))
            assets = self.uses_cards(event)
            data = await self.query(
                entry,
                backend,
                "player.list",
                {"include_ping": True, "include_assets": assets},
            )
            players = data["players"][:30]
            avatars = (
                await self.avatars.avatars([player.get("skin") for player in players])
                if assets
                else [None] * len(players)
            )
            roster = [
                {
                    "name": player["name"],
                    "avatar": avatar,
                    "server": player.get("server", "") if not backend else "",
                    "ping": f"{player['ping']} ms"
                    if player.get("ping") is not None
                    else None,
                }
                for player, avatar in zip(players, avatars, strict=True)
            ]
            remaining = max(0, data["total"] - len(roster))
            card = {
                "layout": "roster",
                "title": f"{entry.endpoint.id}/{backend}"
                if backend
                else entry.endpoint.id,
                "online": data["total"],
                "players": roster,
                "remaining": remaining,
            }
            names = []
            for player in roster:
                metadata = " · ".join(
                    part for part in (player["server"], player["ping"]) if part
                )
                names.append(
                    f"{player['name']}（{metadata}）" if metadata else player["name"]
                )
            if remaining:
                names.append(f"另有 {remaining} 人")
            rows = [("在线", str(data["total"])), ("玩家", "、".join(names) or "暂无")]
            yield await self.display(event, card, rows)
        except LinkError as error:
            yield event.plain_result(str(error))

    @mc.command("player")
    async def player(self, event: AstrMessageEvent, arguments=GreedyStr):
        try:
            target, name = self.split_target(arguments)
            if not name:
                raise LinkError(
                    "bad_request", "用法：/mc player [@服务器] <玩家名称或 UUID>"
                )
            entry, backend = await self.select(event, target, ("player", name))
            request: dict[str, str | bool] = {"player": name}
            assets = self.uses_cards(event)
            if assets:
                request["include_assets"] = True
            data = await self.query(entry, backend, "player.detail", request)
            coordinates = [f"{value:.1f}" for value in data.get("position", [])]
            health = str(data["health"]) if "health" in data else None
            mode = data.get("game_mode")
            server = entry.endpoint.id
            if data.get("server"):
                server += f"/{data['server']}"
            avatar = (
                (await self.avatars.avatars([data.get("skin")]))[0] if assets else None
            )
            card = {
                "layout": "player",
                "title": data["name"] + ("（离线）" if data.get("offline") else ""),
                "server": server,
                "world": data.get("world"),
                "mode": mode,
                "health": health,
                "ping": str(data["ping"]) if "ping" in data else None,
                "coordinates": coordinates,
                "avatar": avatar,
            }
            rows = [
                ("服务器", server),
                ("世界", card["world"]),
                ("模式", mode),
                ("生命值", health),
                ("延迟", f"{data['ping']} ms" if "ping" in data else None),
                ("坐标", " / ".join(coordinates) or None),
                ("最后在线（UTC）", data.get("last_seen")),
            ]
            rows = [(label, value) for label, value in rows if value is not None]
            yield await self.display(event, card, rows)
        except LinkError as error:
            yield event.plain_result(str(error))

    @mc.command("cmd")
    async def command(self, event: AstrMessageEvent, arguments=GreedyStr):
        try:
            target, command = self.split_target(arguments)
            entry, backend = await self.select(event, target, ("command", command))
            output = await self.execute(event, entry, command, backend)
            yield event.plain_result(self.command_result(output))
        except LinkError as error:
            yield event.plain_result(str(error))

    @mc.command("logs")
    async def logs(self, event: AstrMessageEvent, arguments: GreedyStr = ""):
        try:
            target, remainder = self.split_target(arguments)
            count, _, keyword = remainder.partition(" ")
            if count and (
                not count.isascii()
                or not count.isdecimal()
                or len(count) > 3
                or not 1 <= int(count) <= 100
            ):
                raise LinkError(
                    "bad_request", "用法：/mc logs [@服务器] [1～100 行] [关键词]"
                )
            entry, backend = await self.select(event, target, ("logs", remainder))
            if not self.is_command_admin(event, entry):
                raise LinkError("admin_required", "日志仅管理员可查")
            data = await self.query(
                entry,
                backend,
                "log.query",
                {"tail_lines": int(count or 20), "keyword": keyword[:128]},
            )
            yield event.plain_result("\n".join(data["lines"]) or "没有匹配的日志。")
        except LinkError as error:
            yield event.plain_result(str(error))

    @mc.command("actions")
    async def list_actions(self, event: AstrMessageEvent, target: str = ""):
        try:
            entry, backend = await self.select(event, target, ("list_actions", ""))
            actions = (
                entry.custom_commands.values() if self.can_command(event, entry) else []
            )
            if not actions:
                yield event.plain_result("当前账号在这里没有可用的预设动作。")
                return
            lines = []
            for action in actions:
                args = " ".join(f"{name}=<{name}>" for name in action.parameters)
                lines.append(
                    f"/mc action @{entry.endpoint.id}{'/' + backend if backend else ''} {action.id} {args}".rstrip()
                )
                if action.trigger:
                    lines.append(f"快捷用法：{action.trigger}")
                if action.description:
                    lines.append(action.description)
                if action.needs_binding:
                    lines.append("需要已验证的游戏账号")
                for name, parameter in action.parameters.items():
                    detail = {
                        "choice": " / ".join(parameter.choices),
                        "integer": f"{parameter.minimum}～{parameter.maximum} 的整数",
                        "player": "在线玩家名称",
                        "token": "单个参数",
                    }[parameter.kind]
                    lines.append(
                        f"  {name} · {detail}"
                        + (
                            f" · {parameter.description}"
                            if parameter.description
                            else ""
                        )
                    )
            yield event.plain_result("\n".join(lines))
        except LinkError as error:
            yield event.plain_result(str(error))

    @mc.command("action")
    async def action(self, event: AstrMessageEvent, arguments=GreedyStr):
        try:
            target, remainder = self.split_target(arguments)
            action_id, _, fields = remainder.partition(" ")
            if not action_id:
                raise LinkError(
                    "bad_request", "用法：/mc action [@服务器] <动作 ID> [参数=值 ...]"
                )
            entry, backend = await self.select(event, target, ("action", remainder))
            action = entry.custom_commands.get(action_id) or entry.custom_commands.get(
                action_id.casefold()
            )
            if action is None:
                raise LinkError("unknown_action", "未找到该自定义指令。")
            parameters = {}
            for field in fields.split():
                name, separator, value = field.partition("=")
                if not separator or not value or name in parameters:
                    raise LinkError(
                        "invalid_parameters", "请使用 参数=值，且每个参数只填写一次。"
                    )
                parameters[name] = value
            result = await self.run_action(
                event, entry, action, backend, parameters=parameters
            )
            yield event.plain_result(self.command_result(result))
        except LinkError as error:
            yield event.plain_result(str(error))

    @mc.command("bind")
    async def bind(self, event: AstrMessageEvent, arguments=GreedyStr):
        try:
            if event.get_platform_name() == "minecraft":
                raise LinkError("wrong_platform", "请在要绑定的外部会话中发起验证")
            target, name = self.split_target(arguments)
            if not name:
                raise LinkError("bad_request", "用法：/mc bind [@服务器] <在线玩家>")
            entry, backend = await self.select(event, target, ("bind", name))
            if isinstance(entry.endpoint, NativeEndpoint):
                raise LinkError("unsupported", "游戏账号绑定需要服务端适配器")
            endpoint = entry.endpoint.id
            player = await self.query(
                entry, backend, "player.detail", {"player": name, "online_only": True}
            )
            now = monotonic()
            key = (
                endpoint,
                player["uuid"],
                event.unified_msg_origin,
                event.get_sender_id(),
            )
            self.pending_bindings = {
                code: pending
                for code, pending in self.pending_bindings.items()
                if pending[0] > now and pending[1:] != key
            }
            if len(self.pending_bindings) >= 256:
                raise LinkError("busy", "验证请求过多，请稍后再试")
            code = base64.b32encode(secrets.token_bytes(5)).decode("ascii")
            self.pending_bindings[code] = (
                now + 300,
                endpoint,
                player["uuid"],
                event.unified_msg_origin,
                event.get_sender_id(),
            )
            yield event.plain_result(
                f"请让 {player['name']} 在 {endpoint} 内执行 /astrbot bind {code}。验证码 5 分钟内有效。"
            )
        except LinkError as error:
            yield event.plain_result(str(error))

    @mc.command("unbind")
    async def unbind(self, event: AstrMessageEvent, target: str = ""):
        try:
            entry, _ = await self.select(event, target, ("unbind", ""))
            key = self.binding_key(
                entry.endpoint.id, event.unified_msg_origin, event.get_sender_id()
            )
            if key not in self.bindings:
                yield event.plain_result("当前账号还没有游戏绑定。")
                return
            self.write_bindings(
                {name: value for name, value in self.bindings.items() if name != key}
            )
            yield event.plain_result("已解除游戏账号绑定。")
        except LinkError as error:
            yield event.plain_result(str(error))

    async def query_tool(
        self,
        event: AstrMessageEvent,
        target: str,
        operation: str,
        data: dict | None = None,
    ) -> str:
        try:
            entry, backend = await self.select(event, target)
            return json.dumps(
                await self.query(entry, backend, operation, data),
                ensure_ascii=False,
            )
        except LinkError as error:
            return json.dumps(
                {"error": error.code, "message": str(error)}, ensure_ascii=False
            )

    async def targets_tool(self, event: AstrMessageEvent) -> str:
        entries = self.visible(event)
        game = event.get_platform_name() == "minecraft"

        async def describe(entry):
            endpoint = entry.endpoint.id
            backends = []
            if entry.endpoint.role == "velocity":
                if game:
                    _, current = await self.select(event, "")
                    backends = [current] if current else []
                else:
                    backends = await self.proxy_backends(entry)
            return {
                "target": endpoint,
                "role": entry.endpoint.role,
                "connected": self.links[endpoint].ready.is_set(),
                "backends": [f"{endpoint}/{backend}" for backend in backends],
            }

        targets = await asyncio.gather(*(describe(entry) for entry in entries))
        return json.dumps({"targets": targets}, ensure_ascii=False)

    async def status_tool(self, event: AstrMessageEvent, target: str = "") -> str:
        return await self.query_tool(event, target, "server.status")

    async def players_tool(
        self, event: AstrMessageEvent, target: str = "", with_latency: bool = False
    ) -> str:
        data = {"include_ping": True} if with_latency else None
        return await self.query_tool(event, target, "player.list", data)

    async def player_tool(
        self, event: AstrMessageEvent, target: str = "", player: str = ""
    ) -> str:
        return await self.query_tool(event, target, "player.detail", {"player": player})

    async def command_tool(
        self,
        event: AstrMessageEvent,
        command: str,
        target: str = "",
    ) -> str:
        try:
            entry, backend = await self.select(event, target)
            return json.dumps(
                await self.execute(event, entry, command, backend, source="model"),
                ensure_ascii=False,
            )
        except LinkError as error:
            return json.dumps(
                {"error": error.code, "message": str(error)}, ensure_ascii=False
            )
