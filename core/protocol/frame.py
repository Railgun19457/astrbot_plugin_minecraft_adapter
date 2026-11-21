"""Typed protocol Frame envelope models."""

from dataclasses import dataclass
from typing import Any

from .constants import FrameType, Role, StatusCode, TargetScope


@dataclass(frozen=True)
class NodeRef:
    """A protocol node identity used in a route."""

    role: Role
    id: str

    def to_dict(self) -> dict[str, str]:
        """Serialize the node reference using protocol field names."""
        return {"role": self.role.value, "id": self.id}


@dataclass(frozen=True)
class Route:
    """The source, destination, and correlation trace of a frame."""

    src: NodeRef
    dst: NodeRef
    trace_id: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize the route using protocol field names."""
        return {
            "src": self.src.to_dict(),
            "dst": self.dst.to_dict(),
            "trace_id": self.trace_id,
        }


@dataclass(frozen=True)
class Target:
    """The business target for a request or event."""

    scope: TargetScope
    server_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Serialize the target using protocol field names."""
        data = {"scope": self.scope.value}
        if self.server_id is not None:
            data["server_id"] = self.server_id
        return data


@dataclass(frozen=True)
class Status:
    """A protocol status value carried by response and error frames."""

    ok: bool
    code: StatusCode
    message: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize the status using protocol field names."""
        return {"ok": self.ok, "code": self.code.value, "message": self.message}


@dataclass(frozen=True)
class Frame:
    """The immutable Version 3 protocol frame envelope."""

    v: int
    id: str
    type: FrameType
    op: str
    ts: int
    seq: int
    route: Route
    payload: dict[str, Any]
    origin: dict[str, Any] | None = None
    target: Target | None = None
    reply_to: str | None = None
    status: Status | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the frame without null optional fields."""
        data: dict[str, Any] = {
            "v": self.v,
            "id": self.id,
            "type": self.type.value,
            "op": self.op,
            "ts": self.ts,
            "seq": self.seq,
            "route": self.route.to_dict(),
            "payload": self.payload,
        }
        if self.origin is not None:
            data["origin"] = self.origin
        if self.target is not None:
            data["target"] = self.target.to_dict()
        if self.reply_to is not None:
            data["reply_to"] = self.reply_to
        if self.status is not None:
            data["status"] = self.status.to_dict()
        return data
