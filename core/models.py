"""Minecraft 适配器插件的数据模型"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

E = TypeVar("E", bound=Enum)


def safe_enum(enum_class: type[E], value: str, default: E) -> E:
    """安全地解析枚举值，如果无效则返回默认值"""
    try:
        return enum_class(value)
    except ValueError:
        return default


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    return bool(value) if value is not None else default


def _primary_server(servers: list[dict]) -> dict:
    for server in servers:
        if server.get("scope") in {"local", "proxy"}:
            return server
    return servers[0] if servers else {}


class MessageType(str, Enum):
    """根据协议定义的 WebSocket 消息类型"""

    HEARTBEAT = "HEARTBEAT"
    HEARTBEAT_ACK = "HEARTBEAT_ACK"
    CONNECTION_ACK = "CONNECTION_ACK"
    CHAT_REQUEST = "CHAT_REQUEST"
    CHAT_RESPONSE = "CHAT_RESPONSE"
    MESSAGE_FORWARD = "MESSAGE_FORWARD"
    MESSAGE_INCOMING = "MESSAGE_INCOMING"
    PLAYER_JOIN = "PLAYER_JOIN"
    PLAYER_QUIT = "PLAYER_QUIT"
    COMMAND_REQUEST = "COMMAND_REQUEST"
    COMMAND_RESPONSE = "COMMAND_RESPONSE"
    STATUS_UPDATE = "STATUS_UPDATE"
    ERROR = "ERROR"
    DISCONNECT = "DISCONNECT"


class SourceType(str, Enum):
    """消息源类型"""

    PLAYER = "PLAYER"
    SERVER = "SERVER"
    SYSTEM = "SYSTEM"


class TargetType(str, Enum):
    """消息目标类型"""

    PLAYER = "PLAYER"
    BROADCAST = "BROADCAST"
    SERVER = "SERVER"


class ChatMode(str, Enum):
    """聊天模式（用于 AI 聊天）"""

    GROUP = "GROUP"
    PRIVATE = "PRIVATE"


class ErrorCode(int, Enum):
    """根据协议定义的错误码"""

    SUCCESS = 0
    AUTH_INVALID = 1001
    AUTH_EXPIRED = 1002
    AUTH_MISSING = 1003
    PARAM_ERROR = 2001
    FORMAT_ERROR = 2002
    PARAM_MISSING = 2003
    INTERNAL_ERROR = 3001
    SERVICE_UNAVAILABLE = 3002
    NOT_FOUND = 4001
    PLAYER_OFFLINE = 4002
    FEATURE_DISABLED = 4003
    COMMAND_FAILED = 5001
    COMMAND_FILTERED = 5002
    NO_PERMISSION = 5003


@dataclass
class BackendServerInfo:
    """Backend server info reported via Velocity proxy"""

    id: str = ""
    name: str = ""
    display_name: str = ""
    platform: str = ""
    version: str = ""
    motd: str = ""
    online_count: int = 0
    max_players: int = 0
    uptime: int = 0
    uptime_formatted: str = ""
    tps: dict = field(default_factory=dict)
    mspt: float | None = None
    memory: dict = field(default_factory=dict)
    online: bool = True
    scope: str = "backend"

    @classmethod
    def from_dict(cls, data: dict) -> "BackendServerInfo":
        server_id = _str(data.get("id", data.get("serverId", data.get("name", ""))))
        name = _str(data.get("name", server_id))
        return cls(
            id=server_id,
            name=name,
            display_name=_str(data.get("displayName", name)),
            platform=_str(data.get("platform", "")),
            version=_str(data.get("version", "")),
            motd=_str(data.get("motd", "")),
            online_count=_int(data.get("onlineCount", data.get("onlinePlayers", 0))),
            max_players=_int(data.get("maxPlayers", 0)),
            uptime=_int(data.get("uptime", 0)),
            uptime_formatted=_str(data.get("uptimeFormatted", "")),
            tps=_dict(data.get("tps")),
            mspt=_float(data.get("mspt"), 0.0) if data.get("mspt") is not None else None,
            memory=_dict(data.get("memory")),
            online=_bool(data.get("online"), True),
            scope=_str(data.get("scope", "backend")),
        )


@dataclass
class ServerInfo:
    """来自连接或 API 的服务器信息"""

    id: str = ""
    name: str = ""
    display_name: str = ""
    platform: str = ""
    platform_version: str = ""
    minecraft_version: str = ""
    motd: str = ""
    max_players: int = 0
    online_count: int = 0
    uptime: int = 0
    uptime_formatted: str = ""
    # Velocity proxy mode fields
    backends: list[BackendServerInfo] = field(default_factory=list)
    backend_count: int = 0
    aggregate_online: int = 0
    aggregate_max: int = 0
    scope: str = ""
    protocol_version: int = 0
    api_version: str = ""
    features: list[str] = field(default_factory=list)

    @property
    def is_proxy(self) -> bool:
        """Whether this server is a Velocity proxy with backends"""
        return self.scope == "proxy" or self.backend_count > 0 or len(self.backends) > 0

    @classmethod
    def from_dict(cls, data: dict) -> "ServerInfo":
        servers = [s for s in _list(data.get("servers")) if isinstance(s, dict)]
        source = _primary_server(servers) if servers else data
        backends = [
            BackendServerInfo.from_dict(s)
            for s in servers
            if s.get("scope") == "backend"
        ]
        aggregate = _dict(data.get("aggregate"))
        version = _str(source.get("minecraftVersion", source.get("version", "")))
        server_id = _str(source.get("id", source.get("serverId", source.get("name", ""))))
        name = _str(source.get("name", server_id))
        return cls(
            id=server_id,
            name=name,
            display_name=_str(source.get("displayName", name)),
            platform=_str(source.get("platform", "")),
            platform_version=_str(source.get("platformVersion", version)),
            minecraft_version=version,
            motd=_str(source.get("motd", "")),
            max_players=_int(source.get("maxPlayers", 0)),
            online_count=_int(source.get("onlineCount", source.get("onlinePlayers", 0))),
            uptime=_int(source.get("uptime", 0)),
            uptime_formatted=_str(source.get("uptimeFormatted", "")),
            backends=backends,
            backend_count=_int(aggregate.get("backendCount", len(backends))),
            aggregate_online=_int(aggregate.get("totalOnlinePlayers", 0)),
            aggregate_max=_int(aggregate.get("totalMaxPlayers", 0)),
            scope=_str(source.get("scope", "")),
            protocol_version=_int(data.get("protocolVersion", 0)),
            api_version=_str(data.get("apiVersion", "")),
            features=[_str(f) for f in _list(data.get("features"))],
        )


@dataclass
class PlayerInfo:
    """基本玩家信息"""

    uuid: str = ""
    name: str = ""
    display_name: str = ""
    online: bool = True
    ping: int = 0
    world: str = ""
    game_mode: str = ""
    is_op: bool = False
    server: str = ""  # Backend route ID (Velocity proxy mode)
    last_known_server: str = ""
    data_source: str = "live"

    @classmethod
    def from_dict(cls, data: dict) -> "PlayerInfo":
        return cls(
            uuid=_str(data.get("uuid", "")),
            name=_str(data.get("name", "")),
            display_name=_str(data.get("displayName", "")),
            online=_bool(data.get("online"), True),
            ping=_int(data.get("ping", 0)),
            world=_str(data.get("world", "未知")),
            game_mode=_str(data.get("gameMode", "")),
            is_op=_bool(data.get("isOp"), False),
            server=_str(data.get("server", "")),
            last_known_server=_str(data.get("lastKnownServer", "")),
            data_source=_str(data.get("dataSource", "live")),
        )


@dataclass
class PlayerDetail(PlayerInfo):
    """详细玩家信息"""

    health: float = 20.0
    max_health: float = 20.0
    food_level: int = 20
    level: int = 0
    exp: float = 0.0
    total_exp: int = 0
    location: dict = field(default_factory=dict)
    is_flying: bool = False
    online_time: int = 0
    online_time_formatted: str = ""
    first_played: int = 0
    last_played: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "PlayerDetail":
        return cls(
            uuid=_str(data.get("uuid", "")),
            name=_str(data.get("name", "")),
            display_name=_str(data.get("displayName", "")),
            online=_bool(data.get("online"), True),
            ping=_int(data.get("ping", 0)),
            world=_str(data.get("world", "")),
            game_mode=_str(data.get("gameMode", "")),
            is_op=_bool(data.get("isOp"), False),
            server=_str(data.get("server", "")),
            last_known_server=_str(data.get("lastKnownServer", "")),
            data_source=_str(data.get("dataSource", "live")),
            health=_float(data.get("health"), 20.0),
            max_health=max(_float(data.get("maxHealth"), 20.0), 1.0),
            food_level=_int(data.get("foodLevel", 20), 20),
            level=_int(data.get("level", 0)),
            exp=_float(data.get("exp", 0.0)),
            total_exp=_int(data.get("totalExp", 0)),
            location=_dict(data.get("location")),
            is_flying=_bool(data.get("isFlying"), False),
            online_time=_int(data.get("onlineTime", 0)),
            online_time_formatted=_str(data.get("onlineTimeFormatted", "")),
            first_played=_int(data.get("firstPlayed", 0)),
            last_played=_int(data.get("lastPlayed", 0)),
        )


@dataclass
class MCMessageSource:
    """消息源信息"""

    type: SourceType = SourceType.PLAYER
    server_name: str = ""
    server_platform: str = ""
    player_uuid: str = ""
    player_name: str = ""
    player_display_name: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "MCMessageSource":
        server = data.get("server", {})
        player = data.get("player", {})
        return cls(
            type=safe_enum(SourceType, data.get("type", "PLAYER"), SourceType.PLAYER),
            server_name=server.get("name", ""),
            server_platform=server.get("platform", ""),
            player_uuid=player.get("uuid", ""),
            player_name=player.get("name", ""),
            player_display_name=player.get("displayName", ""),
        )


@dataclass
class MCMessageTarget:
    """消息目标信息"""

    type: TargetType = TargetType.BROADCAST
    player_uuid: str = ""
    player_name: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "MCMessageTarget":
        return cls(
            type=safe_enum(
                TargetType, data.get("type", "BROADCAST"), TargetType.BROADCAST
            ),
            player_uuid=data.get("playerUuid", ""),
            player_name=data.get("playerName", ""),
        )

    def to_dict(self) -> dict:
        result = {"type": self.type.value}
        if self.player_uuid:
            result["playerUuid"] = self.player_uuid
        if self.player_name:
            result["playerName"] = self.player_name
        return result


@dataclass
class MCMessage:
    """用于 WebSocket 通信的统一消息结构"""

    type: MessageType
    id: str = ""
    source: MCMessageSource | None = None
    target: MCMessageTarget | None = None
    payload: dict = field(default_factory=dict)
    timestamp: int = 0
    reply_to: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "MCMessage":
        source = None
        target = None
        if "source" in data:
            source = MCMessageSource.from_dict(data["source"])
        if "target" in data:
            target = MCMessageTarget.from_dict(data["target"])

        return cls(
            type=safe_enum(MessageType, data.get("type", "ERROR"), MessageType.ERROR),
            id=data.get("id", ""),
            source=source,
            target=target,
            payload=data.get("payload", {}),
            timestamp=data.get("timestamp", 0),
            reply_to=data.get("replyTo", ""),
        )

    def to_dict(self) -> dict:
        result: dict[str, Any] = {
            "type": self.type.value,
            "id": self.id,
            "timestamp": self.timestamp,
        }
        if self.target:
            result["target"] = self.target.to_dict()
        if self.payload:
            result["payload"] = self.payload
        if self.reply_to:
            result["replyTo"] = self.reply_to
        return result


@dataclass
class ServerConfig:
    """服务器连接配置"""

    enabled: bool = True
    server_id: str = ""
    host: str = "localhost"
    port: int = 8765
    token: str = ""
    enable_ai_chat: bool = True
    text2image: bool = True
    # 消息转发配置
    forward_chat_to_astrbot: bool = True
    forward_chat_format: str = "<{player}> {message}"
    forward_join_leave_to_astrbot: bool = False
    target_sessions: list[str] = field(default_factory=list)
    auto_forward_prefix: str = "*"
    mark_option: str = "emoji"
    # 命令配置
    cmd_enabled: bool = True
    cmd_white_black_list: str = "white"
    cmd_list: list[str] = field(default_factory=list)
    bind_enable: bool = True
    custom_cmd_list: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "ServerConfig":
        server = data.get("server", {})
        message = data.get("message", {})
        cmd = data.get("cmd", {})  # cmd 与 message 处于同一层级，不是嵌套关系

        return cls(
            enabled=data.get("enabled", True),
            server_id=server.get("server_id", ""),
            host=server.get("host", "localhost"),
            port=server.get("port", 8765),
            token=server.get("token", ""),
            enable_ai_chat=data.get("enable_ai_chat", True),
            text2image=data.get("text2image", True),
            forward_chat_to_astrbot=message.get("forward_chat_to_astrbot", True),
            forward_chat_format=message.get(
                "forward_chat_format", "<{player}> {message}"
            ),
            forward_join_leave_to_astrbot=message.get(
                "forward_join_leave_to_astrbot", False
            ),
            target_sessions=message.get("target_sessions", []),
            auto_forward_prefix=message.get("auto_forward_prefix", "*"),
            mark_option=message.get("mark_option", "emoji"),
            cmd_enabled=cmd.get("enabled", True),
            cmd_white_black_list=cmd.get("cmd_white_black_list", "white"),
            cmd_list=cmd.get("cmd_list", []),
            bind_enable=cmd.get("bind_enable", True),
            custom_cmd_list=cmd.get("custom_cmd_list", []),
        )


@dataclass
class BackendServerStatus:
    """Backend server status from Velocity proxy"""

    id: str = ""
    name: str = ""
    display_name: str = ""
    platform: str = ""
    version: str = ""
    online: bool = True
    online_players: int = 0
    max_players: int = 0
    uptime: int = 0
    uptime_formatted: str = ""
    mspt: float | None = None
    tps_1m: float = 0.0
    tps_5m: float = 0.0
    tps_15m: float = 0.0
    memory_used: int = 0
    memory_max: int = 0
    memory_usage_percent: float = 0.0
    scope: str = "backend"

    @classmethod
    def from_dict(cls, data: dict) -> "BackendServerStatus":
        tps = _dict(data.get("tps"))
        memory = _dict(data.get("memory"))
        tps_1m = _float(tps.get("tps1m", tps.get("1m", 0.0)))
        tps_5m = _float(tps.get("tps5m", tps.get("5m", 0.0)))
        tps_15m = _float(tps.get("tps15m", tps.get("15m", 0.0)))
        memory_used = _int(memory.get("used", 0))
        memory_max = _int(memory.get("max", memory.get("total", 0)))
        if memory_max:
            memory_usage_percent = (memory_used / memory_max) * 100
        else:
            memory_usage_percent = 0.0
        server_id = _str(data.get("id", data.get("serverId", data.get("name", ""))))
        name = _str(data.get("name", server_id))
        return cls(
            id=server_id,
            name=name,
            display_name=_str(data.get("displayName", name)),
            platform=_str(data.get("platform", "")),
            version=_str(data.get("version", "")),
            online=_bool(data.get("online"), True),
            online_players=_int(data.get("onlinePlayers", 0)),
            max_players=_int(data.get("maxPlayers", 0)),
            uptime=_int(data.get("uptime", 0)),
            uptime_formatted=_str(data.get("uptimeFormatted", "")),
            mspt=_float(data.get("mspt"), 0.0) if data.get("mspt") is not None else None,
            tps_1m=tps_1m,
            tps_5m=tps_5m,
            tps_15m=tps_15m,
            memory_used=memory_used,
            memory_max=memory_max,
            memory_usage_percent=memory_usage_percent,
            scope=_str(data.get("scope", "backend")),
        )


@dataclass
class ServerStatus:
    """服务器状态信息"""

    id: str = ""
    name: str = ""
    display_name: str = ""
    online: bool = False
    mspt: float | None = None
    tps_1m: float = 0.0
    tps_5m: float = 0.0
    tps_15m: float = 0.0
    memory_used: int = 0
    memory_max: int = 0
    memory_free: int = 0
    memory_usage_percent: float = 0.0
    online_players: int = 0
    max_players: int = 0
    uptime: int = 0
    uptime_formatted: str = ""
    worlds: list[dict] = field(default_factory=list)
    plugins_total: int = 0
    plugins_enabled: int = 0
    # Velocity proxy mode: backend server statuses
    backends: list[BackendServerStatus] = field(default_factory=list)
    scope: str = ""
    protocol_version: int = 0
    api_version: str = ""
    features: list[str] = field(default_factory=list)

    @property
    def is_proxy(self) -> bool:
        """Whether this status contains backend server data"""
        return self.scope == "proxy" or len(self.backends) > 0

    @classmethod
    def from_dict(cls, data: dict) -> "ServerStatus":
        servers = [s for s in _list(data.get("servers")) if isinstance(s, dict)]
        source = _primary_server(servers) if servers else data
        backends = [
            BackendServerStatus.from_dict(s)
            for s in servers
            if s.get("scope") == "backend"
        ]
        tps = _dict(source.get("tps"))
        memory = _dict(source.get("memory"))
        plugins = _dict(source.get("plugins"))

        tps_1m = _float(tps.get("tps1m", tps.get("1m", 0.0)))
        tps_5m = _float(tps.get("tps5m", tps.get("5m", 0.0)))
        tps_15m = _float(tps.get("tps15m", tps.get("15m", 0.0)))

        memory_used = _int(memory.get("used", 0))
        memory_max = _int(memory.get("max", memory.get("total", 0)))
        memory_free = _int(memory.get("free", max(memory_max - memory_used, 0)))
        if memory_max:
            memory_usage_percent = _float(
                memory.get("usagePercent"), (memory_used / memory_max) * 100
            )
        else:
            memory_usage_percent = _float(memory.get("usagePercent", 0.0))

        server_id = _str(source.get("id", source.get("serverId", source.get("name", ""))))
        name = _str(source.get("name", server_id))

        return cls(
            id=server_id,
            name=name,
            display_name=_str(source.get("displayName", name)),
            online=_bool(source.get("online"), True),
            mspt=_float(source.get("mspt"), 0.0) if source.get("mspt") is not None else None,
            tps_1m=tps_1m,
            tps_5m=tps_5m,
            tps_15m=tps_15m,
            memory_used=memory_used,
            memory_max=memory_max,
            memory_free=memory_free,
            memory_usage_percent=memory_usage_percent,
            online_players=_int(source.get("onlinePlayers", 0)),
            max_players=_int(source.get("maxPlayers", 0)),
            uptime=_int(source.get("uptime", 0)),
            uptime_formatted=_str(source.get("uptimeFormatted", "")),
            worlds=_list(source.get("worlds")),
            plugins_total=_int(plugins.get("total", 0)),
            plugins_enabled=_int(plugins.get("enabled", 0)),
            backends=backends,
            scope=_str(source.get("scope", "")),
            protocol_version=_int(data.get("protocolVersion", 0)),
            api_version=_str(data.get("apiVersion", "")),
            features=[_str(f) for f in _list(data.get("features"))],
        )


@dataclass
class LogEntry:
    """服务器日志条目"""

    timestamp: int = 0
    level: str = ""
    logger: str = ""
    message: str = ""
    server: str = ""
    scope: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "LogEntry":
        if isinstance(data, str):
            return cls(message=data)
        if not isinstance(data, dict):
            return cls(message=str(data))
        return cls(
            timestamp=_int(data.get("timestamp", 0)),
            level=_str(data.get("level", "")),
            logger=_str(data.get("logger", "")),
            message=_str(data.get("message", "")),
            server=_str(data.get("server", "")),
            scope=_str(data.get("scope", "")),
        )


@dataclass
class ApiResponse:
    """REST API 响应结构"""

    code: int = 0
    message: str = ""
    data: Any = None
    timestamp: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "ApiResponse":
        return cls(
            code=data.get("code", 0),
            message=data.get("message", ""),
            data=data.get("data"),
            timestamp=data.get("timestamp", 0),
        )

    @property
    def success(self) -> bool:
        return self.code == 0
