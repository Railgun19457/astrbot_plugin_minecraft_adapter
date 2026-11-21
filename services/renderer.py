"""Render Minecraft server/player info as image or text."""

import asyncio
import base64
import contextlib
import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from astrbot.api import logger

if TYPE_CHECKING:
    from ..core.protocol.payloads import (
        PlayerDetail,
        PlayerInfo,
        ServerInfo,
        ServerStatus,
    )


@dataclass
class RenderResult:
    content: str | BytesIO
    is_image: bool

    @property
    def text(self) -> str:
        if self.is_image:
            raise ValueError("无法从图片内容中获取文本")
        return str(self.content)

    @property
    def image(self) -> BytesIO:
        if not self.is_image:
            raise ValueError("无法从文本内容中获取图片")
        return self.content  # type: ignore[return-value]


class InfoRenderer:
    _FONT_FILENAME = "LXGWWenKaiLite-Regular.ttf"
    _FONT_URLS = [
        "https://ghproxy.net/https://raw.githubusercontent.com/lxgw/LxgwWenKai-Lite/main/fonts/TTF/LXGWWenKaiLite-Regular.ttf",
        "https://jsd.cdn.zzko.cn/gh/lxgw/LxgwWenKai-Lite@main/fonts/TTF/LXGWWenKaiLite-Regular.ttf",
        "https://raw.githubusercontent.com/lxgw/LxgwWenKai-Lite/main/fonts/TTF/LXGWWenKaiLite-Regular.ttf",
        "https://cdn.jsdelivr.net/gh/lxgw/LxgwWenKai-Lite@main/fonts/TTF/LXGWWenKaiLite-Regular.ttf",
    ]

    _CARD_W = 1120
    _OUTER_BG = "#f3f4f6"
    _CARD_BG = "#ffffff"
    _PROXY_NAMES = {"vc", "velocity", "proxy", "bungeecord", "waterfall"}

    # Common Colors
    _COLOR_PRIMARY = "#3b82f6"
    _COLOR_SUCCESS = "#059669"
    _COLOR_WARNING = "#d97706"
    _COLOR_DANGER = "#dc2626"
    _COLOR_TEXT_MAIN = "#111827"
    _COLOR_TEXT_SUB = "#6b7280"
    _COLOR_BG_LIGHT = "#f8fafc"
    _COLOR_BG_BADGE = "#eef4ff"

    def __init__(self, text2image_enabled: bool = True, cache_dir: Path | None = None):
        self.text2image_enabled = text2image_enabled
        self._cache_dir = cache_dir or (Path(__file__).parent.parent / ".cache")
        self._font_dir = self._cache_dir / "fonts"
        self._avatar_dir = self._cache_dir / "avatars"
        self._font_path = self._font_dir / self._FONT_FILENAME
        self._assets_ready = False
        self._asset_lock = asyncio.Lock()

    async def _ensure_assets(self):
        if self._assets_ready:
            return
        async with self._asset_lock:
            if self._assets_ready:
                return
            self._font_dir.mkdir(parents=True, exist_ok=True)
            self._avatar_dir.mkdir(parents=True, exist_ok=True)
            await self._ensure_font_cached()
            self._assets_ready = True

    async def _ensure_font_cached(self):
        if self._font_path.exists() and self._font_path.stat().st_size > 0:
            return
        timeout = aiohttp.ClientTimeout(total=60)
        for url in self._FONT_URLS:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.read()
                if len(data) < 100 * 1024:
                    continue
                self._font_path.write_bytes(data)
                logger.info(
                    f"[Renderer] 已缓存中文字体: {self._font_path.name} ({len(data) // 1024}KB)"
                )
                return
            except Exception as exc:
                logger.debug(f"[Renderer] 字体下载失败: {url} -> {exc}")
        logger.warning("[Renderer] 字体下载失败，将回退到系统字体")

    def _font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        # 1. 优先使用已缓存的插件专用中文字体
        try:
            if self._font_path.exists():
                return ImageFont.truetype(str(self._font_path), size=size)
        except Exception as exc:
            logger.debug(f"[Renderer] 加载已缓存字体失败: {exc}")

        # 2. 尝试 AstrBot 数据根目录下的 font.ttf (用户自定义)
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            custom_font = Path(get_astrbot_data_path()) / "font.ttf"
            if custom_font.exists():
                return ImageFont.truetype(str(custom_font), size=size)
        except Exception:
            pass

        # 3. 跨平台常见中文字体 fallback
        # Linux 下如果没有安装字体包，下载通常是最好的选择，但这里也列出常见的避免完全失败
        fallback_fonts = [
            "msyh.ttc",  # Windows - 微软雅黑
            "simsun.ttc",  # Windows - 宋体
            "NotoSansCJK-Regular.ttc",  # Linux (Ubuntu/Debian)
            "wqy-microhei.ttc",  # Linux (CentOS/Alpine)
            "PingFang.ttc",  # macOS
            "DroidSansFallback.ttf",  # 备用
        ]
        for font_name in fallback_fonts:
            try:
                return ImageFont.truetype(font_name, size=size)
            except Exception:
                continue

        # 4. 最后兜底
        if not self._font_path.exists():
            logger.warning(
                f"[Renderer] 无法加载中文字体，渲染结果可能乱码。请确保网络畅通以自动下载字体，或手动放置字体文件到: {self._font_path}"
            )
        return ImageFont.load_default()

    @staticmethod
    def _safe_percent(value: float | int, lo: int = 0, hi: int = 100) -> int:
        try:
            return max(lo, min(hi, int(round(float(value)))))
        except Exception:
            return lo

    @staticmethod
    def _mode_cn(mode: str) -> str:
        return {
            "SURVIVAL": "生存",
            "CREATIVE": "创造",
            "ADVENTURE": "冒险",
            "SPECTATOR": "旁观",
        }.get(mode, mode or "未知")

    def _get_status_color(self, value: float, type: str = "tps") -> str:
        if type == "tps":
            if value >= 19:
                return self._COLOR_SUCCESS
            if value >= 15:
                return self._COLOR_WARNING
            return self._COLOR_DANGER
        if type == "ping":
            if value < 100:
                return self._COLOR_SUCCESS
            if value < 200:
                return self._COLOR_WARNING
            return self._COLOR_DANGER
        if type == "memory":
            if value < 70:
                return self._COLOR_SUCCESS
            if value < 90:
                return self._COLOR_WARNING
            return self._COLOR_DANGER
        return self._COLOR_TEXT_MAIN

    def _draw_progress(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        w: int,
        h: int,
        percent: int,
        color: str,
    ):
        draw.rounded_rectangle((x, y, x + w, y + h), radius=5, fill="#e5e7eb")
        fill_w = int(w * (percent / 100))
        if fill_w > 0:
            draw.rounded_rectangle((x, y, x + fill_w, y + h), radius=5, fill=color)

    def _draw_header(
        self, draw: ImageDraw.ImageDraw, y: int, title: str, sub_title: str
    ):
        draw.rounded_rectangle(
            (22, y, self._CARD_W - 22, y + 126), radius=16, fill=self._COLOR_BG_BADGE
        )
        draw.rectangle((36, y + 18, 47, y + 108), fill=self._COLOR_PRIMARY)
        draw.text(
            (62, y + 18),
            title,
            font=self._font(44),
            fill=self._COLOR_TEXT_MAIN,
        )
        draw.text(
            (62, y + 78),
            sub_title,
            font=self._font(20),
            fill=self._COLOR_TEXT_SUB,
        )
        return y + 144

    def _draw_section_box(
        self,
        draw: ImageDraw.ImageDraw,
        y: int,
        title: str,
        bg_color: str,
        text_color: str,
        height: int,
    ):
        draw.rounded_rectangle(
            (28, y, self._CARD_W - 28, y + height),
            radius=12,
            fill=self._CARD_BG,
            outline="#e5e7eb",
        )
        draw.rounded_rectangle(
            (28, y, self._CARD_W - 28, y + 44),
            radius=12,
            fill=bg_color,
        )
        draw.rectangle((28, y + 30, self._CARD_W - 28, y + 44), fill=bg_color)
        draw.text((44, y + 10), title, font=self._font(24), fill=text_color)

    def _placeholder_avatar_face(self) -> Image.Image:
        face = Image.new("RGBA", (8, 8), "#d1d5db")
        d = ImageDraw.Draw(face)
        for yy in range(0, 8, 2):
            for xx in range((yy // 2) % 2, 8, 2):
                d.point((xx, yy), fill="#9ca3af")
        d.point((2, 3), fill="#374151")
        d.point((5, 3), fill="#374151")
        return face

    @staticmethod
    def _rounded_avatar(img: Image.Image, radius: int = 10) -> Image.Image:
        avatar = img.convert("RGBA")
        mask = Image.new("L", avatar.size, 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle(
            (0, 0, avatar.width, avatar.height), radius=radius, fill=255
        )
        avatar.putalpha(mask)
        return avatar

    @staticmethod
    def _norm(s: str) -> str:
        return (s or "").strip()

    def _is_proxy_like_name(self, name: str) -> bool:
        n = self._norm(name).lower()
        if not n:
            return False
        if n in self._PROXY_NAMES:
            return True
        return any(k in n for k in ("velocity", "proxy", "bungee", "waterfall", "vc"))

    def _get_effective_server_name(
        self, player: "PlayerInfo | PlayerDetail", fallback: str
    ) -> str:
        """优先使用玩家后端服名；独立服场景回退到fallback；不再使用world兜底。"""
        fallback_norm = self._norm(fallback)
        server = self._norm(getattr(player, "server", ""))
        if (
            server
            and server.lower() not in self._PROXY_NAMES
            and server.lower() != fallback_norm.lower()
            and not self._is_proxy_like_name(server)
        ):
            return server
        if fallback_norm and not self._is_proxy_like_name(fallback_norm):
            return fallback_norm
        return ""

    async def _download_image(
        self, session: aiohttp.ClientSession, url: str
    ) -> Image.Image | None:
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
            if not data:
                return None
            return Image.open(BytesIO(data)).convert("RGBA")
        except Exception:
            return None

    async def _fetch_avatar_face(
        self, player_name: str, player_uuid: str
    ) -> Image.Image | None:
        timeout = aiohttp.ClientTimeout(total=10)
        name = self._norm(player_name)
        uuid = self._norm(player_uuid).replace("-", "")
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if name:
                    for url in (
                        f"https://mc-heads.net/avatar/{quote(name)}/8",
                        f"https://minotar.net/helm/{quote(name)}/8.png",
                    ):
                        img = await self._download_image(session, url)
                        if img is not None:
                            return img.resize((8, 8), Image.Resampling.NEAREST)
                if uuid:
                    for url in (
                        f"https://crafatar.com/avatars/{uuid}?size=8&overlay",
                        f"https://mc-heads.net/avatar/{uuid}/8",
                    ):
                        img = await self._download_image(session, url)
                        if img is not None:
                            return img.resize((8, 8), Image.Resampling.NEAREST)

                resolved_uuid = uuid
                if not resolved_uuid and name:
                    lookup = (
                        f"https://api.mojang.com/users/profiles/minecraft/{quote(name)}"
                    )
                    async with session.get(lookup) as resp:
                        if resp.status == 200:
                            profile = await resp.json(content_type=None)
                            resolved_uuid = str(profile.get("id", ""))
                if not resolved_uuid:
                    return None

                profile_url = f"https://sessionserver.mojang.com/session/minecraft/profile/{resolved_uuid}"
                async with session.get(profile_url) as resp:
                    if resp.status != 200:
                        return None
                    profile_data = await resp.json(content_type=None)

                textures_b64 = ""
                for prop in profile_data.get("properties", []):
                    if prop.get("name") == "textures":
                        textures_b64 = prop.get("value", "")
                        break
                if not textures_b64:
                    return None

                decoded = base64.b64decode(textures_b64).decode("utf-8")
                textures_obj = json.loads(decoded)
                skin_url = (
                    textures_obj.get("textures", {}).get("SKIN", {}).get("url", "")
                )
                if not skin_url:
                    return None
                skin = await self._download_image(session, skin_url)
                if skin is None or skin.width < 16 or skin.height < 16:
                    return None
                face = skin.crop((8, 8, 16, 16))
                if skin.width >= 64 and skin.height >= 16:
                    overlay = skin.crop((40, 8, 48, 16))
                    face = Image.alpha_composite(face, overlay)
                return face
        except Exception as exc:
            logger.debug(
                f"[Renderer] 获取玩家头像失败: {player_name}/{player_uuid} -> {exc}"
            )
            return None

    async def _get_avatar(
        self, player_name: str, player_uuid: str, size: int
    ) -> Image.Image:
        key = (
            self._norm(player_name).lower()
            or self._norm(player_uuid).replace("-", "").lower()
            or "unknown"
        )
        path = self._avatar_dir / f"{key}_{size}.png"
        if path.exists():
            try:
                return Image.open(path).convert("RGBA")
            except Exception:
                with contextlib.suppress(Exception):
                    path.unlink()

        face = await self._fetch_avatar_face(player_name, player_uuid)
        if face is None:
            face = self._placeholder_avatar_face()
        avatar = face.resize((size, size), Image.Resampling.NEAREST)
        with contextlib.suppress(Exception):
            avatar.save(path, format="PNG")
        return avatar

    def _new_card(self, estimate_h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        img = Image.new("RGB", (self._CARD_W, max(estimate_h, 240)), self._CARD_BG)
        return img, ImageDraw.Draw(img)

    async def _merge_images_vertical(
        self,
        images: list[Image.Image],
        background: str | None = None,
        gap: int = 8,
        pad: int = 10,
    ) -> BytesIO:
        if len(images) == 1:
            out = BytesIO()
            images[0].save(out, format="PNG", optimize=True)
            out.seek(0)
            return out
        bg = background or self._OUTER_BG
        max_width = max(im.width for im in images)
        total_h = sum(im.height for im in images) + gap * (len(images) - 1) + pad * 2
        merged = Image.new("RGB", (max_width + pad * 2, total_h), bg)
        y = pad
        for im in images:
            x = (merged.width - im.width) // 2
            merged.paste(im, (x, y))
            y += im.height + gap
        out = BytesIO()
        merged.save(out, format="PNG", optimize=True)
        out.seek(0)
        return out

    async def _render_server_status_image(
        self,
        server_info: "ServerInfo",
        server_status: "ServerStatus",
        server_tag: str = "",
    ) -> BytesIO:
        online_count = server_info.online_count or server_status.online_players
        max_players = server_info.max_players or server_status.max_players
        uptime = (
            server_info.uptime_formatted or server_status.uptime_formatted or "未知"
        )
        estimate_h = 430 + len(server_status.worlds) * 72
        if not server_status.is_proxy:
            estimate_h += 80
        if server_status.is_proxy:
            estimate_h += max(1, len(server_status.backends)) * 106

        image, draw = self._new_card(estimate_h)
        body_font = self._font(24)
        small_font = self._font(20)

        y = self._draw_header(
            draw,
            24,
            f"服务器状态  {server_info.name}",
            f"{server_info.platform}  {server_info.minecraft_version}",
        )

        panel_h = 106
        gap = 14
        panel_w = (self._CARD_W - 84 - gap * 2) // 3

        # Stats Panels
        stats = [
            ("在线玩家", f"{online_count}/{max_players}", None),
            ("运行时间", uptime, None),
            (
                "内存使用",
                f"{server_status.memory_used}MB / {server_status.memory_max}MB",
                "memory",
            ),
        ]

        x = 42
        for title, val, stype in stats:
            draw.rounded_rectangle(
                (x, y, x + panel_w, y + panel_h), radius=12, fill=self._COLOR_BG_LIGHT
            )
            draw.text(
                (x + 18, y + 16), title, font=small_font, fill=self._COLOR_TEXT_SUB
            )
            draw.text(
                (x + 18, y + 50),
                val,
                font=self._font(30) if stype != "memory" else body_font,
                fill=self._COLOR_TEXT_MAIN,
            )
            if stype == "memory":
                mem = self._safe_percent(server_status.memory_usage_percent)
                self._draw_progress(
                    draw,
                    x + 18,
                    y + 80,
                    panel_w - 36,
                    12,
                    mem,
                    self._get_status_color(mem, "memory"),
                )
            x += panel_w + gap

        y += panel_h + 18

        if server_info.is_proxy and server_info.aggregate_online > 0:
            draw.rounded_rectangle(
                (42, y, self._CARD_W - 42, y + 48), radius=10, fill="#eff6ff"
            )
            draw.text(
                (58, y + 12),
                f"总在线: {server_info.aggregate_online}/{server_info.aggregate_max}",
                font=body_font,
                fill="#1d4ed8",
            )
            y += 60

        if not server_status.is_proxy:
            draw.rounded_rectangle(
                (42, y, self._CARD_W - 42, y + 84), radius=12, fill=self._COLOR_BG_LIGHT
            )
            draw.text(
                (58, y + 14),
                "TPS (1m / 5m / 15m)",
                font=small_font,
                fill=self._COLOR_TEXT_SUB,
            )
            tx = 58
            for idx, value in enumerate(
                (server_status.tps_1m, server_status.tps_5m, server_status.tps_15m)
            ):
                t = f"{value:.1f}"
                draw.text(
                    (tx, y + 44),
                    t,
                    font=body_font,
                    fill=self._get_status_color(value, "tps"),
                )
                tx += int(draw.textlength(t, font=body_font)) + 14
                if idx < 2:
                    draw.text((tx, y + 44), "|", font=body_font, fill="#9ca3af")
                    tx += 14
            y += 96

        if server_status.worlds:
            draw.text(
                (42, y), "世界列表", font=self._font(30), fill=self._COLOR_TEXT_MAIN
            )
            y += 46
            for world in server_status.worlds:
                draw.rounded_rectangle(
                    (42, y, self._CARD_W - 42, y + 50), radius=10, fill="#f9fafb"
                )
                draw.text(
                    (58, y + 11),
                    str(world.get("name", "world")),
                    font=body_font,
                    fill="#374151",
                )
                metric = f"玩家 {world.get('players', 0)}   实体 {world.get('entities', 0)}   区块 {world.get('loadedChunks', 0)}"
                tw = int(draw.textlength(metric, font=small_font))
                draw.text(
                    (self._CARD_W - 58 - tw, y + 14),
                    metric,
                    font=small_font,
                    fill=self._COLOR_TEXT_SUB,
                )
                y += 60

        card = image.crop((0, 0, self._CARD_W, min(max(y + 28, 280), image.height)))
        out = BytesIO()
        card.save(out, format="PNG", optimize=True)
        out.seek(0)
        return out

    def _effective_player_server_id(self, player: "PlayerDetail", fallback: str) -> str:
        """玩家详情展示用服务器名：群组服优先后端子服，避免展示代理层名称。"""
        return self._get_effective_server_name(player, fallback)

    def _effective_player_info_server_id(
        self, player: "PlayerInfo", fallback: str
    ) -> str:
        return self._get_effective_server_name(player, fallback) or "未标记子服"

    def _flatten_player_cards(
        self,
        cards: list[tuple[str, list["PlayerInfo"], int, str]],
    ) -> list[tuple[str, list["PlayerInfo"], int, str]]:
        """将代理服玩家按后端ID拆平，与独立服同层级展示。"""
        flattened: list[tuple[str, list[PlayerInfo], int, str]] = []
        for sid, players, total, server_name in cards:
            primary_name = self._norm(server_name) or sid
            if not players:
                flattened.append((sid, [], total, primary_name))
                continue

            grouped: dict[str, list[PlayerInfo]] = {}
            for p in players:
                group_id = self._effective_player_info_server_id(p, sid)
                grouped.setdefault(group_id, []).append(p)

            if len(grouped) <= 1:
                only_key = next(iter(grouped.keys())) if grouped else sid
                flattened.append(
                    (
                        only_key,
                        players,
                        total if total > 0 else len(players),
                        (primary_name if only_key == sid else only_key),
                    )
                )
                continue

            for gid, gplayers in grouped.items():
                display_name = primary_name if gid == sid else gid
                flattened.append((gid, gplayers, len(gplayers), display_name))

        return flattened

    async def _render_multi_player_list_image(
        self,
        cards: list[tuple[str, list["PlayerInfo"], int, str]],
    ) -> BytesIO:
        flattened = self._flatten_player_cards(cards)
        total_players = sum(
            (total if total > 0 else len(players)) for _, players, total, _ in flattened
        )
        row_count = sum(max(1, len(players)) for _, players, _, _ in flattened)
        estimate_h = 170 + row_count * 74 + len(flattened) * 56

        image, draw = self._new_card(estimate_h)
        body_font = self._font(24)
        small_font = self._font(20)

        y = self._draw_header(
            draw, 24, f"在线玩家总览 ({total_players})", "实时在线玩家列表"
        )

        if not flattened:
            draw.rounded_rectangle(
                (34, y, self._CARD_W - 34, y + 84), radius=12, fill="#f9fafb"
            )
            draw.text(
                (self._CARD_W // 2 - 110, y + 30),
                "当前没有玩家在线",
                font=body_font,
                fill="#9ca3af",
            )
            y += 96
        else:
            for server_id, players, total, server_name in flattened:
                server_count = total if total > 0 else len(players)
                draw.rounded_rectangle(
                    (34, y, self._CARD_W - 34, y + 40), radius=9, fill="#dbeafe"
                )
                server_title = server_name or server_id
                draw.text(
                    (46, y + 10),
                    f"服务器: {server_title}   ({server_count}人)",
                    font=small_font,
                    fill="#1d4ed8",
                )
                y += 48

                for row_idx, p in enumerate(players):
                    row_bg = self._CARD_BG if row_idx % 2 == 0 else self._COLOR_BG_LIGHT
                    ping_color = self._get_status_color(p.ping, "ping")
                    draw.rounded_rectangle(
                        (42, y, self._CARD_W - 42, y + 68),
                        radius=10,
                        fill=row_bg,
                        outline="#f3f4f6",
                    )
                    draw.rounded_rectangle(
                        (46, y + 8, 52, y + 60), radius=3, fill=ping_color
                    )

                    avatar = await self._get_avatar(p.name, p.uuid, size=50)
                    avatar = self._rounded_avatar(avatar, radius=10)
                    image.paste(avatar, (54, y + 9), avatar)

                    draw.text(
                        (118, y + 11),
                        p.name,
                        font=body_font,
                        fill=self._COLOR_TEXT_MAIN,
                    )
                    mode = self._mode_cn(p.game_mode)
                    line = f"模式 {mode}   世界 {p.world or '未知'}"
                    draw.text(
                        (118, y + 38), line, font=small_font, fill=self._COLOR_TEXT_SUB
                    )

                    pt = f"{p.ping}ms"
                    tw = int(draw.textlength(pt, font=small_font))
                    draw.text(
                        (self._CARD_W - 56 - tw, y + 23),
                        pt,
                        font=small_font,
                        fill=ping_color,
                    )
                    y += 76
                y += 8

        card = image.crop((0, 0, self._CARD_W, min(max(y + 20, 240), image.height)))
        out = BytesIO()
        card.save(out, format="PNG", optimize=True)
        out.seek(0)
        return out

    async def _render_player_detail_image(
        self, player: "PlayerDetail", server_tag: str = ""
    ) -> BytesIO:
        estimate_h = 820 if player.location else 760
        image, draw = self._new_card(estimate_h)
        body_font = self._font(24)
        small_font = self._font(20)

        y = 24
        # --- Header ---
        detail_server_name = self._effective_player_server_id(player, server_tag)
        if detail_server_name:
            badge = f"服务器: {detail_server_name}"
            bw = int(draw.textlength(badge, font=small_font)) + 30
            draw.rounded_rectangle(
                (self._CARD_W - bw - 30, y + 6, self._CARD_W - 30, y + 44),
                radius=10,
                fill="#dbeafe",
            )
            draw.text(
                (self._CARD_W - bw - 14, y + 14), badge, font=small_font, fill="#1d4ed8"
            )

        avatar = await self._get_avatar(player.name, player.uuid, size=92)
        avatar = self._rounded_avatar(avatar, radius=14)
        image.paste(avatar, (34, y), avatar)

        draw.text(
            (142, y + 4), player.name, font=self._font(42), fill=self._COLOR_TEXT_MAIN
        )
        draw.text(
            (142, y + 58), player.uuid, font=small_font, fill=self._COLOR_TEXT_SUB
        )
        if player.is_op:
            draw.rounded_rectangle(
                (430, y + 10, 560, y + 46), radius=8, fill="#fef3c7", outline="#fcd34d"
            )
            draw.text((454, y + 18), "管理员", font=small_font, fill="#b45309")
        y += 136

        # --- Sections ---
        sections = [
            (
                "▶ 基础信息",
                "#ecfeff",
                "#155e75",
                110,
                [
                    ((44, 0), f"世界: {player.world or '未知'}", self._COLOR_TEXT_MAIN),
                    (
                        (370, 0),
                        f"模式: {self._mode_cn(player.game_mode)}",
                        self._COLOR_TEXT_MAIN,
                    ),
                    (
                        (700, 0),
                        f"延迟: {player.ping}ms",
                        self._get_status_color(player.ping, "ping"),
                    ),
                ],
            ),
            (
                "▶ 状态面板",
                "#eef2ff",
                "#3730a3",
                220,
                [
                    (
                        "progress",
                        44,
                        f"生命值 {player.health:.1f}/{player.max_health:.1f}",
                        (player.health / player.max_health * 100)
                        if player.max_health
                        else 0,
                        "#ef4444",
                    ),
                    (
                        "progress",
                        94,
                        f"饥饿值 {player.food_level}/20",
                        player.food_level * 5,
                        "#f59e0b",
                    ),
                    (
                        "progress",
                        144,
                        f"等级 {player.level} ({player.exp * 100:.1f}%)",
                        player.exp * 100,
                        "#10b981",
                    ),
                ],
            ),
            (
                "▶ 在线信息",
                "#f0fdf4",
                "#166534",
                158 if player.location else 110,
                [
                    (
                        (44, 0),
                        f"在线时长: {player.online_time_formatted or '未知'}",
                        self._COLOR_TEXT_MAIN,
                    ),
                ],
            ),
        ]

        if player.location:
            loc_text = f"位置: X={player.location.get('x', 0):.1f}, Y={player.location.get('y', 0):.1f}, Z={player.location.get('z', 0):.1f}"
            sections[2][4].append(((44, 48), loc_text, self._COLOR_TEXT_MAIN))

        for title, bg, tc, h, items in sections:
            self._draw_section_box(draw, y, title, bg, tc, h)
            for item in items:
                if item[0] == "progress":
                    _, py, label, pct, color = item
                    draw.text((44, y + py + 20), label, font=body_font, fill="#374151")
                    self._draw_progress(
                        draw,
                        44,
                        y + py + 54,
                        self._CARD_W - 88,
                        12,
                        self._safe_percent(pct),
                        color,
                    )
                else:
                    (ix, iy), text, color = item
                    draw.text((ix, y + iy + 64), text, font=body_font, fill=color)
            y += h + 24

        card = image.crop((0, 0, self._CARD_W, min(max(y + 10, 260), image.height)))
        out = BytesIO()
        card.save(out, format="PNG", optimize=True)
        out.seek(0)
        return out

    async def render_multi_server_status(
        self,
        cards: list[tuple[str, "ServerInfo", "ServerStatus"]],
        as_image: bool = True,
    ) -> RenderResult:
        if not cards:
            return RenderResult(" 没有可渲染的服务器状态", is_image=False)

        if not as_image or not self.text2image_enabled:
            text = "\n\n".join(
                self._format_server_status_text(info, status, server_tag=tag)
                for tag, info, status in cards
            )
            return RenderResult(text, is_image=False)

        try:
            await self._ensure_assets()
            ims: list[Image.Image] = []
            for tag, info, status in cards:
                single = await self._render_server_status_image(
                    info, status, server_tag=tag
                )
                ims.append(Image.open(single).convert("RGB"))
            out = await self._merge_images_vertical(ims, gap=8, pad=10)
            return RenderResult(out, is_image=True)
        except Exception as exc:
            logger.warning(f"[Renderer] 多服务器状态合图失败，回退文本: {exc}")
            text = "\n\n".join(
                self._format_server_status_text(info, status, server_tag=tag)
                for tag, info, status in cards
            )
            return RenderResult(text, is_image=False)

    async def render_multi_player_list(
        self,
        cards: list[tuple[str, list["PlayerInfo"], int, str]],
        as_image: bool = True,
    ) -> RenderResult:
        if not cards:
            return RenderResult(" 没有可渲染的玩家列表", is_image=False)

        if not as_image or not self.text2image_enabled:
            text = self._format_multi_player_list_text(cards)
            return RenderResult(text, is_image=False)

        try:
            await self._ensure_assets()
            single = await self._render_multi_player_list_image(cards)
            return RenderResult(single, is_image=True)
        except Exception as exc:
            logger.warning(f"[Renderer] 多服务器玩家列表合图失败，回退文本: {exc}")
            text = self._format_multi_player_list_text(cards)
            return RenderResult(text, is_image=False)

    async def render_multi_player_detail(
        self,
        cards: list[tuple[str, "PlayerDetail"]],
        as_image: bool = True,
    ) -> RenderResult:
        if not cards:
            return RenderResult(" 没有可渲染的玩家详情", is_image=False)

        if not as_image or not self.text2image_enabled:
            text = "\n\n".join(
                self._format_player_detail_text(player, server_tag=tag)
                for tag, player in cards
            )
            return RenderResult(text, is_image=False)

        try:
            await self._ensure_assets()
            ims: list[Image.Image] = []
            for tag, player in cards:
                single = await self._render_player_detail_image(player, server_tag=tag)
                ims.append(Image.open(single).convert("RGB"))
            out = await self._merge_images_vertical(ims, gap=8, pad=10)
            return RenderResult(out, is_image=True)
        except Exception as exc:
            logger.warning(f"[Renderer] 多服务器玩家详情合图失败，回退文本: {exc}")
            text = "\n\n".join(
                self._format_player_detail_text(player, server_tag=tag)
                for tag, player in cards
            )
            return RenderResult(text, is_image=False)

    async def render_server_status(
        self,
        server_info: "ServerInfo",
        server_status: "ServerStatus",
        as_image: bool = True,
    ) -> RenderResult:
        return await self.render_multi_server_status(
            [("", server_info, server_status)], as_image=as_image
        )

    async def render_player_list(
        self,
        players: list["PlayerInfo"],
        total: int,
        server_name: str = "",
        as_image: bool = True,
    ) -> RenderResult:
        return await self.render_multi_player_list(
            [("", players, total, server_name)], as_image=as_image
        )

    async def render_player_detail(
        self,
        player: "PlayerDetail",
        server_tag: str = "",
        as_image: bool = True,
    ) -> RenderResult:
        if as_image and self.text2image_enabled:
            try:
                await self._ensure_assets()
                img = await self._render_player_detail_image(
                    player, server_tag=server_tag
                )
                return RenderResult(img, is_image=True)
            except Exception as exc:
                logger.warning(f"[Renderer] 玩家详情图片渲染失败，回退文本: {exc}")
        return RenderResult(
            self._format_player_detail_text(player, server_tag=server_tag),
            is_image=False,
        )

    def _format_multi_player_list_text(
        self,
        cards: list[tuple[str, list["PlayerInfo"], int, str]],
    ) -> str:
        flattened = self._flatten_player_cards(cards)
        total = sum((t if t > 0 else len(ps)) for _, ps, t, _ in flattened)
        lines = [f"👥 在线玩家总览 | {total}人", "────────────────────────────"]
        for sid, players, t, server_name in flattened:
            count = t if t > 0 else len(players)
            display_name = server_name or sid
            lines.append("")
            lines.append(f"📌 服务器: {display_name} ({count}人)")

            if not players:
                lines.append("  当前没有玩家在线")
                continue

            for p in players:
                lines.append(
                    f"  - {p.name} | {self._mode_cn(p.game_mode)} | 世界:{p.world or '未知'} | 延迟:{p.ping}ms"
                )
        return "\n".join(lines)

    def _format_server_status_text(
        self,
        info: "ServerInfo",
        status: "ServerStatus",
        server_tag: str = "",
    ) -> str:
        online = info.online_count or status.online_players
        mx = info.max_players or status.max_players
        uptime = info.uptime_formatted or status.uptime_formatted or "未知"

        title = f"🖥️ 服务器状态 | {info.name}"
        if server_tag:
            title += f" | ID: {server_tag}"

        lines = [
            title,
            "────────────────────────────",
            f"平台: {info.platform} {info.minecraft_version}",
            f"在线: {online}/{mx}",
            f"运行: {uptime}",
            "",
            "📊 性能",
            f"内存: {status.memory_used}MB/{status.memory_max}MB ({status.memory_usage_percent:.1f}%)",
        ]

        if not status.is_proxy:
            lines.append(
                f"TPS: {status.tps_1m:.1f} / {status.tps_5m:.1f} / {status.tps_15m:.1f}"
            )

        if info.is_proxy and info.aggregate_online > 0:
            lines.append(f"总在线: {info.aggregate_online}/{info.aggregate_max}")

        if status.worlds:
            lines.extend(["", "🌍 世界列表"])
            for w in status.worlds:
                lines.append(
                    f"- {w.get('name', 'world')}: 玩家 {w.get('players', 0)}, 实体 {w.get('entities', 0)}, 区块 {w.get('loadedChunks', 0)}"
                )

        # 代理服后端状态不在此处输出，避免与同层级卡片重复。

        return "\n".join(lines)

    def _format_player_detail_text(
        self, player: "PlayerDetail", server_tag: str = ""
    ) -> str:
        detail_server_name = self._get_effective_server_name(player, server_tag)
        lines = [
            f"👤 玩家详情 | {player.name}",
            "────────────────────────────",
            f"服务器: {detail_server_name or '未提供'}",
            f"UUID: {player.uuid}",
            "",
            "【基础信息】",
            f"世界: {player.world or '未知'}",
            f"模式: {self._mode_cn(player.game_mode)}",
            f"延迟: {player.ping}ms",
            "",
            "【状态面板】",
            f"生命值: {player.health:.1f}/{player.max_health:.1f}",
            f"饥饿值: {player.food_level}/20",
            f"等级: {player.level} ({player.exp * 100:.1f}%)",
        ]
        if player.location:
            lines.append(
                f"位置: X={player.location.get('x', 0):.1f}, Y={player.location.get('y', 0):.1f}, Z={player.location.get('z', 0):.1f}"
            )
        lines.append(f"在线时长: {player.online_time_formatted or '未知'}")
        if player.is_op:
            lines.insert(2, "权限: 管理员")
        return "\n".join(lines)
