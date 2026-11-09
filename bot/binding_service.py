from notify_client import notify_auth_updated

import re
import sqlite3
import time

from contextlib import closing
from database import DB_PATH, MAX_AUTH_VERSION


# 标准 Minecraft Java 版用户名格式
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")

def update_auth_version(conn: sqlite3.Connection):
    """使用系统时间更新授权版本，保证严格递增。"""

    now_ms = time.time_ns() // 1_000_000

    result = conn.execute(
        """
        UPDATE auth_state
        SET version = MAX(version + 1, ?)
        WHERE id = 1
          AND typeof(version) = 'integer'
          AND version >= 0
          AND version < ?
        """,
        (now_ms, MAX_AUTH_VERSION),
    )
    if result.rowcount != 1:
        raise sqlite3.IntegrityError("授权版本记录缺失、无效或已达到整数上限")

def valid_mc_name(name: str) -> bool:
    return bool(NAME_PATTERN.fullmatch(name))


def get_binding(group_id: str, qq_id: str) -> str | None:
    """查询指定 QQ 当前绑定的游戏 ID。"""

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn:
        row = conn.execute(
            """
            SELECT mc_name
            FROM offline_bindings
            WHERE group_id = ?
              AND qq_id = ?
              AND status = 'ACTIVE'
            """,
            (group_id, qq_id),
        ).fetchone()

        return row[0] if row else None


def bind_player(
    group_id: str,
    qq_id: str,
    mc_name: str,
) -> tuple[str, str | None]:
    """绑定玩家，返回操作状态和关联的游戏 ID。"""

    if not valid_mc_name(mc_name):
        return "invalid_name", None

    name_key = mc_name.lower()
    now = int(time.time())

    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        # 申请写锁，使检查与写入成为同一个事务
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            """
            SELECT mc_name
            FROM offline_bindings
            WHERE group_id = ?
              AND qq_id = ?
              AND status = 'ACTIVE'
            """,
            (group_id, qq_id),
        ).fetchone()

        if existing:
            conn.rollback()
            return "already_bound", existing[0]

        occupied = conn.execute(
            """
            SELECT id
            FROM offline_bindings
            WHERE mc_name_key = ?
              AND status = 'ACTIVE'
            """,
            (name_key,),
        ).fetchone()

        if occupied:
            conn.rollback()
            return "name_taken", None

        conn.execute(
            """
            INSERT INTO offline_bindings (
                group_id,
                qq_id,
                mc_name,
                mc_name_key,
                status,
                bound_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?)
            """,
            (
                group_id,
                qq_id,
                mc_name,
                name_key,
                now,
                now,
            ),
        )

        update_auth_version(conn)

    notify_auth_updated()
    return "success", mc_name


def unbind_player(group_id: str, qq_id: str, *, left_at: int | None = None) -> str | None:
    """删除当前绑定，并更新权限版本号。"""
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, mc_name, bound_at
            FROM offline_bindings
            WHERE group_id = ?
              AND qq_id = ?
              AND status = 'ACTIVE'
            """,
            (group_id, qq_id),
        ).fetchone()

        if not row:
            return None
        binding_id, mc_name, bound_at = row
        if left_at is not None and bound_at > left_at:
            return None

        _delete_active_bindings(conn, group_id, {qq_id: binding_id})

    notify_auth_updated()
    return mc_name


def binding_audit_snapshot(group_id: str) -> tuple[int, dict[str, int]]:
    """在同一读事务中记录对账所用的版本和绑定。"""
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        conn.execute("BEGIN")
        version = _audit_version(conn)
        rows = conn.execute(
            "SELECT qq_id, id FROM offline_bindings WHERE group_id = ? AND status = 'ACTIVE'",
            (group_id,),
        )
        return version, dict(rows)


def revoke_checked_bindings(group_id: str, version: int, missing: dict[str, int]) -> int | None:
    if not missing:
        return 0
    with closing(sqlite3.connect(DB_PATH, timeout=10)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        # Some supported legacy tables can reuse a deleted row ID.
        if _audit_version(conn) != version:
            return None
        deleted = _delete_active_bindings(conn, group_id, missing)
    if deleted:
        notify_auth_updated()
    return deleted


def _audit_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT version FROM auth_state WHERE id = 1").fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise sqlite3.IntegrityError("授权版本记录无效")
    return row[0]


def _delete_active_bindings(
    conn: sqlite3.Connection, group_id: str, bindings: dict[str, int]
) -> int:
    result = conn.executemany(
        """
        DELETE FROM offline_bindings
        WHERE group_id = ? AND qq_id = ? AND id = ? AND status = 'ACTIVE'
        """,
        ((group_id, qq_id, binding_id) for qq_id, binding_id in bindings.items()),
    )
    if result.rowcount:
        update_auth_version(conn)
    return result.rowcount
