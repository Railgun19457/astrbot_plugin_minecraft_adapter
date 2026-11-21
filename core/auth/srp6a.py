"""Thin pysrp wrapper for the fixed Java-compatible SRP-6a pairing suite."""

import base64
import hashlib
import hmac
from dataclasses import dataclass

import srp

_SRP_CONFIGURED = False


class Srp6aError(ValueError):
    """Raised when SRP pairing input or server evidence is invalid."""


def base64url_encode(value: bytes) -> str:
    """Encode protocol bytes using unpadded Base64url."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def base64url_decode(value: str, name: str) -> bytes:
    """Decode unpadded Base64url bytes with strict input validation.

    Args:
        value: Encoded protocol value.
        name: Field name used in a safe validation error.

    Returns:
        Decoded bytes.

    Raises:
        Srp6aError: If the encoded value is empty or malformed.
    """
    if not isinstance(value, str) or not value:
        raise Srp6aError(f"{name} is required")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, base64.binascii.Error) as exc:
        raise Srp6aError(f"{name} is not valid Base64url") from exc


def pairing_identity(pairing_id: str) -> str:
    """Return the fixed SRP username associated with a pairing record."""
    if not isinstance(pairing_id, str) or not pairing_id:
        raise Srp6aError("pairing_id is required")
    return f"astrbot-minecraft-pairing-v1:{pairing_id}"


@dataclass
class Srp6aClientSession:
    """The sensitive temporary state needed to finish a pairing exchange."""

    user: srp.User
    client_public_value: str
    client_proof: str
    session_key: bytes

    def verify_server_proof(self, server_proof: str) -> bool:
        """Verify M2 and return whether pysrp accepted the server.

        Args:
            server_proof: Base64url encoded SRP server evidence M2.

        Returns:
            Whether M2 matches the proof derived by pysrp.
        """
        proof = base64url_decode(server_proof, "server_proof")
        self.user.verify_session(proof)
        return self.user.authenticated()


def create_client_session(
    pairing_id: str, pairing_code: str, salt: str, server_public_value: str
) -> Srp6aClientSession:
    """Create A, M1, and the SRP session key using the fixed protocol suite.

    Args:
        pairing_id: Public pairing record identifier.
        pairing_code: One-time secret code supplied by the server administrator.
        salt: Base64url encoded SRP salt from ``pair.challenge``.
        server_public_value: Base64url encoded SRP public value B.

    Returns:
        A transient client exchange state.

    Raises:
        Srp6aError: If the peer's SRP parameters are invalid.
    """
    global _SRP_CONFIGURED
    if not _SRP_CONFIGURED:
        srp.rfc5054_enable()
        _SRP_CONFIGURED = True
    if not isinstance(pairing_code, str) or not pairing_code:
        raise Srp6aError("pairing_code is required")
    user = srp.User(
        pairing_identity(pairing_id),
        pairing_code,
        hash_alg=srp.SHA256,
        ng_type=srp.NG_2048,
    )
    _, public_value = user.start_authentication()
    proof = user.process_challenge(
        base64url_decode(salt, "salt"),
        base64url_decode(server_public_value, "server_public_value"),
    )
    if proof is None:
        raise Srp6aError("server_public_value failed SRP safety validation")
    session_key = user.K
    if not session_key:
        raise Srp6aError("SRP session key was not created")
    return Srp6aClientSession(
        user=user,
        client_public_value=base64url_encode(public_value),
        client_proof=base64url_encode(proof),
        session_key=session_key,
    )


def key_binding_confirmation(
    session_key: bytes,
    pairing_id: str,
    client_role: str,
    client_id: str,
    server_role: str,
    server_id: str,
    client_key_id: str,
    client_public_key: str,
    server_key_id: str,
    server_public_key: str,
    salt: str,
    client_public_value: str,
    server_public_value: str,
    client_proof: str,
    server_proof: str,
) -> str:
    """Compute the Java-compatible HMAC that binds SRP to identity keys.

    Args:
        session_key: Authenticated SRP session key K.
        pairing_id: Public pairing record identifier.
        client_role: Client role wire value.
        client_id: Client route identifier.
        server_role: Server role wire value.
        server_id: Server route identifier.
        client_key_id: Local Ed25519 key identifier.
        client_public_key: Local OpenSSH Ed25519 public key.
        server_key_id: Remote Ed25519 key identifier.
        server_public_key: Remote OpenSSH Ed25519 public key.
        salt: Base64url SRP salt.
        client_public_value: Base64url SRP A.
        server_public_value: Base64url SRP B.
        client_proof: Base64url SRP M1.
        server_proof: Base64url SRP M2.

    Returns:
        Base64url HMAC-SHA-256 key binding confirmation.
    """
    client_key_digest = base64url_encode(
        hashlib.sha256(client_public_key.encode("utf-8")).digest()
    )
    server_key_digest = base64url_encode(
        hashlib.sha256(server_public_key.encode("utf-8")).digest()
    )
    transcript = (
        "ASTRBOT-MC-SRP-PAIR-BINDING-V1\n"
        f"pairing_id={pairing_id}\n"
        f"client_role={client_role}\n"
        f"client_id={client_id}\n"
        f"server_role={server_role}\n"
        f"server_id={server_id}\n"
        f"client_key_id={client_key_id}\n"
        f"client_public_key_sha256={client_key_digest}\n"
        f"server_key_id={server_key_id}\n"
        f"server_public_key_sha256={server_key_digest}\n"
        f"salt={salt}\n"
        f"A={client_public_value}\n"
        f"B={server_public_value}\n"
        f"M1={client_proof}\n"
        f"M2={server_proof}\n"
    )
    return base64url_encode(
        hmac.new(session_key, transcript.encode("utf-8"), hashlib.sha256).digest()
    )


def key_binding_matches(expected: str, actual: str) -> bool:
    """Compare Base64url key bindings without timing-sensitive equality."""
    return isinstance(actual, str) and hmac.compare_digest(expected, actual)
