"""Fail-closed persistence for the local Ed25519 identity and trusted servers."""

import base64
import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class IdentityStoreError(RuntimeError):
    """Raised when identity or trust material is absent, inconsistent, or invalid."""


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _raw_public_key(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _fingerprint(public_key: Ed25519PublicKey) -> str:
    return _base64url(hashlib.sha256(_raw_public_key(public_key)).digest())


def _open_ssh_public_key(public_key: Ed25519PublicKey, key_id: str) -> str:
    encoded = public_key.public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    return f"{encoded} {key_id}"


def _load_open_ssh_public_key(value: str) -> Ed25519PublicKey:
    try:
        key = serialization.load_ssh_public_key(value.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise IdentityStoreError("stored public key is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise IdentityStoreError("stored public key is not Ed25519")
    return key


@dataclass(frozen=True)
class LocalIdentity:
    """Stable local node identity used in routes and authentication.

    Attributes:
        node_id: Stable AstrBot node identifier.
        key_id: Fingerprint-derived identifier accepted by the Java peer.
        fingerprint: SHA-256 fingerprint of the raw Ed25519 public key.
        public_key_open_ssh: Public key representation sent over the protocol.
        private_key: Private key used only to sign authentication transcripts.
    """

    node_id: str
    key_id: str
    fingerprint: str
    public_key_open_ssh: str
    private_key: Ed25519PrivateKey


@dataclass(frozen=True)
class TrustRecord:
    """A server key bound to one canonical endpoint and route identity."""

    schema_version: int
    canonical_url: str
    role: str
    server_id: str
    key_id: str
    public_key: str
    fingerprint: str
    paired_at: str

    def public_key_object(self) -> Ed25519PublicKey:
        """Return the validated Ed25519 key preserved in this trust record."""
        return _load_open_ssh_public_key(self.public_key)


class IdentityStore:
    """Own the plugin-private local identity and endpoint-specific trust records."""

    def __init__(self, data_path: Path):
        self._base_path = data_path
        self._identity_path = data_path / "identity"
        self._trust_path = data_path / "trust"
        self._metadata_path = self._identity_path / "identity.json"
        self._private_path = self._identity_path / "ed25519-private.pem"
        self._public_path = self._identity_path / "ed25519-public.pem"

    def load_or_create_identity(self) -> LocalIdentity:
        """Load the stable local identity or atomically create it on first use.

        Returns:
            The loaded or newly generated identity.

        Raises:
            IdentityStoreError: If existing identity files are incomplete or inconsistent.
        """
        files = (self._metadata_path, self._private_path, self._public_path)
        present = tuple(path.exists() for path in files)
        if any(present) and not all(present):
            raise IdentityStoreError(
                "identity files are incomplete; reset and re-pair explicitly"
            )
        if not any(present):
            private_key = Ed25519PrivateKey.generate()
            public_key = private_key.public_key()
            fingerprint = _fingerprint(public_key)
            identity = LocalIdentity(
                node_id=f"astrbot-{uuid.uuid4().hex}",
                key_id=f"astrbot-{fingerprint}",
                fingerprint=fingerprint,
                public_key_open_ssh=_open_ssh_public_key(
                    public_key, f"astrbot-{fingerprint}"
                ),
                private_key=private_key,
            )
            self._identity_path.mkdir(parents=True, exist_ok=True)
            self._write_atomic_json(
                self._metadata_path,
                {
                    "schema_version": 1,
                    "node_id": identity.node_id,
                    "key_id": identity.key_id,
                    "fingerprint": identity.fingerprint,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self._write_atomic(
                self._private_path,
                private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                ),
                private=True,
            )
            self._write_atomic(
                self._public_path,
                (_open_ssh_public_key(public_key, identity.key_id) + "\n").encode(
                    "utf-8"
                ),
                private=False,
            )
            return identity

        try:
            metadata = json.loads(self._metadata_path.read_text(encoding="utf-8"))
            private_key = serialization.load_pem_private_key(
                self._private_path.read_bytes(), password=None
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IdentityStoreError("identity files cannot be loaded") from exc
        if not isinstance(private_key, Ed25519PrivateKey):
            raise IdentityStoreError("stored private key is not Ed25519")
        if not isinstance(metadata, dict) or metadata.get("schema_version") != 1:
            raise IdentityStoreError("identity metadata schema is unsupported")
        node_id = metadata.get("node_id")
        key_id = metadata.get("key_id")
        fingerprint = metadata.get("fingerprint")
        if not all(
            isinstance(value, str) and value for value in (node_id, key_id, fingerprint)
        ):
            raise IdentityStoreError("identity metadata is incomplete")
        public_key = private_key.public_key()
        expected_fingerprint = _fingerprint(public_key)
        expected_key_id = f"astrbot-{expected_fingerprint}"
        if fingerprint != expected_fingerprint or key_id != expected_key_id:
            raise IdentityStoreError("identity metadata does not match the private key")
        stored_public = _load_open_ssh_public_key(
            self._public_path.read_text(encoding="utf-8").strip()
        )
        if _raw_public_key(stored_public) != _raw_public_key(public_key):
            raise IdentityStoreError("public key does not match the private key")
        return LocalIdentity(
            node_id=node_id,
            key_id=key_id,
            fingerprint=fingerprint,
            public_key_open_ssh=_open_ssh_public_key(public_key, key_id),
            private_key=private_key,
        )

    def load_trust(
        self, canonical_url: str, role: str, server_id: str
    ) -> TrustRecord | None:
        """Load a trusted server key only when endpoint binding exactly matches.

        Args:
            canonical_url: Canonical configured endpoint URL.
            role: Expected remote role.
            server_id: Expected remote server identifier.

        Returns:
            The matching trust record, or ``None`` if the endpoint is unpaired.

        Raises:
            IdentityStoreError: If the existing record is malformed or inconsistent.
        """
        path = self._trust_file(canonical_url, role, server_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            record = TrustRecord(**data)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IdentityStoreError("trust record cannot be loaded") from exc
        if (
            record.schema_version != 1
            or record.canonical_url != canonical_url
            or record.role != role
            or record.server_id != server_id
            or not record.key_id
        ):
            raise IdentityStoreError("trust record endpoint binding is invalid")
        if _fingerprint(record.public_key_object()) != record.fingerprint:
            raise IdentityStoreError(
                "trust record fingerprint does not match public key"
            )
        return record

    def save_trust(
        self,
        canonical_url: str,
        role: str,
        server_id: str,
        key_id: str,
        public_key: str,
    ) -> TrustRecord:
        """Atomically save a newly SRP-verified trusted server key.

        Args:
            canonical_url: Canonical endpoint URL.
            role: Verified remote role.
            server_id: Verified remote server identifier.
            key_id: Remote key identifier.
            public_key: Remote OpenSSH Ed25519 public key.

        Returns:
            The saved endpoint-specific trust record.

        Raises:
            IdentityStoreError: If a key change or invalid public key is detected.
        """
        key = _load_open_ssh_public_key(public_key)
        record = TrustRecord(
            schema_version=1,
            canonical_url=canonical_url,
            role=role,
            server_id=server_id,
            key_id=key_id,
            public_key=public_key.strip(),
            fingerprint=_fingerprint(key),
            paired_at=datetime.now(timezone.utc).isoformat(),
        )
        existing = self.load_trust(canonical_url, role, server_id)
        if existing and (
            existing.key_id != record.key_id
            or existing.fingerprint != record.fingerprint
        ):
            raise IdentityStoreError(
                "server key changed; forget the endpoint before re-pairing"
            )
        self._trust_path.mkdir(parents=True, exist_ok=True)
        self._write_atomic_json(
            self._trust_file(canonical_url, role, server_id), asdict(record)
        )
        return record

    def forget_trust(self, canonical_url: str, role: str, server_id: str) -> bool:
        """Delete exactly one endpoint trust record.

        Args:
            canonical_url: Canonical endpoint URL.
            role: Remote server role.
            server_id: Remote server identifier.

        Returns:
            Whether an existing trust record was removed.
        """
        try:
            self._trust_file(canonical_url, role, server_id).unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise IdentityStoreError("trust record could not be removed") from exc
        return True

    def _trust_file(self, canonical_url: str, role: str, server_id: str) -> Path:
        digest = hashlib.sha256(
            f"{canonical_url}\n{role}\n{server_id}".encode()
        ).hexdigest()
        return self._trust_path / f"{digest}.json"

    def _write_atomic_json(self, path: Path, value: dict[str, object]) -> None:
        self._write_atomic(
            path,
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode(
                "utf-8"
            )
            + b"\n",
            private=True,
        )

    @staticmethod
    def _write_atomic(path: Path, data: bytes, private: bool) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            if private:
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
            os.replace(temporary, path)
        except OSError as exc:
            raise IdentityStoreError(f"cannot write {path.name}") from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
