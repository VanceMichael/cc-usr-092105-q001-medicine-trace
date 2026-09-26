"""版本化仅追加存储。

所有原始来源材料逐字存入 source_record，任何更正只能追加新版本，
不提供 UPDATE/DELETE 业务接口。记录链 (prev_hash -> record_hash) 与
封存包 (sealed_package) 共同支撑事后防篡改核验。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# 六类法定来源
SOURCE_KINDS = (
    "trace_event",   # 药品赋码与扫码事件（一药一码、流通采集）
    "settlement",    # 参保人就医结算
    "voucher",       # 票、账、货、款材料
    "seizure",       # 仓库扣押登记
    "online_sale",   # 网络销售线索
    "cooperation",   # 办案协作函件/协查
)


def utcnow() -> str:
    """单调时间戳，统一 UTC，排序与水位判定只用它。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_digest(payload: Any) -> str:
    """对任意 JSON 兼容负载计算规范哈希（SHA-256）。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_record (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_kind     TEXT NOT NULL CHECK (source_kind IN (%s)),
    source_org      TEXT NOT NULL,          -- 出具单位
    source_doc_no   TEXT,                   -- 原始单据/文号
    business_key    TEXT NOT NULL,          -- 来源内业务主键（事件号/结算号）
    version         INTEGER NOT NULL,       -- 同一 business_key 的追加版本，从 1 起
    supersedes      INTEGER,                -- 被本版更正的 source_record.id
    correction_reason TEXT,                 -- 更正原因（第 2 版起必填）
    business_time   TEXT,                   -- 业务发生时间（来源自报，可迟到）
    received_at     TEXT NOT NULL,          -- 入库时间（系统时间，只增不改）
    payload_json    TEXT NOT NULL,          -- 逐字原始报文
    payload_digest  TEXT NOT NULL,
    dedup_digest    TEXT NOT NULL,          -- 归一化去重指纹
    duplicate_of    INTEGER,                -- 命中已存在记录则指向其 id
    upload_batch    TEXT NOT NULL,
    prev_hash       TEXT,                   -- 哈希链前一记录哈希
    record_hash     TEXT NOT NULL,          -- 本记录链哈希
    UNIQUE(source_kind, business_key, version)
);

CREATE TABLE IF NOT EXISTS upload_batch (
    batch_id    TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_org  TEXT NOT NULL,
    received_at TEXT NOT NULL,
    total       INTEGER NOT NULL,
    stored      INTEGER NOT NULL,           -- 新增（含更正版本）
    duplicates  INTEGER NOT NULL,           -- 完全重复
    rejected    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS watermark (
    source_kind TEXT PRIMARY KEY,
    high_watermark TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS current_view (
    -- 每条业务键当前有效版本的投影，派生数据，可由仅追加日志重建
    source_kind  TEXT NOT NULL,
    business_key TEXT NOT NULL,
    record_id    INTEGER NOT NULL,
    version      INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    digest       TEXT NOT NULL,
    PRIMARY KEY (source_kind, business_key)
);

CREATE TABLE IF NOT EXISTS batch_job (
    job_id       TEXT PRIMARY KEY,
    rule_version TEXT NOT NULL,
    scope_json   TEXT NOT NULL,
    params_json  TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    trigger      TEXT NOT NULL,             -- manual / scheduled / late_data
    stats_json   TEXT
);

CREATE TABLE IF NOT EXISTS risk_lead (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_no        TEXT NOT NULL UNIQUE,
    rule_code      TEXT NOT NULL,
    rule_version   TEXT NOT NULL,
    severity       TEXT NOT NULL CHECK (severity IN ('low','medium','high')),
    title          TEXT NOT NULL,
    explanation    TEXT NOT NULL,           -- 人话解释：为什么报、依据什么
    subject_type   TEXT NOT NULL,           -- box / insured / org / batch
    subject_id     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    -- open / reviewing / confirmed_risk / dismissed / referred
    created_at     TEXT NOT NULL,
    first_job_id   TEXT NOT NULL,
    latest_job_id  TEXT NOT NULL,
    latest_version INTEGER NOT NULL DEFAULT 1,
    revision       INTEGER NOT NULL DEFAULT 1,  -- 研判修订号：机器复评或人工决定都递增
    UNIQUE(rule_code, subject_type, subject_id)
);

CREATE TABLE IF NOT EXISTS lead_version (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL REFERENCES risk_lead(id),
    version_no      INTEGER NOT NULL,
    job_id          TEXT NOT NULL,
    rule_version    TEXT NOT NULL,
    params_json     TEXT NOT NULL,
    evidence_json   TEXT NOT NULL,          -- 输入证据快照（record_id+digest）
    data_cut_id     INTEGER NOT NULL,       -- 采用的数据版本切点
    explanation     TEXT NOT NULL,
    severity        TEXT NOT NULL,
    produced_at     TEXT NOT NULL,
    UNIQUE(lead_id, version_no)
);

CREATE TABLE IF NOT EXISTS data_cut (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    cut_no      TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    max_record_id INTEGER NOT NULL,        -- 切点：source_record.id <= 此值
    digest      TEXT NOT NULL,              -- 切点内全部当前版本摘要
    note        TEXT
);

CREATE TABLE IF NOT EXISTS case_file (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_no     TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    region      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    sealed_at   TEXT,
    seal_digest TEXT,
    status      TEXT NOT NULL DEFAULT 'active'  -- active / sealed / transferred
);

CREATE TABLE IF NOT EXISTS case_member (
    case_id   INTEGER NOT NULL REFERENCES case_file(id),
    user_id   TEXT NOT NULL,
    role      TEXT NOT NULL CHECK (role IN ('investigator','supervisor','viewer')),
    added_at  TEXT NOT NULL,
    PRIMARY KEY (case_id, user_id)
);

CREATE TABLE IF NOT EXISTS lead_decision (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL REFERENCES risk_lead(id),
    case_id     INTEGER REFERENCES case_file(id),
    seq         INTEGER NOT NULL,           -- 同一线索上的决定序号，只增
    action      TEXT NOT NULL,
    -- claim / start_review / annotate / confirm_risk / dismiss / refer / seal / transfer
    actor       TEXT NOT NULL,
    comment     TEXT,
    lock_version INTEGER NOT NULL,          -- 动作时线索版本（乐观锁）
    made_at     TEXT NOT NULL,
    payload_json TEXT,
    UNIQUE(lead_id, seq)
);

CREATE TABLE IF NOT EXISTS lead_link (
    lead_id     INTEGER NOT NULL REFERENCES risk_lead(id),
    case_id     INTEGER NOT NULL REFERENCES case_file(id),
    linked_at   TEXT NOT NULL,
    linked_by   TEXT NOT NULL,
    PRIMARY KEY (lead_id, case_id)
);

CREATE TABLE IF NOT EXISTS sealed_package (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      INTEGER NOT NULL REFERENCES case_file(id),
    package_no   TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    manifest_json TEXT NOT NULL,            -- [{kind,key,version,record_id,digest}]
    package_digest TEXT NOT NULL,           -- 对清单与全部记录哈希的封存哈希
    record_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id       INTEGER NOT NULL REFERENCES case_file(id),
    transfer_no   TEXT NOT NULL UNIQUE,
    to_org        TEXT NOT NULL,
    to_region     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    package_id    INTEGER NOT NULL REFERENCES sealed_package(id),
    status        TEXT NOT NULL DEFAULT 'sent', -- sent / received
    request_note  TEXT
);

CREATE TABLE IF NOT EXISTS transfer_receipt (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id  INTEGER NOT NULL REFERENCES transfer(id),
    received_at  TEXT NOT NULL,
    receiver     TEXT NOT NULL,
    receiver_org TEXT NOT NULL,
    package_intact INTEGER NOT NULL,        -- 回执方核验封存哈希结果 0/1
    note         TEXT,
    digest_at_receipt TEXT NOT NULL         -- 回执时复算的封存哈希
);

CREATE INDEX IF NOT EXISTS idx_sr_kind_key ON source_record(source_kind, business_key);
CREATE INDEX IF NOT EXISTS idx_sr_batch ON source_record(upload_batch);
CREATE INDEX IF NOT EXISTS idx_sr_payload ON source_record(payload_json)
""" % (",".join(f"'{k}'" for k in SOURCE_KINDS),)


class Store:
    """线程安全的 SQLite 封装；写操作全部串行化。"""

    def __init__(self, path: str | Path = ":memory:"):
        self._path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---- 哈希链 -------------------------------------------------------

    def _last_hash(self, conn) -> str | None:
        row = conn.execute(
            "SELECT record_hash FROM source_record ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["record_hash"] if row else None

    @staticmethod
    def _chain_hash(prev_hash: str | None, digest: str, received_at: str,
                    source_kind: str, business_key: str, version: int) -> str:
        h = hashlib.sha256()
        h.update((prev_hash or "GENESIS").encode())
        h.update(digest.encode())
        h.update(received_at.encode())
        h.update(f"{source_kind}|{business_key}|{version}".encode())
        return h.hexdigest()

    # ---- 摄入 ---------------------------------------------------------

    def ingest_batch(self, source_kind: str, source_org: str,
                     records: Iterable[dict], batch_id: str | None = None,
                     received_at: str | None = None) -> dict:
        """摄入一批原始记录。

        每条记录需含 business_key、payload；可含 source_doc_no、business_time、
        version、supersedes_key、correction_reason。
        完全重复（dedup_digest 命中且非更正）跳过；同键新负载记为新版本。
        """
        if source_kind not in SOURCE_KINDS:
            raise ValueError(f"未知来源类型: {source_kind}")
        records = list(records)
        batch_id = batch_id or f"B-{canonical_digest([source_org, records])[:16]}"
        received_at = received_at or utcnow()
        stats = {"total": len(records), "stored": 0, "duplicates": 0,
                 "rejected": 0, "late": 0, "late_keys": [], "errors": []}

        with self.tx() as conn:
            for seq, rec in enumerate(records):
                try:
                    late = self._ingest_one(conn, source_kind, source_org, rec,
                                            batch_id, received_at)
                    stats["stored"] += 1
                    if late:
                        stats["late"] += 1
                        stats["late_keys"].append(rec.get("business_key"))
                except DuplicateRecord:
                    stats["duplicates"] += 1
                except Exception as exc:  # 单条坏数据不影响整批
                    stats["rejected"] += 1
                    stats["errors"].append({"seq": seq,
                                            "business_key": rec.get("business_key"),
                                            "error": str(exc)})
            conn.execute(
                "INSERT INTO upload_batch(batch_id,source_kind,source_org,"
                "received_at,total,stored,duplicates,rejected) VALUES(?,?,?,?,?,?,?,?)",
                (batch_id, source_kind, source_org, received_at, stats["total"],
                 stats["stored"], stats["duplicates"], stats["rejected"]))
            # 水位线按"业务时间"推进：业务时间早于已见最大业务时间即迟到
            btimes = [r.get("business_time") for r in records if r.get("business_time")]
            self._advance_watermark(conn, source_kind,
                                    max(btimes) if btimes else received_at)
        return {"batch_id": batch_id, **stats}

    def _ingest_one(self, conn, source_kind, source_org, rec, batch_id,
                    received_at) -> bool:
        """摄入单条；返回该记录是否属于迟到数据。"""
        bk = rec.get("business_key")
        payload = rec.get("payload")
        if not bk or payload is None:
            raise ValueError("记录缺少 business_key 或 payload")
        btime = rec.get("business_time")
        late = False
        wm = conn.execute(
            "SELECT high_watermark FROM watermark WHERE source_kind=?",
            (source_kind,)).fetchone()
        if wm and btime and btime < wm["high_watermark"]:
            late = True
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = canonical_digest(payload)
        # 去重指纹不区分送达时间/批次，避免同报文重复入库
        dedup = canonical_digest({"k": source_kind, "b": bk, "p": digest})

        dup = conn.execute(
            "SELECT id FROM source_record WHERE dedup_digest=? LIMIT 1",
            (dedup,)).fetchone()
        if dup:
            # 记录重复上传事实（仍写一条轻量标记，保留重复送达的审计痕迹）
            prev = self._last_hash(conn)
            mark_payload = {"_duplicate_of": dup["id"], "_batch": batch_id}
            mark_digest = canonical_digest(mark_payload)
            cur = conn.execute(
                "INSERT INTO source_record(source_kind,source_org,source_doc_no,"
                "business_key,version,correction_reason,business_time,"
                "received_at,payload_json,payload_digest,dedup_digest,"
                "duplicate_of,upload_batch,prev_hash,record_hash) "
                "VALUES(?,?,?,?,0,NULL,?,?,?,?,?,?,?,?,?)",
                (source_kind, source_org, rec.get("source_doc_no"), bk,
                 rec.get("business_time"), received_at,
                 json.dumps(mark_payload, ensure_ascii=False), mark_digest,
                 canonical_digest({"dup": dedup}), dup["id"], batch_id,
                 prev, self._chain_hash(prev, mark_digest, received_at,
                                        source_kind, bk, 0)))
            raise DuplicateRecord(cur.lastrowid, dup["id"])

        row = conn.execute(
            "SELECT id,version FROM source_record WHERE source_kind=? "
            "AND business_key=? AND version>0 ORDER BY version DESC LIMIT 1",
            (source_kind, bk)).fetchone()
        if row is None:
            version = int(rec.get("version", 1))
            if version != 1:
                raise ValueError(f"新键 {bk} 首版必须为 1，收到 {version}")
            supersedes = None
            reason = rec.get("correction_reason")
        else:
            version = row["version"] + 1
            supersedes = row["id"]
            reason = rec.get("correction_reason")
            if not reason:
                raise ValueError(f"更正版本必须提供 correction_reason: {bk}")
        prev = self._last_hash(conn)
        cur = conn.execute(
            "INSERT INTO source_record(source_kind,source_org,source_doc_no,"
            "business_key,version,supersedes,correction_reason,business_time,"
            "received_at,payload_json,payload_digest,dedup_digest,"
            "duplicate_of,upload_batch,prev_hash,record_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_kind, source_org, rec.get("source_doc_no"), bk, version,
             supersedes, reason, rec.get("business_time"), received_at,
             payload_json, digest, dedup, None, batch_id, prev,
             self._chain_hash(prev, digest, received_at, source_kind, bk,
                              version)))
        record_id = cur.lastrowid
        # 更新当前版本投影（UPSERT，仅追加日志仍是事实来源）
        conn.execute(
            "INSERT INTO current_view(source_kind,business_key,record_id,version,"
            "payload_json,digest) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(source_kind,business_key) DO UPDATE SET "
            "record_id=excluded.record_id,version=excluded.version,"
            "payload_json=excluded.payload_json,digest=excluded.digest",
            (source_kind, bk, record_id, version, payload_json, digest))
        return late

    def _advance_watermark(self, conn, source_kind, ts):
        conn.execute(
            "INSERT INTO watermark(source_kind,high_watermark) VALUES(?,?) "
            "ON CONFLICT(source_kind) DO UPDATE SET "
            "high_watermark=MAX(high_watermark,excluded.high_watermark)",
            (source_kind, ts))

    # ---- 读取 ---------------------------------------------------------

    def current(self, source_kind: str, business_key: str) -> dict | None:
        """返回当前有效版本（含 record_id/version/digest/payload）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM current_view WHERE source_kind=? AND business_key=?",
                (source_kind, business_key)).fetchone()
        return None if row is None else self._view_row(row)

    def history(self, source_kind: str, business_key: str) -> list[dict]:
        """返回某业务键的全部版本（含重复标记），按版本序。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM source_record WHERE source_kind=? AND business_key=? "
                "ORDER BY id", (source_kind, business_key)).fetchall()
        return [dict(r) for r in rows]

    def all_current(self, source_kind: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM current_view WHERE source_kind=? ORDER BY business_key",
                (source_kind,)).fetchall()
        return [self._view_row(r) for r in rows]

    @staticmethod
    def _view_row(row) -> dict:
        return {
            "source_kind": row["source_kind"],
            "business_key": row["business_key"],
            "record_id": row["record_id"],
            "version": row["version"],
            "digest": row["digest"],
            "payload": json.loads(row["payload_json"]),
        }

    def max_record_id(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(id),0) AS m FROM source_record").fetchone()
        return row["m"]

    def verify_chain(self) -> dict:
        """重算整条哈希链，返回核验结果（封存之外的日常防篡改检查）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id,prev_hash,record_hash,payload_json,payload_digest,"
                "received_at,source_kind,business_key,version "
                "FROM source_record ORDER BY id"
            ).fetchall()
            prev = None
            for r in rows:
                # 重算报文摘要：只改 payload_json 而不改 digest 同样视为篡改
                if canonical_digest(json.loads(r["payload_json"])) \
                        != r["payload_digest"]:
                    return {"ok": False, "broken_at": r["id"]}
                expect = self._chain_hash(prev, r["payload_digest"],
                                          r["received_at"], r["source_kind"],
                                          r["business_key"], r["version"])
                if r["prev_hash"] != prev or r["record_hash"] != expect:
                    return {"ok": False, "broken_at": r["id"]}
                prev = r["record_hash"]
            return {"ok": True, "records": len(rows)}


class DuplicateRecord(Exception):
    """完全重复上传：跳过新版本，仅留送达痕迹。"""

    def __init__(self, mark_id: int, original_id: int):
        super().__init__(f"重复记录，标记 id={mark_id} 指向原始 id={original_id}")
        self.mark_id = mark_id
        self.original_id = original_id
