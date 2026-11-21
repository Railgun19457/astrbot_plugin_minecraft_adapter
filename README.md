# AstrBot Minecraft 适配器

连接 Minecraft 服务器和 AstrBot，实现消息互通和服务器管理功能。

![:name](https://count.getloli.com/@astrbot_plugin_minecraft_adapter?name=astrbot_plugin_minecraft_adapter&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

> [!note]
> 孪生项目:[AstrBot Adapter](https://github.com/railgun19457/AstrBotAdapter)
> 配套 Minecraft 端插件提供 Version 3 安全 WebSocket 协议，支持 Bukkit、Paper、Folia 和 Velocity。

## 主要功能
- 群服互通，支持将mc服务器与其他连接到AstrBot的平台互通消息
- 服务器管理，支持服务器状态查询，远程指令执行等
- AI聊天，支持游戏内和AstrBot聊天

## 命令
- `mc help` - 显示帮助信息和自定义指令列表
- `mc status` - 查看服务器状态
- `mc list` - 查看在线玩家列表
- `mc player <玩家ID>` - 查看玩家详细信息
- `mc cmd <指令>` - 远程执行服务器指令
- `mc bind <游戏ID>` - 绑定你的游戏ID
- `mc unbind` - 解除绑定

当前会话关联多个服务器时，需要区分服务器的指令会显示服务器列表，发送编号选择目标服务器

## 配置说明

> 支持添加多服务器，添加的每个服务器将作为独立的平台适配器处理
> 以下说明仅包含部分重要配置
 
### 服务器连接信息
  - 服务器ID - 用于区分不同服务器
  - **安全 WebSocket 地址** - 填写 `wss://主机/astrbot`。仅 `localhost`、`127.0.0.1` 和 `[::1]` 可使用 `ws://`；远程连接必须经 TLS 反向代理或认证 VPN。
  - **目标服务端角色** - 独立服选择 `standalone`，Velocity 入口选择 `velocity`。
  - **首次配对** - 在 Java 端创建一次性配对码后，将 `pairing_id` 与 `pairing_code` 一起填入。首次认证使用 SRP-6a；成功后插件保存双方 Ed25519 公钥绑定，后续自动进行双向签名认证。
  - **配对后处理** - 成功后插件只在内存中清除配对码；请从持久化配置中手动删除 `pairing_id` 与 `pairing_code`。若服务端密钥变更或需更换目标端点，删除该端点的本地 trust record 后再重新配对；不要删除整个插件数据目录，以免无意更换 AstrBot 身份。
  - **版本与能力** - Java 端必须支持协议 `astrbot-minecraft` v1，并至少声明 `core.v1`、`auth.ed25519.v1` 和 `auth.srp6a-pairing.v1`；聊天、玩家、命令与 AI 功能还分别需要其对应的 `*.v1` capability。
  - 不再支持旧版地址、端口、Token、REST API 或 WebSocket query token 配置。
### 消息转发配置
  - 聊天消息格式为，消息发送到其他平台时（如QQ），使用的格式，支持占位符`{player}`和`{message}`，代表发送者游戏ID和消息内容
  - **目标会话** 消息转发和消息监听的目标会话，使用会话umo区分，可通过sid指令获取，**这个配置标志着MC服务器和群聊的互相绑定关系**，群聊中获取mc服务器信息和执行指令等，需要服务器和会话绑定
  - 转发前缀 - 带有该前缀的消息将转发到绑定的服务器中
  - 转发提醒方式 - 支持 文本提醒/贴表情/不提醒
### 指令相关配置
  - 指令 黑/白名单 -用于保障服务器安全，防止恶意指令影响服务器
  - 绑定功能，用户绑定后，提供占位符`{sender}`用于自定义指令
  - 自定义指令
    - 使用`<<>>`分隔，左侧为自定义指令，右侧为实际在服务器中执行的指令
    - 支持`{sender}`占位符，会自动替换为发送指令的用户绑定的游戏ID
    - 支持自定义参数占位符，格式为`<&xxx&>`,左右一致即可自动替换，参数之间使用空格分隔
    - 例 `tp <&X&> <&y&> <&z&><<>>tp {sender} <&X&> <&y&> <&z&>`
      - 其中`<&X&>` `<&y&>` `<&z&>`为三个自定义参数占位符，在`<<>>`的左右都要有
      - `{sender}`会在执行时替换为发送者的游戏ID
      - 假设用户A绑定了游戏ID `Misaka`，并在群聊中发送`tp 114 514 1919`,实际执行的指令为`tp Misaka 114 514 1919`
      - 自定义参数将用户输入的坐标参数传递到了实际指令中，{sender}参数则提供了tp的游戏ID
  
## 更新日志
### v3.0.0
- 迁移至 Version 3 Frame 协议；移除 Token、REST API 和 URL query 认证。
- 首次连接使用 SRP-6a 配对码认证，后续使用绑定的 Ed25519 密钥双向认证。
- 默认要求远程连接使用 `wss://`，并使用单一认证 WebSocket 承载消息、状态查询和命令。

### v2.0.2 (2026-2-23)
- 将官方t2i改为本地pillow渲染
- 更新渲染模板
- 优化多服务器查询和渲染逻辑

### v2.0.1 (2026-2-15)
- 重构后首个正式版，对应mc端版本 v2.0.5
- 适配ws/http端口共用
- 添加服务器信息图片渲染功能
- 添加转发消息提醒模式选项
- 添加指令黑白名单功能
- 添加自定义指令，支持自定义占位符
- 添加用户绑定功能，用于自定义指令
- 适配多服务器配置
- 优化伪消息平台
### v2.0.0-beta (2026-2-2)
- 完全重写，适配全新2.0版本mc服务端插件

### v1.3.0 (2025-11-12)
- 添加AI对话功能
### v1.2.0 (2025-11-12)
- 进行两次重构
- 修复了数个bug
- 添加快捷转发功能

### v1.0.0 (2025-11-09)
- 首次发布
- 支持 WebSocket 实时通信
- 支持 REST API 查询
- 实现基本的消息转发和指令执行功能
- 支持自动重连

## 许可证

MIT License
