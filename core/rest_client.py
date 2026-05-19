"""Minecraft 服务器通信的 REST API 客户端"""

from typing import Any
from urllib.parse import quote

import aiohttp

from astrbot.api import logger

from .models import (
    ApiResponse,
    LogEntry,
    PlayerDetail,
    PlayerInfo,
    ServerInfo,
    ServerStatus,
)

# REST API 常量
DEFAULT_REQUEST_TIMEOUT = 30  # 默认请求超时（秒）
HEALTH_CHECK_TIMEOUT = 5  # 健康检查超时（秒）
MAX_LOG_LINES = 1000  # 最大日志行数


class RestClient:
    """与 Minecraft 服务器通信的 REST API 客户端"""

    def __init__(
        self,
        server_id: str,
        host: str,
        port: int,
        token: str,
        request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    ):
        self.server_id = server_id
        self.host = host
        self.port = port
        self.token = token
        self._request_timeout = request_timeout
        self._session: aiohttp.ClientSession | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/api/v1"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        """关闭 HTTP 会话"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        json_data: dict | None = None,
    ) -> ApiResponse:
        """向服务器发送 HTTP 请求"""
        url = f"{self.base_url}{endpoint}"

        try:
            session = await self._get_session()
            async with session.request(
                method,
                url,
                headers=self.headers,
                params=params,
                json=json_data,
                timeout=aiohttp.ClientTimeout(total=self._request_timeout),
            ) as resp:
                data = await resp.json()
                return ApiResponse.from_dict(data)

        except aiohttp.ClientConnectorError:
            logger.error(f"[MC-{self.server_id}] 无法连接到服务器")
            return ApiResponse(code=3002, message="服务器连接失败")
        except TimeoutError:
            logger.error(f"[MC-{self.server_id}] 请求超时")
            return ApiResponse(code=3002, message="请求超时")
        except Exception as e:
            logger.error(f"[MC-{self.server_id}] 请求错误: {e}")
            return ApiResponse(code=3001, message=str(e))

    async def _get(self, endpoint: str, params: dict | None = None) -> ApiResponse:
        return await self._request("GET", endpoint, params=params)

    async def _post(self, endpoint: str, json_data: dict | None = None) -> ApiResponse:
        return await self._request("POST", endpoint, json_data=json_data)

    # 服务器 APIs

    async def get_server_info(self) -> tuple[ServerInfo | None, str]:
        """获取服务器信息"""
        resp = await self._get("/server/info")
        if resp.success and resp.data:
            info = ServerInfo.from_dict(resp.data)
            if info.is_proxy:
                logger.debug(
                    f"[MC-{self.server_id}] 代理模式: "
                    f"{info.backend_count}个后端, "
                    f"总在线{info.aggregate_online}/{info.aggregate_max}"
                )
            return info, ""
        return None, resp.message

    async def get_server_status(self) -> tuple[ServerStatus | None, str]:
        """获取服务器状态"""
        resp = await self._get("/server/status")
        if resp.success and resp.data:
            status = ServerStatus.from_dict(resp.data)
            if status.is_proxy:
                logger.debug(
                    f"[MC-{self.server_id}] 代理状态: "
                    f"{len(status.backends)}个后端已上报"
                )
            return status, ""
        return None, resp.message

    async def health_check(self) -> bool:
        """检查服务器是否健康（不需要认证）"""
        try:
            session = await self._get_session()
            url = f"http://{self.host}:{self.port}/api/v1/health"
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT)
            ) as resp:
                data = await resp.json()
                return data.get("code") == 0
        except Exception:
            return False

    # 玩家 APIs

    async def get_players(
        self, detail: bool = False, include_offline: bool = False
    ) -> tuple[list[PlayerInfo], int, str]:
        """获取在线玩家列表"""
        resp = await self._get(
            "/players",
            params={
                "detail": str(detail).lower(),
                "includeOffline": str(include_offline).lower(),
            },
        )
        if resp.success and resp.data:
            players = [PlayerInfo.from_dict(p) for p in resp.data.get("players", [])]
            total = resp.data.get("total", resp.data.get("count", len(players)))
            return players, total, ""
        return [], 0, resp.message

    async def get_player(self, identifier: str) -> tuple[PlayerDetail | None, str]:
        """通过 UUID 或名称获取玩家详细信息"""
        resp = await self._get(f"/players/{quote(identifier, safe='')}")
        if resp.success and resp.data:
            return PlayerDetail.from_dict(resp.data), ""
        return None, resp.message

    # Aliases for backward compatibility
    get_player_by_uuid = get_player
    get_player_by_name = get_player

    # 命令 APIs

    async def execute_command(
        self,
        command: str,
        executor: str = "CONSOLE",
        player_uuid: str | None = None,
        is_async: bool = False,
        target_server_id: str | None = None,
    ) -> tuple[bool, str, Any]:
        """在服务器上执行命令

        参数:
            command: 要执行的指令（不带/）
            executor: 执行者 CONSOLE/PLAYER
            player_uuid: executor为PLAYER时指定
            is_async: 是否异步执行
            target_server_id: 目标后端路由 ID（仅代理端有效）

        返回:
            tuple: (成功, 输出/错误消息, 原始数据)
        """
        json_data: dict[str, Any] = {
            "command": command,
            "executor": executor,
            "async": is_async,
        }
        if player_uuid:
            json_data["playerUuid"] = player_uuid
        if target_server_id:
            json_data["targetServerId"] = target_server_id

        resp = await self._post("/command/execute", json_data=json_data)

        if resp.success and resp.data:
            if is_async:
                return True, resp.data.get("taskId", ""), resp.data
            return (
                resp.data.get("success", False),
                resp.data.get("output", ""),
                resp.data,
            )

        return False, resp.message, None

    # 日志 APIs

    async def get_logs(
        self,
        lines: int = 100,
        level: str | None = None,
        keyword: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> tuple[list[LogEntry], str]:
        """获取服务器日志"""
        params: dict[str, Any] = {"lines": min(lines, MAX_LOG_LINES)}
        if level:
            params["level"] = level
        if keyword:
            params["keyword"] = keyword
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        resp = await self._get("/logs", params=params)
        if resp.success and resp.data:
            logs = [LogEntry.from_dict(log) for log in resp.data.get("logs", [])]
            return logs, ""
        return [], resp.message
