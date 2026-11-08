# MC-QQ-Auth

MC-QQ-Auth 面向 Minecraft Java 版离线社区服务器。玩家在指定 QQ 群绑定游戏 ID，服务端将绑定保存为授权名单，通过 HTTP 快照和 WebSocket 更新提醒提供给游戏端。当前 QQ 端与授权 API 已实现，游戏端准入仍在开发中。

## 功能状态

| 功能 | 状态 | 验证范围 |
| --- | --- | --- |
| QQ 绑定、查询、解绑的业务逻辑 | 已实现且已测试 | 回归测试覆盖 SQLite 写入、查询、撤销、大小写去重和授权版本更新。 |
| SQLite 初始化、迁移与授权 API | 已实现且已测试 | 回归测试使用临时数据库和合成配置，覆盖快照、Bearer 鉴权、WebSocket 消息及通知失败后的提交保留。 |
| 目标 QQ 群成员入群和退群事件记录 | 已实现，真实事件待测试 | 已注册事件处理器，仍需验证 NapCat 对真实入退群事件的投递。 |
| Minecraft 插件集成 | 开发中 | 骨架可用 JDK 17 和 Maven 构建，提供 `/mcqqauth` 状态命令和默认 `OBSERVE` 配置。实际 Mohist 面板服加载待验证。 |
| 游戏端 HTTPS/WSS 同步、重连恢复、入服拦截与在线踢出 | 尚未实现 | `OBSERVE` 当前只显示配置的模式和状态。 |
| 退群自动解绑 | 尚未实现 | 退群事件处理器目前只记录日志。 |

## 系统架构与技术栈

**QQ 群 → NapCat → NoneBot2 → SQLite → FastAPI → HTTP/WSS → Minecraft 插件（计划接入）**

NapCat、Bot 和授权 API 部署在同一台主机，Minecraft 服务端在另一台主机。Bot 写入 SQLite 后通知 FastAPI，游戏端将来通过 HTTPS 拉取完整名单，通过 WSS 得知何时刷新。系统使用 Python 3.12、NoneBot2 / OneBot v11、FastAPI / Uvicorn、SQLite 和 Docker Compose。插件使用 Java 17、Maven 和 Spigot API 1.20.1，目标服务端为 Mohist 1.20.1。详见 [架构说明](docs/architecture.md)。

## QQ 群使用方式

| 命令 | 行为 |
| --- | --- |
| `/绑定 Steve` | 将当前 QQ 账号绑定到游戏 ID `Steve`。 |
| `/我的绑定` | 查看当前绑定。 |
| `/解绑` | 解除当前绑定。 |

游戏名接受 3–16 位英文字母、数字或下划线。显示保留原有大小写，名称占用检查使用小写。

## 快速开始

在仓库根目录创建 Python 3.12 环境并运行回归测试。测试自带合成配置和临时数据库。

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

全新开发目录可初始化数据库并单独启动本机 API。下面的群号和 Token 仅为演示数据。

```sh
python bot/migrate.py
TARGET_GROUP_ID=1 MC_API_TOKEN=demo-api-token MC_NOTIFY_TOKEN=demo-notify-token \
  python -m uvicorn --app-dir bot mc_api:app --host 127.0.0.1 --port 18765
```

另开一个终端运行 `curl --fail http://127.0.0.1:18765/internal/health` 检查进程。需要接入 NapCat、配置 Bot 或编译插件时，按 [部署与构建说明](docs/deployment.md) 操作。

## 安全边界

- 当前模型将 QQ 账号与离线服玩家名关联。玩家名可以被冒用，QQ 群绑定无法证明 Minecraft 账号归属。生产服务器应保留独立的游戏登录身份认证。
- Token、QQ 登录会话、运行配置和数据库应保存在受保护的运行目录，排除在 Git 跟踪之外。
- 内部通知接口 `POST /internal/mc/notify` 仅允许本机或受信私网访问。异地主机使用的快照和 WebSocket 应经 HTTPS/WSS 代理并使用 API Token。

## 文档

- [部署与构建](docs/deployment.md)
- [系统架构与授权同步](docs/architecture.md)
- [HTTP 与 WebSocket API](docs/api.md)

## 许可证

项目的开源许可证尚未确定。
