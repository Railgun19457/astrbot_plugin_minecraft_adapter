"""Encrypted WebSocket client for AstrBotAdapter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import struct
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

log = logging.getLogger(__name__)
_DOMAIN = b"astrbot-minecraft/v1/"
MAX_FRAME = 65_536
MAX_PAYLOAD = 47_000
_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class LinkError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def node_id(value: str) -> str:
    if not isinstance(value, str) or not _NODE_ID.fullmatch(value):
        raise ValueError(
            "Expected a node ID with 1–64 letters, digits, underscores or hyphens"
        )
    return value


def endpoint_url(value: str) -> str:
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ValueError("Expected a WebSocket URL without whitespace")
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or not parsed.path.startswith("/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port == 0
    ):
        raise ValueError("Expected a WebSocket URL with a path and no URL credentials")
    if parsed.scheme == "wss":
        return value
    if parsed.scheme == "ws" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return value
    raise ValueError("Remote endpoints must use wss://")


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url value")
    result = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
    if encode(result) != value:
        raise ValueError("Invalid base64url representation")
    return result


def transcript(purpose: str, *fields: str) -> bytes:
    message = bytearray(_DOMAIN + purpose.encode() + b"\0")
    for field in fields:
        binary = field.encode("utf-8")
        message.extend(struct.pack(">I", len(binary)))
        message.extend(binary)
    return bytes(message)


def public_bytes(key: Ed25519PublicKey | X25519PublicKey) -> str:
    return encode(
        key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


def object_frame(text: str) -> dict:
    result = json.loads(text)
    if not isinstance(result, dict):
        raise LinkError("protocol", "Protocol frames must be JSON objects")
    return result


class SealedChannel:
    def __init__(self, send_key: bytes, receive_key: bytes):
        self._outbound = AESGCM(send_key)
        self._inbound = AESGCM(receive_key)
        self._send_seq = 0
        self._receive_seq = 0

    @classmethod
    def client(
        cls,
        ephemeral: X25519PrivateKey,
        server_key: X25519PublicKey,
        proof: bytes,
        pairing: bytes,
    ) -> SealedChannel:
        shared = ephemeral.exchange(server_key) + pairing
        salt = hashlib.sha256(proof).digest()
        keys = [
            HKDF(hashes.SHA256(), 32, salt, _DOMAIN + role).derive(shared)
            for role in (b"client", b"server")
        ]
        return cls(keys[0], keys[1])

    def seal(self, message: dict) -> str:
        payload = json.dumps(
            message, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(payload) > MAX_PAYLOAD:
            raise ValueError("Request exceeds the message limit")
        seq = self._send_seq
        aad = struct.pack(">Q", seq)
        ciphertext = self._outbound.encrypt(b"\0" * 4 + aad, payload, aad)
        self._send_seq += 1
        return json.dumps(
            {"seq": seq, "ciphertext": encode(ciphertext)}, separators=(",", ":")
        )

    def open(self, frame: dict) -> dict:
        seq = frame.get("seq")
        if type(seq) is not int or seq != self._receive_seq:
            raise ValueError("Unexpected message sequence")
        aad = struct.pack(">Q", seq)
        plaintext = self._inbound.decrypt(
            b"\0" * 4 + aad, decode(frame.get("ciphertext")), aad
        )
        if len(plaintext) > MAX_PAYLOAD:
            raise ValueError("Payload exceeds the message limit")
        result = object_frame(plaintext.decode("utf-8"))
        self._receive_seq += 1
        return result


class IdentityStore:
    def __init__(self, directory: Path):
        self._file = directory / "link-state.json"
        directory.mkdir(parents=True, exist_ok=True)
        if self._file.exists():
            self._state = object_frame(self._file.read_text(encoding="utf-8"))
            self.key = Ed25519PrivateKey.from_private_bytes(
                decode(self._state["private_key"])
            )
            if not isinstance(self._state["trusted"], dict):
                raise ValueError("Invalid identity store")
        else:
            self.key = Ed25519PrivateKey.generate()
            self._state = {
                "private_key": encode(self.key.private_bytes_raw()),
                "trusted": {},
            }
            self._write(self._state)

    def trusted(self, endpoint: str, role: str) -> str | None:
        return self._state["trusted"].get(f"{role}/{endpoint}")

    def remember(self, endpoint: str, role: str, key: str) -> None:
        current = self.trusted(endpoint, role)
        if current is not None and current != key:
            raise LinkError("untrusted", "The saved server identity does not match")
        next_state = {
            "private_key": self._state["private_key"],
            "trusted": {**self._state["trusted"], f"{role}/{endpoint}": key},
        }
        self._write(next_state)
        self._state = next_state

    def _write(self, values: dict) -> None:
        descriptor, filename = tempfile.mkstemp(prefix=".state-", dir=self._file.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(values, stream, indent=2)
            temporary.replace(self._file)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Endpoint:
    id: str
    role: str
    url: str
    pairing_id: str = ""
    pairing_code: str = ""


EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class Link:
    def __init__(
        self,
        endpoint: Endpoint,
        client_name: str,
        identity: IdentityStore,
        event_handler: EventHandler,
        on_paired: Callable[[], None] | None = None,
    ):
        self.endpoint = endpoint
        self.client_name = node_id(client_name)
        self.store = identity
        self._event_handler = event_handler
        self._on_paired = on_paired
        self._socket: aiohttp.ClientWebSocketResponse | None = None
        self._channel: SealedChannel | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self.capabilities: frozenset[str] = frozenset()
        self.ready = asyncio.Event()

    async def serve(self) -> None:
        delay = 1
        renew = False
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(sock_connect=8)
        ) as http:
            while True:
                try:
                    await self.connect_once(http, renew=renew)
                    renew = False
                    delay = 1
                except LinkError as error:
                    if (
                        error.code == "authentication"
                        and self.endpoint.pairing_id
                        and self.store.trusted(self.endpoint.id, self.endpoint.role)
                    ):
                        renew = not renew
                    log.warning("Minecraft %s: %s", self.endpoint.id, error.code)
                except (
                    aiohttp.ClientError,
                    TimeoutError,
                    OSError,
                    ValueError,
                    InvalidSignature,
                    InvalidTag,
                ):
                    log.warning(
                        "Minecraft %s: connection or authentication failed",
                        self.endpoint.id,
                    )
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)

    async def connect_once(
        self, http: aiohttp.ClientSession, *, renew: bool = False
    ) -> None:
        async with http.ws_connect(
            self.endpoint.url, heartbeat=15, max_msg_size=MAX_FRAME
        ) as socket:
            async with asyncio.timeout(12):
                channel, capabilities = await self._authenticate(socket, renew=renew)
            events: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=64)
            handler = asyncio.create_task(self._process_events(events))
            self._socket, self._channel, self.capabilities = (
                socket,
                channel,
                capabilities,
            )
            self.ready.set()
            try:
                async for incoming in socket:
                    if incoming.type != aiohttp.WSMsgType.TEXT:
                        raise LinkError("protocol", "Expected a text frame")
                    frame = channel.open(object_frame(incoming.data))
                    if frame.get("type") == "response":
                        request_id = frame.get("id")
                        if not isinstance(request_id, str):
                            raise ValueError("Missing response identifier")
                        future = self._pending.get(request_id)
                        if future is not None and not future.done():
                            future.set_result(frame)
                    elif frame.get("type") == "event":
                        operation, data = frame.get("op"), frame.get("data")
                        if not isinstance(operation, str) or not isinstance(data, dict):
                            raise ValueError("Invalid event")
                        if not events.full():
                            events.put_nowait((operation, data))
                        else:
                            log.warning(
                                "Minecraft %s: event queue is full", self.endpoint.id
                            )
                    else:
                        raise LinkError("protocol", "Unexpected message type")
            finally:
                self.ready.clear()
                self._socket, self._channel, self.capabilities = None, None, frozenset()
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(
                            LinkError("disconnected", "Connection lost")
                        )
                self._pending.clear()
                handler.cancel()
                with suppress(asyncio.CancelledError):
                    await handler

    async def _process_events(self, events: asyncio.Queue[tuple[str, dict]]) -> None:
        while True:
            operation, data = await events.get()
            try:
                await self._event_handler(operation, data)
            except Exception:
                log.exception("Minecraft %s: event handling failed", self.endpoint.id)

    async def request(self, operation: str, data: dict | None = None) -> dict:
        if operation not in self.capabilities:
            if not self.ready.is_set():
                raise LinkError("offline", "服务器未连接")
            raise LinkError("unsupported", "服务器不支持该操作")
        if len(self._pending) >= 16:
            raise LinkError("busy", "请求过多，请稍后再试")
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(8):
                async with self._send_lock:
                    if (
                        self._socket is None
                        or self._channel is None
                        or self._socket.closed
                    ):
                        raise LinkError("offline", "服务器未连接")
                    await self._socket.send_str(
                        self._channel.seal(
                            {
                                "type": "request",
                                "id": request_id,
                                "op": operation,
                                "data": data or {},
                            }
                        )
                    )
                response = await future
                if response.get("ok") is not True:
                    raise LinkError(
                        str(response.get("code", "request_failed")),
                        str(response.get("message", "请求失败")),
                    )
                if not isinstance(response.get("data"), dict):
                    raise LinkError("protocol", "响应缺少有效的数据")
                return response["data"]
        except TimeoutError as error:
            detail = (
                "执行结果未知，请先人工核对"
                if operation == "command.run"
                else "请求超时"
            )
            raise LinkError("timeout", detail) from error
        except (OSError, aiohttp.ClientError) as error:
            raise LinkError("disconnected", "连接中断") from error
        finally:
            self._pending.pop(request_id, None)
            if future.done() and not future.cancelled():
                future.exception()
            elif not future.done():
                future.cancel()

    @staticmethod
    async def _receive(socket: aiohttp.ClientWebSocketResponse) -> dict:
        incoming = await socket.receive()
        if incoming.type != aiohttp.WSMsgType.TEXT:
            raise LinkError("authentication", "握手未完成")
        return object_frame(incoming.data)

    async def _authenticate(
        self, socket: aiohttp.ClientWebSocketResponse, *, renew: bool
    ) -> tuple[SealedChannel, frozenset[str]]:
        challenge = await self._receive(socket)
        fields = tuple(
            challenge.get(key) for key in ("node", "role", "key", "ephemeral", "nonce")
        )
        if (
            challenge.get("type") != "auth.challenge"
            or challenge.get("version") != 1
            or fields[:2] != (self.endpoint.id, self.endpoint.role)
            or any(not isinstance(value, str) or len(value) > 128 for value in fields)
        ):
            raise LinkError("untrusted", "服务端 ID、角色或版本不匹配")
        _, _, server_key, server_ephemeral, server_nonce = fields
        pinned = self.store.trusted(self.endpoint.id, self.endpoint.role)
        if pinned is not None and pinned != server_key:
            raise LinkError("untrusted", "服务端密钥与已保存的身份不符")
        wants_pairing = not pinned or renew
        pairing_id = self.endpoint.pairing_id if wants_pairing else ""
        code = decode(self.endpoint.pairing_code) if pairing_id else b""
        if wants_pairing and (len(code) != 24 or len(decode(pairing_id)) != 16):
            raise LinkError("pairing_required", "请填写有效的一次性配对信息")

        peer_key = serialization.load_der_public_key(decode(server_key))
        peer_ephemeral = serialization.load_der_public_key(decode(server_ephemeral))
        if not isinstance(peer_key, Ed25519PublicKey) or not isinstance(
            peer_ephemeral, X25519PublicKey
        ):
            raise LinkError("authentication", "Invalid public key types")
        if len(decode(server_nonce)) != 32:
            raise ValueError("Invalid server challenge nonce")
        peer_key.verify(
            decode(challenge.get("signature")), transcript("challenge", *fields)
        )
        ephemeral = X25519PrivateKey.generate()
        public_key = public_bytes(self.store.key.public_key())
        ephemeral_key = public_bytes(ephemeral.public_key())
        nonce = encode(secrets.token_bytes(32))
        proof = transcript(
            "authenticate",
            *fields,
            self.client_name,
            "astrbot",
            public_key,
            ephemeral_key,
            nonce,
            pairing_id,
        )
        payload = {
            "type": "auth.prove",
            "node": self.client_name,
            "key": public_key,
            "ephemeral": ephemeral_key,
            "nonce": nonce,
            "pairing_id": pairing_id,
            "signature": encode(self.store.key.sign(proof)),
        }
        if code:
            payload["pairing_proof"] = encode(hmac.digest(code, proof, "sha256"))
        channel = SealedChannel.client(ephemeral, peer_ephemeral, proof, code)
        await socket.send_json(payload)
        if channel.open(await self._receive(socket)).get("type") != "auth.result":
            raise ValueError("Invalid server key confirmation")
        if not pinned:
            self.store.remember(self.endpoint.id, self.endpoint.role, server_key)
        await socket.send_str(channel.seal({"type": "auth.confirm"}))
        ready = channel.open(await self._receive(socket))
        capabilities = ready.get("capabilities")
        if (
            ready.get("type") != "auth.ready"
            or not isinstance(capabilities, list)
            or any(not isinstance(item, str) for item in capabilities)
        ):
            raise ValueError("Invalid authentication result")
        if self.endpoint.pairing_id and self._on_paired:
            self._on_paired()
        return channel, frozenset(capabilities)
