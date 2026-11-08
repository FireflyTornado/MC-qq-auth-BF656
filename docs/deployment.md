# 构建与部署入口

## Python 3.12 环境

在仓库根目录创建虚拟环境。Ubuntu 24.04 可使用系统的 Python 3.12，若缺少 `venv` 模块则安装系统的 `python3.12-venv` 包。

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip check
```

Windows 可以用 `py -3.12 -m venv .venv` 创建环境，再通过 `.venv\Scripts\python.exe` 执行同样的 pip、迁移和测试命令。生产运行和本地测试使用同一份依赖锁。Python 服务运行需要 Python、SQLite 和已安装的运行依赖，构建 Minecraft 插件需要独立的 Java 工具链。

## 本地配置与数据库

| 组件 | 模板 | 本机配置位置 | 常用工作目录 |
| --- | --- | --- | --- |
| NoneBot 与 FastAPI | `bot/.env.example` | `bot/.env` | `bot/` |
| NapCat Compose | `napcat/.env.example` | `napcat/.env` | `napcat/` |

在仓库根目录复制 Bot 配置模板，已有 `.env` 时沿用当前文件。

```sh
cp -n bot/.env.example bot/.env
```

替换占位值。将 `TARGET_GROUP_ID=0` 改为目标群的数字编号，保留 `ONEBOT_WS_URLS` 的 JSON 数组格式。OneBot Token 要与 NapCat 的实际配置一致，API 与 Notify 必须使用不同的 Token。进程环境变量优先于 `bot/.env`，显式空值也会覆盖文件值。Bot 和 API 在启动时读取配置，通知客户端在发送时读取。更换凭据时应同步更新调用端并重启相应服务。

在仓库根目录运行 `python bot/migrate.py` 初始化或升级默认数据库 `bot/data/auth.db`。`python bot/migrate.py --db <路径>` 可为迁移命令指定其他目标。升级已有库前先做一致性备份，迁移报错时保留原库并检查原因。

数据库就绪后，可在仓库根目录用 `python bot/bot.py` 和 `python -m uvicorn --app-dir bot mc_api:app --host 127.0.0.1 --port 18765` 分别启动两个进程。在 `bot/` 中也可继续使用 `python bot.py` 和 `python -m uvicorn mc_api:app --host 127.0.0.1 --port 18765`。

通知默认发送到 `http://127.0.0.1:18765/internal/mc/notify`。可用 `MC_NOTIFY_URL` 覆盖为可信网络上的直连 HTTP(S) 地址。通知请求绕过环境代理并拒绝重定向。

## NapCat Compose

在 `napcat/` 下将 `.env.example` 复制为 `.env`，填写自己的 WebUI Token。已有部署应保留实际 `.env`，并核对 `data/qq`、`data/config`、`data/plugins` 的原挂载位置，以便继续使用现有会话和配置。

```sh
cd napcat
cp -n .env.example .env
# Fill WEBUI_TOKEN before starting NapCat.
docker compose up -d napcat
```

Compose 将 WebUI 映射到本机 `6099`，将容器内的 OneBot WebSocket `3001` 映射到本机 `13001`。两个宿主机端口均绑定回环地址。在 NapCat 中配置 OneBot 正向 WebSocket 服务及其 Token，Bot 的默认模板连接 `ws://127.0.0.1:13001/`。

## HTTPS/WSS 反向代理边界

FastAPI 按上面的命令监听回环地址。异地游戏主机接入时，只转发所需的 `/internal/mc/snapshot` 和 `/internal/mc/ws`，启用 TLS，并尽可能限制到受信来源。向上游保留 Authorization 头，避免将其写入代理日志。WebSocket 转发需支持 HTTP/1.1 Upgrade 并设置合适的空闲超时，快照应遵循 `Cache-Control: no-store`。

`/internal/mc/notify` 应从公网路由中排除，`/internal/health` 留给本机或受信探活。WebUI 和 OneBot 端口同样保留在回环地址。完整协议见 [API 文档](api.md)。

## 本地测试和锁文件维护

开发环境按同一条 `pip install -r requirements.txt` 命令安装依赖。测试使用标准库 `unittest`、`sqlite3` 和临时文件，HTTP/WebSocket 测试直接调用 ASGI 应用。当前无需单独的开发锁、TestClient、httpx 或 pytest。

```sh
python -m unittest discover -s tests -v
```

[CI 工作流](../.github/workflows/ci.yml) 在 Ubuntu 上检查锁定依赖、Python 语法和回归测试，并另用 Java 17 构建和检查 JAR。Mohist 加载需在独立游戏测试服验证。

`requirements.in` 声明经过验证的直接依赖。`requirements.txt` 固定直接和间接依赖的版本，`colorama` 和 `win32-setctime` 带 Windows 条件。更新时先按需调整 `requirements.in`，再在干净的 Python 3.12 环境中解析。

```sh
resolve_report="$(mktemp)"
python -m pip install --dry-run --ignore-installed --report "$resolve_report" -r requirements.in
```

将报告中 `install` 记录的包名和版本整理为按包名排序的 `包名==版本` 条目，保留 `nonebot2[websockets]` 和平台条件。核对 Windows 与 Linux 的依赖差异，再从新锁文件安装，执行 `pip check` 和完整测试。解析报告只用于生成锁文件，放在临时位置。当前锁固定版本，软件包文件仍由包仓库提供。

## Minecraft 插件构建

使用 JDK 17 和 Maven。已验证 Maven 3.8.7，运行 `mvn -version` 可确认实际使用的 Java 版本。

```sh
cd minecraft-plugin
mvn -B -ntp verify
```

产物为 `minecraft-plugin/target/MCQQAuth-0.1.0.jar`。Spigot API 1.20.1 以 `provided` 作用域参与编译，由兼容的服务端运行时提供。插件默认 `OBSERVE`，当前只显示模式和状态。

Spigot API 使用 `1.20.1-R0.1-SNAPSHOT`，本次解析到 `1.20.1-R0.1-20230921.163938-66`。仓库可更新或清理快照，JAR 打包时间也会影响文件字节。若发布需要逐字节复现，应固定已验证的快照构件及其校验值、构建工具和归档时间，并在独立环境中重建比对。
