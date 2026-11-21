"""Strict client-side state machine for SRP pairing and Ed25519 reauthentication."""

import hashlib
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ..config import ServerConfig
from ..protocol.constants import (
    CLIENT_CAPABILITIES,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    SRP6A_SUITE,
    Capability,
    FrameType,
    Op,
    Role,
)
from ..protocol.frame import Frame, NodeRef, Route, Status
from .auth_transcript import (
    ClientProof,
    ServerProof,
    capabilities_sha256,
    client_proof,
    connection_role,
    server_proof,
)
from .identity_store import (
    IdentityStore,
    IdentityStoreError,
    LocalIdentity,
    TrustRecord,
)
from .srp6a import (
    Srp6aClientSession,
    Srp6aError,
    base64url_decode,
    base64url_encode,
    create_client_session,
    key_binding_confirmation,
    key_binding_matches,
)


class AuthenticationError(RuntimeError):
    """Raised when a remote frame cannot authenticate the configured endpoint."""


class AuthState(str, Enum):
    """The accepted authentication states for one physical WebSocket connection."""

    IDLE = "idle"
    PAIR_INIT = "pair_init"
    PAIR_PROVED = "pair_proved"
    AUTH_HELLO_SENT = "auth_hello_sent"
    AUTH_CHALLENGED = "auth_challenged"
    READY = "ready"
    FAILED = "failed"


@dataclass
class _PendingAuth:
    client_nonce: str
    hello_id: str
    hello_ts: int
    trace_id: str
    server_key_id: str = ""
    server_nonce: str = ""
    challenge_id: str = ""
    challenge_expires_at: int = 0
    challenge_frame_id: str = ""
    selected_v: int = 0
    server_capabilities: tuple[str, ...] = ()
    auth_frame_id: str = ""
    auth_ts: int = 0
    client_signature_sha256: str = ""


class AuthSession:
    """Authenticate one endpoint, allowing only the expected frame at each step."""

    def __init__(
        self,
        config: ServerConfig,
        identity: LocalIdentity,
        identity_store: IdentityStore,
    ):
        self.config = config
        self.identity = identity
        self.identity_store = identity_store
        self.state = AuthState.IDLE
        self.session_id = ""
        self.server_time: int | None = None
        self.server_capabilities: tuple[str, ...] = ()
        self._trust: TrustRecord | None = None
        self._srp: Srp6aClientSession | None = None
        self._pair_trace_id = ""
        self._pair_challenge: Frame | None = None
        self._pending_auth: _PendingAuth | None = None

    @property
    def ready(self) -> bool:
        """Whether this physical connection completed mutual authentication."""
        return self.state is AuthState.READY

    @property
    def trust(self) -> TrustRecord | None:
        """Return the verified trust record for a paired endpoint."""
        return self._trust

    def begin(self, seq: int) -> Frame:
        """Start pairing or reauthentication based on endpoint-local trust.

        Args:
            seq: The next monotonic connection-local outgoing sequence number.

        Returns:
            The initial ``pair.init`` or ``auth.hello`` frame.

        Raises:
            AuthenticationError: If local identity or trust data cannot be used.
        """
        if self.state is not AuthState.IDLE:
            raise AuthenticationError("authentication is already in progress")
        try:
            self._trust = self.identity_store.load_trust(
                self.config.url,
                self.config.expected_role.value,
                self.config.server_id,
            )
        except IdentityStoreError as exc:
            self._fail("local trust record cannot be used")
            raise AuthenticationError("local trust record cannot be used") from exc
        if self._trust is not None:
            return self._begin_reauthentication(seq)
        return self._begin_pairing(seq)

    def receive(self, frame: Frame, seq: int) -> Frame | None:
        """Validate one authentication frame and optionally construct the next frame.

        Args:
            frame: A strict envelope-validated incoming frame.
            seq: The next monotonic connection-local outgoing sequence number.

        Returns:
            The next authentication frame, or ``None`` once authentication is ready.

        Raises:
            AuthenticationError: If the frame is unexpected or proof verification fails.
        """
        try:
            if self.state is AuthState.PAIR_INIT:
                return self._receive_pair_challenge(frame, seq)
            if self.state is AuthState.PAIR_PROVED:
                self._receive_pair_complete(frame)
                return None
            if self.state is AuthState.AUTH_HELLO_SENT:
                return self._receive_auth_challenge(frame, seq)
            if self.state is AuthState.AUTH_CHALLENGED:
                self._receive_auth_result(frame)
                return None
            raise AuthenticationError(
                f"unexpected authentication frame in {self.state}"
            )
        except (
            AuthenticationError,
            Srp6aError,
            IdentityStoreError,
            InvalidSignature,
        ) as exc:
            self._fail("authentication failed")
            if isinstance(exc, AuthenticationError):
                raise
            raise AuthenticationError("authentication failed") from exc

    def close(self) -> None:
        """Erase all transient credentials retained for this connection."""
        self._srp = None
        self._pair_challenge = None
        self._pending_auth = None
        self._pair_trace_id = ""
        if self.state is not AuthState.FAILED:
            self.state = AuthState.IDLE

    def _begin_pairing(self, seq: int) -> Frame:
        if not self.config.pairing_id or not self.config.pairing_code:
            raise AuthenticationError(
                "endpoint is unpaired; pairing_id and pairing_code are required"
            )
        frame_id = self._id()
        trace_id = self._id()
        self._pair_trace_id = trace_id
        self.state = AuthState.PAIR_INIT
        return Frame(
            v=PROTOCOL_VERSION,
            id=frame_id,
            type=FrameType.HELLO,
            op=Op.PAIR_INIT.value,
            ts=self._now(),
            seq=seq,
            route=self._route(trace_id),
            payload={
                "protocol": self._protocol_range(),
                "key_id": self.identity.key_id,
                "public_key": self.identity.public_key_open_ssh,
                "pairing_id": self.config.pairing_id,
                "capabilities": list(CLIENT_CAPABILITIES),
            },
        )

    def _receive_pair_challenge(self, frame: Frame, seq: int) -> Frame:
        self._assert_frame(
            frame, FrameType.CHALLENGE, Op.PAIR_CHALLENGE, self._pair_trace_id
        )
        payload = frame.payload
        if (
            payload.get("pairing_id") != self.config.pairing_id
            or payload.get("suite") != SRP6A_SUITE
            or payload.get("selected_v") != PROTOCOL_VERSION
        ):
            raise AuthenticationError(
                "pairing challenge does not match the configured suite"
            )
        self._assert_server_route(frame)
        self._assert_not_expired(payload.get("expires_at"))
        capabilities = self._capabilities(payload.get("capabilities"))
        if Capability.AUTH_SRP6A_PAIRING.value not in capabilities:
            raise AuthenticationError("server does not advertise SRP pairing")
        self._string(payload, "server_key_id")
        server_public_key = self._string(payload, "server_public_key")
        self._parse_public_key(server_public_key)
        self._srp = create_client_session(
            self.config.pairing_id,
            self.config.pairing_code,
            self._string(payload, "salt"),
            self._string(payload, "server_public_value"),
        )
        self._pair_challenge = frame
        self.state = AuthState.PAIR_PROVED
        return Frame(
            v=PROTOCOL_VERSION,
            id=self._id(),
            type=FrameType.AUTH,
            op=Op.PAIR_PROVE.value,
            ts=self._now(),
            seq=seq,
            route=self._route(self._pair_trace_id),
            payload={
                "key_id": self.identity.key_id,
                "public_key": self.identity.public_key_open_ssh,
                "client_public_value": self._srp.client_public_value,
                "client_proof": self._srp.client_proof,
            },
        )

    def _receive_pair_complete(self, frame: Frame) -> None:
        if self._srp is None or self._pair_challenge is None:
            raise AuthenticationError("pairing state is incomplete")
        self._assert_frame(
            frame, FrameType.AUTH_RESULT, Op.PAIR_COMPLETE, self._pair_trace_id
        )
        self._assert_server_route(frame)
        self._assert_success(frame.status, frame.payload.get("status"))
        payload = frame.payload
        if (
            payload.get("ok") is not True
            or payload.get("trust_mode") != "srp_paired_key"
        ):
            raise AuthenticationError("pairing completion was rejected")
        server_key_id = self._string(payload, "key_id")
        server_public_key = self._string(payload, "public_key")
        self._parse_public_key(server_public_key)
        if not self._srp.verify_server_proof(self._string(payload, "server_proof")):
            raise AuthenticationError("server SRP proof is invalid")
        challenge_payload = self._pair_challenge.payload
        if server_key_id != challenge_payload.get(
            "server_key_id"
        ) or server_public_key != challenge_payload.get("server_public_key"):
            raise AuthenticationError(
                "server pairing key changed during authentication"
            )
        expected_binding = key_binding_confirmation(
            self._srp.session_key,
            self.config.pairing_id,
            Role.ASTRBOT.value,
            self.identity.node_id,
            self.config.expected_role.value,
            self.config.server_id,
            self.identity.key_id,
            self.identity.public_key_open_ssh,
            server_key_id,
            server_public_key,
            self._string(challenge_payload, "salt"),
            self._srp.client_public_value,
            self._string(challenge_payload, "server_public_value"),
            self._srp.client_proof,
            self._string(payload, "server_proof"),
        )
        if not key_binding_matches(
            expected_binding, self._string(payload, "key_binding")
        ):
            raise AuthenticationError("SRP key binding confirmation is invalid")
        capabilities = self._capabilities(payload.get("capabilities"))
        if Capability.AUTH_SRP6A_PAIRING.value not in capabilities:
            raise AuthenticationError("pairing completion capability set is invalid")
        self._trust = self.identity_store.save_trust(
            self.config.url,
            self.config.expected_role.value,
            self.config.server_id,
            server_key_id,
            server_public_key,
        )
        self._finish(payload, capabilities)

    def _begin_reauthentication(self, seq: int) -> Frame:
        frame_id = self._id()
        trace_id = self._id()
        now = self._now()
        self._pending_auth = _PendingAuth(
            client_nonce=base64url_encode(uuid.uuid4().bytes + uuid.uuid4().bytes),
            hello_id=frame_id,
            hello_ts=now,
            trace_id=trace_id,
        )
        self.state = AuthState.AUTH_HELLO_SENT
        return Frame(
            v=PROTOCOL_VERSION,
            id=frame_id,
            type=FrameType.HELLO,
            op=Op.AUTH_HELLO.value,
            ts=now,
            seq=seq,
            route=self._route(trace_id),
            payload={
                "protocol": self._protocol_range(),
                "key_id": self.identity.key_id,
                "client_nonce": self._pending_auth.client_nonce,
                "capabilities": list(CLIENT_CAPABILITIES),
            },
        )

    def _receive_auth_challenge(self, frame: Frame, seq: int) -> Frame:
        if self._trust is None or self._pending_auth is None:
            raise AuthenticationError("trusted authentication state is incomplete")
        pending = self._pending_auth
        self._assert_frame(
            frame, FrameType.CHALLENGE, Op.AUTH_CHALLENGE, pending.trace_id
        )
        self._assert_server_route(frame)
        payload = frame.payload
        self._assert_not_expired(payload.get("expires_at"))
        if payload.get("selected_v") != PROTOCOL_VERSION:
            raise AuthenticationError("server selected an unsupported protocol version")
        server_key_id = self._string(payload, "server_key_id")
        if server_key_id != self._trust.key_id:
            raise AuthenticationError("server key ID differs from trust record")
        server_capabilities = self._capabilities(payload.get("capabilities"))
        if Capability.AUTH_ED25519.value not in server_capabilities:
            raise AuthenticationError(
                "server does not advertise Ed25519 authentication"
            )
        auth_frame_id = self._id()
        auth_ts = self._now()
        transcript = client_proof(
            ClientProof(
                selected_v=PROTOCOL_VERSION,
                client_min_v=PROTOCOL_VERSION,
                client_max_v=PROTOCOL_VERSION,
                connection_role=connection_role(
                    Role.ASTRBOT, self.config.expected_role
                ),
                client_role=Role.ASTRBOT,
                client_id=self.identity.node_id,
                server_role=self.config.expected_role,
                server_id=self.config.server_id,
                client_key_id=self.identity.key_id,
                server_key_id=server_key_id,
                client_public_key_sha256=self.identity.fingerprint,
                expected_server_public_key_sha256=self._trust.fingerprint,
                client_nonce=pending.client_nonce,
                server_nonce=self._string(payload, "server_nonce"),
                challenge_id=self._string(payload, "challenge_id"),
                challenge_expires_at=int(payload["expires_at"]),
                hello_id=pending.hello_id,
                challenge_frame_id=frame.id,
                client_hello_ts=pending.hello_ts,
                auth_ts=auth_ts,
                client_capabilities_sha256=capabilities_sha256(CLIENT_CAPABILITIES),
                server_capabilities_sha256=capabilities_sha256(server_capabilities),
            )
        )
        signature = self.identity.private_key.sign(transcript.encode("utf-8"))
        self._pending_auth = _PendingAuth(
            client_nonce=pending.client_nonce,
            hello_id=pending.hello_id,
            hello_ts=pending.hello_ts,
            trace_id=pending.trace_id,
            server_key_id=server_key_id,
            server_nonce=self._string(payload, "server_nonce"),
            challenge_id=self._string(payload, "challenge_id"),
            challenge_expires_at=int(payload["expires_at"]),
            challenge_frame_id=frame.id,
            selected_v=PROTOCOL_VERSION,
            server_capabilities=server_capabilities,
            auth_frame_id=auth_frame_id,
            auth_ts=auth_ts,
            client_signature_sha256=base64url_encode(
                hashlib.sha256(signature).digest()
            ),
        )
        self.state = AuthState.AUTH_CHALLENGED
        return Frame(
            v=PROTOCOL_VERSION,
            id=auth_frame_id,
            type=FrameType.AUTH,
            op=Op.AUTH_PROVE_CLIENT.value,
            ts=auth_ts,
            seq=seq,
            route=self._route(pending.trace_id),
            payload={
                "key_id": self.identity.key_id,
                "public_key": self.identity.public_key_open_ssh,
                "signature": base64url_encode(signature),
            },
        )

    def _receive_auth_result(self, frame: Frame) -> None:
        if self._trust is None or self._pending_auth is None:
            raise AuthenticationError("trusted authentication state is incomplete")
        pending = self._pending_auth
        self._assert_frame(
            frame, FrameType.AUTH_RESULT, Op.AUTH_RESULT, pending.trace_id
        )
        self._assert_server_route(frame)
        self._assert_success(frame.status, frame.payload.get("status"))
        payload = frame.payload
        if payload.get("ok") is not True or payload.get("trust_mode") != "paired_key":
            raise AuthenticationError("server rejected trusted authentication")
        if (
            self._string(payload, "key_id") != self._trust.key_id
            or self._string(payload, "public_key") != self._trust.public_key
        ):
            raise AuthenticationError("server key differs from trust record")
        server_capabilities = self._capabilities(payload.get("capabilities"))
        if tuple(server_capabilities) != pending.server_capabilities:
            raise AuthenticationError(
                "server capabilities changed during authentication"
            )
        server_time = payload.get("server_time")
        if not isinstance(server_time, int) or server_time <= 0:
            raise AuthenticationError("server_time is invalid")
        transcript = server_proof(
            ServerProof(
                selected_v=pending.selected_v,
                client_min_v=PROTOCOL_VERSION,
                client_max_v=PROTOCOL_VERSION,
                connection_role=connection_role(
                    Role.ASTRBOT, self.config.expected_role
                ),
                client_role=Role.ASTRBOT,
                client_id=self.identity.node_id,
                server_role=self.config.expected_role,
                server_id=self.config.server_id,
                client_key_id=self.identity.key_id,
                server_key_id=self._trust.key_id,
                client_public_key_sha256=self.identity.fingerprint,
                server_public_key_sha256=self._trust.fingerprint,
                client_nonce=pending.client_nonce,
                server_nonce=pending.server_nonce,
                challenge_id=pending.challenge_id,
                challenge_expires_at=pending.challenge_expires_at,
                hello_id=pending.hello_id,
                challenge_frame_id=pending.challenge_frame_id,
                auth_frame_id=pending.auth_frame_id,
                auth_result_ts=server_time,
                client_signature_sha256=pending.client_signature_sha256,
                client_capabilities_sha256=capabilities_sha256(CLIENT_CAPABILITIES),
                server_capabilities_sha256=capabilities_sha256(server_capabilities),
            )
        )
        public_key = self._trust.public_key_object()
        public_key.verify(
            base64url_decode(self._string(payload, "signature"), "signature"),
            transcript.encode("utf-8"),
        )
        self._finish(payload, server_capabilities)

    def _finish(self, payload: dict[str, Any], capabilities: tuple[str, ...]) -> None:
        session_id = payload.get("session_id")
        server_time = payload.get("server_time")
        if not isinstance(session_id, str) or not session_id:
            raise AuthenticationError("authenticated session_id is missing")
        if not isinstance(server_time, int) or server_time <= 0:
            raise AuthenticationError("authenticated server_time is invalid")
        self.session_id = session_id
        self.server_time = server_time
        self.server_capabilities = capabilities
        self._srp = None
        self._pair_challenge = None
        self._pending_auth = None
        self.config.pairing_code = ""
        self.state = AuthState.READY

    def _assert_frame(
        self, frame: Frame, frame_type: FrameType, op: Op, trace_id: str
    ) -> None:
        if frame.type is not frame_type or frame.op != op.value:
            raise AuthenticationError("unexpected authentication operation")
        if frame.route.trace_id != trace_id:
            raise AuthenticationError("authentication trace does not match")

    def _assert_server_route(self, frame: Frame) -> None:
        if (
            frame.route.src.role is not self.config.expected_role
            or frame.route.src.id != self.config.server_id
            or frame.route.dst.role is not Role.ASTRBOT
            or frame.route.dst.id != self.identity.node_id
        ):
            raise AuthenticationError("authentication route does not match endpoint")

    @staticmethod
    def _assert_success(status: Status | None, payload_status: object) -> None:
        if status is None or not status.ok:
            raise AuthenticationError("authentication status is unsuccessful")
        if not isinstance(payload_status, dict):
            raise AuthenticationError("authentication payload status is missing")
        if payload_status.get("ok") is not True or payload_status.get("code") != "ok":
            raise AuthenticationError("authentication payload status is unsuccessful")

    def _route(self, trace_id: str) -> Route:
        return Route(
            src=NodeRef(Role.ASTRBOT, self.identity.node_id),
            dst=NodeRef(self.config.expected_role, self.config.server_id),
            trace_id=trace_id,
        )

    @staticmethod
    def _protocol_range() -> dict[str, Any]:
        return {
            "name": PROTOCOL_NAME,
            "min_v": PROTOCOL_VERSION,
            "max_v": PROTOCOL_VERSION,
        }

    @staticmethod
    def _capabilities(value: object) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise AuthenticationError("capabilities are invalid")
        return tuple(value)

    @staticmethod
    def _string(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise AuthenticationError(f"authentication field {field} is missing")
        return value

    @staticmethod
    def _parse_public_key(value: str) -> Ed25519PublicKey:
        try:
            from cryptography.hazmat.primitives import serialization

            key = serialization.load_ssh_public_key(value.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise AuthenticationError("server public key is invalid") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise AuthenticationError("server public key is not Ed25519")
        return key

    @staticmethod
    def _assert_not_expired(value: object) -> None:
        if not isinstance(value, int) or value <= int(time.time() * 1000):
            raise AuthenticationError("authentication challenge is expired")

    @staticmethod
    def _id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    def _fail(self, _reason: str) -> None:
        self._srp = None
        self._pair_challenge = None
        self._pending_auth = None
        self.state = AuthState.FAILED
