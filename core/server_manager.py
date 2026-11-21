"""Version 3 Minecraft server client lifecycle management."""

from collections.abc import Awaitable, Callable

from astrbot.api import logger

from .auth.auth_session import AuthSession
from .auth.identity_store import IdentityStore
from .config import ServerConfig
from .protocol.frame import Frame
from .protocol.payloads import ServerInfo
from .server_client import ServerClient
from .transport.session import V3Session

EventHandler = Callable[[str, Frame], Awaitable[None]]
ConnectHandler = Callable[[str, ServerInfo], Awaitable[None]]
DisconnectHandler = Callable[[str, str], Awaitable[None]]


class ServerConnection:
    """One configured V3 server client and its cached presentation identity."""

    def __init__(
        self,
        config: ServerConfig,
        identity_store: IdentityStore,
        on_event: EventHandler | None,
        on_connect: ConnectHandler | None,
        on_disconnect: DisconnectHandler | None,
    ):
        self.config = config
        self.server_info: ServerInfo | None = None
        identity = identity_store.load_or_create_identity()
        session = V3Session(config, AuthSession(config, identity, identity_store))
        self.client = ServerClient(config, session)
        if on_event is not None:
            self.client.add_event_handler(
                lambda frame: on_event(config.server_id, frame)
            )
        session.set_connection_handlers(
            self._connected if on_connect is not None else None,
            self._disconnected if on_disconnect is not None else None,
        )
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect

    @property
    def server_id(self) -> str:
        """Return the configured server identifier."""
        return self.config.server_id

    @property
    def connected(self) -> bool:
        """Whether the V3 client has an authenticated connection."""
        return self.client.connected

    async def start(self) -> None:
        """Start the connection's secure reconnect loop."""
        await self.client.start()

    async def stop(self) -> None:
        """Stop the connection's secure reconnect loop."""
        await self.client.stop()

    async def _connected(self) -> None:
        self.server_info = ServerInfo(
            name=self.config.display_name or self.server_id,
        )
        if self._on_connect is not None:
            await self._on_connect(self.server_id, self.server_info)

    async def _disconnected(self, reason: str) -> None:
        self.server_info = None
        if self._on_disconnect is not None:
            await self._on_disconnect(self.server_id, reason)


class ServerManager:
    """Manage isolated Version 3 connections for all configured MC servers."""

    def __init__(self, identity_store: IdentityStore):
        self._identity_store = identity_store
        self._servers: dict[str, ServerConnection] = {}
        self._on_event: EventHandler | None = None
        self._on_connect: ConnectHandler | None = None
        self._on_disconnect: DisconnectHandler | None = None

    def set_event_handler(self, handler: EventHandler) -> None:
        """Set the Frame event dispatcher before adding servers."""
        self._on_event = handler

    def set_connect_handler(self, handler: ConnectHandler) -> None:
        """Set the authentication-success lifecycle callback."""
        self._on_connect = handler

    def set_disconnect_handler(self, handler: DisconnectHandler) -> None:
        """Set the authenticated-session disconnect callback."""
        self._on_disconnect = handler

    def add_server(self, config: ServerConfig) -> bool:
        """Register one unique V3 server configuration."""
        if config.server_id in self._servers:
            logger.warning("[ServerManager] Duplicate server ID: %s", config.server_id)
            return False
        self._servers[config.server_id] = ServerConnection(
            config,
            self._identity_store,
            self._on_event,
            self._on_connect,
            self._on_disconnect,
        )
        return True

    def get_server(self, server_id: str) -> ServerConnection | None:
        """Return one registered V3 connection by identifier."""
        return self._servers.get(server_id)

    def get_all_servers(self) -> dict[str, ServerConnection]:
        """Return a shallow map of every registered V3 connection."""
        return self._servers.copy()

    def get_connected_servers(self) -> list[ServerConnection]:
        """Return connections that completed mutual authentication."""
        return [server for server in self._servers.values() if server.connected]

    async def start_all(self) -> None:
        """Start all enabled secure clients."""
        for server in self._servers.values():
            if server.config.enabled:
                await server.start()

    async def stop_all(self) -> None:
        """Stop every secure client and wait for cleanup."""
        for server in self._servers.values():
            await server.stop()
