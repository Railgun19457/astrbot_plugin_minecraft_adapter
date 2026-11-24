"""AstrBot's managed Minecraft message platform."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING
from uuid import uuid4

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    AstrMessageEvent,
    Group,
    MessageMember,
    MessageSession,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)

if TYPE_CHECKING:
    from .main import MinecraftAdapter

runtime: MinecraftAdapter | None = None


@register_platform_adapter(
    "minecraft",
    "Minecraft 游戏会话",
    {"type": "minecraft", "id": "minecraft", "enable": False},
    adapter_display_name="Minecraft",
    logo_path="logo.png",
    support_streaming_message=False,
)
class MinecraftPlatform(Platform):
    def __init__(
        self, config: dict, platform_settings: dict, event_queue: asyncio.Queue
    ):
        super().__init__(config, event_queue)
        self._closed = asyncio.Event()
        self._metadata = PlatformMetadata(
            name="minecraft",
            description="Minecraft 游戏会话",
            id=str(config["id"]),
            adapter_display_name="Minecraft",
            support_streaming_message=False,
        )

    def meta(self) -> PlatformMetadata:
        return self._metadata

    async def run(self) -> None:
        await self._closed.wait()

    async def terminate(self) -> None:
        self._closed.set()

    def publish(self, endpoint: str, data: dict, *, ai: bool) -> None:
        public = not ai or data.get("public") is True
        player = data["uuid"]
        session_id = f"{endpoint}/{'' if public else player}"
        message = AstrBotMessage()
        message.type = (
            MessageType.GROUP_MESSAGE if public else MessageType.FRIEND_MESSAGE
        )
        message.self_id = self.meta().id
        message.session_id = session_id
        message.message_id = uuid4().hex
        message.group = Group(endpoint, endpoint) if public else None
        message.sender = MessageMember(player, data["name"])
        message.message = [Plain(data["text"])]
        message.message_str = data["text"]
        message.raw_message = data
        event = MinecraftEvent(self, message)
        event.is_wake = ai
        event.is_at_or_wake_command = ai
        self.commit_event(event)

    async def deliver(self, session_id: str, message_chain: MessageChain) -> None:
        # AstrBot retains the existing platform instance when reloading the plugin.
        manager = getattr(sys.modules.get(__name__), "runtime", None)
        if manager is None:
            return
        endpoint, separator, recipient = session_id.rpartition("/")
        if not separator:
            return
        text = "\n".join(
            item.text for item in message_chain.chain if isinstance(item, Plain)
        )
        if text.strip():
            await manager.send_game_reply(endpoint, recipient, text)

    async def send_by_session(
        self, session: MessageSession, message_chain: MessageChain
    ) -> None:
        await self.deliver(session.session_id, message_chain)
        await super().send_by_session(session, message_chain)


class MinecraftEvent(AstrMessageEvent):
    def __init__(self, platform: MinecraftPlatform, message: AstrBotMessage):
        super().__init__(
            message.message_str, message, platform.meta(), message.session_id
        )
        self._platform = platform

    async def send(self, message: MessageChain) -> None:
        await self._platform.deliver(self.session_id, message)
        await super().send(message)
