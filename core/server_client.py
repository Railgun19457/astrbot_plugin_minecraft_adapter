"""High-level Version 3 Minecraft domain client."""

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .config import ServerConfig
from .protocol.constants import Op, TargetScope
from .protocol.frame import Frame, Target
from .protocol.payloads import (
    BackendServerInfo,
    BackendServerStatus,
    PlayerDetail,
    PlayerInfo,
    ServerInfo,
    ServerStatus,
    plain_text_message,
)
from .transport.request_manager import RequestError
from .transport.session import V3Session

EventHandler = Callable[[Frame], Awaitable[None]]


class ServerClientError(RuntimeError):
    """Raised when a domain request has an unsuccessful V3 response."""


class ServerClient:
    """Expose business operations after an authenticated V3 session is ready."""

    def __init__(self, config: ServerConfig, session: V3Session):
        self.config = config
        self.session = session
        self._event_handlers: list[EventHandler] = []
        self.session.add_event_handler(self._dispatch_event)

    @property
    def server_id(self) -> str:
        """Return the configured remote server identifier."""
        return self.config.server_id

    @property
    def connected(self) -> bool:
        """Whether the underlying session completed mutual authentication."""
        return self.session.connected

    def add_event_handler(self, handler: EventHandler) -> None:
        """Register a handler for server-originated Version 3 events."""
        self._event_handlers.append(handler)

    async def start(self) -> None:
        """Start the secure session and its reconnect loop."""
        await self.session.start()

    async def stop(self) -> None:
        """Stop the secure session and release transport resources."""
        await self.session.stop()

    async def get_server_status(self) -> tuple[ServerInfo, ServerStatus]:
        """Fetch a renderer-compatible server status snapshot."""
        payload = await self._request(
            Op.SERVER_STATUS,
            {"server_ids": [self.server_id], "include_performance": True},
            Target(TargetScope.SERVER, self.server_id),
        )
        items = payload.get("servers")
        if not isinstance(items, list) or not items:
            raise ServerClientError("server.status response has no server entries")
        return self._status_models(items)

    async def get_players(self) -> tuple[list[PlayerInfo], int]:
        """Fetch online player records from the configured endpoint."""
        payload = await self._request(
            Op.PLAYER_LIST,
            {"include_details": True, "include_recent_offline": False},
            Target(TargetScope.ALL_SERVERS),
        )
        servers = payload.get("servers")
        if not isinstance(servers, list):
            raise ServerClientError("player.list response has no server entries")
        players: list[PlayerInfo] = []
        total = 0
        for server in servers:
            if not isinstance(server, dict):
                continue
            total += self._integer(server.get("online_count"))
            raw_players = server.get("players")
            if isinstance(raw_players, list):
                players.extend(
                    PlayerInfo.from_v3(item)
                    for item in raw_players
                    if isinstance(item, dict)
                )
        return players, total or len(players)

    async def get_player(self, identifier: str) -> PlayerDetail:
        """Fetch one player by Minecraft name or UUID."""
        if not identifier.strip():
            raise ServerClientError("player identifier is required")
        query_type = "uuid" if self._looks_like_uuid(identifier) else "name"
        payload = await self._request(
            Op.PLAYER_DETAIL,
            {
                "query": {"type": query_type, "value": identifier.strip()},
                "include_history": True,
            },
            Target(TargetScope.SERVER, self.server_id),
        )
        player = payload.get("player")
        if not isinstance(player, dict):
            raise ServerClientError("player.detail response has no player")
        return PlayerDetail.from_v3(player)

    async def send_external_message(
        self,
        platform: str,
        user_id: str,
        user_name: str,
        conversation_id: str,
        conversation_type: str,
        content: str,
    ) -> bool:
        """Broadcast one external-platform message into Minecraft."""
        response = await self._request(
            Op.CHAT_EXTERNAL,
            {
                "dedupe_key": self._dedupe_key("chat"),
                "sender": {
                    "type": "external_user",
                    "platform": platform or "unknown",
                    "id": user_id or "unknown",
                    "name": user_name,
                    "display_name": user_name,
                },
                "conversation": {
                    "platform": platform or "unknown",
                    "type": conversation_type or "unknown",
                    "id": conversation_id or "unknown",
                },
                "bridge": {
                    "origin_platform": platform or "unknown",
                    "origin_conversation_id": conversation_id or "unknown",
                    "suppress_echo": True,
                },
                "message": plain_text_message(content, self._now()),
            },
            Target(TargetScope.SERVER, self.server_id),
        )
        return response.get("delivered") is True

    async def send_private_message(
        self, player_uuid: str, player_name: str, content: str
    ) -> bool:
        """Send a private message to a Minecraft player."""
        response = await self._request(
            Op.CHAT_PRIVATE,
            {
                "dedupe_key": self._dedupe_key("private"),
                "player": {
                    "uuid": player_uuid,
                    "name": player_name,
                    "server_id": self.server_id,
                },
                "message": plain_text_message(content, self._now()),
            },
            Target(TargetScope.SERVER, self.server_id),
        )
        return response.get("delivered") is True

    async def reply_to_ai(
        self,
        reply_to_message_id: str,
        content: str,
        private_player_uuid: str = "",
        private_player_name: str = "",
    ) -> bool:
        """Deliver an AstrBot response to an AI group or private request."""
        delivery: dict[str, str] = {"type": "broadcast"}
        if private_player_uuid or private_player_name:
            delivery = {
                "type": "private",
                "player_uuid": private_player_uuid,
                "player_name": private_player_name,
            }
        response = await self._request(
            Op.AI_REPLY,
            {
                "dedupe_key": self._dedupe_key("ai"),
                "reply_to_message_id": reply_to_message_id,
                "delivery": delivery,
                "message": plain_text_message(content, self._now()),
            },
            Target(TargetScope.SERVER, self.server_id),
        )
        return response.get("delivered") is True

    async def execute_command(
        self,
        command: str,
        requested_by: dict[str, str],
        target_server: str | None = None,
    ) -> dict[str, Any]:
        """Execute a console command using the Java command job contract."""
        target = Target(TargetScope.SERVER, target_server or self.server_id)
        return await self._request(
            Op.COMMAND_EXECUTE,
            {
                "dedupe_key": self._dedupe_key("command"),
                "command": command.lstrip("/").strip(),
                "executor": {"type": "console"},
                "requested_by": requested_by,
                "request_timeout_ms": self.config.request_timeout_ms,
                "execution_timeout_ms": self.config.request_timeout_ms,
                "mode": "immediate",
            },
            target,
        )

    async def get_logs(
        self,
        lines: int = 100,
        level: str = "",
        keyword: str = "",
        since: int | None = None,
        until: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query bounded server log entries through the Version 3 RPC.

        Args:
            lines: Maximum number of requested log records.
            level: Optional minimum log level.
            keyword: Optional content filter.
            since: Optional inclusive start timestamp in milliseconds.
            until: Optional inclusive end timestamp in milliseconds.

        Returns:
            Flattened log records from successful server responses.
        """
        payload = await self._request(
            Op.LOG_QUERY,
            {
                key: value
                for key, value in {
                    "tail_lines": max(1, min(lines, 1_000)),
                    "since": since,
                    "until": until,
                    "level": level or None,
                    "keyword": keyword or None,
                    "max_lines": max(1, min(lines, 1_000)),
                }.items()
                if value is not None
            },
            Target(TargetScope.SERVER, self.server_id),
        )
        entries: list[dict[str, Any]] = []
        servers = payload.get("servers")
        if not isinstance(servers, list):
            return entries
        for server in servers:
            if not isinstance(server, dict):
                continue
            records = server.get("lines")
            if isinstance(records, list):
                entries.extend(item for item in records if isinstance(item, dict))
        return entries

    async def _request(
        self, op: Op, payload: dict[str, Any], target: Target
    ) -> dict[str, Any]:
        try:
            response = await self.session.request(op, payload, target)
        except (RequestError, RuntimeError) as exc:
            raise ServerClientError(str(exc)) from exc
        if response.status is None:
            raise ServerClientError("server response has no status")
        if not response.status.ok:
            raise ServerClientError(
                response.status.message or response.status.code.value
            )
        return response.payload

    async def _dispatch_event(self, frame: Frame) -> None:
        for handler in tuple(self._event_handlers):
            await handler(frame)

    def _status_models(self, items: list[Any]) -> tuple[ServerInfo, ServerStatus]:
        normalized = [item for item in items if isinstance(item, dict)]
        if not normalized:
            raise ServerClientError(
                "server.status response has no valid server entries"
            )
        primary = next(
            (item for item in normalized if item.get("server_id") == self.server_id),
            normalized[0],
        )
        backends = [item for item in normalized if item is not primary]
        memory = (
            primary.get("memory") if isinstance(primary.get("memory"), dict) else {}
        )
        performance = (
            primary.get("performance")
            if isinstance(primary.get("performance"), dict)
            else {}
        )
        used = self._integer(memory.get("used_mb"))
        maximum = self._integer(memory.get("max_mb"))
        primary_status = primary.get("status")
        if not isinstance(primary_status, dict):
            primary_status = {}
        info = ServerInfo(
            name=str(
                primary.get("display_name")
                or primary.get("server_id")
                or self.server_id
            ),
            platform=str(primary.get("platform") or ""),
            platform_version=str(primary.get("version") or ""),
            minecraft_version=str(primary.get("version") or ""),
            max_players=self._integer(primary.get("max_players")),
            online_count=self._integer(primary.get("online_count")),
            uptime_formatted=self._format_duration(
                self._integer(primary.get("uptime_ms"))
            ),
            aggregate_online=sum(
                self._integer(item.get("online_count")) for item in normalized
            ),
            aggregate_max=sum(
                self._integer(item.get("max_players")) for item in normalized
            ),
        )
        status = ServerStatus(
            online=bool(primary_status.get("ok", True)),
            tps_1m=float(performance.get("tps") or 0),
            tps_5m=float(performance.get("tps") or 0),
            tps_15m=float(performance.get("tps") or 0),
            memory_used=used,
            memory_max=maximum,
            memory_free=max(0, maximum - used),
            memory_usage_percent=(used / maximum * 100) if maximum else 0.0,
            online_players=self._integer(primary.get("online_count")),
            max_players=self._integer(primary.get("max_players")),
            uptime_formatted=self._format_duration(
                self._integer(primary.get("uptime_ms"))
            ),
        )
        for backend in backends:
            backend_memory = (
                backend.get("memory") if isinstance(backend.get("memory"), dict) else {}
            )
            backend_performance = (
                backend.get("performance")
                if isinstance(backend.get("performance"), dict)
                else {}
            )
            backend_status = backend.get("status")
            if not isinstance(backend_status, dict):
                backend_status = {}
            backend_used = self._integer(backend_memory.get("used_mb"))
            backend_max = self._integer(backend_memory.get("max_mb"))
            info.backends.append(
                BackendServerInfo(
                    name=str(
                        backend.get("display_name") or backend.get("server_id") or ""
                    ),
                    platform=str(backend.get("platform") or ""),
                    version=str(backend.get("version") or ""),
                    online_count=self._integer(backend.get("online_count")),
                    max_players=self._integer(backend.get("max_players")),
                    uptime_formatted=self._format_duration(
                        self._integer(backend.get("uptime_ms"))
                    ),
                )
            )
            status.backends.append(
                BackendServerStatus(
                    name=str(
                        backend.get("display_name") or backend.get("server_id") or ""
                    ),
                    platform=str(backend.get("platform") or ""),
                    version=str(backend.get("version") or ""),
                    online=bool(backend_status.get("ok", True)),
                    online_players=self._integer(backend.get("online_count")),
                    max_players=self._integer(backend.get("max_players")),
                    uptime_formatted=self._format_duration(
                        self._integer(backend.get("uptime_ms"))
                    ),
                    tps_1m=float(backend_performance.get("tps") or 0),
                    tps_5m=float(backend_performance.get("tps") or 0),
                    tps_15m=float(backend_performance.get("tps") or 0),
                    memory_used=backend_used,
                    memory_max=backend_max,
                    memory_usage_percent=(backend_used / backend_max * 100)
                    if backend_max
                    else 0.0,
                )
            )
        return info, status

    def _dedupe_key(self, operation: str) -> str:
        return f"astrbot:{self.server_id}:{operation}:{uuid.uuid4().hex}"

    @staticmethod
    def _integer(value: object) -> int:
        return value if isinstance(value, int) else 0

    @staticmethod
    def _looks_like_uuid(value: str) -> bool:
        try:
            uuid.UUID(value)
        except ValueError:
            return False
        return True

    @staticmethod
    def _format_duration(value: int) -> str:
        if value <= 0:
            return "未知"
        seconds = value // 1_000
        hours, seconds = divmod(seconds, 3_600)
        minutes, seconds = divmod(seconds, 60)
        if hours:
            return f"{hours}小时{minutes}分"
        if minutes:
            return f"{minutes}分{seconds}秒"
        return f"{seconds}秒"

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1_000)
