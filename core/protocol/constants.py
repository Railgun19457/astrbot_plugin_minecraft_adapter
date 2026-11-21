"""Constants shared by every Version 3 protocol layer."""

from enum import Enum


class StringEnum(str, Enum):
    """Python 3.10-compatible string enumeration base class."""


PROTOCOL_NAME = "astrbot-minecraft"
PROTOCOL_VERSION = 1
SRP6A_SUITE = "srp-6a-rfc5054-sha256-2048"


class Role(StringEnum):
    """Protocol node roles."""

    ASTRBOT = "astrbot"
    STANDALONE = "standalone"
    VELOCITY = "velocity"
    BACKEND = "backend"


class FrameType(StringEnum):
    """Protocol frame types."""

    HELLO = "hello"
    CHALLENGE = "challenge"
    AUTH = "auth"
    AUTH_RESULT = "auth_result"
    EVENT = "event"
    REQUEST = "request"
    RESPONSE = "response"
    ACK = "ack"
    HEARTBEAT = "heartbeat"
    ERROR = "error"


class TargetScope(StringEnum):
    """Business request target scopes."""

    SELF = "self"
    SERVER = "server"
    ALL_SERVERS = "all_servers"


class StatusCode(StringEnum):
    """Status codes defined by the Java protocol contract."""

    OK = "ok"
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    AUTH_FAILED = "auth_failed"
    AUTH_CODE_INVALID = "auth_code_invalid"
    AUTH_CODE_EXPIRED = "auth_code_expired"
    AUTH_CODE_USED = "auth_code_used"
    AUTH_CODE_ATTEMPTS_EXCEEDED = "auth_code_attempts_exceeded"
    PAIRING_FAILED = "pairing_failed"
    PROTOCOL_VERSION_UNSUPPORTED = "protocol_version_unsupported"
    UNSUPPORTED_OPERATION = "unsupported_operation"
    PERMISSION_DENIED = "permission_denied"
    TARGET_NOT_FOUND = "target_not_found"
    TARGET_OFFLINE = "target_offline"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    COMMAND_DISABLED = "command_disabled"
    COMMAND_BLOCKED = "command_blocked"
    PLAYER_NOT_FOUND = "player_not_found"
    PLAYER_OFFLINE = "player_offline"
    BACKEND_DISABLED = "backend_disabled"
    BACKEND_NOT_TRUSTED = "backend_not_trusted"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    INTERNAL_ERROR = "internal_error"


class Op(StringEnum):
    """Operations accepted by protocol revision 1.1.0."""

    PAIR_INIT = "pair.init"
    PAIR_CHALLENGE = "pair.challenge"
    PAIR_PROVE = "pair.prove"
    PAIR_COMPLETE = "pair.complete"
    AUTH_HELLO = "auth.hello"
    AUTH_CHALLENGE = "auth.challenge"
    AUTH_PROVE_CLIENT = "auth.prove_client"
    AUTH_RESULT = "auth.result"
    CORE_ACK = "core.ack"
    CORE_PING = "core.ping"
    CORE_PONG = "core.pong"
    CORE_ERROR = "core.error"
    CORE_GOODBYE = "core.goodbye"
    CORE_CAPABILITIES_UPDATED = "core.capabilities.updated"
    CHAT_PLAYER = "chat.player"
    CHAT_EXTERNAL = "chat.external"
    CHAT_PRIVATE = "chat.private"
    PLAYER_JOIN = "player.join"
    PLAYER_LEAVE = "player.leave"
    PLAYER_LIST = "player.list"
    PLAYER_DETAIL = "player.detail"
    SERVER_STATUS = "server.status"
    SERVER_SNAPSHOT = "server.snapshot"
    COMMAND_EXECUTE = "command.execute"
    COMMAND_COMPLETED = "command.completed"
    AI_GROUP_REQUEST = "ai.group.request"
    AI_PRIVATE_REQUEST = "ai.private.request"
    AI_REPLY = "ai.reply"
    LOG_QUERY = "log.query"
    CONFIG_SYNC = "config.sync"


class Capability(StringEnum):
    """Capabilities implemented by this client."""

    CORE = "core.v1"
    AUTH_ED25519 = "auth.ed25519.v1"
    AUTH_SRP6A_PAIRING = "auth.srp6a-pairing.v1"
    CHAT = "chat.v1"
    PLAYER = "player.v1"
    SERVER = "server.v1"
    COMMAND = "command.v1"
    AI = "ai.v1"
    LOG = "log.v1"


CLIENT_CAPABILITIES = tuple(capability.value for capability in Capability)

OP_FRAME_TYPES: dict[Op, frozenset[FrameType]] = {
    Op.PAIR_INIT: frozenset({FrameType.HELLO}),
    Op.PAIR_CHALLENGE: frozenset({FrameType.CHALLENGE}),
    Op.PAIR_PROVE: frozenset({FrameType.AUTH}),
    Op.PAIR_COMPLETE: frozenset({FrameType.AUTH_RESULT}),
    Op.AUTH_HELLO: frozenset({FrameType.HELLO}),
    Op.AUTH_CHALLENGE: frozenset({FrameType.CHALLENGE}),
    Op.AUTH_PROVE_CLIENT: frozenset({FrameType.AUTH}),
    Op.AUTH_RESULT: frozenset({FrameType.AUTH_RESULT}),
    Op.CORE_ACK: frozenset({FrameType.ACK}),
    Op.CORE_PING: frozenset({FrameType.HEARTBEAT}),
    Op.CORE_PONG: frozenset({FrameType.HEARTBEAT}),
    Op.CORE_ERROR: frozenset({FrameType.ERROR}),
    Op.CORE_GOODBYE: frozenset({FrameType.EVENT}),
    Op.CORE_CAPABILITIES_UPDATED: frozenset({FrameType.EVENT}),
    Op.CHAT_PLAYER: frozenset({FrameType.EVENT}),
    Op.CHAT_EXTERNAL: frozenset({FrameType.REQUEST, FrameType.RESPONSE}),
    Op.CHAT_PRIVATE: frozenset({FrameType.REQUEST, FrameType.RESPONSE}),
    Op.PLAYER_JOIN: frozenset({FrameType.EVENT}),
    Op.PLAYER_LEAVE: frozenset({FrameType.EVENT}),
    Op.PLAYER_LIST: frozenset({FrameType.REQUEST, FrameType.RESPONSE}),
    Op.PLAYER_DETAIL: frozenset({FrameType.REQUEST, FrameType.RESPONSE}),
    Op.SERVER_STATUS: frozenset({FrameType.REQUEST, FrameType.RESPONSE}),
    Op.SERVER_SNAPSHOT: frozenset({FrameType.EVENT}),
    Op.COMMAND_EXECUTE: frozenset({FrameType.REQUEST, FrameType.RESPONSE}),
    Op.COMMAND_COMPLETED: frozenset({FrameType.EVENT}),
    Op.AI_GROUP_REQUEST: frozenset({FrameType.EVENT}),
    Op.AI_PRIVATE_REQUEST: frozenset({FrameType.EVENT}),
    Op.AI_REPLY: frozenset({FrameType.REQUEST, FrameType.RESPONSE}),
    Op.LOG_QUERY: frozenset({FrameType.REQUEST, FrameType.RESPONSE}),
    Op.CONFIG_SYNC: frozenset({FrameType.REQUEST, FrameType.RESPONSE}),
}
