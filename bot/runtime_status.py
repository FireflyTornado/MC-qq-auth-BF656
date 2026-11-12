import json
import platform
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from threading import Lock
from typing import Literal, TypeVar

import psutil
from mcstatus import JavaServer

import database
from app_config import load_config


UNAVAILABLE = "Unavailable"
HEALTH_TIMEOUT = 2
HEALTH_PORT = 18765
MINECRAFT_TIMEOUT = 2
CPU_SAMPLE_INTERVAL = 0.12
OutcomeKind = Literal["reconcile", "notify"]
T = TypeVar("T")

_outcome_lock = Lock()
_outcomes: dict[OutcomeKind, tuple[str, datetime]] = {}


def record_outcome(kind: OutcomeKind, summary: str) -> None:
    with _outcome_lock:
        _outcomes[kind] = (summary, datetime.now(UTC))


def _format_outcome(outcome: tuple[str, datetime] | None) -> str:
    if outcome is None:
        return "No attempts yet"
    summary, when = outcome
    return f"{summary} / {when:%H:%M UTC}"


@dataclass(frozen=True)
class HealthResult:
    status_code: int | None
    latency_ms: float
    error: str | None = None
    minecraft_connections: int | None = None

    @property
    def summary(self) -> str:
        return f"HTTP {self.status_code}" if self.status_code is not None else self.error or UNAVAILABLE


def check_fastapi(*, include_connections: bool = False) -> HealthResult:
    """Probe the fixed loopback health endpoint."""
    started = time.monotonic()
    try:
        with closing(HTTPConnection("127.0.0.1", HEALTH_PORT, timeout=HEALTH_TIMEOUT)) as connection:
            connection.request("GET", "/internal/health")
            with connection.getresponse() as response:
                status = response.status
                latency = (time.monotonic() - started) * 1000
                connections = None
                if status == 200 and include_connections:
                    payload = _probe(lambda: json.loads(response.read(4096)))
                    count = payload.get("minecraft_connections") if isinstance(payload, dict) else None
                    if type(count) is int and count >= 0:
                        connections = count
                return HealthResult(status, latency, minecraft_connections=connections)
    except TimeoutError:
        error = "Timeout"
    except OSError:
        error = "Connection failed"
    except HTTPException:
        error = "Invalid HTTP response"
    return HealthResult(None, (time.monotonic() - started) * 1000, error)


@dataclass(frozen=True)
class ResourceUsage:
    used: int
    total: int
    percent: float


@dataclass(frozen=True)
class MinecraftStatus:
    reachable: bool
    online_players: int | None = None
    max_players: int | None = None


@dataclass(frozen=True)
class RuntimeStatus:
    checked_at: datetime
    connected: bool | None = None
    api: HealthResult | None = None
    sqlite_readable: bool | None = None
    system_name: str | None = None
    python_version: str | None = None
    cpu_cores: int | None = None
    cpu_percent: float | None = None
    memory: ResourceUsage | None = None
    disk: ResourceUsage | None = None
    system_seconds: float | None = None
    bot_seconds: float | None = None
    connection_seconds: float | None = None
    active_bindings: int | None = None
    auth_version: int | None = None
    last_reconcile: str = "No attempts yet"
    last_notice: str = "No attempts yet"
    minecraft: MinecraftStatus | None = None


def _probe(check: Callable[[], T]) -> T | None:
    try:
        return check()
    except Exception:
        # Status fields never contain exception text, which can include paths or addresses.
        return None


def check_minecraft() -> MinecraftStatus | None:
    address = (load_config().get("MC_STATUS_ADDRESS") or "").strip()
    if not address:
        return None
    reply = _probe(lambda: JavaServer.lookup(address, timeout=MINECRAFT_TIMEOUT).status(tries=1))
    if reply is None:
        return MinecraftStatus(False)
    online, maximum = reply.players.online, reply.players.max
    if type(online) is int and online >= 0 and type(maximum) is int and maximum >= 0:
        return MinecraftStatus(True, online, maximum)
    return MinecraftStatus(True)


def _binding_status(group_id: str) -> tuple[int, int]:
    uri = database.DB_PATH.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as connection, connection:
        connection.execute("BEGIN")
        version = connection.execute("SELECT version FROM auth_state WHERE id = 1").fetchone()
        if version is None or type(version[0]) is not int or version[0] < 0:
            raise sqlite3.DatabaseError("Invalid authorization version")
        (count,) = connection.execute(
            "SELECT COUNT(*) FROM offline_bindings WHERE group_id = ? AND status = 'ACTIVE'",
            (group_id,),
        ).fetchone()
        return version[0], count


def _system_name() -> str:
    name = platform.system()
    if name == "Linux":
        release = _probe(platform.freedesktop_os_release)
        if release:
            return " ".join(filter(None, (release.get("NAME") or name, release.get("VERSION_ID"))))
    if name == "Windows":
        return f"{name} {platform.release()}".rstrip()
    if name == "Darwin":
        return f"macOS {platform.mac_ver()[0]}".rstrip()
    return name


def _cpu_percent() -> float | None:
    # A positive interval measures two samples, including on the first request.
    percent = psutil.cpu_percent(interval=CPU_SAMPLE_INTERVAL)
    return percent if 0 <= percent <= 100 else None


def _usage(used: int, total: int, percent: float) -> ResourceUsage | None:
    if total > 0 and 0 <= used <= total and 0 <= percent <= 100:
        return ResourceUsage(used, total, percent)
    return None


def _memory() -> ResourceUsage | None:
    memory = psutil.virtual_memory()
    return _usage(memory.total - memory.available, memory.total, memory.percent)


def _disk() -> ResourceUsage | None:
    opt = Path("/opt")
    disk = psutil.disk_usage(str(opt if opt.is_dir() else Path("/")))
    return _usage(disk.used, disk.total, disk.percent)


def collect_status(group_id: str, *, connected: bool, connected_for: float | None) -> RuntimeStatus:
    api = _probe(lambda: check_fastapi(include_connections=True))
    minecraft = _probe(check_minecraft)
    binding_status = _probe(lambda: _binding_status(group_id))
    version, count = binding_status if binding_status is not None else (None, None)
    with _outcome_lock:
        reconciliation = _format_outcome(_outcomes.get("reconcile"))
        notification = _format_outcome(_outcomes.get("notify"))
    return RuntimeStatus(
        connected=connected,
        api=api,
        sqlite_readable=binding_status is not None,
        system_name=_probe(_system_name),
        python_version=_probe(platform.python_version),
        cpu_cores=_probe(psutil.cpu_count),
        cpu_percent=_probe(_cpu_percent),
        memory=_probe(_memory),
        disk=_probe(_disk),
        system_seconds=_probe(lambda: time.time() - psutil.boot_time()),
        bot_seconds=_probe(lambda: time.time() - psutil.Process().create_time()),
        connection_seconds=connected_for if connected else None,
        active_bindings=count,
        auth_version=version,
        last_reconcile=reconciliation,
        last_notice=notification,
        minecraft=minecraft,
        checked_at=datetime.now(UTC),
    )
