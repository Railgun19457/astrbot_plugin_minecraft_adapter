"""AstrBot 的 Minecraft 适配器插件

本插件实现了 Minecraft 服务器与 AstrBot 之间的通信，
提供 AI 聊天、消息转发和服务器管理功能。
"""

import asyncio
from contextlib import suppress
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .core.auth.identity_store import IdentityStore
from .core.config import ConfigError, ServerConfig
from .core.protocol.constants import Op
from .core.protocol.frame import Frame
from .core.protocol.payloads import ServerInfo
from .core.server_manager import ServerManager
from .handlers.commands import CommandHandler
from .platform.adapter import MCPlatformAdapter
from .services.binding import BindingService
from .services.message_bridge import MessageBridge
from .services.renderer import InfoRenderer


@register(
    "astrbot_plugin_minecraft_adapter",
    "AstrBot",
    "Minecraft服务器适配器插件，支持AI聊天、消息互通、服务器管理",
    "3.0.0",
    "https://github.com/AstrBotDevs/astrbot_plugin_minecraft_adapter",
)
class MinecraftAdapterPlugin(Star):
    """Minecraft 适配器主插件类"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        # 获取插件数据路径
        plugin_data_path = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_minecraft_adapter"
        )
        plugin_data_path.mkdir(parents=True, exist_ok=True)
        self._plugin_data_path = plugin_data_path

        # 初始化服务
        self.identity_store = IdentityStore(plugin_data_path)
        self.server_manager = ServerManager(self.identity_store)
        self.binding_service = BindingService(plugin_data_path)
        self.message_bridge = MessageBridge(context, self.server_manager)

        # 服务器配置缓存
        self._server_configs: dict[str, ServerConfig] = {}

        # 平台适配器
        self._adapters: dict[str, MCPlatformAdapter] = {}
        self._adapter_tasks: dict[str, asyncio.Task] = {}

        # 命令处理器
        self.command_handler: CommandHandler | None = None

        # 设置消息处理器
        self.server_manager.set_event_handler(self._on_server_event)
        self.server_manager.set_connect_handler(self._on_server_connect)
        self.server_manager.set_disconnect_handler(self._on_server_disconnect)

        # 后台任务
        self._init_task: asyncio.Task | None = None

        # 加载配置并启动服务器
        self._init_task = self._schedule_task(self._initialize(), "initialize")

    def _schedule_task(self, coro, task_name: str) -> asyncio.Task | None:
        """创建后台任务并统一处理异常，避免未捕获异常导致静默失败。"""
        try:
            task = asyncio.create_task(coro)
        except RuntimeError as exc:
            coro.close()
            logger.error(f"[MC Adapter] 无法启动后台任务 {task_name}: {exc}")
            return None

        task.add_done_callback(lambda t: self._on_task_done(task_name, t))
        return task

    def _on_task_done(self, task_name: str, task: asyncio.Task):
        """后台任务完成时的统一回调。"""
        try:
            exc = task.exception()
            if exc:
                logger.error(f"[MC Adapter] 后台任务 {task_name} 异常退出: {exc}")
        except asyncio.CancelledError:
            pass

    async def _initialize(self):
        """初始化插件"""
        if not self.config.get("enabled", True):
            logger.info("[MC Adapter] 插件已禁用")
            return

        # 解析服务器配置
        mc_servers = self.config.get("mc_servers", [])
        if not mc_servers:
            logger.warning("[MC Adapter] 未配置任何服务器")
            return

        for server_data in mc_servers:
            if not server_data.get("enabled", True):
                continue

            try:
                config = ServerConfig.from_dict(server_data)
            except ConfigError as exc:
                logger.error(
                    "[MC Adapter] Ignoring invalid V3 server configuration: %s", exc
                )
                continue

            self._server_configs[config.server_id] = config

            # 将服务器添加到管理器
            if not self.server_manager.add_server(config):
                self._server_configs.pop(config.server_id, None)
                logger.error("[MC Adapter] Duplicate server ID: %s", config.server_id)
                continue

            # 注册到消息桥接
            self.message_bridge.register_server(config)

            logger.info(f"[MC Adapter] 已配置服务器: {config.server_id}")

        # 初始化渲染器
        # 检查是否有任何服务器启用了 text2image
        any_text2image = any(c.text2image for c in self._server_configs.values())
        renderer = InfoRenderer(
            text2image_enabled=any_text2image,
            cache_dir=self._plugin_data_path / "renderer_cache",
        )

        # 初始化命令处理器
        self.command_handler = CommandHandler(
            server_manager=self.server_manager,
            binding_service=self.binding_service,
            renderer=renderer,
            get_server_config=lambda sid: self._server_configs.get(sid),
        )

        # 为每个服务器注册自定义命令
        for server_id, config in self._server_configs.items():
            if config.custom_cmd_list:
                self.command_handler.register_custom_commands(
                    server_id, config.custom_cmd_list
                )

        # 启动所有服务器
        await self.server_manager.start_all()

        logger.info(
            f"[MC Adapter] 插件已初始化，配置了 {len(self._server_configs)} 个服务器"
        )

    async def _on_server_event(self, server_id: str, frame: Frame):
        """Dispatch a validated Version 3 server event."""
        config = self._server_configs.get(server_id)
        if not config:
            return

        logger.debug("[MC-%s] Received V3 event: %s", server_id, frame.op)

        if frame.op in {Op.AI_GROUP_REQUEST.value, Op.AI_PRIVATE_REQUEST.value}:
            adapter = self._adapters.get(server_id)
            if adapter:
                await adapter.handle_ai_request(frame)
            return

        if frame.op in {
            Op.CHAT_PLAYER.value,
            Op.PLAYER_JOIN.value,
            Op.PLAYER_LEAVE.value,
        }:
            await self.message_bridge.handle_server_event(server_id, frame)

    async def _on_server_connect(self, server_id: str, info: ServerInfo):
        """处理服务器连接"""
        config = self._server_configs.get(server_id)
        if not config:
            return

        logger.info(
            f"[MC-{server_id}] 已连接到 {info.name} "
            f"({info.platform} {info.minecraft_version})"
        )

        # 如果启用了 AI 聊天，创建平台适配器
        if config.enable_ai_chat:
            server = self.server_manager.get_server(server_id)
            if server:
                # 防止重复连接时遗留旧适配器/任务
                old_adapter = self._adapters.pop(server_id, None)
                if old_adapter:
                    await old_adapter.stop()

                old_task = self._adapter_tasks.pop(server_id, None)
                if old_task and not old_task.done():
                    old_task.cancel()

                event_queue = (
                    self.context.platform_manager.event_queue
                    if self.context and self.context.platform_manager
                    else asyncio.Queue()
                )
                adapter = MCPlatformAdapter(
                    server_config=config,
                    server_connection=server,
                    event_queue=event_queue,
                )
                self._adapters[server_id] = adapter

                # 启动适配器（非阻塞）
                task = self._schedule_task(adapter.run(), f"adapter:{server_id}")
                if task:
                    self._adapter_tasks[server_id] = task

                logger.info(f"[MC-{server_id}] 平台适配器已注册")

    async def _on_server_disconnect(self, server_id: str, reason: str):
        """处理服务器断开连接"""
        logger.warning(f"[MC-{server_id}] 已断开连接: {reason}")

        # 停止平台适配器
        adapter = self._adapters.pop(server_id, None)
        if adapter:
            await adapter.stop()

        task = self._adapter_tasks.pop(server_id, None)
        if task and not task.done():
            task.cancel()

    # 命令处理器

    @filter.command_group("mc")
    def mc_group(self):
        """我的世界服务器管理命令"""
        pass

    @mc_group.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        async for result in self._dispatch_command("handle_help", event):
            yield result

    @mc_group.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看服务器状态"""
        async for result in self._dispatch_command("handle_status", event):
            yield result

    @mc_group.command("list")
    async def cmd_list(self, event: AstrMessageEvent):
        """查看在线玩家列表"""
        async for result in self._dispatch_command("handle_list", event):
            yield result

    @mc_group.command("player")
    async def cmd_player(self, event: AstrMessageEvent, player_id: str):
        """查看玩家详细信息"""
        async for result in self._dispatch_command("handle_player", event, player_id):
            yield result

    @mc_group.command("cmd")
    async def cmd_execute(self, event: AstrMessageEvent, command=GreedyStr):
        """远程执行服务器指令"""
        async for result in self._dispatch_command("handle_cmd", event, str(command)):
            yield result

    @mc_group.command("bind")
    async def cmd_bind(self, event: AstrMessageEvent, player_id: str):
        """绑定游戏ID"""
        async for result in self._dispatch_command("handle_bind", event, player_id):
            yield result

    @mc_group.command("unbind")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """解除绑定"""
        async for result in self._dispatch_command("handle_unbind", event):
            yield result

    async def _dispatch_command(self, method_name: str, event: AstrMessageEvent, *args):
        """统一分发到命令处理器，减少重复样板代码。"""
        if not self.command_handler:
            return

        handler = getattr(self.command_handler, method_name, None)
        if not handler:
            logger.warning(f"[MC Adapter] 未找到命令处理方法: {method_name}")
            return

        async for result in handler(event, *args):
            yield result

    # 消息转发监听器

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听需要转发到 MC 服务器的消息，并处理自定义指令和待选操作

        Note: mc 子命令（help/status/list 等）已由 @mc_group.command()
        装饰器处理，此处仅处理：
        1. 待选操作的数字输入（多服务器选择）
        2. 自定义指令匹配
        3. 消息转发到 MC 服务器
        """
        # Skip messages already handled by command group (wake prefix / @)
        if event.is_at_or_wake_command:
            return

        if not self.command_handler:
            # No command handler, only try message forwarding
            if await self.message_bridge.handle_external_message(event):
                event.stop_event()
            return

        text = event.get_message_str().strip()
        umo = event.unified_msg_origin

        # Handle pending server/backend selection (number input)
        if text and text.isdigit() and self.command_handler.has_pending_action(umo):
            async for result in self.command_handler.dispatch_number_selection(event):
                yield result
            event.stop_event()
            return

        # Check custom commands
        async for result in self.command_handler.handle_custom_command(event):
            yield result
        if event.get_extra("custom_cmd_matched"):
            event.stop_event()
            return

        # Forward message to MC server(s)
        if await self.message_bridge.handle_external_message(event):
            event.stop_event()

    async def terminate(self):
        """插件终止时的清理工作"""
        logger.info("[MC Adapter] 正在关闭...")

        # 停止初始化任务
        if self._init_task and not self._init_task.done():
            self._init_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._init_task
        self._init_task = None

        # 停止所有适配器
        for adapter in self._adapters.values():
            await adapter.stop()
        self._adapters.clear()

        # 取消所有适配器任务
        for task in self._adapter_tasks.values():
            if not task.done():
                task.cancel()
        if self._adapter_tasks:
            await asyncio.gather(*self._adapter_tasks.values(), return_exceptions=True)
        self._adapter_tasks.clear()

        # 停止所有服务器连接
        await self.server_manager.stop_all()

        logger.info("[MC Adapter] 关闭完成")
