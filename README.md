# MC-QQ-Auth

MC-QQ-Auth 面向 Minecraft Java 版离线社区服务器。玩家在指定 QQ 群绑定游戏 ID，Minecraft 插件通过 HTTPS 快照和 WSS 更新提醒获取授权名单，并据此执行游戏端准入。

## 功能状态

| 功能 | 状态 | 验证范围 |
| --- | --- | --- |
| QQ 绑定、查询、解绑的业务逻辑 | 已实现且已测试 | 回归测试覆盖 SQLite 写入、查询、撤销、大小写去重和授权版本更新。 |
| SQLite 初始化、迁移与授权 API | 已实现且已测试 | 回归测试使用临时数据库和合成配置，覆盖快照、Bearer 鉴权、WebSocket 消息及通知失败后的提交保留。 |
| 目标 QQ 群退群自动撤权与成员对账 | 已实现，本地模拟测试 | 主动退群和被移出群均会撤权，OneBot 连接后补查离线期间遗漏的事件。真实 NapCat 事件与成员列表待验证。 |
| Minecraft 插件观察、入服公告与白名单 | 已实现，本地模拟测试 | 覆盖 HTTPS/WSS 同步、重连、公告交互、登录拦截和在线踢出调度。真实 Mohist 面板服验收待进行。 |

## 系统架构与技术栈

**QQ 群 → NapCat → NoneBot2 → SQLite → FastAPI → HTTPS/WSS → Minecraft 插件**

NapCat、Bot 和授权 API 部署在同一台主机，Minecraft 服务端在另一台主机。Bot 写入 SQLite 后通知 FastAPI，游戏端通过 HTTPS 拉取完整名单，通过 WSS 得知何时刷新。系统使用 Python 3.12、NoneBot2 / OneBot v11、FastAPI / Uvicorn、SQLite 和 Docker Compose。插件使用 Java 17、Maven 和 Spigot API 1.20.1，目标服务端为 Mohist 1.20.1。详见 [架构说明](docs/architecture.md)。

## QQ 群使用方式

所有指令均在目标群使用。

| 中文 | English | 行为 |
| --- | --- | --- |
| `/绑定 Steve` | `/bind Steve` | 将当前 QQ 账号绑定到游戏 ID `Steve`。 |
| `/我的绑定` | `/binding` | 查看当前绑定。 |
| `/解绑` | `/unbind` | 解除当前绑定。 |
| `/认证测试` | `/ping` | 查看本机授权 API 的健康检查状态码和请求耗时。 |
| `/帮助` | `/help` | 查看双语指令及权限。 |
| `/授权对账` | `/reconcile` | 超级用户专用。核对群成员并撤销已离群的绑定。 |
| `/信息` | `/info` | 超级用户专用。发送 Bot、授权 API 和主机的状态图。 |

机器人回复、欢迎语、退群及移出通知、状态图标题和标签都可以在 `bot/messages.yml` 中自定义。入群、主动退群和被移出群的通知独立设置，默认关闭，授权与撤权照常执行。状态图显示当前 Bot 账号、服务状态、系统资源、有效绑定数和运行时长，页脚使用 Bot 服务器的当地时间。默认文案与可用占位符见 [配置模板](bot/messages.example.yml)，创建配置和重启步骤见 [部署说明](docs/deployment.md#机器人文案与群通知)。

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

## Minecraft 插件 0.3.0

- `OBSERVE` 同步授权名单并记录入服判断。只有成功入服且确认未授权的玩家才会收到可关闭的 QQ 绑定公告。授权服务故障和撤权均允许玩家继续游玩。
- `ENFORCE` 对新登录执行白名单检查，在服务未就绪时拒绝准入，并按已确认的新名单移出失去授权的在线玩家。

新安装的模板显式使用 `mode: OBSERVE`。从 0.2.0 升级且已有配置缺少 `mode` 时继续使用 `ENFORCE`。管理员可用 `/mcqqauth` 核对模式、同步状态及公告开关。在 `plugins/MCQQAuth/config.yml` 修改模式或设置公告后完整重启 Mohist。QQ 群号、公告文案及维护窗口流程见 [Minecraft 插件部署说明](docs/deployment.md#minecraft-插件构建)。

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
