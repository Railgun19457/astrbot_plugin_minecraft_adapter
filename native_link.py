"""Native Minecraft Java status and remote console transport."""

import asyncio
import ipaddress
import json
import re
import struct
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import ClassVar, TypeVar

from mcstatus import JavaServer

from .actions import PLAYER_NAME, command_root
from .link import EventHandler, LinkError, node_id

_T = TypeVar("_T")
_LIST = re.compile(r"There are (\d+) of a max of (\d+) players online:[ \t]*(.*)")
_COMMAND_BYTES = 1400


@dataclass(frozen=True)
class NativeEndpoint:
    id: str
    host: str
    game_port: int
    query_port: int
    rcon_port: int
    password: str = field(repr=False)
    role: ClassVar[str] = "native"

    @classmethod
    def parse(cls, server: dict) -> "NativeEndpoint":
        host = server.get("host", "127.0.0.1")
        if not isinstance(host, str):
            raise TypeError("server.host 必须为 IP 或主机名")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,253}", host):
                raise ValueError("server.host 必须为 IP 或主机名") from None
        game = server.get("game_port", 25565)
        query = server.get("query_port", 0)
        rcon = server.get("rcon_port", 0)
        for name, port, minimum in (
            ("game_port", game, 1),
            ("query_port", query, 0),
            ("rcon_port", rcon, 0),
        ):
            if type(port) is not int or not minimum <= port <= 65535:
                raise ValueError(f"server.{name} 必须是 {minimum}～65535 的整数")
        password = server.get("rcon_password", "")
        if rcon and (
            not isinstance(password, str)
            or not password
            or any(char in password for char in "\0\r\n")
            or len(password.encode("utf-8")) > 128
        ):
            raise ValueError("已启用 RCON，请填写不超过 128 字节的单行密码")
        return cls(
            node_id(server.get("server_id", "")),
            host,
            game,
            query,
            rcon,
            password if rcon else "",
        )


def _packet(request_id: int, kind: int, text: str) -> bytes:
    body = text.encode("utf-8")
    return struct.pack("<iii", len(body) + 10, request_id, kind) + body + b"\0\0"


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    size = int.from_bytes(await reader.readexactly(4), "little", signed=True)
    if not 10 <= size <= 8192:
        raise ValueError("Invalid RCON packet size")
    data = await reader.readexactly(size)
    if not data.endswith(b"\0\0"):
        raise ValueError("Invalid RCON packet footer")
    request_id, kind = struct.unpack_from("<ii", data)
    return request_id, kind, data[8:-2]


async def _probe(operation: Awaitable[_T]) -> _T | None:
    try:
        async with asyncio.timeout(4):
            return await operation
    except (OSError, EOFError, ValueError, LookupError, TypeError, struct.error):
        return None


class NativeLink:
    def __init__(self, endpoint: NativeEndpoint, events: EventHandler | None = None):
        self.endpoint = endpoint
        self.ready = asyncio.Event()
        self._events = events
        self._game = JavaServer(
            endpoint.host, endpoint.game_port, query_port=endpoint.query_port or None
        )
        self._rcon_slots = asyncio.Semaphore(4)

    async def serve(self) -> None:
        previous: dict[str, str] | None = None
        try:
            while True:
                try:
                    if self._events:
                        roster = await self.players()
                        if roster["source"] != "ping" and roster["total"] == len(
                            roster["players"]
                        ):
                            current = {
                                player["name"].casefold(): player["name"]
                                for player in roster["players"]
                            }
                            if previous is not None:
                                for operation, old, updated in (
                                    ("player.join", previous, current),
                                    ("player.leave", current, previous),
                                ):
                                    for name in sorted(updated.keys() - old.keys()):
                                        await self._events(
                                            operation, {"name": updated[name]}
                                        )
                            previous = current
                        else:
                            previous = None
                    else:
                        await self.status()
                except LinkError:
                    self.ready.clear()
                    previous = None
                await asyncio.sleep(20)
        finally:
            self.ready.clear()

    async def request(self, operation: str, data: dict | None = None) -> dict:
        data = data or {}
        match operation:
            case "server.status":
                return await self.status()
            case "player.list":
                return await self.players()
            case "player.detail":
                requested = data.get("player", "")
                if (
                    not isinstance(requested, str)
                    or not requested.strip()
                    or len(requested) > 64
                ):
                    raise LinkError("bad_request", "请输入玩家名称或 UUID")
                roster = await self.players()
                player = requested.strip().casefold()
                for row in roster["players"]:
                    if player in (
                        row["name"].casefold(),
                        row.get("uuid", "").casefold(),
                    ):
                        return {**row, "source": roster["source"]}
                raise LinkError("player_unavailable", "可用的在线名单中未找到该玩家")
            case "command.run":
                command = data["command"].strip().removeprefix("/")
                command_root(command)
                output = await self.rcon(command)
                return {
                    "accepted": True,
                    "output": [line[:320] for line in output.splitlines()[:16]],
                    "source": "rcon",
                }
            case "chat.send" if data.get("kind") == "bridge":
                return await self.send_chat(data)
            case _:
                raise LinkError("unsupported", "当前操作需要服务端适配器")

    async def rcon(self, command: str, *, read_only: bool = False) -> str:
        if not self.endpoint.rcon_port:
            raise LinkError("disabled", "原生直连未启用 RCON")
        if len(command.encode("utf-8")) > _COMMAND_BYTES or "\0" in command:
            raise LinkError("bad_request", "RCON 指令超过长度限制或格式无效")
        writer = None
        dispatched = False
        try:
            async with asyncio.timeout(5), self._rcon_slots:
                reader, writer = await asyncio.open_connection(
                    self.endpoint.host, self.endpoint.rcon_port
                )
                writer.write(_packet(1, 3, self.endpoint.password))
                await writer.drain()
                request_id, kind, _ = await _read_packet(reader)
                if (request_id, kind) == (1, 0):
                    request_id, kind, _ = await _read_packet(reader)
                if (request_id, kind) != (1, 2):
                    raise LinkError("rcon_auth", "RCON 拒绝认证，请检查密码")
                dispatched = True
                writer.write(_packet(2, 2, command))
                await writer.drain()
                output = bytearray()
                for index in range(64):
                    request_id, kind, part = await _read_packet(reader)
                    if (request_id, kind) == (3, 0) and index:
                        self.ready.set()
                        return output.decode("utf-8", errors="replace")
                    if (request_id, kind) != (2, 0):
                        raise ValueError("Unexpected RCON reply")
                    output.extend(part[: max(0, 16_384 - len(output))])
                    if index == 0:
                        # The Minecraft console answers unknown packet types with their own request ID.
                        writer.write(_packet(3, 0, ""))
                        await writer.drain()
                raise ValueError("Incomplete RCON reply")
        except (OSError, EOFError, ValueError) as error:
            if dispatched and not read_only:
                raise LinkError(
                    "command_unknown", "指令结果未知，请先确认游戏服状态。"
                ) from error
            raise LinkError(
                "rcon_unavailable", "无法连接 RCON 或读取控制台返回"
            ) from error
        finally:
            if writer is not None:
                writer.close()

    async def _rcon_roster(self) -> tuple[int, int, list[dict]]:
        output = await self.rcon("list", read_only=True)
        match = _LIST.fullmatch(output.strip())
        if not match:
            raise LinkError(
                "unavailable",
                "RCON 的 list 回显不符合原版格式，可使用 UDP Query 获取名单",
            )
        online, capacity, names = match.groups()
        return (
            int(online),
            int(capacity),
            [
                {"name": name}
                for name in (part.strip() for part in names.split(","))
                if PLAYER_NAME.fullmatch(name)
            ],
        )

    async def status(self) -> dict:
        ping = await _probe(self._game.async_status(tries=1))
        query = None
        if ping is None and self.endpoint.query_port:
            query = await _probe(self._game.async_query(tries=1))
        info = {
            "server_id": self.endpoint.id,
            "name": self.endpoint.id,
            "platform": "Minecraft Java",
            "reachable": True,
        }
        if ping is not None:
            info.update(
                source="ping",
                minecraft=ping.version.name[:128],
                online=ping.players.online,
                capacity=ping.players.max,
                motd=ping.motd.to_plain()[:200],
            )
            if ping.icon and len(ping.icon) <= 32_768:
                info["icon"] = ping.icon
        elif query is not None:
            info.update(
                source="query",
                minecraft=query.software.version[:128],
                online=query.players.online,
                capacity=query.players.max,
                motd=query.motd.to_plain()[:200],
            )
        elif self.endpoint.rcon_port:
            online, capacity, _ = await self._rcon_roster()
            info.update(source="rcon", online=online, capacity=capacity)
        else:
            raise LinkError("offline", "服务器的游戏端口或 Query 当前不可达")
        self.ready.set()
        return info

    async def players(self) -> dict:
        query = (
            await _probe(self._game.async_query(tries=1))
            if self.endpoint.query_port
            else None
        )
        if query is not None:
            total = query.players.online
            rows = [
                {"name": name}
                for name in query.players.list
                if PLAYER_NAME.fullmatch(name)
            ]
            source = "query"
        else:
            failure = None
            if self.endpoint.rcon_port:
                try:
                    total, _, rows = await self._rcon_roster()
                    return {"total": total, "players": rows[:250], "source": "rcon"}
                except LinkError as error:
                    failure = error
            ping = await _probe(self._game.async_status(tries=1))
            if ping is None:
                raise failure or LinkError("offline", "服务器在线名单当前不可用")
            total = ping.players.online
            rows = [
                {"name": player.name, "uuid": player.id}
                for player in ping.players.sample or []
                if PLAYER_NAME.fullmatch(player.name)
            ]
            source = "ping"
        self.ready.set()
        return {"total": total, "players": rows[:250], "source": source}

    async def send_chat(self, data: dict) -> dict:
        total, _, _ = await self._rcon_roster()
        if not total:
            return {"delivered": 0}
        remaining = f"[{data['platform']}] {data['sender']}: {data['text']}"
        while remaining:
            size = len(remaining)
            while True:
                component = json.dumps({"text": remaining[:size]}, ensure_ascii=False)
                command = f"tellraw @a {component}"
                if len(command.encode("utf-8")) <= _COMMAND_BYTES:
                    break
                size //= 2
            output = await self.rcon(command)
            if output.strip() and not output.startswith("Sent "):
                raise LinkError(
                    "chat_delivery",
                    "RCON 未确认聊天转发，请检查子服控制台的 tellraw 支持",
                )
            remaining = remaining[size:]
        return {"delivered": total}
