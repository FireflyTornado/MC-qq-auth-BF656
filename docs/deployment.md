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

群成员对账在 OneBot 连接成功后自动执行。在 Bot 运行配置中填写 NoneBot 的 `SUPERUSERS` JSON 数组后，配置的超级用户可在目标群使用 `/授权对账` 或 `/reconcile` 触发对账，并用 `/信息` 或 `/info` 查看状态图。群详情交叉校验依赖当前 Compose 固定的 NapCat 所提供的 `get_group_detail_info`。名单请求失败、为空、两次名单不一致或人数校验失败时，对账跳过撤权。

`/认证测试` 和 `/ping` 访问本机 `127.0.0.1:18765/internal/health`，返回状态码与耗时。超级用户触发 `/info` 后，目标群会看到当前 Bot 的完整 QQ 号、昵称、头像以及服务、资源和授权状态。页脚只显示 Bot 服务器的当地日期、时间及 UTC 偏移。磁盘优先统计 `/opt`，缺失时统计根目录。

可在 `bot/.env` 设置 `MC_STATUS_ADDRESS=play.example.org:25565`，使用 Minecraft Java 服务器列表探测游戏服，并显示服务器返回的在线人数。留空时显示「未探测」。服务状态图上的 WSS 来自本机 API 的授权连接数，可独立显示授权通道是否连接。

昵称来自 OneBot 的当前登录信息。Bot 通过 HTTPS 从 `q1.qlogo.cn` 按自身 QQ 号获取头像，成功缓存 30 分钟，失败缓存 1 分钟。下载不可用时使用内置机器人图标。SQLite 状态和有效绑定数来自同一次只读授权查询。FastAPI 返回 HTTP 200 时亮绿灯，非 200 响应和超时亮黄灯，连接失败亮红灯。

### 机器人文案与群通知

Bot 在启动时读取同目录的 UTF-8 文件 `messages.yml`。在 Ubuntu 首次升级到 3.4.2.2 时，先更新源码和依赖，再创建配置。以下命令适用于以 root 运行的 [Bot unit](../deploy/systemd/mc-qq-bot.service)。创建时使用仅所有者可读写的权限，文件已存在时保留原件。

```bash
cd /opt/mc-qq-auth
sudo sh -c 'umask 077; set -C; cat bot/messages.example.yml > bot/messages.yml'
sudoedit /opt/mc-qq-auth/bot/messages.yml
```

实际运行用户若与模板不同，将文件的读取权限授予该用户，并仅让负责维护的系统用户修改。需要完整示例时查看 [messages.example.yml](../bot/messages.example.yml)，也可以在 `messages.yml` 中只填写要覆盖的字段。例如，以下配置启用自定义欢迎语，关闭主动退群提示，保留被移出群的提示。

```yaml
events:
  join:
    enabled: true
    message: |-
      欢迎 {member} 加入服务器社区！
      请发 /bind 游戏ID 完成绑定。
  leave:
    enabled: false
  kick:
    enabled: true
commands:
  bind:
    success: "已绑定 Minecraft ID：{mc_name}"
  help:
    message: "发送 /bind 游戏ID 绑定账号，发送 /binding 查询。"
info:
  title: "社区授权状态"
  labels:
    bindings: "有效绑定"
```

三个开关分别控制入群、主动退群和被移出群的消息，默认均为 `false`。现有配置显式设置为 `true` 的通知继续开启。关闭任意开关后，成员事件记录、退群撤权和授权同步照常执行。离群通知会按撤权结果选择 `revoked`、`no_binding` 或 `failed`。

中文和英文别名共用 `commands` 下的字段。使用 `|-` 编写多行回复，使用 `{{` 和 `}}` 显示字面花括号。每个字段可用的占位符见模板注释。`{member}` 在欢迎语中生成对新成员的 QQ @。其他动态值和配置文案均作为纯文本发送。指令权限继续由目标群和 `SUPERUSERS` 控制。

每条文案最多 1000 字符，配置文件最多 64 KiB。缺失字段、空文案、类型错误、非法占位符和过长文案使用该字段的内置默认值。请用布尔值 `true` 或 `false` 设置通知开关。无法解析或读取文件时使用全部内置默认值，群通知因此默认关闭，日志只记录错误类别和已知字段。状态图使用 `info.title` 和仍在显示的 `info.labels.*`，超宽时截断并加省略号。`info.footer` 和隐藏字段的旧标签仍可解析，便于保留已有配置。

Ubuntu 可用 `sudo apt install fonts-noto-cjk` 为状态图启用中文。字体缺失时，Bot 记录一次提示并使用内置字体和英文备用标签。

日常编辑 `messages.yml` 后，只需重启 Bot。

```bash
sudo systemctl restart mc-qq-bot.service
```

`bot/messages.yml` 是 Git 忽略的生产配置。后续升级沿用该文件，可在升级前将其备份到私有目录。安全升级工具会拒绝与现有本地文件冲突的传入路径。首次升级前已创建配置的环境，应先将该文件移到私有备份目录，升级完成后放回。后续模板新增字段会由内置默认值补齐。

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

升级到阶段 3.4.2 时，按维护流程重启 `mc-qq-api.service` 和 `mc-qq-bot.service`，先确认 API 健康再启动 Bot。现有 SQLite 结构和依赖锁可直接使用。游戏端继续使用现有插件，API 重启后插件会自动重连并读取最新快照。

升级到阶段 3.4.2.1 时，用更新后的 `requirements.txt` 安装 Pillow 和 psutil，再重启 Bot 以加载新指令。手工更新时，API 本身无需重启。现有升级工具会将依赖锁变更视为共享依赖更新，并安排重启 API 和 Bot。

升级到阶段 3.4.2.2 时，用更新后的 `requirements.txt` 安装 PyYAML。现有安全升级工具会提前下载锁定的 wheel，并按共享依赖更新流程重启 API 和 Bot。升级完成后可按 [机器人文案与群通知](#机器人文案与群通知) 创建或恢复配置。

升级到阶段 3.4.2.3 时，沿用现有 `messages.yml`。手工升级源码后重启 Bot 即可使用新版卡片，建议先确认服务器时区和中文字体。

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

### 安全升级工具

`deploy/update.sh` 用于 Ubuntu 24.04 上的人工升级。从生产工作目录执行，检查和升级分别选择一条命令。

```sh
cd /opt/mc-qq-auth
sudo bash deploy/update.sh --check
sudo bash deploy/update.sh --check --local
sudo bash deploy/update.sh --apply REVIEWED_COMMIT_SHA
```

- 默认模式与 `--check` 相同。它获取 GitHub 的 `main`，更新 Git 对象和 `FETCH_HEAD`，验证远端历史未回退或分叉后才更新 `origin/main`。它检查工作区、目标变更、配置文件和服务状态。
- `--check --local` 只使用缓存的 `origin/main`，不联网或更新 Git 引用，因此无法确认 GitHub 是否有新提交。两种检查模式都保持源码、虚拟环境、数据库和服务原样。
- `--apply` 接受完整的 40 位 SHA 或至少 7 位且能唯一解析的十六进制前缀。执行前将 `REVIEWED_COMMIT_SHA` 替换为已审核的 SHA。目标必须在本次取得的 `origin/main` 历史中，并能从当前 `HEAD` 快进。管理员须先核对目标完整 SHA 与 CI 结果，再决定是否进入维护窗口。工具不会查询或替代这项审核。

使用前确认已有 `main` 跟踪 `origin/main`，root 能访问预期 GitHub 仓库，现有 unit、回环端口、NapCat 容器名和运行目录符合本页约定。工具需要 Git、Bash、`flock`、systemd 工具、Docker CLI、`sqlite3`、curl 和 iproute2，以及已有的 `bot/.venv`。应先在隔离 Ubuntu 上演练。首次引入工具的提交用下方的手工流程安装。运行工具期间避免其他管理员并发修改 Git、运行目录或服务。`flock` 锁住仓库的 `.git` 目录，仅约束使用同一锁的升级工具进程。

Apply 按以下顺序执行。

1. 验证 Git 工作区和目标。存在未解决的 Git 操作、源码修改、非忽略的未跟踪文件或与目标冲突的忽略路径时停止。当前索引和目标都必须排除 `bot/.env`、`napcat/.env`、`bot/data/`、`napcat/data/`、`bot/.venv/`。
2. 按差异制定操作。Bot 或 API 专属代码只重启受影响服务，依赖或共享 Python 代码变更涉及两项服务，纯文档和构建变更只更新源码。systemd unit、NapCat 运行配置或 `deploy/update.sh` 有改动时，转到下方的手工流程。
3. 涉及服务时先核验现有 API、Bot 和 NapCat 的健康。依赖有变化时，先按目标锁文件从 PyPI 下载 wheel。下载失败时服务保持运行。
4. 涉及服务时先做在线 SQLite 一致性备份，再停受影响服务。需要迁移时先停 Bot 再停 API，确认两者均为 `inactive`，并在停写后再做一份备份。停写期间管理员也要暂停其他数据库写入者。
5. 用 `git merge --ff-only --no-autostash --no-overwrite-ignore` 合入已经审核的 SHA。涉及依赖时，停服期间从预下载的 wheel 离线安装并运行 `pip check`。涉及 SQLite 结构时调用项目的 `bot/migrate.py`，并检查活动库的 `quick_check` 和 `user_version`。
6. 启动受影响服务，先确认 API 的进程 PID、`127.0.0.1:18765` 监听、健康 `200` 和未授权快照 `401`，再确认 Bot 的当前运行实例在 journald 中记录了 OneBot 连接，且其进程有到 `127.0.0.1:13001` 的已建立连接。NapCat 保持运行。日志级别需保留 OneBot 的连接记录。

备份保存在仓库外的 `/opt/mc-qq-auth-backups/`。目录要求 root 所有、权限 `0700`，每份备份使用独立文件名，权限 `0600`。工具使用 SQLite `.backup`，只有非空、`quick_check` 返回 `ok` 且 `user_version` 与源一致的文件才会被报告为有效备份。中断留下的文件需重新核验后才能用于恢复。

#### 失败后的人工恢复

| 报告阶段 | 已完成的操作与下一步 |
| --- | --- |
| `preflight`、`wheel-download`、`backup` | 源码和服务保持原状。核对 Git 或运行配置，修复下载或备份错误后再试。 |
| `stop`、`offline-backup` | 可能已有服务停止，源码仍为原版本。核对 unit 状态与库状态，确认安全后可手工启动原服务。 |
| `git-update` | 相关服务已停止。检查 `git rev-parse HEAD` 和 `git status --short`，确认当前版本及本地文件，再决定后续。 |
| `dependencies` | 源码已更新，虚拟环境可能只安装了一部分。用经审核的锁文件完成安装并通过 `pip check`，或准备与旧版本配套的环境。 |
| `migration` | 源码已更新，数据库可能需要确认迁移结果。保持停写，核对活动库和最近一份有效备份的结构版本与完整性。 |
| `restart`、`health` | 工具会尝试停止本次尝试启动的服务。核对实际状态、有效 unit、端口和本机日志，再决定何时启动。 |

工具不会回退 Git 或恢复数据库。回到旧源码前应确认它能读取当前数据库结构。迁移可能没有逆向步骤，恢复旧备份可能丢失随后产生的绑定。需要恢复时先停写，保全当前库及最后一份有效备份，再由管理员选择恢复点。重复 Apply 同一个已落盘的 SHA 会停止，需按实际失败阶段恢复。

### NapCat 容器状态说明

升级工具可能输出 `NapCat running none.`。

其中 `running` 表示 Docker 容器处于运行状态，而 `none` 表示该容器没有配置 Docker Healthcheck，并不代表 NapCat 未启动。

对于当前部署，`running none` 属于可接受的容器状态。升级工具还会在涉及服务更新时检查 API 和 OneBot 的实际连接状态。

如需独立确认容器是否正常运行，可以执行：

```bash
docker inspect mc-qq-napcat --format '{{.State.Status}}'
```

正常情况下应输出 `running`。

### 手工升级流程

1. 在 `/opt/mc-qq-auth` 执行 `git fetch origin`。在同一个 Bash 会话中用 `previous_commit=$(git rev-parse HEAD)` 和 `target_commit=$(git rev-parse --verify 'origin/main^{commit}')` 记录两个完整 SHA。核对目标提交的变更和 GitHub CI 结果。
2. 确认当前分支为 `main`，`git status --short` 没有待处理变更，并用 `git merge-base --is-ancestor HEAD "$target_commit"` 确认可快进。`target_commit` 应设为已核对的完整 SHA。检查目标提交不会跟踪或覆盖现有私有配置和数据。下例应没有输出，其他自定义运行路径也应核对。

   ```sh
   git ls-tree -r --name-only "$target_commit" -- .env .venv bot/.env bot/.venv bot/data bot/messages.yml napcat/.env napcat/data
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

4. 停止受影响服务。更新两项服务共用的源码、依赖或数据库时，先停 Bot，再停 API。需要迁移时，同时停止其他写入者，并在停写后重新执行第 3 步，在新的私有备份目录中保存最后一次已提交的状态。保留已有 `.env`、`bot/messages.yml`、`bot/data/`、`bot/.venv/` 和 NapCat 挂载。
5. 快进到已核对的提交，并拒绝覆盖已忽略的本地文件。快进失败或当前 SHA 与审核目标不同，先查明原因。安装锁定依赖时使用生产虚拟环境，保持相关服务停止。

   ```sh
   git merge --ff-only --no-autostash --no-overwrite-ignore "$target_commit" &&
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
mvn -B -ntp clean verify
```

部署产物为 `minecraft-plugin/target/MCQQAuth-0.4.1.jar`。Spigot API 1.20.1 以 `provided` 作用域参与编译，由兼容的服务端提供。JAR 内置并隔离 Jetty、Gson 及所需的日志依赖。Maven 测试通过本机回环地址运行模拟 HTTPS/WSS 服务并生成临时证书，也会模拟 Bukkit 登录、入服、公告界面和踢出调度。

0.4.1 可直接沿用 0.4.0 或 0.3.2 的配置。服务端主动发送 Ping 时，插件继续正常响应 Pong。游戏名支持 1–16 位，授权匹配忽略大小写。

从要求游戏名为 3–16 位的版本升级时，先替换游戏服上的插件 JAR 并重启，再更新 Bot，避免旧插件收到短名后拒绝整份授权名单。若已有超过 16 位的绑定，升级前先处理这些记录。数据库校验会拒绝含有超长记录的库，插件也会拒绝含有超长名字的整份授权名单。无需迁移表结构。

Spigot API 使用 `1.20.1-R0.1-SNAPSHOT`，本次解析到 `1.20.1-R0.1-20230921.163938-66`。仓库可更新或清理快照，JAR 打包时间也会影响文件字节。若发布需要逐字节复现，应固定已验证的快照构件及其校验值、构建工具和归档时间，并在独立环境中重建比对。

### Minecraft 插件配置

首次启动会从 JAR 生成 `plugins/MCQQAuth/config.yml`，其中显式设置 `mode: OBSERVE`。升级时保留已有配置和 Token。旧配置缺少 `mode` 时继续使用 `ENFORCE`，因此升级后仍执行白名单。模式值允许大小写混用。配置文件无法读取，或顶层 `mode` 重复、无效时，插件记录错误并按未就绪的 `ENFORCE` 拒绝新登录。

| 配置 | 用途 |
| --- | --- |
| `mode` | `OBSERVE` 观察并提示，允许 MCQQAuth 判定为未授权或未知的玩家登录，也允许被撤权的玩家留在服内。`ENFORCE` 拒绝未授权和未知状态的新登录，按确认的快照执行在线撤权。 |
| `api.snapshot-url` | 默认 `https://auth.bf656.nightbugclub.cn/internal/mc/snapshot`，路径无末尾斜杠。 |
| `api.websocket-url` | 默认 `wss://auth.bf656.nightbugclub.cn/internal/mc/ws`，路径无末尾斜杠。 |
| `api.token` | 填写后端的 `MC_API_TOKEN`，两条连接均通过 Bearer 请求头鉴权。实际值只保存在面板服的运行配置中。 |
| `api.connect-timeout-ms`、`api.request-timeout-ms` | 默认 5000、8000 毫秒，分别限制连接与请求总耗时。 |
| `sync.reconnect-min-ms`、`sync.reconnect-max-ms` | 默认 1000、30000 毫秒，故障后指数退避，成功同步后恢复初始等待。 |
| `messages.unauthorized`、`messages.not-ready`、`messages.revoked` | 未授权拒绝、服务未就绪拒绝和撤销踢出提示。 |

连接使用 Java 信任库验证证书链和主机名。WSS 每隔约 15 秒发送 Ping，10 秒内缺少对应 Pong 会进入重连。Ping/Pong 只检查连接活性。插件会在启动和重连后获取完整快照，并按更高版本提醒刷新。客户端最多接收 2 MiB 的快照。若恢复数据库备份导致版本回退，运行中的插件将保持未就绪，需在维护窗口先核实恢复后的名单和版本，再重新启动游戏服。

离线模式仅按服务端收到的游戏名比对授权名单。知道已绑定游戏名的人可以冒用它，QQ 绑定无法证明连接者就是该 QQ 账号的持有人。

#### 观察模式入服公告

```yaml
mode: OBSERVE

observe:
  notice:
    enabled: true
    show-delay-ticks: 30
    cooldown-seconds: 86400
    group-number: "123456789"
    title: "社区认证提醒"
    message:
      - "你的 Minecraft 游戏 ID 尚未完成社区认证。"
      - "请加入群 {group}，发送 /绑定 {player} 完成绑定。"
    confirm-button: "我知道了，继续游戏"
```

| `observe.notice` 配置 | 默认值与限制 |
| --- | --- |
| `enabled` | 默认 `true`。设为 `false` 时仍记录观察结果。 |
| `show-delay-ticks` | 默认 30 Tick，可设为 1–1200 的整数。 |
| `cooldown-seconds` | 默认 86400 秒（24 小时），可设为 0–2592000 的整数。`0` 允许下次连接再次展示。 |
| `group-number` | 用引号包裹实际 QQ 群号。接受 5–20 位十进制数字，首位须为 1–9。缺失或无效时显示「未配置」。 |
| `title`、`confirm-button` | 分别默认为「社区认证提醒」和「我知道了，继续游戏」，各限 32 个字符。 |
| `message` | 默认说明社区认证和 QQ 群绑定步骤，接受 1–6 行，每行最多 80 个字符。 |

标题、按钮和正文支持 `{player}`（当前游戏名）和 `{group}`（群号或「未配置」）。其他占位符按原文本显示，替换后的文字按上述长度截断。类型、范围或文案无效时使用相应默认值。公告由游戏服配置控制。

Bukkit 只能在玩家成功进入游戏后打开背包界面。名单未就绪时状态为 `UNKNOWN`，等待一次同步确认。只有有效名单确认玩家未授权才会尝试打开公告。延迟期间若玩家完成绑定并同步、离线、已有其他菜单或快照失效，本次跳过。按钮和 Esc 都可关闭窗口并继续游玩。点击确认只记录本次会话的确认，绑定仍由玩家在 QQ 群完成。

安装 AuthMe 时，若当前连接还未展示公告，AuthMe 登录或注册成功后会按同样的延迟重新检查一次。公告仍遵循授权状态、冷却和其他插件的菜单规则。AuthMe 为可选集成。

同一连接最多展示一次。窗口实际打开后，按小写游戏名计算冷却。**插件重启后，内存冷却记录会重置。** `/mcqqauth` 需要 `mcqqauth.admin` 权限，可查看当前模式、授权服务状态、最近确认版本与名单人数，以及入服公告是否启用。

### 在线切换准入模式

控制台和有 `mcqqauth.admin` 权限的管理员可执行以下操作。该权限默认授予 OP。

| 指令 | 行为 |
| --- | --- |
| `/mcqqauth mode` | 查询当前模式与生效的准入策略。 |
| `/mcqqauth mode observe` | 停止 MCQQAuth 登录拦截和在线撤权。后续入服按公告配置处理，其他插件的准入策略继续生效。 |
| `/mcqqauth mode enforce` | 对新登录执行授权检查。已在线的未授权或状态未知玩家获得当前连接的豁免，已授权玩家仍受后续撤权约束。 |
| `/mcqqauth mode enforce --kick-online` | 使用就绪的授权名单检查在线玩家，清除已检查会话的豁免，踢出未授权玩家并报告人数。已处于 `ENFORCE` 时也可执行。 |

切换无需重启 Mohist，现有 HTTPS/WSS 客户端继续同步。插件先原子保存 `config.yml` 中的模式，再应用到运行中，重启时读取已保存的值。若配置缺少 `mode`，可安全编辑的旧配置会在切换到 `OBSERVE` 时补上该字段。公告、Token、端点等设置仍在启动时读取。

会话豁免在玩家退出时清除，重新登录时按当前模式验证。后续确认该玩家获得授权时也会解除豁免。进入 `ENFORCE` 会清理等待中的公告和本插件自己的公告窗口，保留冷却时间。回到 `OBSERVE` 后，新入服玩家按正常流程触发公告。

使用 `--kick-online` 时，如果保存模式前授权未就绪，原模式继续生效。保存后若同步中断或名单等待刷新，在线清理会在下一次检查时中止，已保存的模式继续生效，指令会报告已完成的人数。

需要解除 MCQQAuth 准入拦截时，执行 `/mcqqauth mode observe`。已收到拒绝的客户端需要重新连接。若文件无法原子替换、权限不足或 `mode` 的 YAML 结构无法安全编辑，指令会报告失败。修复文件及目录权限，或将顶层字段整理为单独一行的 `mode: OBSERVE` 或 `mode: ENFORCE` 后重试。文件从无效状态修复后，如需重新读取其他设置并启动授权同步，请完整重启 Mohist。

### Mohist 面板服维护窗口

1. 使用 Java 17 和 Mohist 1.20.1 的独立测试服，配置隔离的 HTTPS/WSS 模拟授权端点、测试 Token 和测试游戏名。构建 JAR，备份待升级服的旧插件与配置。
2. 停服后将 `plugins/` 中的旧版替换为 `MCQQAuth-0.4.1.jar`，只保留一个 MCQQAuth JAR。保留已有 API 地址、Token 和文件访问权限。首次安装可从 JAR 提取默认配置，填写两条授权端点和 Token。
3. **阶段 A · 观察**。显式配置 `mode: OBSERVE` 与 `observe.notice.enabled: true`，填写 QQ 群号并启动游戏服。用 `/mcqqauth` 确认模式和同步状态。用已授权、未授权、同步暂不可用三种测试状态入服，核对公告、日志及同步恢复后的表现。检查原版客户端的中文文案、Esc、确认按钮、背包操作和其他插件的登录菜单。
4. **阶段 B · 强制**。保留已授权和未授权的测试玩家在线，执行 `/mcqqauth mode enforce`。验证新登录、在线豁免、退出重连和撤权。用隔离的测试名单演练显式清理，核对踢出人数和日志。在 WSS 断开时核对清理被拒、新登录被拒及重连后的恢复。确认切换本身未产生新连接，再重启测试服核对模式保存结果。
5. 生产环境先按观察阶段核对同步和公告，再用指令切换。修改公告等其他配置后，在维护窗口完整停止并重新启动 Mohist。完整回滚时停服，移出 `MCQQAuth-0.4.1.jar`，恢复同一次备份的旧 JAR 与配置，再启动并核对 `/mcqqauth`。
