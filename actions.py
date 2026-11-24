"""Validated Minecraft command lists and shortcuts."""

import re
from dataclasses import dataclass

from .link import LinkError, node_id

PLAYER_NAME = re.compile(r"[A-Za-z0-9_.]{1,32}\Z")
_FIELD = re.compile(
    r"\{([A-Za-z_][A-Za-z0-9_]{0,31})\}|<&([A-Za-z_][A-Za-z0-9_]{0,31})&>"
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,31}\Z")
_CHOICE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,63}\Z")
_TOKEN = re.compile(r"[-A-Za-z0-9_.:+~^/]{1,64}\Z")
_INTEGER = re.compile(r"-?[0-9]{1,10}\Z")
_COMMAND_ROOT = re.compile(r"[a-z0-9_.-]+(?::[a-z0-9_.-]+)?\Z")


def command_root(command: str) -> str:
    root = command.strip().removeprefix("/").partition(" ")[0].lower()
    if not _COMMAND_ROOT.fullmatch(root) or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 for char in command
    ):
        raise LinkError("bad_request", "请输入有效的单行 Minecraft 指令")
    return root


def matches_root(roots: frozenset[str], root: str) -> bool:
    return root in roots or root.partition(":")[2] in roots


@dataclass(frozen=True)
class CommandList:
    mode: str
    roots: frozenset[str]

    @classmethod
    def parse(cls, section: dict, path: str) -> "CommandList":
        mode = section.get("cmd_white_black_list", "white")
        if mode not in ("white", "black"):
            raise ValueError(f"{path}.cmd_white_black_list 只接受 white 或 black")
        return cls(mode, parse_roots(section.get("cmd_list", []), f"{path}.cmd_list"))

    def allows(self, root: str) -> bool:
        listed = matches_root(self.roots, root)
        return listed if self.mode == "white" else not listed


def parse_roots(values: object, path: str) -> frozenset[str]:
    if not isinstance(values, list):
        raise TypeError(f"{path} 必须为指令名列表")
    roots = set()
    for value in values:
        root = value.strip().removeprefix("/").lower() if isinstance(value, str) else ""
        if not _COMMAND_ROOT.fullmatch(root):
            raise ValueError(f"{path} 只接受指令名，例如 list 或 minecraft:time")
        roots.add(root)
    return frozenset(roots)


def field_name(field: re.Match[str]) -> str:
    return field[1] or field[2]


@dataclass(frozen=True)
class ActionParameter:
    kind: str
    description: str
    choices: tuple[str, ...]
    minimum: int
    maximum: int

    @classmethod
    def parse(cls, name: str, row: dict) -> "ActionParameter":
        kind = row.get("kind", row.get("__template_key", "choice"))
        description = row.get("description", "")
        if not isinstance(description, str) or kind not in (
            "choice",
            "integer",
            "player",
        ):
            raise ValueError(f"参数 {name} 的类型或说明无效")
        if kind == "choice":
            choices = row.get("choices", [])
            if (
                not isinstance(choices, list)
                or not 1 <= len(choices) <= 32
                or any(
                    not isinstance(choice, str) or not _CHOICE.fullmatch(choice)
                    for choice in choices
                )
            ):
                raise ValueError(f"参数 {name} 需要填写 1 至 32 个单词选项")
            return cls(kind, description, tuple(dict.fromkeys(choices)), 0, 0)
        if kind == "integer":
            minimum, maximum = row.get("min", 0), row.get("max", 100)
            if (
                type(minimum) is not int
                or type(maximum) is not int
                or not (-2_147_483_648 <= minimum <= maximum <= 2_147_483_647)
            ):
                raise ValueError(f"参数 {name} 的整数范围无效")
            return cls(kind, description, (), minimum, maximum)
        return cls(kind, description, (), 0, 0)

    def validate(self, name: str, value: object) -> str:
        if self.kind == "token":
            if isinstance(value, str) and _TOKEN.fullmatch(value):
                return value
            raise LinkError(
                "invalid_parameters", f"{name} 需要一个参数，可填玩家名、坐标或资源 ID"
            )
        if self.kind == "choice":
            if isinstance(value, str) and value in self.choices:
                return value
            raise LinkError(
                "invalid_parameters",
                f"{name} 必须是以下选项之一：{'、'.join(self.choices)}",
            )
        if self.kind == "integer":
            if isinstance(value, str) and _INTEGER.fullmatch(value):
                value = int(value)
            if type(value) is int and self.minimum <= value <= self.maximum:
                return str(value)
            raise LinkError(
                "invalid_parameters",
                f"{name} 必须是 {self.minimum} 至 {self.maximum} 之间的整数",
            )
        if isinstance(value, str) and PLAYER_NAME.fullmatch(value):
            return value
        raise LinkError("invalid_parameters", f"{name} 必须是在线玩家名称")


@dataclass(frozen=True)
class CustomCommand:
    id: str
    trigger: str
    command: str
    description: str
    parameters: dict[str, ActionParameter]
    needs_binding: bool
    trigger_tokens: tuple[str, ...]

    @classmethod
    def parse(cls, row: str | dict) -> "CustomCommand":
        if isinstance(row, str) or "mapping" in row:
            mapping = row if isinstance(row, str) else row["mapping"]
            if not isinstance(mapping, str):
                raise ValueError("自定义指令映射必须是文本")
            trigger, sep, command = mapping.partition("<<>>")
            tokens = trigger.split()
            if (
                not sep
                or not tokens
                or _FIELD.search(tokens[0])
                or tokens[0].startswith("@")
            ):
                raise ValueError("自定义指令请使用 触发词 <&参数&><<>>服务端指令")
            parameters = {
                field_name(field): ActionParameter("token", "", (), 0, 0)
                for field in _FIELD.finditer(trigger)
            }
            # The leading literal also identifies the shortcut in /mc action.
            return cls.create(tokens[0].casefold(), trigger, command, "", parameters)

        key = node_id(row.get("id", ""))
        trigger, command, description = (
            row.get("trigger", ""),
            row.get("command", ""),
            row.get("description", ""),
        )
        parameters = {}
        rows = row.get("parameters", [])
        if not isinstance(rows, list) or len(rows) > 8:
            raise ValueError(f"动作 {key} 最多可配置 8 个参数")
        for parameter in rows:
            if not isinstance(parameter, dict):
                raise TypeError(f"动作 {key} 含无效的参数配置")
            name = parameter.get("name")
            if (
                not isinstance(name, str)
                or not _NAME.fullmatch(name)
                or name in parameters
                or name == "sender"
            ):
                raise ValueError(f"动作 {key} 含无效或重复的参数名")
            parameters[name] = ActionParameter.parse(name, parameter)
        return cls.create(key, trigger, command, description, parameters)

    @classmethod
    def create(
        cls,
        key: str,
        trigger: str,
        command: str,
        description: str,
        parameters: dict[str, ActionParameter],
    ) -> "CustomCommand":
        if (
            any(not isinstance(value, str) for value in (trigger, command, description))
            or not command.strip()
        ):
            raise ValueError(f"动作 {key} 的指令或说明无效")
        try:
            command_root(command.strip())
        except LinkError as error:
            raise ValueError(f"动作 {key} 需要固定的指令名和单行指令模板") from error
        command = command.strip().removeprefix("/")
        fields = {field_name(field) for field in _FIELD.finditer(command)}
        if len(parameters) > 8 or "sender" in parameters:
            raise ValueError(f"动作 {key} 最多可配置 8 个参数，sender 由绑定提供")
        if fields - {"sender"} != parameters.keys():
            raise ValueError(f"动作 {key} 的占位符与参数声明不一致")

        tokens = trigger.split()
        trigger_fields = []
        for index, token in enumerate(tokens):
            if _FIELD.search(token):
                field = _FIELD.fullmatch(token)
                if index == 0 or field is None:
                    raise ValueError(f"动作 {key} 的触发词参数须用空格分隔")
                trigger_fields.append(field_name(field))
        if trigger_fields:
            if (
                len(trigger_fields) != len(parameters)
                or set(trigger_fields) != parameters.keys()
            ):
                raise ValueError(f"动作 {key} 的触发词与参数声明不一致")
        elif tokens:
            tokens.extend(f"<&{name}&>" for name in parameters)
        return cls(
            key,
            " ".join(tokens),
            command,
            description.strip(),
            parameters,
            "sender" in fields,
            tuple(tokens),
        )

    def match(self, words: list[str]) -> dict[str, str] | None:
        if not self.trigger_tokens or len(words) != len(self.trigger_tokens):
            return None
        values = {}
        for expected, value in zip(self.trigger_tokens, words, strict=True):
            field = _FIELD.fullmatch(expected)
            if field is not None:
                values[field_name(field)] = value
            elif expected.casefold() != value.casefold():
                return None
        return values

    def validate(self, values: dict) -> dict[str, str]:
        if not isinstance(values, dict) or set(values) != self.parameters.keys():
            requested = "、".join(self.parameters) or "无"
            raise LinkError(
                "invalid_parameters", f"请提供且仅提供动作声明的参数：{requested}"
            )
        return {
            name: parameter.validate(name, values[name])
            for name, parameter in self.parameters.items()
        }

    def render(self, values: dict[str, str]) -> str:
        return _FIELD.sub(lambda field: values[field_name(field)], self.command)
