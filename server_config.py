"""Parse the user-authored server template list."""

from dataclasses import dataclass
from string import Formatter

from .actions import CommandList, CustomCommand, command_root, matches_root, parse_roots
from .link import Endpoint, LinkError, endpoint_url, node_id
from .native_link import NativeEndpoint

DEFAULT_CHAT_FORMAT = "[{server}] {player}：{message}"
CHAT_FIELDS = {
    "server",
    "server_id",
    "backend",
    "player",
    "player_name",
    "player_id",
    "uuid",
    "message",
}


def object_section(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return value


def identifiers(data: dict, name: str) -> frozenset[str]:
    result = data.get(name, [])
    if not isinstance(result, list) or any(
        not isinstance(item, str) or not item.strip() for item in result
    ):
        raise ValueError(f"{name} must be a list of strings")
    return frozenset(item.strip() for item in result)


def flag(data: dict, name: str, default: bool = False) -> bool:
    value = data.get(name, default)
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


@dataclass(frozen=True)
class ServerEntry:
    endpoint: Endpoint | NativeEndpoint
    sessions: frozenset[str]
    prefix: str
    bridge: bool
    player_events: bool
    ai: bool
    chat_format: str
    mark_option: str
    command_enabled: bool
    commands: CommandList
    admin_commands: frozenset[str]
    admin_umos: frozenset[str]
    custom_commands: dict[str, CustomCommand]

    @classmethod
    def parse(cls, row: dict) -> "ServerEntry":
        server = object_section(row, "server")
        message = object_section(row, "message")
        commands = object_section(row, "cmd")
        role = row.get("__template_key")
        if role == "native":
            endpoint = NativeEndpoint.parse(server)
        elif role in ("standalone", "velocity"):
            pair_id, pair_code = (
                server.get("pairing_id", ""),
                server.get("pairing_code", ""),
            )
            if (
                not isinstance(pair_id, str)
                or not isinstance(pair_code, str)
                or bool(pair_id) != bool(pair_code)
            ):
                raise ValueError(
                    "server.pairing_id 和 server.pairing_code 必须一起填写"
                )
            endpoint = Endpoint(
                node_id(server.get("server_id", "")),
                role,
                endpoint_url(server.get("url", "")),
                pair_id,
                pair_code,
            )
        else:
            raise ValueError("请选择独立服、Velocity 群组服或原生直连模板")
        prefix = message.get("auto_forward_prefix", "*")
        if not isinstance(prefix, str):
            raise TypeError("message.auto_forward_prefix must be text")
        chat_format = message.get("forward_chat_format", DEFAULT_CHAT_FORMAT)
        try:
            if (
                not isinstance(chat_format, str)
                or not chat_format.strip()
                or any(
                    field is not None
                    and (field not in CHAT_FIELDS or spec or conversion)
                    for _, field, spec, conversion in Formatter().parse(chat_format)
                )
            ):
                raise ValueError("unsupported format")
        except ValueError as error:
            raise ValueError(
                "message.forward_chat_format 含无效的消息占位符"
            ) from error
        mark_option = message.get("mark_option", "emoji")
        if mark_option not in ("emoji", "none"):
            raise ValueError("message.mark_option 只接受 emoji 或 none")
        rules = CommandList.parse(commands, "cmd")
        admin_commands = parse_roots(
            commands.get("admin_commands", []), "cmd.admin_commands"
        )
        action_list = commands.get("custom_commands", [])
        if not isinstance(action_list, list):
            raise TypeError("cmd.custom_commands must be a list")
        actions = {}
        triggers = set()
        for action in action_list:
            if not isinstance(action, (str, dict)):
                raise TypeError("cmd.custom_commands contains an invalid action")
            command = CustomCommand.parse(action)
            try:
                root = command_root(command.command)
            except LinkError as error:
                raise ValueError(f"动作 {command.id} 的指令名无效") from error
            if matches_root(admin_commands, root):
                raise ValueError(f"动作 {command.id} 与 cmd.admin_commands 冲突")
            if command.id in actions or (
                command.trigger and command.trigger.casefold() in triggers
            ):
                raise ValueError(
                    f"cmd.custom_commands 含重复的动作或触发词：{command.id}"
                )
            actions[command.id] = command
            triggers.add(command.trigger.casefold())
        return cls(
            endpoint,
            identifiers(message, "target_sessions"),
            prefix,
            flag(message, "forward_chat_to_game")
            if role == "native"
            else flag(message, "forward_chat_to_astrbot", True),
            flag(message, "forward_join_leave_to_astrbot"),
            role != "native" and flag(row, "enable_ai_chat", True),
            chat_format,
            mark_option,
            flag(commands, "enabled"),
            rules,
            admin_commands,
            identifiers(commands, "admin_umos"),
            actions,
        )
