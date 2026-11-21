"""Payload construction and conversion for Version 3 domain operations."""

from dataclasses import dataclass, field
from typing import Any


def plain_text_message(
    content: str, created_at: int, message_id: str = ""
) -> dict[str, Any]:
    """Create the contract's plain text message payload.

    Args:
        content: Message content to deliver.
        created_at: Unix epoch timestamp in milliseconds.
        message_id: Optional source message identifier.

    Returns:
        A ``common.text_message`` payload.
    """
    payload: dict[str, Any] = {
        "content": content,
        "plain_text": content,
        "format": "plain",
        "mentions": [],
        "created_at": created_at,
    }
    if message_id:
        payload["message_id"] = message_id
    return payload


@dataclass
class BackendServerInfo:
    """Server information rendered as a Velocity backend card."""

    name: str = ""
    platform: str = ""
    version: str = ""
    online_count: int = 0
    max_players: int = 0
    uptime_formatted: str = ""
    tps: dict[str, float] = field(default_factory=dict)
    memory: dict[str, int] = field(default_factory=dict)
    online: bool = True


@dataclass
class ServerInfo:
    """Server information retained for existing renderers and command UX."""

    name: str = ""
    platform: str = ""
    platform_version: str = ""
    minecraft_version: str = ""
    max_players: int = 0
    online_count: int = 0
    uptime_formatted: str = ""
    backends: list[BackendServerInfo] = field(default_factory=list)
    aggregate_online: int = 0
    aggregate_max: int = 0

    @property
    def is_proxy(self) -> bool:
        """Whether this server aggregates Velocity backend results."""
        return bool(self.backends)


@dataclass
class BackendServerStatus:
    """Status values rendered for one Velocity backend."""

    name: str = ""
    platform: str = ""
    version: str = ""
    online: bool = True
    online_players: int = 0
    max_players: int = 0
    uptime_formatted: str = ""
    tps_1m: float = 0.0
    tps_5m: float = 0.0
    tps_15m: float = 0.0
    memory_used: int = 0
    memory_max: int = 0
    memory_usage_percent: float = 0.0


@dataclass
class ServerStatus:
    """Server status retained for the existing renderer."""

    online: bool = False
    tps_1m: float = 0.0
    tps_5m: float = 0.0
    tps_15m: float = 0.0
    memory_used: int = 0
    memory_max: int = 0
    memory_free: int = 0
    memory_usage_percent: float = 0.0
    online_players: int = 0
    max_players: int = 0
    uptime_formatted: str = ""
    worlds: list[dict[str, Any]] = field(default_factory=list)
    backends: list[BackendServerStatus] = field(default_factory=list)

    @property
    def is_proxy(self) -> bool:
        """Whether this status contains backend results."""
        return bool(self.backends)


@dataclass
class PlayerInfo:
    """A player item converted from a Version 3 player list response."""

    uuid: str = ""
    name: str = ""
    display_name: str = ""
    ping: int = 0
    world: str = ""
    game_mode: str = ""
    is_op: bool = False
    server: str = ""

    @classmethod
    def from_v3(cls, data: dict[str, Any]) -> "PlayerInfo":
        """Convert a ``player.list`` item into renderer-compatible data."""
        return cls(
            uuid=str(data.get("uuid", "")),
            name=str(data.get("name", "")),
            display_name=str(data.get("display_name", "")),
            ping=int(data.get("ping_ms") or 0),
            world=str(data.get("world", "")),
            game_mode=str(data.get("game_mode", "")),
            server=str(data.get("server_id", "")),
        )


@dataclass
class PlayerDetail(PlayerInfo):
    """A player detail item converted from a Version 3 response."""

    online: bool = False
    health: float = 20.0
    max_health: float = 20.0
    food_level: int = 20
    level: int = 0
    exp: float = 0.0
    location: dict[str, Any] = field(default_factory=dict)
    first_played: int = 0
    last_played: int = 0
    online_time: int = 0

    @classmethod
    def from_v3(cls, data: dict[str, Any]) -> "PlayerDetail":
        """Convert a ``player.detail`` item into renderer-compatible data."""
        location = (
            data.get("location") if isinstance(data.get("location"), dict) else {}
        )
        return cls(
            uuid=str(data.get("uuid", "")),
            name=str(data.get("name", "")),
            display_name=str(data.get("display_name", "")),
            ping=int(data.get("ping_ms") or 0),
            world=str(data.get("world", "")),
            game_mode=str(data.get("game_mode", "")),
            is_op=bool(data.get("is_operator", False)),
            server=str(data.get("server_id", "")),
            online=bool(data.get("online", False)),
            health=float(data.get("health") or 0),
            max_health=float(data.get("max_health") or 0),
            food_level=int(data.get("food_level") or 0),
            level=int(data.get("level") or 0),
            exp=float(data.get("exp") or 0),
            location=location,
            first_played=int(data.get("first_played_at") or 0),
            last_played=int(data.get("last_played_at") or 0),
            online_time=int(data.get("online_duration_ms") or 0),
        )
