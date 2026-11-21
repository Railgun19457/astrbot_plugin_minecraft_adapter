"""Version 3 plugin configuration parsing and endpoint safety checks."""

from dataclasses import dataclass, field
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .protocol.constants import Role


class ConfigError(ValueError):
    """Raised when a server configuration is unsafe or incomplete."""


@dataclass(frozen=True)
class ReconnectConfig:
    """Reconnect timing settings for a server endpoint."""

    initial_delay_ms: int = 1_000
    max_delay_ms: int = 60_000
    jitter_ratio: float = 0.2


def _validate_timeout(value: object, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _validate_reconnect(data: object) -> ReconnectConfig:
    """Validate reconnect values before a connection loop uses them.

    Args:
        data: Optional configuration mapping supplied by AstrBot.

    Returns:
        Bounded reconnect configuration.

    Raises:
        ConfigError: If a value is not usable for safe backoff.
    """
    settings = data if isinstance(data, dict) else {}
    initial = _validate_timeout(
        settings.get("initial_delay_ms", 1_000),
        "reconnect.initial_delay_ms",
        250,
        60_000,
    )
    maximum = _validate_timeout(
        settings.get("max_delay_ms", 60_000),
        "reconnect.max_delay_ms",
        initial,
        300_000,
    )
    try:
        jitter = float(settings.get("jitter_ratio", 0.2))
    except (TypeError, ValueError) as exc:
        raise ConfigError("reconnect.jitter_ratio must be a number") from exc
    if not 0 <= jitter <= 1:
        raise ConfigError("reconnect.jitter_ratio must be between 0 and 1")
    return ReconnectConfig(initial, maximum, jitter)


def _is_loopback(host: str | None) -> bool:
    return host is not None and host.lower() in {"localhost", "127.0.0.1", "::1"}


def canonical_endpoint_url(value: object) -> str:
    """Validate a V3 endpoint URL and return its canonical representation.

    Args:
        value: Configured endpoint URL.

    Returns:
        A normalized WSS URL, or a permitted local WS URL.

    Raises:
        ConfigError: If the URL can leak credentials or uses unsafe transport.
    """
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("server.url is required")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("server.url contains an invalid port") from exc
    if parsed.scheme not in {"ws", "wss"}:
        raise ConfigError("server.url must use wss:// or a local ws:// endpoint")
    if not parsed.hostname:
        raise ConfigError("server.url must include a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("server.url must not include credentials, query, or fragment")
    if parsed.path != "/astrbot":
        raise ConfigError("server.url path must be exactly /astrbot")
    if parsed.scheme == "ws" and not _is_loopback(parsed.hostname):
        raise ConfigError(
            "remote ws:// endpoints are forbidden; configure wss:// instead"
        )

    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default_port = 443 if parsed.scheme == "wss" else 80
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    normalized = SplitResult(parsed.scheme, netloc, "/astrbot", "", "")
    return urlunsplit(normalized)


@dataclass
class ServerConfig:
    """A validated V3 endpoint and the existing user-facing feature settings."""

    enabled: bool = True
    server_id: str = ""
    display_name: str = ""
    url: str = ""
    expected_role: Role = Role.STANDALONE
    pairing_id: str = ""
    pairing_code: str = ""
    reconnect: ReconnectConfig = field(default_factory=ReconnectConfig)
    request_timeout_ms: int = 10_000
    heartbeat_interval_ms: int = 30_000
    enable_ai_chat: bool = True
    text2image: bool = True
    forward_chat_to_astrbot: bool = True
    forward_chat_format: str = "<{player}> {message}"
    forward_join_leave_to_astrbot: bool = False
    target_sessions: list[str] = field(default_factory=list)
    auto_forward_prefix: str = "*"
    mark_option: str = "emoji"
    cmd_enabled: bool = True
    cmd_white_black_list: str = "white"
    cmd_list: list[str] = field(default_factory=list)
    bind_enable: bool = True
    custom_cmd_list: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "ServerConfig":
        """Parse one AstrBot server template without accepting V2 credentials.

        Args:
            data: One item from the ``mc_servers`` configuration list.

        Returns:
            A validated V3 server configuration.

        Raises:
            ConfigError: If configuration is legacy, malformed, or unsafe.
        """
        if not isinstance(data, dict):
            raise ConfigError("server configuration must be an object")
        server = data.get("server", {})
        if not isinstance(server, dict):
            raise ConfigError("server must be an object")
        legacy_fields = {"host", "port", "token"}.intersection(server)
        if legacy_fields:
            fields = ", ".join(sorted(legacy_fields))
            raise ConfigError(
                f"legacy V2 fields are not supported: {fields}; configure server.url"
            )

        server_id = str(server.get("server_id", "")).strip()
        if not server_id:
            raise ConfigError("server.server_id is required")
        if len(server_id) > 128 or any(char.isspace() for char in server_id):
            raise ConfigError("server.server_id must be a non-whitespace identifier")
        role_value = str(server.get("expected_role", Role.STANDALONE.value)).lower()
        try:
            role = Role(role_value)
        except ValueError as exc:
            raise ConfigError(
                "server.expected_role must be standalone or velocity"
            ) from exc
        if role not in {Role.STANDALONE, Role.VELOCITY}:
            raise ConfigError("server.expected_role must be standalone or velocity")

        message = data.get("message", {})
        command = data.get("cmd", {})
        if not isinstance(message, dict) or not isinstance(command, dict):
            raise ConfigError("message and cmd must be objects")
        pairing_id = str(server.get("pairing_id", "")).strip()
        pairing_code = str(server.get("pairing_code", "")).strip()
        if bool(pairing_id) != bool(pairing_code):
            raise ConfigError("pairing_id and pairing_code must be configured together")

        mark_option = str(message.get("mark_option", "emoji"))
        if mark_option not in {"text", "emoji", "none"}:
            raise ConfigError("message.mark_option must be text, emoji, or none")
        list_mode = str(command.get("cmd_white_black_list", "white")).lower()
        if list_mode not in {"white", "black", "none"}:
            raise ConfigError("cmd.cmd_white_black_list must be white, black, or none")

        return cls(
            enabled=bool(data.get("enabled", True)),
            server_id=server_id,
            display_name=str(server.get("display_name", "")).strip(),
            url=canonical_endpoint_url(server.get("url")),
            expected_role=role,
            pairing_id=pairing_id,
            pairing_code=pairing_code,
            reconnect=_validate_reconnect(server.get("reconnect")),
            request_timeout_ms=_validate_timeout(
                server.get("request_timeout_ms", 10_000),
                "server.request_timeout_ms",
                1_000,
                120_000,
            ),
            heartbeat_interval_ms=_validate_timeout(
                server.get("heartbeat_interval_ms", 30_000),
                "server.heartbeat_interval_ms",
                5_000,
                300_000,
            ),
            enable_ai_chat=bool(data.get("enable_ai_chat", True)),
            text2image=bool(data.get("text2image", True)),
            forward_chat_to_astrbot=bool(message.get("forward_chat_to_astrbot", True)),
            forward_chat_format=str(
                message.get("forward_chat_format", "<{player}> {message}")
            ),
            forward_join_leave_to_astrbot=bool(
                message.get("forward_join_leave_to_astrbot", False)
            ),
            target_sessions=[str(item) for item in message.get("target_sessions", [])],
            auto_forward_prefix=str(message.get("auto_forward_prefix", "*")),
            mark_option=mark_option,
            cmd_enabled=bool(command.get("enabled", True)),
            cmd_white_black_list=list_mode,
            cmd_list=[str(item) for item in command.get("cmd_list", [])],
            bind_enable=bool(command.get("bind_enable", True)),
            custom_cmd_list=[str(item) for item in command.get("custom_cmd_list", [])],
        )
