"""Strict JSON codec for Version 3 protocol frames."""

import json
from typing import Any

from .constants import (
    OP_FRAME_TYPES,
    PROTOCOL_VERSION,
    FrameType,
    Op,
    Role,
    StatusCode,
    TargetScope,
)
from .frame import Frame, NodeRef, Route, Status, Target

MAX_FRAME_BYTES = 256 * 1024
MAX_IDENTIFIER_LENGTH = 256
MAX_PAYLOAD_DEPTH = 16
MAX_COLLECTION_ITEMS = 1_024
MAX_STRING_LENGTH = 32_768


class ProtocolError(ValueError):
    """Raised when a peer supplies an invalid Version 3 protocol frame."""


def _require_string(
    value: object, name: str, maximum: int = MAX_IDENTIFIER_LENGTH
) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProtocolError(
            f"{name} must be a non-empty string of at most {maximum} chars"
        )
    return value


def _require_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{name} must be an object")
    return value


def _validate_value(value: object, depth: int = 0) -> None:
    if depth > MAX_PAYLOAD_DEPTH:
        raise ProtocolError("payload nesting limit exceeded")
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise ProtocolError("payload string length limit exceeded")
        return
    if isinstance(value, dict):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ProtocolError("payload object item limit exceeded")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > MAX_IDENTIFIER_LENGTH:
                raise ProtocolError("payload key is invalid")
            _validate_value(child, depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_COLLECTION_ITEMS:
            raise ProtocolError("payload list item limit exceeded")
        for child in value:
            _validate_value(child, depth + 1)
        return
    if value is not None and not isinstance(value, bool | int | float):
        raise ProtocolError("payload contains an unsupported JSON value")


def _parse_node(value: object, name: str) -> NodeRef:
    node = _require_object(value, name)
    if set(node) != {"role", "id"}:
        raise ProtocolError(f"{name} must contain only role and id")
    try:
        role = Role(_require_string(node.get("role"), f"{name}.role"))
    except ValueError as exc:
        raise ProtocolError(f"{name}.role is unsupported") from exc
    return NodeRef(role=role, id=_require_string(node.get("id"), f"{name}.id"))


def _parse_route(value: object) -> Route:
    route = _require_object(value, "route")
    if set(route) != {"src", "dst", "trace_id"}:
        raise ProtocolError("route must contain only src, dst, and trace_id")
    return Route(
        src=_parse_node(route.get("src"), "route.src"),
        dst=_parse_node(route.get("dst"), "route.dst"),
        trace_id=_require_string(route.get("trace_id"), "route.trace_id"),
    )


def _parse_target(value: object) -> Target:
    target = _require_object(value, "target")
    try:
        scope = TargetScope(_require_string(target.get("scope"), "target.scope"))
    except ValueError as exc:
        raise ProtocolError("target.scope is unsupported") from exc
    server_id = target.get("server_id")
    if scope is TargetScope.SERVER:
        if set(target) != {"scope", "server_id"}:
            raise ProtocolError("target contains unsupported fields")
        return Target(
            scope=scope, server_id=_require_string(server_id, "target.server_id")
        )
    if server_id is not None:
        raise ProtocolError("target.server_id is only valid for server scope")
    if set(target) != {"scope"}:
        raise ProtocolError("target contains unsupported fields")
    return Target(scope=scope)


def _parse_status(value: object) -> Status:
    status = _require_object(value, "status")
    if not isinstance(status.get("ok"), bool):
        raise ProtocolError("status.ok must be boolean")
    try:
        code = StatusCode(_require_string(status.get("code"), "status.code"))
    except ValueError as exc:
        raise ProtocolError("status.code is unsupported") from exc
    ok = status["ok"]
    if ok != (code is StatusCode.OK):
        raise ProtocolError("status.ok and status.code disagree")
    return Status(
        ok=ok,
        code=code,
        message=_require_string(
            status.get("message"), "status.message", MAX_STRING_LENGTH
        ),
    )


def _validate_frame_shape(
    frame_type: FrameType,
    target: Target | None,
    reply_to: str | None,
    status: Status | None,
) -> None:
    if frame_type in {FrameType.EVENT, FrameType.REQUEST} and target is None:
        raise ProtocolError(f"target is required for {frame_type.value}")
    if frame_type is FrameType.RESPONSE and (reply_to is None or status is None):
        raise ProtocolError("reply_to and status are required for response")
    if frame_type is FrameType.ACK and reply_to is None:
        raise ProtocolError("reply_to is required for ack")
    if frame_type is FrameType.ERROR and (reply_to is None or status is None):
        raise ProtocolError("reply_to and status are required for error")


def _validate_origin(origin: object, frame_type: FrameType) -> None:
    """Validate the bounded backend-origin envelope used by Velocity events."""
    if origin is None:
        return
    if frame_type is not FrameType.EVENT:
        raise ProtocolError("origin is only valid for events")
    value = _require_object(origin, "origin")
    if not {"role", "id"}.issubset(value):
        raise ProtocolError("origin must contain role and id")
    try:
        Role(_require_string(value.get("role"), "origin.role"))
    except ValueError as exc:
        raise ProtocolError("origin.role is unsupported") from exc
    _require_string(value.get("id"), "origin.id")
    server_id = value.get("server_id")
    if server_id is not None:
        _require_string(server_id, "origin.server_id")


def validate_frame(frame: Frame) -> None:
    """Validate a locally constructed frame before it is sent.

    Args:
        frame: Immutable protocol frame to validate.

    Raises:
        ProtocolError: If the envelope violates the Version 3 contract.
    """
    try:
        op = Op(frame.op)
    except ValueError as exc:
        raise ProtocolError("operation is unsupported") from exc
    if frame.v != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    _require_string(frame.id, "id")
    if frame.type not in OP_FRAME_TYPES[op]:
        raise ProtocolError("operation is invalid for frame type")
    if not isinstance(frame.ts, int) or frame.ts <= 0:
        raise ProtocolError("ts must be a positive integer")
    if not isinstance(frame.seq, int) or frame.seq <= 0:
        raise ProtocolError("seq must be a positive integer")
    _parse_route(frame.route.to_dict())
    _validate_value(frame.payload)
    _validate_origin(frame.origin, frame.type)
    if frame.target is not None:
        _parse_target(frame.target.to_dict())
    if frame.reply_to is not None:
        _require_string(frame.reply_to, "reply_to")
    if frame.status is not None:
        _parse_status(frame.status.to_dict())
    _validate_frame_shape(frame.type, frame.target, frame.reply_to, frame.status)


def decode_frame(raw: str | bytes) -> Frame:
    """Decode and strictly validate a single peer frame.

    Args:
        raw: UTF-8 JSON received from the WebSocket peer.

    Returns:
        A validated immutable protocol frame.

    Raises:
        ProtocolError: If the envelope, operation, or JSON payload is invalid.
    """
    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolError("frame exceeds maximum size")
    try:
        data = json.loads(encoded)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("frame is not valid JSON") from exc
    data = _require_object(data, "frame")
    required = {"v", "id", "type", "op", "ts", "seq", "route", "payload"}
    if not required.issubset(data):
        raise ProtocolError("frame is missing required fields")
    if data.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    try:
        frame_type = FrameType(_require_string(data.get("type"), "type"))
        op = Op(_require_string(data.get("op"), "op"))
    except ValueError as exc:
        raise ProtocolError("frame type or operation is unsupported") from exc
    if frame_type not in OP_FRAME_TYPES[op]:
        raise ProtocolError("operation is invalid for frame type")
    if not isinstance(data.get("ts"), int) or data["ts"] <= 0:
        raise ProtocolError("ts must be a positive integer")
    if not isinstance(data.get("seq"), int) or data["seq"] <= 0:
        raise ProtocolError("seq must be a positive integer")
    payload = _require_object(data.get("payload"), "payload")
    _validate_value(payload)
    target = _parse_target(data["target"]) if "target" in data else None
    status = _parse_status(data["status"]) if "status" in data else None
    reply_to = (
        _require_string(data["reply_to"], "reply_to") if "reply_to" in data else None
    )
    _validate_frame_shape(frame_type, target, reply_to, status)
    origin = data.get("origin")
    _validate_origin(origin, frame_type)
    return Frame(
        v=PROTOCOL_VERSION,
        id=_require_string(data.get("id"), "id"),
        type=frame_type,
        op=op.value,
        ts=data["ts"],
        seq=data["seq"],
        route=_parse_route(data.get("route")),
        payload=payload,
        origin=origin,
        target=target,
        reply_to=reply_to,
        status=status,
    )


def encode_frame(frame: Frame) -> str:
    """Encode a validated local frame with compact deterministic JSON."""
    validate_frame(frame)
    return json.dumps(frame.to_dict(), ensure_ascii=False, separators=(",", ":"))
