# AstrBot Minecraft 适配器

用于连接 Minecraft 服务器的 AstrBot 插件。

`v3_2` 需要 AstrBot 4.30.0-beta.1 或以上版本，可接入 Paper、Folia、Fabric 独立服和 Velocity 群组服。

1. 在插件配置的 **MC 服务器列表** 中选择 **独立服（Paper / Folia / Fabric）**、**Velocity 群组服**或**原生直连**模板。前两种填写服务端的 `server_id`、WebSocket 地址，以及 `/astrbot pair create astrbot <client_id>` 返回的配对 ID 和配对码。同机可使用默认地址 `ws://127.0.0.1:8765/astrbot`，跨机器使用 `wss://`。
2. 将群聊或私聊会话的 UMO 添加到 `message.target_sessions`，例如 `onebot:GroupMessage:123456`。可用 `/mc help` 查看当前会话可用指令。完成首次配对后，插件会清理配置里的配对字段。
3. 如需游戏内 AI，在 AstrBot 的消息平台设置中新增并启用一个 Minecraft 平台，再为其会话配置可用的模型。服务器连接配置仍由插件内的模板管理。

适配器接入时，游戏内和外部会话均可用 `*消息` 转发。查询支持 `/mc status`、`/mc list` 和 `/mc player <玩家>`。配置根级的 `text2image` 对全部服务器生效，默认使用 Typst 卡片。关闭、渲染失败或在游戏内查询时使用纯文本。会话配置多台服务器时，可在子命令后加入 `@服务器ID` 选择目标。

Velocity 会从代理自动读取子服。用 `/mc status @network` 查询代理与子服，用 `/mc list @network/lobby` 查询指定子服。子服 ID 在绑定的入口中唯一时可简写为 `@lobby`。有多个候选时可在 60 秒内回复编号。群聊转发会送到该代理上当前连接着子服的玩家。执行代理指令时用代理 ID，执行子服指令时用 `/mc cmd @network/lobby <指令>`，且服务端需要接入子服适配器或为该子服配置 RCON。

`message.forward_chat_format` 控制游戏发到平台的格式，可用 `{player}`（玩家名）、`{player_id}` / `{uuid}`（UUID）、`{server}`（服务器或入口/子服）、`{server_id}`、`{backend}` 和 `{message}`。平台消息被至少一名在线玩家收到时，`message.mark_option=emoji` 给原消息贴表情。设为 `none` 时关闭提醒。

从 AstrBot 的插件列表启停整个插件。模板内的「启用此服务器」只控制对应的服务器连接。

状态卡使用服务端加载的 `server-icon.png`。名单和详情使用在线玩家档案的皮肤头像，AstrBot 从 Minecraft 官方纹理服务获取皮肤。图标或皮肤缺失时使用通用占位图标。
状态的内存百分比以 JVM 最大堆内存为基准，运行时间精确到分钟。在线名单显示服务端记录的玩家延迟。

`/mc bind <在线玩家>` 生成短期验证码，由该玩家在游戏里用 `/astrbot bind <验证码>` 确认。`/mc unbind` 解除绑定。

`/mc player @服务器 <玩家>` 可查询服务端保存的离线快照。代理下查询离线玩家时指定子服。管理员可用 `/mc logs @服务器 [行数] [关键词]` 查看近期日志，默认 20 行，最多 100 行。目标服务端需启用日志查询。

### 原生直连

在模板里填写 Java 服务器主机和游戏端口。游戏端口 Ping 可读到版本、MOTD、人数、容量和服务器图标。开启服务端 `enable-query=true` 后，在模板填写相同的 `query.port`，即可获取在线名字名单。名单依次使用 UDP Query、RCON 的 `list` 回显或 Ping 提供的玩家样本。Query 和 RCON 端口均可填 `0` 关闭。

使用远程指令时，在 `server.properties` 开启 `enable-rcon=true`，填好 `rcon.port` 与 `rcon.password` 并重启游戏服。在模板填写相同的端口和密码，绑定 WebChat 或其他会话 UMO，在该条目的「远程指令」开启手动执行并配置名单。模型工具继续使用根级的独立名单。RCON 返回控制台文本，通过私网或环回地址连接。

开启「转发平台消息到游戏」后，绑定会话发送 `*消息` 会通过 RCON 的 `tellraw` 发给在线玩家。进退服通知可选，每约 20 秒用 Query 或 RCON 轮询完整名单。游戏内聊天上报、游戏内 AI、账号验证、使用 `{sender}` 的快捷指令和服务端日志依赖适配器接入。

### 远程指令与模型工具

远程指令默认关闭，模型工具默认不注册。指令执行限于当前会话绑定的服务器，并经过服务端白名单检查。

- **普通指令** 开启该服务器的 `cmd.enabled` 后，绑定会话的成员可用 `/mc cmd @survival <指令>` 执行通过 `cmd.cmd_white_black_list` 和 `cmd.cmd_list` 的指令。名单填写第一段指令名，例如 `list`、`time`。白名单空列表放行零条指令，黑名单空列表采用服务端白名单。
- **自定义指令** 在 `cmd.custom_commands` 点「添加更多」逐条添加，或用「批量导入」按行粘贴。每项填写一条 `触发词 <&name&><<>>服务端指令`。成员可直接在绑定会话发送快捷词和参数。可用 `@服务器 快捷词 参数` 或 `@入口/子服 快捷词 参数` 指定目标。右侧可用 `<&name&>` 或 `{name}` 引用参数，也可嵌在物品组件内。`{sender}` 引用已绑定的游戏账号。
- **管理指令** 在 `cmd.admin_commands` 配置。AstrBot 管理员自动获得权限，也可用 `cmd.admin_umos` 授权额外会话。填写群聊 UMO 会授权该群的所有成员。管理名单优先于普通名单。
- **模型工具** 在根级 `llm_tools.enabled_tools` 选择注册项。模型指令在当前会话可访问的服务器上，按 `llm_tools.cmd_white_black_list` 和 `llm_tools.cmd_list` 执行，并经过服务端白名单。AstrBot 管理员身份与管理员 UMO 用于手动管理指令。

例如可添加下面两条，分别通过 `时间 daytime` 和 `head Steve` 触发。服务端白名单需开放 `time` 和 `give`，`head Steve` 还需要成员绑定游戏账号。用 `/mc actions @survival` 查看可用指令。

```text
时间 <&kind&><<>>time query {kind}
head <&player&><<>>give {sender} minecraft:player_head[minecraft:profile='<&player&>'] 1
```

[Minecraft 服务端插件](https://github.com/Railgun19457/AstrBotAdapter) · [Forge 相关项目](https://github.com/OMSociety/AstrBotAdapter_Forge)

[更新日志](CHANGELOG.md) · [MIT 许可证](LICENSE)
