"""Authenticated WebSocket session for the Version 3 Minecraft protocol."""

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from astrbot.api import logger

from ..auth.auth_session import AuthenticationError, AuthSession
from ..config import ServerConfig
from ..protocol.codec import ProtocolError, decode_frame, encode_frame
from ..protocol.constants import PROTOCOL_VERSION, FrameType, Op, Role, TargetScope
from ..protocol.frame import Frame, NodeRef, Route, Target
from .backoff import ReconnectBackoff
from .request_manager import RequestError, RequestManager

EventHandler = Callable[[Frame], Awaitable[None]]
ConnectionHandler = Callable[[], Awaitable[None]]
DisconnectHandler = Callable[[str], Awaitable[None]]


class SessionError(RuntimeError):
    """Raised when a Version 3 session cannot send a valid operation."""


class V3Session:
    """Own one reconnecting WebSocket and its connection-scoped state.

    A generation starts only after authentication succeeds. Pending RPCs are
    failed at generation end so responses from a later connection cannot be
    applied to requests from an earlier connection.
    """

    def __init__(self, config: ServerConfig, auth: AuthSession):
        self.config = config
        self.auth = auth
        self._requests = RequestManager()
        self._backoff = ReconnectBackoff(config.reconnect)
        self._event_handlers: list[EventHandler] = []
        self._on_connected: ConnectionHandler | None = None
        self._on_disconnected: DisconnectHandler | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._running = False
        self._ready = False
        self._generation = 0
        self._send_seq = 0
        self._receive_seq = 0
        self._authenticated_remote_ids: set[str] = set()
        self._send_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        """Whether this session is mutually authenticated and usable."""
        return self._ready and self._ws is not None and not self._ws.closed

    @property
    def generation(self) -> int:
        """Return the currently authenticated connection generation."""
        return self._generation

    @property
    def session_id(self) -> str:
        """Return the authenticated remote session identifier."""
        return self.auth.session_id

    def add_event_handler(self, handler: EventHandler) -> None:
        """Register an event handler invoked after frame validation."""
        self._event_handlers.append(handler)

    def set_connection_handlers(
        self,
        on_connected: ConnectionHandler | None,
        on_disconnected: DisconnectHandler | None,
    ) -> None:
        """Configure lifecycle callbacks owned by the server client."""
        self._on_connected = on_connected
        self._on_disconnected = on_disconnected

    async def start(self) -> None:
        """Start the reconnect loop once; this method does not wait for a peer."""
        if self._run_task is None or self._run_task.done():
            self._running = True
            self._run_task = asyncio.create_task(
                self._run(), name=f"mc-v3:{self.config.server_id}"
            )

    async def stop(self) -> None:
        """Stop transport activity and release every connection-scoped resource."""
        self._running = False
        if self._run_task is not None and self._run_task is not asyncio.current_task():
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
        self._run_task = None
        await self._close_connection("session stopped", notify=False)
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def request(
        self,
        op: Op,
        payload: dict[str, Any],
        target: Target,
        timeout_ms: int | None = None,
    ) -> Frame:
        """Send an authenticated request and await its correlated response.

        Args:
            op: Request operation accepted by the Java peer.
            payload: Operation payload conforming to the contract.
            target: Explicit request target.
            timeout_ms: Optional bounded override for response timeout.

        Returns:
            The peer response, including a possibly unsuccessful status.

        Raises:
            SessionError: If the session is not ready or the frame is invalid.
            RequestError: If it cannot be correlated or times out.
        """
        if not self.connected:
            raise SessionError("server is not authenticated")
        frame = self._frame(FrameType.REQUEST, op, payload, target=target)
        try:
            future = self._requests.create(frame.id)
        except RequestError:
            raise
        try:
            await self._send_frame(frame)
            timeout = (timeout_ms or self.config.request_timeout_ms) / 1_000
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._requests.cancel(frame.id)
            raise RequestError(f"request timed out: {op.value}") from exc
        except Exception:
            self._requests.cancel(frame.id)
            raise

    async def _run(self) -> None:
        while self._running:
            reason = "connection closed"
            try:
                await self._connect_and_authenticate()
                self._backoff.reset()
                await self._receive_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = self._safe_reason(exc)
                logger.warning(
                    "[MC-%s] Version 3 session ended: %s", self.config.server_id, reason
                )
            finally:
                was_ready = self._ready
                await self._close_connection(reason, notify=was_ready)
            if self._running:
                await asyncio.sleep(self._backoff.next_delay())

    async def _connect_and_authenticate(self) -> None:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self.config.request_timeout_ms / 1_000
            )
            self._session = aiohttp.ClientSession(timeout=timeout)
        self._ws = await self._session.ws_connect(
            self.config.url,
            max_msg_size=256 * 1024,
            autoping=True,
            heartbeat=None,
        )
        self._send_seq = 0
        self._receive_seq = 0
        self._authenticated_remote_ids.clear()
        initial = self.auth.begin(self._next_send_seq())
        await self._send_frame(initial, increment=False)
        while not self.auth.ready:
            message = await asyncio.wait_for(
                self._ws.receive(), timeout=self.config.request_timeout_ms / 1_000
            )
            if message.type is not aiohttp.WSMsgType.TEXT:
                raise SessionError("connection closed during authentication")
            frame = self._decode_incoming(message.data, require_next_sequence=True)
            if frame.type is FrameType.ERROR:
                raise SessionError(self._frame_error_message(frame))
            next_frame = self.auth.receive(frame, self._send_seq + 1)
            if next_frame is not None:
                self._send_seq = next_frame.seq
                await self._send_frame(next_frame, increment=False)
        self._ready = True
        self._authenticated_remote_ids.add(self.config.server_id)
        self._generation += 1
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(self._generation),
            name=f"mc-v3-heartbeat:{self.config.server_id}",
        )
        if self._on_connected is not None:
            await self._on_connected()

    async def _receive_loop(self) -> None:
        if self._ws is None:
            raise SessionError("WebSocket is unavailable")
        async for message in self._ws:
            if message.type is aiohttp.WSMsgType.TEXT:
                frame = self._decode_incoming(message.data, require_next_sequence=True)
                await self._handle_frame(frame)
            elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING}:
                return
            elif message.type is aiohttp.WSMsgType.ERROR:
                raise SessionError("WebSocket transport error")

    def _decode_incoming(self, raw: str, require_next_sequence: bool) -> Frame:
        try:
            frame = decode_frame(raw)
        except ProtocolError as exc:
            raise SessionError("peer sent an invalid protocol frame") from exc
        if require_next_sequence and frame.seq != self._receive_seq + 1:
            raise SessionError("peer frame sequence is not monotonic")
        self._receive_seq = frame.seq
        expected_source = (
            frame.route.src.role is self.config.expected_role
            and frame.route.src.id == self.config.server_id
        )
        expected_destination = (
            frame.route.dst.role is Role.ASTRBOT
            and frame.route.dst.id in {self.auth.identity.node_id, "*"}
        )
        if not expected_source or not expected_destination:
            raise SessionError("peer frame route does not match endpoint")
        if self._ready and frame.route.src.id not in self._authenticated_remote_ids:
            raise SessionError("peer frame source is not authenticated")
        if frame.origin is not None:
            if self.config.expected_role is not Role.VELOCITY:
                raise SessionError("only Velocity may forward a server event")
            if frame.type is not FrameType.EVENT:
                raise SessionError("only events may include a forwarded origin")
            origin_role = frame.origin.get("role")
            origin_id = frame.origin.get("id")
            origin_server_id = frame.origin.get("server_id")
            if (
                origin_role != Role.BACKEND.value
                or not isinstance(origin_id, str)
                or not origin_id
                or not isinstance(origin_server_id, str)
                or not origin_server_id
                or frame.target is None
                or frame.target.scope is not TargetScope.SERVER
                or frame.target.server_id != origin_server_id
            ):
                raise SessionError("forwarded backend event has invalid origin")
        return frame

    async def _handle_frame(self, frame: Frame) -> None:
        if frame.type in {
            FrameType.RESPONSE,
            FrameType.ERROR,
        } and self._requests.resolve(frame):
            return
        if frame.type is FrameType.RESPONSE:
            self._requests.resolve(frame)
            return
        if frame.type is FrameType.HEARTBEAT and frame.op == Op.CORE_PING.value:
            await self._send_frame(
                self._frame(
                    FrameType.HEARTBEAT,
                    Op.CORE_PONG,
                    {
                        "session_id": self.auth.session_id,
                        "server_time": int(time.time() * 1_000),
                    },
                    reply_to=frame.id,
                    trace_id=frame.route.trace_id,
                )
            )
            return
        if frame.type is FrameType.HEARTBEAT and frame.op == Op.CORE_PONG.value:
            return
        if frame.type is FrameType.ERROR:
            raise SessionError(self._frame_error_message(frame))
        if frame.type is FrameType.EVENT:
            for handler in tuple(self._event_handlers):
                try:
                    await handler(frame)
                except Exception as exc:
                    logger.exception(
                        "[MC-%s] Version 3 event handler failed: %s",
                        self.config.server_id,
                        self._safe_reason(exc),
                    )
            return
        raise SessionError("unexpected post-authentication frame")

    async def _heartbeat_loop(self, generation: int) -> None:
        while self._running and self._ready and generation == self._generation:
            await asyncio.sleep(self.config.heartbeat_interval_ms / 1_000)
            if not self.connected or generation != self._generation:
                return
            try:
                await self._send_frame(
                    self._frame(
                        FrameType.HEARTBEAT,
                        Op.CORE_PING,
                        {"session_id": self.auth.session_id},
                        trace_id=self._id(),
                    )
                )
            except Exception as exc:
                logger.warning(
                    "[MC-%s] Heartbeat failed: %s",
                    self.config.server_id,
                    self._safe_reason(exc),
                )
                return

    async def _send_frame(self, frame: Frame, increment: bool = True) -> None:
        if self._ws is None or self._ws.closed:
            raise SessionError("WebSocket is unavailable")
        if increment:
            frame = self._with_next_sequence(frame)
        try:
            encoded = encode_frame(frame)
        except ProtocolError as exc:
            raise SessionError("local protocol frame is invalid") from exc
        async with self._send_lock:
            await self._ws.send_str(encoded)

    async def _close_connection(self, reason: str, notify: bool) -> None:
        self._ready = False
        self._requests.fail_all(reason)
        if self._heartbeat_task is not None:
            heartbeat_task = self._heartbeat_task
            self._heartbeat_task = None
            if heartbeat_task is not asyncio.current_task():
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        self.auth.close()
        if notify and self._on_disconnected is not None:
            await self._on_disconnected(reason)

    def _frame(
        self,
        frame_type: FrameType,
        op: Op,
        payload: dict[str, Any],
        target: Target | None = None,
        reply_to: str | None = None,
        trace_id: str | None = None,
    ) -> Frame:
        return Frame(
            v=PROTOCOL_VERSION,
            id=self._id(),
            type=frame_type,
            op=op.value,
            ts=int(time.time() * 1_000),
            seq=0,
            route=Route(
                src=NodeRef(Role.ASTRBOT, self.auth.identity.node_id),
                dst=NodeRef(self.config.expected_role, self.config.server_id),
                trace_id=trace_id or self._id(),
            ),
            target=target,
            reply_to=reply_to,
            payload=payload,
        )

    def _with_next_sequence(self, frame: Frame) -> Frame:
        return Frame(
            v=frame.v,
            id=frame.id,
            type=frame.type,
            op=frame.op,
            ts=frame.ts,
            seq=self._next_send_seq(),
            route=frame.route,
            payload=frame.payload,
            origin=frame.origin,
            target=frame.target,
            reply_to=frame.reply_to,
            status=frame.status,
        )

    def _next_send_seq(self) -> int:
        self._send_seq += 1
        return self._send_seq

    @staticmethod
    def _id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _safe_reason(exc: Exception) -> str:
        if isinstance(
            exc, (AuthenticationError, ProtocolError, RequestError, SessionError)
        ):
            return str(exc)
        return type(exc).__name__

    @staticmethod
    def _frame_error_message(frame: Frame) -> str:
        """Return a safe peer error message without exposing payload content."""
        if frame.status is not None and frame.status.message:
            return frame.status.message
        return "peer returned a protocol error"
