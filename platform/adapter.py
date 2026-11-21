"""Minecraft 平台适配器，用于 AI 聊天"""

import asyncio

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import At, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
)
from astrbot.core.platform.astr_message_event import MessageSesion

from ..core.config import ServerConfig
from ..core.protocol.constants import Op
from ..core.protocol.frame import Frame
from ..core.server_manager import ServerConnection
from .event import MinecraftMessageEvent


def _extract_text(message_chain: MessageChain) -> str:
    """Extract plain text content from a MessageChain."""
    return "".join(
        component.text
        for component in message_chain.chain
        if isinstance(component, Plain)
    )


class MCPlatformAdapter(Platform):
    """用于 Minecraft 服务器的平台适配器"""

    def __init__(
        self,
        server_config: ServerConfig,
        server_connection: ServerConnection,
        event_queue: asyncio.Queue,
    ):
        super().__init__({}, event_queue)
        self.server_config = server_config
        self.server_connection = server_connection
        self._platform_name = f"minecraft_{server_config.server_id}"
        self._running = False

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name=self._platform_name,
            description=f"Minecraft Server: {self.server_config.server_id}",
            id=self._platform_name,
        )

    async def send_by_session(
        self, session: MessageSesion, message_chain: MessageChain
    ):
        """通过会话发送消息"""
        content = _extract_text(message_chain)
        if not content:
            return

        # session.session_id 仅存会话标识（如 uuid / Server），
        # 消息类型由 session.message_type 提供。
        identifier = session.session_id
        msg_type = session.message_type

        if getattr(msg_type, "value", str(msg_type)) == "FriendMessage":
            # 私聊给特定玩家
            await self.server_connection.client.send_private_message(
                player_uuid=identifier,
                player_name="",
                content=content,
            )
        else:
            await self.server_connection.client.reply_to_ai(
                reply_to_message_id=f"session:{self.server_config.server_id}:{identifier}",
                content=content,
            )

        await super().send_by_session(session, message_chain)

    async def run(self):
        """主运行循环 - 由插件管理，而不是平台"""
        self._running = True
        logger.info(f"[MC-{self.server_config.server_id}] 平台适配器已启动")
        # 实际连接由 ServerManager 管理
        # 此方法只是保持平台“活着”
        # 使用 asyncio.Event 进行正确的异步等待
        self._stop_event = asyncio.Event()
        await self._stop_event.wait()

    async def stop(self):
        """停止平台适配器"""
        self._running = False
        if hasattr(self, "_stop_event"):
            self._stop_event.set()

    async def handle_ai_request(self, frame: Frame):
        """Convert a Version 3 AI request event into an AstrBot event."""
        if not self.server_config.enable_ai_chat:
            return
        if frame.op not in {Op.AI_GROUP_REQUEST.value, Op.AI_PRIVATE_REQUEST.value}:
            return
        payload = frame.payload
        player = payload.get("player")
        message = payload.get("message")
        hint = payload.get("conversation_hint")
        if (
            not isinstance(player, dict)
            or not isinstance(message, dict)
            or not isinstance(hint, dict)
        ):
            return
        private = frame.op == Op.AI_PRIVATE_REQUEST.value
        content = str(message.get("plain_text") or message.get("content") or "")
        player_uuid = str(player.get("uuid") or "")
        player_name = str(player.get("display_name") or player.get("name") or "")
        if not content or not player_uuid:
            return

        # 创建 AstrBotMessage
        abm = AstrBotMessage()

        # 根据聊天模式设置消息类型
        if private:
            abm.type = MessageType.FRIEND_MESSAGE
            abm.session_id = player_uuid
        else:
            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = "Server"
            abm.session_id = "Server"

        abm.self_id = self.server_config.server_id
        abm.message_id = str(message.get("message_id") or frame.id)
        abm.sender = MessageMember(user_id=player_uuid, nickname=player_name)
        if not private:
            abm.message = [At(qq=abm.self_id), Plain(text=content)]
        else:
            abm.message = [Plain(text=content)]
        abm.message_str = content
        abm.raw_message = payload

        # 创建事件
        event = MinecraftMessageEvent(
            message_str=content,
            message_obj=abm,
            platform_meta=self.meta(),
            session_id=abm.session_id,
            server_connection=self.server_connection,
            private=private,
            request_id=str(message.get("message_id") or frame.id),
            player_uuid=player_uuid,
            player_name=player_name,
        )

        # 将事件提交到队列
        self.commit_event(event)
        logger.debug(
            f"[MC-{self.server_config.server_id}] "
            f"AI request from {player_name}: {content[:50]}..."
        )
