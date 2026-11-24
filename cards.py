"""Render Minecraft query results with the bundled Typst template."""

import asyncio
import base64
import binascii
import json
import re
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from time import monotonic

import aiohttp
from astrbot.api import logger
from PIL import Image

_MAX_SKIN_BYTES = 128 * 1024
_TEXTURE_HASH = re.compile(r"[0-9a-f]{32,64}")


def _card_png(raw: bytes, *, skin: bool = False) -> str | None:
    """Decode and normalize a Minecraft PNG before handing it to Typst."""
    try:
        with Image.open(BytesIO(raw), formats=["PNG"]) as source:
            if skin:
                width, height = source.size
                if width not in (64, 128, 256, 512) or height not in (
                    width,
                    width // 2,
                ):
                    return None
                scale = width // 64
                pixels = source.convert("RGBA")
                face = pixels.crop((8 * scale, 8 * scale, 16 * scale, 16 * scale))
                face.alpha_composite(
                    pixels.crop((40 * scale, 8 * scale, 48 * scale, 16 * scale))
                )
                normalized = face.resize((64, 64), Image.Resampling.NEAREST)
            else:
                if source.size != (64, 64):
                    return None
                normalized = source.convert("RGBA")
            png = BytesIO()
            normalized.save(png, format="PNG")
            return png.getvalue().hex()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None


def server_icon(value: object) -> str | None:
    prefix = "data:image/png;base64,"
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 32768:
        return None
    try:
        return _card_png(base64.b64decode(value[len(prefix) :], validate=True))
    except binascii.Error:
        return None


class AvatarCache:
    """Retrieve immutable skins from the official texture host."""

    def __init__(self):
        self._http: aiohttp.ClientSession | None = None
        self._entries: OrderedDict[str, tuple[str | None, float]] = OrderedDict()

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def avatars(self, skins: list[object]) -> list[str | None]:
        hashes = [
            skin if isinstance(skin, str) and _TEXTURE_HASH.fullmatch(skin) else None
            for skin in skins
        ]
        requested = list(dict.fromkeys(skin for skin in hashes if skin is not None))
        results = dict(
            zip(
                requested,
                await asyncio.gather(*(self._get(skin) for skin in requested)),
                strict=True,
            )
        )
        return [results.get(skin) for skin in hashes]

    async def _get(self, skin: str) -> str | None:
        cached = self._entries.get(skin)
        if cached and cached[1] > monotonic():
            self._entries.move_to_end(skin)
            return cached[0]
        if self._http is None:
            self._http = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=8),
                timeout=aiohttp.ClientTimeout(total=4),
            )
        avatar = await self._download(self._http, skin)
        self._entries[skin] = (avatar, monotonic() + (3600 if avatar else 30))
        self._entries.move_to_end(skin)
        if len(self._entries) > 256:
            self._entries.popitem(last=False)
        return avatar

    @staticmethod
    async def _download(http: aiohttp.ClientSession, skin: str) -> str | None:
        url = f"https://textures.minecraft.net/texture/{skin}"
        try:
            async with http.get(url, allow_redirects=False) as response:
                if response.status != 200:
                    return None
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > _MAX_SKIN_BYTES:
                        return None
                return await asyncio.to_thread(_card_png, bytes(raw), skin=True)
        except (aiohttp.ClientError, TimeoutError):
            return None


async def render_card(model: dict) -> bytes | None:
    try:
        import typst

        rendered = await asyncio.to_thread(
            typst.compile,
            str(Path(__file__).with_name("card.typ")),
            format="png",
            ppi=144,
            sys_inputs={"card": json.dumps(model, ensure_ascii=False)},
        )
        return rendered if isinstance(rendered, bytes) else None
    except (ImportError, OSError, RuntimeError, ValueError) as error:
        logger.warning(f"Minecraft Typst card could not be rendered: {error}")
        return None
