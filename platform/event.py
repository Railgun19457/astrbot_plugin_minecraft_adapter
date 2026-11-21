"""Minecraft 平台消息事件"""

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import AstrBotMessage, PlatformMetadata

from ..core.server_manager import ServerConnection


def _extract_text(message_chain: MessageChain) -> str:
    """Extract plain text content from a MessageChain."""
    return "".join(
        component.text
        for component in message_chain.chain
        if isinstance(component, Plain)
    )


class MinecraftMessageEvent(AstrMessageEvent):
    """AstrBot event backed by a Version 3 Minecraft AI request."""

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        server_connection: ServerConnection,
        private: bool,
        request_id: str,
        player_uuid: str,
        player_name: str,
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.server_connection = server_connection
        self.private = private
        self.request_id = request_id
        self.player_uuid = player_uuid
        self.player_name = player_name

    async def send(self, message: MessageChain):
        """将响应消息发送回 Minecraft"""
        content = _extract_text(message)
        if not content:
            return

        await self.server_connection.client.reply_to_ai(
            reply_to_message_id=self.request_id,
            content=content,
            private_player_uuid=self.player_uuid if self.private else "",
            private_player_name=self.player_name if self.private else "",
        )

        await super().send(message)
