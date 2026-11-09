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

## Ubuntu systemd 部署

[API unit](../deploy/systemd/mc-qq-api.service) 和 [Bot unit](../deploy/systemd/mc-qq-bot.service) 是按部署约定编写的候选模板。上线前核对当前安装的 unit 和 drop-in。两个服务以 `root` 运行，工作目录为 `/opt/mc-qq-auth/bot`，解释器使用 `bot/.venv/bin/python`。前文的仓库根目录虚拟环境用于本地开发，systemd 使用 `bot/.venv`。

首次准备生产虚拟环境时，可在 `/opt/mc-qq-auth` 执行以下命令。升级已有环境时按后文先停服务。

```sh
cd /opt/mc-qq-auth
sudo python3.12 -m venv bot/.venv
sudo bot/.venv/bin/python -m pip install -r requirements.txt
```

准备好 `bot/.env` 和已初始化的 SQLite 数据库后再启动服务。unit 文件采用 `root:root`、`0644`。由 root 持有的 `bot/.env` 和数据库宜使用 `0600`，`bot/data/` 宜使用 `0700`。应用从自己的目录读取 `bot/.env`。unit 的 `UMask=0077` 限制新建文件的默认权限，已有文件权限需单独核对。

Bot 通过 `Wants=` 请求一同启动 API，通过 `After=` 排在 API 的启动作业之后。API 启动失败时 Bot 仍可保存绑定，通知失败由现有机制处理。`Type=exec` 只确认解释器已执行，API 就绪需检查本机端口和健康接口。NapCat 仍由 Compose 独立管理。

### 安装与验证 unit

先查看实际加载的文件和覆盖项的位置。在本机对照现有内容，保留必要的覆盖配置。

```sh
sudo systemctl show -p FragmentPath -p DropInPaths mc-qq-api.service mc-qq-bot.service
```

在维护窗口由管理员执行下例。先写入与安装目录同一文件系统的临时目录，检查非空和语法，再逐个原子替换。两份文件都安装后才重新加载 systemd 配置。

```sh
sudo bash <<'SH'
set -euo pipefail

source_dir=/opt/mc-qq-auth/deploy/systemd
unit_dir=/etc/systemd/system
units=(mc-qq-api.service mc-qq-bot.service)
test -x /opt/mc-qq-auth/bot/.venv/bin/python

stage=$(mktemp -d "$unit_dir/.mc-qq-auth.XXXXXX")
trap 'rm -f -- "$stage/mc-qq-api.service" "$stage/mc-qq-bot.service"; rmdir -- "$stage"' EXIT

for unit in "${units[@]}"; do
    destination="$unit_dir/$unit"
    if [[ -L "$destination" || ( -e "$destination" && ! -f "$destination" ) ]]; then
        printf 'Review existing unit path before installing: %s\n' "$destination" >&2
        exit 1
    fi
    test -s "$source_dir/$unit"
    install -o root -g root -m 0644 -- "$source_dir/$unit" "$stage/$unit"
    test -s "$stage/$unit"
done

systemd-analyze verify --recursive-errors=no "$stage/mc-qq-api.service" "$stage/mc-qq-bot.service"

backup=$(mktemp -d /var/backups/mc-qq-auth-units.XXXXXX)
for unit in "${units[@]}"; do
    if [[ -s "$unit_dir/$unit" ]]; then
        install -o root -g root -m 0600 -- "$unit_dir/$unit" "$backup/$unit"
    fi
done
printf 'Previous unit backup directory: %s\n' "$backup"

for unit in "${units[@]}"; do
    mv -fT -- "$stage/$unit" "$unit_dir/$unit"
done
systemctl daemon-reload
SH
```

`daemon-reload` 更新 unit 定义，运行中的进程要到下次启动或重启才使用新定义。若遇到 0 字节 unit 或写入中断，先用 `sudo test -s /etc/systemd/system/mc-qq-api.service` 和 Bot 对应路径检查，再查看 `systemctl show` 的 `LoadState`、`FragmentPath` 和 `DropInPaths`。在文件齐全前暂停 reload 和 restart。用可信的非空备份或仓库模板重新做临时验证与替换。若两份文件只替换了一份，先恢复或完成另一份，再执行 `daemon-reload`。符号链接和手动屏蔽状态由管理员先核对。

### 服务管理与健康检查

按需要选择启动、停止或重启。首次上线先启动 API，检查健康后再启动 Bot。

```sh
sudo systemctl start mc-qq-api.service
sudo systemctl start mc-qq-bot.service

sudo systemctl stop mc-qq-bot.service
sudo systemctl stop mc-qq-api.service

sudo systemctl restart mc-qq-api.service mc-qq-bot.service
```

两个服务由 systemd 在异常退出 5 秒后尝试重启。管理员停止服务时，systemd 先发送 SIGTERM，若 30 秒内仍未退出则强制终止。开机自启由管理员显式控制，`enable` 和 `disable` 本身不启动或停止进程。若只取消 API 自启，已启用的 Bot 仍可在启动时拉起 API。

```sh
sudo systemctl enable mc-qq-api.service mc-qq-bot.service
sudo systemctl disable mc-qq-api.service mc-qq-bot.service
```

```sh
sudo systemctl status mc-qq-api.service mc-qq-bot.service --no-pager
sudo journalctl -u mc-qq-api.service -u mc-qq-bot.service -n 100 --no-pager
sudo journalctl -u mc-qq-api.service -u mc-qq-bot.service -f
```

确认 `18765` 只监听 `127.0.0.1`，健康接口返回 `{"status":"ok"}`，未授权快照返回 `401`。健康接口只检查 API 进程，数据库完整性另行核验。

```sh
sudo ss -ltnp 'sport = :18765'
curl -fsS http://127.0.0.1:18765/internal/health
test "$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:18765/internal/mc/snapshot)" = 401
```

### 手工升级流程

1. 在 `/opt/mc-qq-auth` 执行 `git fetch origin`。在同一个 Bash 会话中用 `previous_commit=$(git rev-parse HEAD)` 和 `target_commit=$(git rev-parse --verify 'origin/main^{commit}')` 记录两个完整 SHA。核对目标提交的变更和 GitHub CI 结果。
2. 确认当前分支为 `main`，`git status --short` 没有待处理变更，并用 `git merge-base --is-ancestor HEAD "$target_commit"` 确认可快进。`target_commit` 应设为已核对的完整 SHA。检查目标提交不会跟踪或覆盖现有私有配置和数据。下例应没有输出，其他自定义运行路径也应核对。

   ```sh
   git ls-tree -r --name-only "$target_commit" -- .env .venv bot/.env bot/.venv bot/data napcat/.env napcat/data
   ```

3. 用 SQLite `.backup` 做一致性快照。以下命令在同一个 Bash 会话中执行，要求已安装 `sqlite3` CLI。遇到任一步失败都应停在备份阶段。

   ```sh
   db=/opt/mc-qq-auth/bot/data/auth.db
   sudo test -s "$db" &&
     backup_dir=$(sudo mktemp -d /var/backups/mc-qq-auth-db.XXXXXX) &&
     backup_db="$backup_dir/auth.db" &&
     sudo sqlite3 -readonly -batch -bail "$db" ".backup '$backup_db'" &&
     sudo test -s "$backup_db" &&
     test "$(sudo sqlite3 -readonly -batch -bail "$backup_db" 'PRAGMA integrity_check;')" = ok
   ```

4. 停止受影响服务。更新两项服务共用的源码、依赖或数据库时，先停 Bot，再停 API。需要迁移时，同时停止其他写入者，并在停写后重新执行第 3 步，在新的私有备份目录中保存最后一次已提交的状态。保留已有 `.env`、`bot/data/`、`bot/.venv/` 和 NapCat 挂载。
5. 拉取已核对的提交。快进失败或当前 SHA 与审核目标不同，先查明原因。安装锁定依赖时使用生产虚拟环境，保持相关服务停止。

   ```sh
   git pull --ff-only origin "$target_commit" &&
     test "$(git rev-parse HEAD)" = "$target_commit" &&
     sudo /opt/mc-qq-auth/bot/.venv/bin/python -m pip install -r requirements.txt
   ```

6. 如需迁移，先确认停写后的备份非空且完整性检查通过，再运行 `sudo /opt/mc-qq-auth/bot/.venv/bin/python bot/migrate.py`。迁移成功后确认活动数据库非空，并用 `sudo sqlite3 -readonly -batch -bail "$db" 'PRAGMA integrity_check;'` 验证返回 `ok`。
7. 用生产虚拟环境执行 `python -m pip check` 和 `python -m unittest discover -s tests -v`。测试使用临时 SQLite 和合成配置。若 unit 有变更，按安装步骤验证并重新加载。
8. 先启动 API，核对回环端口、健康与未授权响应，再启动 Bot 并检查两项服务的日志。若安装、迁移、测试或健康检查失败，停止已启动的受影响服务，核对上一版本的源码和依赖以及最后一份有效备份，再决定恢复步骤。

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
