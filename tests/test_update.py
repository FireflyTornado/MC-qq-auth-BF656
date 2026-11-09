"""Exercise the updater against real local Git history and simulated services."""

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "deploy" / "update.sh"
ORIGIN = "https://github.com/FireflyTornado/MC-qq-auth-BF656.git"
API = "mc-qq-api.service"
BOT = "mc-qq-bot.service"


def local_path(value):
    root = Path(os.environ["UPDATE_FIXTURE"])
    shell_root = os.environ["UPDATE_FIXTURE_SHELL"]
    # MSYS may rewrite environment paths for native Python but leave paths
    # embedded in a SQLite dot-command untouched.
    for prefix in (shell_root, "/tmp/" + root.name):
        if value.startswith(prefix + "/"):
            return root / value[len(prefix) + 1:]
    if os.name == "nt" and re.match(r"^/[a-zA-Z]/", value):
        value = value[1] + ":" + value[2:]
    return Path(value)


def mock_command(name, args):
    root = Path(os.environ["UPDATE_FIXTURE"])
    events = root / "events"
    faults = set(os.environ.get("UPDATE_FAULTS", "").split(","))
    state_path = root / "services.json"

    def record(event):
        with events.open("a", encoding="utf-8") as out:
            out.write(event + "\n")

    if name == "install":
        assert args[:-1] == ["-d", "-m", "0700", "--"]
        local_path(args[-1]).mkdir(mode=0o700)
        return 0
    if name == "id":
        print("0")
        return 0
    if name == "flock":
        return int("lock" in faults)
    if name == "stat":
        print("0:700" if local_path(args[-1]).is_dir() else "0:600")
        return 0
    if name == "docker":
        if args[0] != "inspect":
            record("unexpected-docker-" + args[0])
            return 1
        print("running healthy" if "napcat" not in faults else "exited none")
        return 0
    if name == "sleep":
        return 0

    state = json.loads(state_path.read_text(encoding="utf-8"))
    if name == "systemctl":
        command, unit = args[0], args[-1]
        if command == "is-active":
            return 0 if state[unit] == "active" else 3
        if command == "show":
            prop = args[args.index("-p") + 1]
            print({"MainPID": "100" if unit == API else "200",
                   "InvocationID": "a" * 32, "ActiveState": state[unit]}[prop])
            return 0
        if command in {"start", "stop"}:
            record(f"{command}:{unit}")
            state[unit] = "active" if command == "start" else "inactive"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            return int(command == "start" and "start" in faults)
    if name == "ss":
        if "-ltnp" in args:
            print('LISTEN 0 2048 127.0.0.1:18765 0.0.0.0:* users:(("python",pid=100,fd=3))')
        else:
            print('ESTAB 0 0 127.0.0.1:52000 127.0.0.1:13001 users:(("python",pid=200,fd=4))')
        return 0
    if name == "journalctl":
        assert "_SYSTEMD_INVOCATION_ID=" + "a" * 32 in args
        print("INFO: Bot 10001 connected")
        if "onebot" in faults and f"start:{BOT}" in events.read_text(encoding="utf-8"):
            print("INFO: Bot 10001 disconnected")
        return 0
    if name == "curl":
        if "health" in faults and f"start:{API}" in events.read_text(encoding="utf-8"):
            print("503", end="")
        else:
            print("200" if args[-1].endswith("/health") else "401", end="")
        return 0
    if name == "sqlite3":
        source, operation = local_path(args[-2]), args[-1]
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            if operation.startswith(".backup "):
                destination = local_path(shlex.split(operation)[1])
                record("backup:stopped" if all(v == "inactive" for v in state.values()) else "backup:online")
                if "backup-exit" in faults:
                    return 1
                if "empty-backup" not in faults:
                    with closing(sqlite3.connect(destination)) as dest:
                        conn.backup(dest)
                return 0
            if operation == "PRAGMA quick_check;" and "quick-check" in faults and source.parent.name == "backups":
                print("synthetic corruption")
            else:
                print(conn.execute(operation).fetchone()[0])
            return 0
    if name == "python":
        if "pip" in args:
            command = next(x for x in args if x in {"download", "install", "check"})
            record("pip:" + command)
            if command == "install":
                assert all(v == "inactive" for v in state.values())
                assert "--no-index" in args
            return int(command in faults)
        if args == ["-B", "bot/migrate.py"]:
            assert all(v == "inactive" for v in state.values())
            record("migrate")
            if "migration" in faults:
                return 1
            with closing(sqlite3.connect("bot/data/auth.db")) as conn:
                conn.execute("PRAGMA user_version = 2")
            return 0
    raise AssertionError((name, args))


def find_bash():
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")
    if git:
        path = Path(subprocess.check_output([git, "--exec-path"], text=True).strip())
        bash = path.parents[2] / "bin" / "bash.exe"
        if bash.is_file():
            return str(bash)
    return None


@unittest.skipUnless(find_bash() and shutil.which("git"), "requires Bash and Git")
class UpdateTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="mc-qq-update-tests-")
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.upstream = self.root / "upstream"
        self.repo = self.root / "project"
        self.backups = self.root / "backups"
        self.events = self.root / "events"
        self.events.touch()
        self.state = self.root / "services.json"
        self.state.write_text(json.dumps({API: "active", BOT: "active"}), encoding="utf-8")
        self.bash = find_bash()
        self.git_exe = shutil.which("git")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1")
        self.upstream.mkdir()
        self.git(self.upstream, "init", "-b", "main", "--template=")
        for path, content in {
            ".gitignore": (SOURCE.parent.parent / ".gitignore").read_text(encoding="utf-8"),
            "README.md": "before\n", "requirements.txt": "example==1.0\n",
            "requirements.in": "example==1.0\n",
            "bot/bot.py": "# baseline\n", "bot/mc_api.py": "# baseline\n",
            "bot/database.py": "# baseline\n", "bot/migrate.py": "# baseline\n",
            "bot/migrations/001.sql": "-- initial\n", "napcat/compose.yaml": "services: {}\n",
            "deploy/systemd/mc-qq-api.service": "# baseline\n",
        }.items():
            self.write(self.upstream / path, content)
        self.initial = self.commit_upstream()
        self.git(self.root, "clone", "--quiet", str(self.upstream), str(self.repo))
        for path in ("bot/.env", "napcat/.env"):
            self.write(self.repo / path, "synthetic fixture\n")
        (self.repo / "napcat/data").mkdir()
        (self.repo / "bot/data").mkdir()
        self.db = self.repo / "bot/data/auth.db"
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE sample (value TEXT)")
            conn.execute("INSERT INTO sample VALUES ('synthetic')")
            conn.execute("PRAGMA user_version = 1")
        wrappers = self.root / "bin"
        wrappers.mkdir()
        wrapper = '#!/usr/bin/env bash\nexec "$UPDATE_TEST_PYTHON" "$UPDATE_DRIVER" --mock "$(basename "$0")" "$@"\n'
        for command in ("id", "flock", "systemctl", "docker", "sqlite3", "curl", "ss", "journalctl", "stat", "install", "sleep"):
            path = wrappers / command
            self.write(path, wrapper)
            path.chmod(0o755)
        venv_python = self.repo / "bot/.venv/bin/python"
        self.write(venv_python, wrapper)
        venv_python.chmod(0o755)
        self.env.update(UPDATE_FIXTURE=str(self.root), UPDATE_REAL_GIT=self.git_exe,
                        UPDATE_TEST_PYTHON=Path(sys.executable).as_posix(), UPDATE_DRIVER=Path(__file__).resolve().as_posix())
        shell_root = self.shell_path(self.root)
        self.env["UPDATE_FIXTURE_SHELL"] = shell_root
        git_wrapper = wrappers / "git"
        self.write(git_wrapper, """#!/usr/bin/env bash
args=("$@")
while [[ $1 == -c ]]; do shift 2; done
case "$1" in
  remote)
    if [[ $2 == get-url ]]; then
      if [[ $UPDATE_FAULTS == *origin* ]]; then
        printf '%s\\n' https://example.invalid/other.git
      else
        printf '%s\\n' "$UPDATE_EXPECTED_ORIGIN"
      fi
      exit 0
    fi ;;
  fetch|merge) printf '%s\\n' "$1" >> "$UPDATE_FIXTURE_SHELL/events" ;;
esac
exec "$UPDATE_REAL_GIT" "${args[@]}"
""")
        git_wrapper.chmod(0o755)
        self.env["UPDATE_EXPECTED_ORIGIN"] = ORIGIN
        shell_repo = self.shell_path(self.repo)
        tool = SOURCE.read_text(encoding="utf-8")
        tool = tool.replace("PROJECT=/opt/mc-qq-auth BACKUPS=/opt/mc-qq-auth-backups",
                            f"PROJECT={shlex.quote(shell_repo)} BACKUPS={shlex.quote(shell_root + '/backups')}", 1)
        tool = tool.replace("/tmp/mc-qq-auth-update.", shell_root + "/mc-qq-auth-update.")
        if os.name == "nt":
            # MSYS cannot open a Windows directory on this descriptor. The lock command is mocked.
            tool = tool.replace("exec {lock_fd}<.git\n", "exec {lock_fd}<.git/HEAD\n")
        self.tool = self.root / "update.sh"
        self.write(self.tool, tool)
        self.wrapper_dir = self.shell_path(wrappers)

    def shell_path(self, path):
        return subprocess.check_output(
            [self.bash, "--noprofile", "--norc", "-c", 'cd -- "$1" && pwd -P', "_", path.as_posix()],
            env=self.env, text=True,
        ).strip()

    def git(self, cwd, *args):
        return subprocess.check_output(
            [self.git_exe, "-c", "user.name=Update Test", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-c", "core.hooksPath=/dev/null", *args],
            cwd=cwd, env=self.env, stderr=subprocess.PIPE, text=True,
        ).strip()

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def commit_upstream(self):
        self.git(self.upstream, "add", "--", ".")
        self.git(self.upstream, "commit", "-m", "synthetic deployment target")
        return self.git(self.upstream, "rev-parse", "HEAD")

    def incoming(self, *paths):
        for path in paths:
            file = self.upstream / path
            if path == "requirements.txt":
                self.write(file, "example==2.0\n")
            else:
                self.write(file, (file.read_text(encoding="utf-8") if file.exists() else "") + "# synthetic change\n")
        return self.commit_upstream()

    def run_tool(self, *args, faults=(), okay=False):
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "-c", 'export PATH="$1:$PATH"; shift; exec bash "$@"',
             "_", self.wrapper_dir, self.tool.as_posix(), *args],
            cwd=self.repo, env={**self.env, "UPDATE_FAULTS": ",".join(faults)},
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode == 0, okay, out)
        self.assertNotIn("synthetic fixture", out)
        return out

    def observed(self):
        return self.events.read_text(encoding="utf-8").splitlines()

    def test_default_and_local_checks_do_not_update_source(self):
        target = self.incoming("requirements.txt", "bot/migrations/001.sql", "deploy/systemd/mc-qq-api.service")
        local = self.run_tool("--check", "--local", okay=True)
        self.assertIn("GitHub freshness is unknown", local)
        self.assertEqual(self.observed(), [])
        for _ in range(2):
            out = self.run_tool(okay=True)
            self.assertIn("dependencies=1 migration=1 systemd=1", out)
            self.assertIn(target, out)
        self.assertEqual(self.observed(), ["fetch", "fetch"])
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), self.initial)

    def test_dirty_or_untracked_worktree(self):
        self.write(self.repo / "local.txt", "untracked\n")
        self.assertIn("Worktree has local modifications", self.run_tool())
        (self.repo / "local.txt").unlink()
        self.write(self.repo / "README.md", "modified\n")
        self.assertIn("Worktree has local modifications", self.run_tool())
        self.assertEqual(self.observed(), [])

    def test_ignored_incoming_collision(self):
        path = "bot/messages.yml"
        self.write(self.upstream / path, "incoming\n")
        self.git(self.upstream, "add", "-f", "--", path)
        target = self.commit_upstream()
        self.write(self.repo / path, "local\n")
        self.assertIn("conflicts", self.run_tool("--apply", target))
        self.assertEqual((self.repo / path).read_text(), "local\n")
        self.assertEqual(self.observed(), ["fetch"])

    def test_update_preserves_ignored_message_config(self):
        path = "bot/messages.yml"
        local = "events: {leave: {enabled: false}}\n"
        self.write(self.repo / path, local)
        lock = (SOURCE.parent.parent / "requirements.txt").read_text(encoding="utf-8")
        self.write(self.upstream / "requirements.txt", lock)
        target = self.incoming("bot/messages.example.yml")
        out = self.run_tool("--apply", target, okay=True)
        self.assertIn("dependencies=1 migration=0", out)
        self.assertEqual((self.repo / path).read_text(encoding="utf-8"), local)
        self.assertTrue((self.repo / "bot/messages.example.yml").is_file())
        self.assertEqual(self.git(self.repo, "ls-files", "--", path), "")

    def test_missing_or_nonhex_sha(self):
        self.assertIn("missing or ambiguous", self.run_tool("--apply", "0123456789"))
        self.assertIn("hexadecimal", self.run_tool("--apply", "origin/main"))

    def test_remote_rewrite_does_not_advance_cached_ref(self):
        target = self.incoming("README.md")
        self.run_tool(okay=True)
        self.git(self.upstream, "checkout", "--detach", self.initial)
        self.incoming("bot/bot.py")
        self.git(self.upstream, "branch", "-f", "main", "HEAD")
        self.assertIn("diverged or was rewound", self.run_tool())
        self.assertEqual(self.git(self.repo, "rev-parse", "origin/main"), target)

    def test_missing_config_and_lock_conflict(self):
        self.assertIn("repository lock", self.run_tool(faults=("lock",)))
        (self.repo / "bot/.env").unlink()
        self.assertIn("unsafe runtime file: bot/.env", self.run_tool())
        self.assertEqual(self.observed(), [])

    def test_reject_target_tracking_runtime_file(self):
        self.write(self.upstream / "bot/.env", "synthetic fixture\n")
        self.git(self.upstream, "add", "-f", "--", "bot/.env")
        target = self.commit_upstream()
        self.assertIn("Target tracks a protected", self.run_tool("--apply", target))
        self.assertEqual(self.observed(), ["fetch"])

    def test_manual_unit_or_compose_change(self):
        target = self.incoming("deploy/systemd/mc-qq-api.service", "napcat/compose.yaml")
        out = self.run_tool("--apply", target)
        self.assertIn("systemd=1 napcat=1", out)
        self.assertIn("Apply does not install unit changes", out)
        self.assertEqual(self.observed(), ["fetch"])

    def test_reject_unexpected_origin_and_napcat_baseline(self):
        self.assertIn("Origin is not", self.run_tool(faults=("origin",)))
        target = self.incoming("bot/bot.py")
        self.assertIn("NapCat baseline", self.run_tool("--apply", target, faults=("napcat",)))
        self.assertEqual(self.observed(), ["fetch"])

    def test_updater_change_requires_manual_update(self):
        target = self.incoming("deploy/update.sh")
        self.assertIn("replace the running updater", self.run_tool("--apply", target))
        self.assertEqual(self.observed(), ["fetch"])

    def test_onebot_failure_after_bot_only_update_leaves_api_active(self):
        target = self.incoming("bot/bot.py")
        self.assertIn("Bot OneBot connection did not verify",
                      self.run_tool("--apply", target, faults=("onebot",)))
        self.assertEqual(self.observed(), [
            "fetch", "backup:online", f"stop:{BOT}", "merge", f"start:{BOT}", f"stop:{BOT}",
        ])
        self.assertEqual(json.loads(self.state.read_text())[API], "active")

    def test_backup_failures_leave_running_services(self):
        target = self.incoming("bot/bot.py")
        for fault, message in (("backup-exit", "backup failed"), ("empty-backup", "Backup is empty"),
                               ("quick-check", "Backup quick_check")):
            with self.subTest(fault=fault):
                out = self.run_tool("--apply", target, faults=(fault,))
                self.assertIn(message, out)
                self.assertNotIn("merge", self.observed())
                self.assertFalse(any(x.startswith("stop:") for x in self.observed()))
                self.assertEqual(list(self.backups.iterdir()), [])

    def test_pip_download_and_install_failure_boundaries(self):
        target = self.incoming("requirements.txt")
        self.assertIn("phase wheel-download", self.run_tool("--apply", target, faults=("download",)))
        self.assertEqual(self.observed(), ["fetch", "pip:download"])
        self.events.write_text("", encoding="utf-8")
        self.assertIn("phase dependencies", self.run_tool("--apply", target, faults=("install",)))
        self.assertEqual(self.observed(), [
            "fetch", "pip:download", "backup:online", f"stop:{BOT}", f"stop:{API}", "merge", "pip:install",
        ])
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), target)
        self.assertEqual(len(list(self.backups.iterdir())), 1)

    def test_migration_failure_leaves_verified_backups(self):
        target = self.incoming("bot/migrations/001.sql")
        self.assertIn("phase migration", self.run_tool("--apply", target, faults=("migration",)))
        self.assertEqual(self.observed(), [
            "fetch", "backup:online", f"stop:{BOT}", f"stop:{API}", "backup:stopped", "merge", "migrate",
        ])
        self.assertEqual(len(list(self.backups.iterdir())), 2)

    def test_start_and_health_failure_stop_attempted_unit(self):
        target = self.incoming("bot/mc_api.py")
        self.assertIn("phase restart", self.run_tool("--apply", target, faults=("start",)))
        self.assertEqual(self.observed(), [
            "fetch", "backup:online", f"stop:{API}", "merge", f"start:{API}", f"stop:{API}",
        ])
        self.git(self.repo, "checkout", "main")
        self.state.write_text(json.dumps({API: "active", BOT: "active"}), encoding="utf-8")
        self.events.write_text("", encoding="utf-8")
        target = self.incoming("bot/mc_api.py")
        self.assertIn("phase health", self.run_tool("--apply", target, faults=("health",)))
        self.assertEqual(self.observed(), [
            "fetch", "backup:online", f"stop:{API}", "merge", f"start:{API}", f"stop:{API}",
        ])

    def test_successful_full_upgrade_and_noop_retry(self):
        target = self.incoming("requirements.txt", "bot/migrations/001.sql")
        self.assertIn("Upgrade finished", self.run_tool("--apply", target[:12], okay=True))
        self.assertEqual(self.observed(), [
            "fetch", "pip:download", "backup:online", f"stop:{BOT}", f"stop:{API}", "backup:stopped",
            "merge", "pip:install", "pip:check", "migrate", f"start:{API}", f"start:{BOT}",
        ])
        self.assertEqual(self.git(self.repo, "rev-parse", "HEAD"), target)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(len(list(self.backups.iterdir())), 2)
        self.assertIn("already at the target", self.run_tool("--apply", target))
        self.assertEqual(self.observed()[-1], "fetch")
        self.assertEqual(len(list(self.backups.iterdir())), 2)

    def test_source_only_upgrade_does_not_touch_services(self):
        target = self.incoming("README.md")
        self.assertIn("Source-only update", self.run_tool("--apply", target, okay=True))
        self.assertEqual(self.observed(), ["fetch", "merge"])
        self.assertFalse(self.backups.exists())


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--mock":
        raise SystemExit(mock_command(sys.argv[2], sys.argv[3:]))
    unittest.main()
