"""Byte-exact Version 1 Ed25519 authentication transcript construction."""

import base64
import hashlib
from collections.abc import Collection
from dataclasses import dataclass

from ..protocol.constants import Role


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _safe(value: object) -> str:
    return "" if value is None else str(value).replace("\n", " ")


def capabilities_sha256(capabilities: Collection[str] | None) -> str:
    """Return Java's sorted newline-joined capability hash."""
    joined = "\n".join(sorted(capabilities or ()))
    return _base64url(hashlib.sha256(joined.encode("utf-8")).digest())


def connection_role(client_role: Role, server_role: Role) -> str:
    """Return the Java-compatible connection role description."""
    pairs = {
        (Role.ASTRBOT, Role.STANDALONE): "astrbot-to-standalone",
        (Role.ASTRBOT, Role.VELOCITY): "astrbot-to-velocity",
        (Role.BACKEND, Role.VELOCITY): "backend-to-velocity",
    }
    return pairs.get(
        (client_role, server_role), f"{client_role.value}-to-{server_role.value}"
    )


@dataclass(frozen=True)
class CommonFields:
    """Header fields included in both authentication transcript directions."""

    selected_v: int
    client_min_v: int
    client_max_v: int
    connection_role: str
    client_role: Role
    client_id: str
    server_role: Role
    server_id: str
    client_key_id: str
    server_key_id: str


@dataclass(frozen=True)
class ClientProof(CommonFields):
    """Fields signed by the AstrBot client in ``auth.prove_client``."""

    client_public_key_sha256: str
    expected_server_public_key_sha256: str
    client_nonce: str
    server_nonce: str
    challenge_id: str
    challenge_expires_at: int
    hello_id: str
    challenge_frame_id: str
    client_hello_ts: int
    auth_ts: int
    client_capabilities_sha256: str
    server_capabilities_sha256: str


@dataclass(frozen=True)
class ServerProof(CommonFields):
    """Fields signed by the remote Java server in ``auth.result``."""

    client_public_key_sha256: str
    server_public_key_sha256: str
    client_nonce: str
    server_nonce: str
    challenge_id: str
    challenge_expires_at: int
    hello_id: str
    challenge_frame_id: str
    auth_frame_id: str
    auth_result_ts: int
    client_signature_sha256: str
    client_capabilities_sha256: str
    server_capabilities_sha256: str


def _header(direction: str, fields: CommonFields) -> list[tuple[str, object]]:
    return [
        ("direction", direction),
        ("selected_v", fields.selected_v),
        ("client_min_v", fields.client_min_v),
        ("client_max_v", fields.client_max_v),
        ("connection_role", fields.connection_role),
        ("client_role", fields.client_role.value),
        ("client_id", fields.client_id),
        ("server_role", fields.server_role.value),
        ("server_id", fields.server_id),
        ("client_key_id", fields.client_key_id),
        ("server_key_id", fields.server_key_id),
    ]


def _render(lines: list[tuple[str, object]]) -> str:
    return "ASTRBOT-MC-AUTH-V1\n" + "".join(
        f"{key}={_safe(value)}\n" for key, value in lines
    )


def client_proof(fields: ClientProof) -> str:
    """Build the exact UTF-8 text signed by an AstrBot client."""
    lines = _header("client-prove", fields) + [
        ("client_public_key_sha256", fields.client_public_key_sha256),
        ("expected_server_public_key_sha256", fields.expected_server_public_key_sha256),
        ("client_nonce", fields.client_nonce),
        ("server_nonce", fields.server_nonce),
        ("challenge_id", fields.challenge_id),
        ("challenge_expires_at", fields.challenge_expires_at),
        ("hello_id", fields.hello_id),
        ("challenge_frame_id", fields.challenge_frame_id),
        ("client_hello_ts", fields.client_hello_ts),
        ("auth_ts", fields.auth_ts),
        ("client_capabilities_sha256", fields.client_capabilities_sha256),
        ("server_capabilities_sha256", fields.server_capabilities_sha256),
    ]
    return _render(lines)


def server_proof(fields: ServerProof) -> str:
    """Build the exact UTF-8 text verified from a Java server response."""
    lines = _header("server-prove", fields) + [
        ("client_public_key_sha256", fields.client_public_key_sha256),
        ("server_public_key_sha256", fields.server_public_key_sha256),
        ("client_nonce", fields.client_nonce),
        ("server_nonce", fields.server_nonce),
        ("challenge_id", fields.challenge_id),
        ("challenge_expires_at", fields.challenge_expires_at),
        ("hello_id", fields.hello_id),
        ("challenge_frame_id", fields.challenge_frame_id),
        ("auth_frame_id", fields.auth_frame_id),
        ("auth_result_ts", fields.auth_result_ts),
        ("client_signature_sha256", fields.client_signature_sha256),
        ("client_capabilities_sha256", fields.client_capabilities_sha256),
        ("server_capabilities_sha256", fields.server_capabilities_sha256),
    ]
    return _render(lines)
