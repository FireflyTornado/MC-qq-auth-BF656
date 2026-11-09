import os
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
from typing import Literal

import psutil

import database


UNAVAILABLE = "Unavailable"
HEALTH_TIMEOUT = 2
HEALTH_PORT = 18765
OutcomeKind = Literal["reconcile", "notify"]
Row = tuple[str, str]

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

    @property
    def summary(self) -> str:
        return f"HTTP {self.status_code}" if self.status_code is not None else self.error or UNAVAILABLE


def check_fastapi() -> HealthResult:
    """Read only the HTTP status from the fixed loopback health endpoint."""
    started = time.monotonic()
    status = None
    error = None
    try:
        with closing(HTTPConnection("127.0.0.1", HEALTH_PORT, timeout=HEALTH_TIMEOUT)) as connection:
            connection.request("GET", "/internal/health")
            with connection.getresponse() as response:
                status = response.status
    except TimeoutError:
        error = "Timeout"
    except OSError:
        error = "Connection failed"
    except HTTPException:
        error = "Invalid HTTP response"
    return HealthResult(status, (time.monotonic() - started) * 1000, error)


@dataclass(frozen=True)
class RuntimeStatus:
    sections: tuple[tuple[str, tuple[Row, ...]], ...]
    checked_at: datetime


def _probe(check: Callable[[], str]) -> str:
    try:
        return check() or UNAVAILABLE
    except Exception:
        # Status fields never contain exception text, which can include paths or addresses.
        return UNAVAILABLE


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


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return UNAVAILABLE
    days, remainder = divmod(max(0, int(seconds)), 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours:02}h {minutes:02}m {seconds:02}s"


def _usage(used: int, total: int, percent: float) -> str:
    gib = 1024 ** 3
    return f"{used / gib:.1f} / {total / gib:.1f} GiB ({percent:.0f}%)"


def _memory() -> str:
    memory = psutil.virtual_memory()
    return _usage(memory.total - memory.available, memory.total, memory.percent)


def _disk() -> str:
    opt = Path("/opt")
    disk = psutil.disk_usage(str(opt if opt.is_dir() else Path("/")))
    return _usage(disk.used, disk.total, disk.percent)


def _cores() -> str:
    cores = psutil.cpu_count()
    return str(cores) if cores else UNAVAILABLE


def collect_status(group_id: str, *, connected: bool, connected_for: float | None) -> RuntimeStatus:
    api = _probe(lambda: check_fastapi().summary)
    try:
        version, count = map(str, _binding_status(group_id))
        sqlite_status = "Readable"
    except Exception:
        sqlite_status = version = count = UNAVAILABLE
    with _outcome_lock:
        reconciliation = _format_outcome(_outcomes.get("reconcile"))
        notification = _format_outcome(_outcomes.get("notify"))
    return RuntimeStatus((
        ("Service health", (
            ("Bot", "Running"),
            ("OneBot", "Connected" if connected else "Disconnected"),
            ("FastAPI", api),
            ("SQLite", sqlite_status),
        )),
        ("Uptime", (
            ("System", _probe(lambda: _duration(time.time() - psutil.boot_time()))),
            ("Bot process", _probe(lambda: _duration(time.time() - psutil.Process().create_time()))),
            ("OneBot connection", _duration(connected_for) if connected else UNAVAILABLE),
        )),
        ("Host", (
            ("Operating system", _probe(platform.system)),
            ("Python", _probe(platform.python_version)),
            ("Logical CPU cores", _probe(_cores)),
            ("Memory usage", _probe(_memory)),
            ("Disk usage", _probe(_disk)),
        )),
        ("Authorization", (
            ("Authorization version", version),
            ("Active bindings", count),
            ("Last member reconciliation", reconciliation),
            ("Last authorization notice", notification),
        )),
    ), datetime.now(UTC))
