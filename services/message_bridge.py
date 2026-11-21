"""MC 与其他平台之间转发消息的消息桥接服务"""

import re
import time
from typing import TYPE_CHECKING

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain

from ..core.config import ServerConfig
from ..core.protocol.constants import Op
from ..core.protocol.frame import Frame

if TYPE_CHECKING:
    from astrbot.core.star.context import Context

    from ..core.server_manager import ServerManager


# 消息转发反馈的 Emoji 响应常量
EMOJI_OK_GESTURE = 124  # 👌
EMOJI_THUMBS_UP = 76  # 👍
EMOJI_LOVE = 66  # ❤️
EMOJI_ROSE = 63  # 🌹


class MessageBridge:
    """在 MC 服务器和 AstrBot 会话之间转发消息的服务"""

    def __init__(self, context: "Context", server_manager: "ServerManager"):
        self.context = context
        self.server_manager = server_manager
        # 从会话 UMO 到希望接收消息的服务器配置的映射
        self._session_to_servers: dict[str, list[tuple[str, ServerConfig]]] = {}
        # 从 server_id 到配置的映射
        self._server_configs: dict[str, ServerConfig] = {}
        # Track recently forwarded messages to suppress echo
        # Key: (server_id, content_hash), Value: timestamp
        self._recently_forwarded: dict[tuple[str, str], float] = {}
        # Echo suppression window in seconds
        self._echo_suppress_window = 5.0

    def register_server(self, config: ServerConfig):
        """注册用于消息转发的服务器"""
        self._server_configs[config.server_id] = config

        # 为目标会话构建反向映射
        for session in config.target_sessions:
            if session not in self._session_to_servers:
                self._session_to_servers[session] = []
            self._session_to_servers[session].append((config.server_id, config))

    def unregister_server(self, server_id: str):
        """从消息转发中取消注册服务器"""
        config = self._server_configs.pop(server_id, None)
        if config:
            # 从反向映射中移除
            for session in config.target_sessions:
                if session in self._session_to_servers:
                    self._session_to_servers[session] = [
                        (sid, cfg)
                        for sid, cfg in self._session_to_servers[session]
                        if sid != server_id
                    ]

    async def handle_server_event(self, server_id: str, frame: Frame) -> bool:
        """Forward a Version 3 chat or player lifecycle event to AstrBot."""
        config = self._server_configs.get(server_id)
        if not config:
            return False

        payload = frame.payload
        if frame.op == Op.CHAT_PLAYER.value:
            if not config.forward_chat_to_astrbot:
                return False
            message = payload.get("message")
            content = message.get("plain_text", "") if isinstance(message, dict) else ""
            echo_key = (server_id, content)
            now = time.time()
            if echo_key in self._recently_forwarded:
                if (
                    now - self._recently_forwarded[echo_key]
                    < self._echo_suppress_window
                ):
                    del self._recently_forwarded[echo_key]
                    return False
                del self._recently_forwarded[echo_key]
        elif frame.op in {Op.PLAYER_JOIN.value, Op.PLAYER_LEAVE.value}:
            if not config.forward_join_leave_to_astrbot:
                return False
        else:
            return False

        # 获取目标会话
        targets = config.target_sessions
        if not targets:
            return False

        # 格式化消息内容
        content = self._format_server_event(frame, config)
        if not content:
            return False

        # 发送到每个目标会话
        for target_umo in targets:
            await self._send_to_session(target_umo, content)

        return True

    def _format_server_event(self, frame: Frame, config: ServerConfig) -> str:
        """Format a validated V3 server event for an external session."""
        payload = frame.payload
        player = (
            payload.get("player") if isinstance(payload.get("player"), dict) else {}
        )
        player_name = str(player.get("display_name") or player.get("name") or "未知")
        if frame.op == Op.CHAT_PLAYER.value:
            message = (
                payload.get("message")
                if isinstance(payload.get("message"), dict)
                else {}
            )
            content = str(message.get("plain_text") or message.get("content") or "")
            return config.forward_chat_format.format(
                player=player_name, message=content
            )

        if frame.op in {Op.PLAYER_JOIN.value, Op.PLAYER_LEAVE.value}:
            server_name = str(player.get("server_id") or "")
            online = payload.get("online_count", 0)
            max_players = payload.get("max_players", 0)
            count_part = f" ({online}/{max_players})" if max_players else ""
            server_part = f" {server_name}" if server_name else "服务器"

            if frame.op == Op.PLAYER_JOIN.value:
                return f"🟢 {player_name} 加入了{server_part}{count_part}"

            reason = payload.get("reason", "QUIT")
            reason_text = {
                "QUIT": "离开",
                "KICK": "被踢出",
                "TIMEOUT": "超时断开",
            }.get(reason, "离开")
            return f"🔴 {player_name} {reason_text}了{server_part}{count_part}"

        return ""

    async def _send_to_session(self, umo: str, content: str):
        """
        通过平台管理器发送消息到特定会话。

        参数:
            umo: 格式为 'platform:type:id' 的统一消息源
            content: 要发送的消息内容

        注意:
            解析 UMO 以查找目标平台并通过平台管理器发送。
            如果 UMO 格式无效或找不到平台，则记录警告。
        """
        try:
            # 创建消息链
            message_chain = MessageChain([Plain(text=content)])

            # 使用 Context 直接发送，内部会解析 UMO
            sent = await self.context.send_message(umo, message_chain)
            if not sent:
                logger.warning(f"[MessageBridge] 未找到平台: {umo}")

        except Exception as e:
            logger.error(f"[MessageBridge] 发送消息失败: {e}")

    async def handle_external_message(self, event: AstrMessageEvent) -> bool:
        """处理来自外部平台的消息并在需要时转发到 MC

        如果消息被转发则返回 True。
        """
        # 获取消息内容
        message_str = event.message_str
        umo = event.unified_msg_origin

        # 检查每个服务器配置
        any_forwarded = False
        for server_id, config in self._server_configs.items():
            # 检查此会话是否在目标会话列表中
            if not config.target_sessions or umo not in config.target_sessions:
                continue

            # 前缀为空时转发全部消息，否则检查前缀
            if config.auto_forward_prefix:
                if not message_str.startswith(config.auto_forward_prefix):
                    continue
                # 移除前缀
                content = message_str[len(config.auto_forward_prefix) :].strip()
            else:
                content = message_str.strip()

            if not content:
                continue

            # 获取发送者信息
            sender_name = event.get_sender_name()
            sender_id = event.get_sender_id()
            platform_name = event.get_platform_name()

            # 发送到 MC 服务器
            server = self.server_manager.get_server(server_id)
            if server and server.connected:
                success = await server.client.send_external_message(
                    platform=platform_name,
                    user_id=sender_id,
                    user_name=sender_name,
                    conversation_id=umo,
                    conversation_type=self._conversation_type(umo),
                    content=content,
                )

                if success:
                    # Track this message to suppress echo
                    echo_key = (server_id, content)
                    self._recently_forwarded[echo_key] = time.time()
                    # Clean up old entries
                    self._cleanup_recently_forwarded()
                    # Send feedback based on mark_option (only once)
                    if not any_forwarded:
                        await self._send_forward_feedback(event, config)
                    any_forwarded = True

        return any_forwarded

    async def _send_forward_feedback(
        self, event: AstrMessageEvent, config: ServerConfig
    ):
        """在消息转发成功后发送反馈

        Behavior by mark_option:
        - "none": do nothing
        - "emoji": only react with emoji, no text message
        - "text": only send text confirmation "✓ 消息已转发"
        """
        mark_option = config.mark_option

        if mark_option == "none":
            return

        elif mark_option == "emoji":
            # Only emoji reaction, no text
            await self._react_with_emoji(event)

        elif mark_option == "text":
            # Only text confirmation
            try:
                await event.send(MessageChain([Plain(text="✓ 消息已转发")]))
            except Exception:
                pass

    def _cleanup_recently_forwarded(self):
        """Clean up expired entries in the recently forwarded tracker"""
        now = time.time()
        expired = [
            k
            for k, t in self._recently_forwarded.items()
            if now - t > self._echo_suppress_window
        ]
        for k in expired:
            del self._recently_forwarded[k]

    async def _react_with_emoji(
        self, event: AstrMessageEvent, emoji_id: int = EMOJI_OK_GESTURE
    ):
        """
        使用 napcat/onebot API 对消息作出表情符号反应。

        参数:
            event: 要反应的消息事件
            emoji_id: 要使用的表情符号 ID (默认: EMOJI_OK_GESTURE)
                常见表情符号 ID:
                - EMOJI_OK_GESTURE (124): 👌
                - EMOJI_THUMBS_UP (76): 👍
                - EMOJI_LOVE (66): ❤️
                - EMOJI_ROSE (63): 🌹
        """
        platform_name = event.get_platform_name()

        # 仅 aiocqhttp (OneBot v11) 支持表情符号反应
        if platform_name != "aiocqhttp":
            return

        try:
            # 运行时惰性导入以避免循环依赖
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            if not isinstance(event, AiocqhttpMessageEvent):
                return

            # 获取机器人口端
            client = event.bot
            message_id = event.message_obj.message_id

            # 调用 napcat/onebot API 设置表情符号反应
            # API: set_msg_emoji_like
            payloads = {
                "message_id": int(message_id),
                "emoji_id": str(emoji_id),
            }

            await client.api.call_action("set_msg_emoji_like", **payloads)
            logger.debug(
                f"[MessageBridge] 已对消息 {message_id} 作出表情响应 {emoji_id}"
            )

        except Exception as e:
            # 表情符号反应失败，这不是关键错误
            logger.debug(f"[MessageBridge] 表情响应失败: {e}")

    def get_servers_for_session(self, umo: str) -> list[str]:
        """获取目标会话包含该 UMO 的服务器 ID 列表"""
        result = []
        for server_id, config in self._server_configs.items():
            if config.target_sessions and umo in config.target_sessions:
                result.append(server_id)
        return result

    @staticmethod
    def _conversation_type(umo: str) -> str:
        """Extract the AstrBot unified message origin's message type safely."""
        parts = umo.split(":", 2)
        return parts[1] if len(parts) > 1 else "unknown"

    def strip_color_codes(self, text: str) -> str:
        """从文本中移除 Minecraft 颜色代码"""
        # 移除 § 后跟任意字符
        return re.sub(r"§[0-9a-fk-or]", "", text)
