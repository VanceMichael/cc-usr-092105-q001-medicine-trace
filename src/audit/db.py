"""数据库连接、初始化与基础上下文。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def canonical_hash(value: dict | list | str) -> str:
    """对任意 JSON 可序列化内容计算稳定的 sha256。"""
    if isinstance(value, str):
        blob = value.encode("utf-8")
    else:
        blob = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """初始化表结构并写入内置角色账号。"""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seeds = [
        ("u_yibin_01", "宜宾医保稽核员 甲", "case_worker"),
        ("u_yibin_02", "宜宾医保稽核员 乙", "case_worker"),
        ("u_seal_01", "证据封存专员", "sealing_officer"),
        ("u_public", "公众查询入口", "public"),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO users(user_id, display_name, role) VALUES (?,?,?)",
        seeds,
    )


class AuditError(Exception):
    """业务规则错误的统一基类。"""


class PermissionDenied(AuditError):
    """角色无权执行该操作。"""


class ConflictError(AuditError):
    """并发冲突（线索已被他人锁定或版本已变化）。"""


class SealedCaseError(AuditError):
    """案件已封存，禁止再追加或修改。"""
