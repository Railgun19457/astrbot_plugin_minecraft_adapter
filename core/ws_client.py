"""Minecraft 服务器通信的 WebSocket 客户端"""

import asyncio
import time
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import aiohttp

from astrbot.api import logger

from .models import MCMessage, MessageType, ServerInfo

# 连接常量
DEFAULT_RECONNECT_DELAY = 1  # 初始重连延迟（秒）
MAX_RECONNECT_DELAY = 60  # 最大重连延迟（秒）
DEFAULT_HEARTBEAT_INTERVAL = 30  # 心跳间隔（秒）
CONNECTION_TIMEOUT = 10  # 连接超时（秒）
COMMAND_RESPONSE_TIMEOUT = 30  # 命令执行结果等待超时（秒）


class WebSocketClient:
    """与 Minecraft 服务器通信的 WebSocket 客户端"""

    def __init__(
        self,
        server_id: str,
        host: str,
        port: int,
        token: str,
        on_message: Callable[[MCMessage], Coroutine[Any, Any, None]] | None = None,
        on_connect: Callable[[ServerInfo], Coroutine[Any, Any, None]] | None = None,
        on_disconnect: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        max_reconnect_delay: int = MAX_RECONNECT_DELAY,
    ):
        self.server_id = server_id
        self.host = host
        self.port = port
        self.token = token
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._connected = False
        self._reconnect_delay = DEFAULT_RECONNECT_DELAY
        self._max_reconnect_delay = max_reconnect_delay
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_task: asyncio.Task | None = None
        self._session_id: str = ""
        self._server_info: ServerInfo | None = None
        self._pending_command_requests: dict[str, asyncio.Future[MCMessage]] = {}

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def server_info(self) -> ServerInfo | None:
        return self._server_info

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws?token={self.token}"

    async def connect(self) -> bool:
        """建立 WebSocket 连接"""
        if self._connected:
            return True

        try:
            if self._session is None:
                self._session = aiohttp.ClientSession()

            logger.info(f"[MC-{self.server_id}] 正在连接到 {self.host}:{self.port}")
            self._ws = await self._session.ws_connect(
                self.ws_url,
                heartbeat=self._heartbeat_interval,
            )

            # 等待 CONNECTION_ACK
            msg = await asyncio.wait_for(self._ws.receive(), timeout=CONNECTION_TIMEOUT)
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = msg.json()
                if data.get("type") == MessageType.CONNECTION_ACK.value:
                    payload = data.get("payload", {})
                    self._session_id = payload.get("sessionId", "")
                    server_data = dict(payload.get("serverInfo", {}) or {})
                    server_data["protocolVersion"] = payload.get("protocolVersion", 0)
                    server_data["apiVersion"] = payload.get("apiVersion", "")
                    server_data["features"] = payload.get("features", [])
                    self._server_info = ServerInfo.from_dict(server_data)
                    self._connected = True
                    self._reconnect_delay = DEFAULT_RECONNECT_DELAY  # 成功后重置

                    logger.info(
                        f"[MC-{self.server_id}] 已连接到 {self._server_info.name} "
                        f"({self._server_info.platform} {self._server_info.minecraft_version})"
                    )

                    if self.on_connect:
                        await self.on_connect(self._server_info)

                    return True

            logger.error(f"[MC-{self.server_id}] 接收 CONNECTION_ACK 失败: {msg}")
            return False

        except asyncio.TimeoutError:
            logger.error(f"[MC-{self.server_id}] 连接超时")
            return False
        except Exception as e:
            logger.error(f"[MC-{self.server_id}] 连接失败: {e}")
            return False

    async def disconnect(self):
        """关闭 WebSocket 连接"""
        self._running = False
        self._connected = False
        self._fail_pending_commands("连接已断开")

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self._ws and not self._ws.closed:
            await self._ws.close()
            self._ws = None

        if self._session:
            await self._session.close()
            self._session = None

        logger.info(f"[MC-{self.server_id}] 已断开连接")

    async def start(self):
        """启动 WebSocket 客户端，带有自动重连"""
        self._running = True

        while self._running:
            if not self._connected:
                success = await self.connect()
                if not success:
                    # 重连的指数退避
                    logger.info(
                        f"[MC-{self.server_id}] "
                        f"将在 {self._reconnect_delay}秒后尝试重连..."
                    )
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2, self._max_reconnect_delay
                    )
                    continue

            # 启动心跳任务
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            # 消息接收循环
            try:
                await self._receive_loop()
            except Exception as e:
                logger.error(f"[MC-{self.server_id}] 接收循环异常: {e}")
            finally:
                self._connected = False
                self._fail_pending_commands("连接丢失")
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()

                if self.on_disconnect and self._running:
                    await self.on_disconnect("连接丢失")

    async def _receive_loop(self):
        """主消息接收循环"""
        if not self._ws:
            return

        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = msg.json()
                    mc_msg = MCMessage.from_dict(data)
                    await self._handle_message(mc_msg)
                except Exception as e:
                    logger.error(f"[MC-{self.server_id}] 解析消息异常: {e}")

            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"[MC-{self.server_id}] WebSocket 错误: {msg.data}")
                break

            elif msg.type == aiohttp.WSMsgType.CLOSED:
                logger.info(f"[MC-{self.server_id}] WebSocket 已关闭")
                break

    async def _handle_message(self, msg: MCMessage):
        """
        处理传入的 WebSocket 消息。

        参数:
            msg: 要处理的 MCMessage 对象

        注意:
            处理不同类型的消息，包括心跳、断开连接、错误消息，
            并将其他类型转发给注册的处理器。
        """
        if msg.type == MessageType.HEARTBEAT:
            # 响应服务器心跳
            await self._send_heartbeat_ack(msg.id)

        elif msg.type == MessageType.HEARTBEAT_ACK:
            # 心跳已确认
            pass

        elif msg.type == MessageType.DISCONNECT:
            reason = msg.payload.get("reason", "未知")
            message = msg.payload.get("message", "")
            logger.warning(
                f"[MC-{self.server_id}] 服务器断开连接: {reason} - {message}"
            )
            self._connected = False

        elif msg.type == MessageType.ERROR:
            if self._complete_pending_command(msg):
                return
            code = msg.payload.get("code", 0)
            error_msg = msg.payload.get("message", "")
            logger.error(f"[MC-{self.server_id}] 错误 {code}: {error_msg}")

        elif msg.type == MessageType.COMMAND_RESPONSE:
            if self._complete_pending_command(msg):
                return
            if self.on_message:
                try:
                    await self.on_message(msg)
                except Exception as e:
                    logger.error(f"[MC-{self.server_id}] 消息处理器异常: {e}")

        else:
            # 转发到消息处理器
            if self.on_message:
                try:
                    await self.on_message(msg)
                except Exception as e:
                    logger.error(f"[MC-{self.server_id}] 消息处理器异常: {e}")

    async def _heartbeat_loop(self):
        """定期发送心跳。"""
        while self._connected and self._running:
            await asyncio.sleep(self._heartbeat_interval)
            if self._connected:
                await self._send_heartbeat()

    async def _send_heartbeat(self):
        """发送心跳消息"""
        msg = {
            "type": MessageType.HEARTBEAT.value,
            "id": str(uuid.uuid4()),
            "timestamp": int(time.time() * 1000),
        }
        await self._send(msg)

    async def _send_heartbeat_ack(self, msg_id: str):
        """发送心跳确认"""
        msg = {
            "type": MessageType.HEARTBEAT_ACK.value,
            "id": msg_id,
            "timestamp": int(time.time() * 1000),
        }
        await self._send(msg)

    async def _send(self, data: dict) -> bool:
        """通过 WebSocket 发送 JSON 数据"""
        if not self._ws or self._ws.closed:
            return False

        try:
            await self._ws.send_json(data)
            return True
        except Exception as e:
            logger.error(f"[MC-{self.server_id}] 发送错误: {e}")
            return False

    async def send_message(self, msg: MCMessage) -> bool:
        """通过 WebSocket 发送 MCMessage"""
        return await self._send(msg.to_dict())

    async def send_chat_response(
        self,
        reply_to: str,
        target_type: str,
        chat_mode: str,
        content: str,
        player_uuid: str = "",
        success: bool = True,
        error_message: str = "",
    ) -> bool:
        """发送 AI 聊天响应"""
        msg = {
            "type": MessageType.CHAT_RESPONSE.value,
            "id": str(uuid.uuid4()),
            "replyTo": reply_to,
            "target": {
                "type": target_type,
            },
            "payload": {
                "content": content,
                "chatMode": chat_mode,
                "success": success,
                "errorMessage": error_message if not success else None,
            },
            "timestamp": int(time.time() * 1000),
        }

        if player_uuid:
            msg["target"]["playerUuid"] = player_uuid

        return await self._send(msg)

    async def send_incoming_message(
        self,
        platform: str,
        user_id: str,
        user_name: str,
        content: str,
        target_type: str = "BROADCAST",
        player_uuid: str = "",
    ) -> bool:
        """将来自外部平台的消息发送到 MC 服务器"""
        msg = {
            "type": MessageType.MESSAGE_INCOMING.value,
            "id": str(uuid.uuid4()),
            "target": {
                "type": target_type,
            },
            "payload": {
                "source": {
                    "platform": platform,
                    "userId": user_id,
                    "userName": user_name,
                },
                "content": content,
            },
            "timestamp": int(time.time() * 1000),
        }

        if player_uuid:
            msg["target"]["playerUuid"] = player_uuid

        return await self._send(msg)

    async def send_command_request(
        self,
        command: str,
        executor: str = "CONSOLE",
        player_uuid: str | None = None,
        target_server_id: str | None = None,
    ) -> bool:
        """发送命令执行请求"""
        msg = {
            "type": MessageType.COMMAND_REQUEST.value,
            "id": str(uuid.uuid4()),
            "payload": {
                "command": command,
                "executor": executor,
            },
            "timestamp": int(time.time() * 1000),
        }

        if player_uuid:
            msg["payload"]["playerUuid"] = player_uuid
        if target_server_id:
            msg["payload"]["targetServerId"] = target_server_id

        return await self._send(msg)

    async def execute_command(
        self,
        command: str,
        executor: str = "CONSOLE",
        player_uuid: str | None = None,
        target_server_id: str | None = None,
        timeout: int = COMMAND_RESPONSE_TIMEOUT,
    ) -> tuple[bool, str, dict | None]:
        """发送 COMMAND_REQUEST 并等待同 session 的结果。"""
        if not self._connected:
            return False, "WebSocket 未连接", None

        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[MCMessage] = loop.create_future()
        self._pending_command_requests[request_id] = future

        msg = {
            "type": MessageType.COMMAND_REQUEST.value,
            "id": request_id,
            "payload": {
                "command": command,
                "executor": executor,
            },
            "timestamp": int(time.time() * 1000),
        }
        if player_uuid:
            msg["payload"]["playerUuid"] = player_uuid
        if target_server_id:
            msg["payload"]["targetServerId"] = target_server_id

        if not await self._send(msg):
            self._pending_command_requests.pop(request_id, None)
            return False, "命令请求发送失败", None

        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending_command_requests.pop(request_id, None)
            return False, "等待命令执行结果超时", None
        except Exception as e:
            self._pending_command_requests.pop(request_id, None)
            return False, str(e), None

        payload = response.payload or {}
        if response.type == MessageType.ERROR:
            message = payload.get("detail") or payload.get("message") or "命令执行失败"
            return False, message, payload

        success = bool(payload.get("success", False))
        output = (
            payload.get("output")
            or payload.get("errorMessage")
            or payload.get("message")
            or ("Command executed" if success else "命令执行失败")
        )
        return success, output, payload

    def _complete_pending_command(self, msg: MCMessage) -> bool:
        if not hasattr(self, "_pending_command_requests"):
            self._pending_command_requests = {}
        if not msg.reply_to:
            return False
        future = self._pending_command_requests.pop(msg.reply_to, None)
        if future is None:
            return False
        if not future.done():
            future.set_result(msg)
        return True

    def _fail_pending_commands(self, reason: str):
        if not hasattr(self, "_pending_command_requests"):
            self._pending_command_requests = {}
        if not self._pending_command_requests:
            return
        pending = list(self._pending_command_requests.values())
        self._pending_command_requests.clear()
        for future in pending:
            if not future.done():
                future.set_exception(ConnectionError(reason))
