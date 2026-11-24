"""Opt-in AstrBot FunctionTool registrations."""

from collections.abc import Awaitable, Callable

from astrbot.api import FunctionTool
from pydantic.dataclasses import dataclass


@dataclass
class MinecraftTool(FunctionTool):
    callback: Callable[..., Awaitable[str]] | None = None

    async def call(self, context, **kwargs) -> str:
        return await self.callback(context.context.event, **kwargs)


def build_tools(adapter) -> dict[str, tuple[MinecraftTool, ...]]:
    target = {
        "type": "string",
        "description": "服务器 ID 或 Velocity 入口/子服。只有一个入口时可省略。",
    }

    def tool(name, description, callback, fields=None, required=()):
        return MinecraftTool(
            name=f"minecraft_{name}",
            description=description,
            parameters={
                "type": "object",
                "properties": fields or {},
                "required": list(required),
                "additionalProperties": False,
            },
            callback=callback,
        )

    return {
        "targets": (
            tool(
                "targets",
                "查询当前会话绑定的 Minecraft 服务器和可用子服。",
                adapter.targets_tool,
            ),
        ),
        "status": (
            tool(
                "status",
                "查询 Minecraft 服务器状态。",
                adapter.status_tool,
                {"target": target},
            ),
            tool(
                "players",
                "查询 Minecraft 在线玩家。可附带玩家延迟。",
                adapter.players_tool,
                {
                    "target": target,
                    "with_latency": {
                        "type": "boolean",
                        "description": "返回延迟时最多展示 30 名玩家。",
                    },
                },
            ),
        ),
        "player": (
            tool(
                "player",
                "查询 Minecraft 玩家详情。支持服务端保存的离线快照。",
                adapter.player_tool,
                {
                    "target": target,
                    "player": {
                        "type": "string",
                        "description": "玩家名称或 UUID。",
                    },
                },
                ("player",),
            ),
        ),
        "command": (
            tool(
                "command",
                "按模型指令名单，在已绑定的 Minecraft 服务器、Velocity 代理或指定子服执行控制台指令。",
                adapter.command_tool,
                {
                    "target": target,
                    "command": {
                        "type": "string",
                        "description": "要执行的单行 Minecraft 控制台指令。",
                    },
                },
                ("command",),
            ),
        ),
    }
