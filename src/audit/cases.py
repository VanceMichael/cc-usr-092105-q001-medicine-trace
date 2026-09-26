"""案件、人工研判、证据封存与交接。

关键约束：

* 风险线索只是"提示"；``dismiss`` 或 ``confirm_for_transfer`` 等结论只能由
  人工账号在持锁研判后写入，引擎本身永远不写结论。
* 多人并发研判同一条线索时使用显式锁 + 令牌，锁有过期时间，避免互相覆盖。
* 封存对案件当时引用到的全部数据版本和原始记录做清单哈希；封存后的案件
  不再接受新的研判或线索，只能在其基础上移送交接。
* 交接必须引用封存清单，接收方回执写回同一条交接记录（追加，不改写）。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from .db import AuditError, ConflictError, PermissionDenied, SealedCaseError, canonical_hash

LOCK_TTL_MIN = 30

# 研判动作 -> 线索新状态
_ACTION_STATUS = {
    "note": "in_review",
    "escalate": "in_review",
    "request_coop": "in_review",
    "dismiss": "dismissed",
    "confirm_for_transfer": "confirmed_transfer",
}


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _role(conn, user_id: str) -> str:
    row = conn.execute("SELECT role, active FROM users WHERE user_id=?",
                       (user_id,)).fetchone()
    if not row or not row["active"]:
        raise AuditError(f"账号不存在或已停用: {user_id}")
    return row["role"]


def _audit(conn, actor: str, action: str, target_type: str, target_id: str,
           result: str = "ok", detail: str | None = None, role: str | None = None):
    conn.execute(
        """INSERT INTO access_log(actor, role, action, target_type, target_id,
           result, detail) VALUES (?,?,?,?,?,?,?)""",
        (actor, role, action, target_type, target_id, result, detail),
    )


# ---------------------------------------------------------------------------
# 案件
# ---------------------------------------------------------------------------
def create_case(conn, user_id: str, title: str, clue_ids: list[str] | None = None) -> str:
    role = _role(conn, user_id)
    if role != "case_worker":
        raise PermissionDenied("只有案件人员可以立案")
    case_id = _new_id("case")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO cases(case_id, title, created_by) VALUES (?,?,?)",
            (case_id, title, user_id),
        )
        for clue_id in clue_ids or []:
            conn.execute(
                "INSERT OR IGNORE INTO case_clues(case_id, clue_id) VALUES (?,?)",
                (case_id, clue_id),
            )
        _audit(conn, user_id, "create_case", "case", case_id, role=role)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return case_id


def attach_clue(conn, user_id: str, case_id: str, clue_id: str) -> None:
    role = _role(conn, user_id)
    if role != "case_worker":
        raise PermissionDenied("只有案件人员可以归并线索")
    case = conn.execute("SELECT status FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if not case:
        raise AuditError("案件不存在")
    if case["status"] in ("sealed", "transferred", "closed"):
        raise SealedCaseError("案件已封存/移送，不能再归并线索")
    conn.execute(
        "INSERT OR IGNORE INTO case_clues(case_id, clue_id) VALUES (?,?)",
        (case_id, clue_id),
    )
    _audit(conn, user_id, "attach_clue", "clue", clue_id,
           detail=f"case={case_id}", role=role)


# ---------------------------------------------------------------------------
# 并发研判
# ---------------------------------------------------------------------------
def acquire_lock(conn, clue_id: str, user_id: str) -> str:
    """锁定线索供本人研判，返回锁令牌。"""
    _role(conn, user_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT lock_owner, lock_token, locked_at, status FROM risk_clues WHERE clue_id=?",
            (clue_id,)).fetchone()
        if not row:
            raise AuditError("线索不存在")
        token = uuid.uuid4().hex
        if row["lock_owner"] and row["lock_owner"] != user_id:
            locked_at = datetime.fromisoformat(row["locked_at"].replace("Z", "+00:00"))
            if _now_dt() - locked_at < timedelta(minutes=LOCK_TTL_MIN):
                raise ConflictError(
                    f"线索正由 {row['lock_owner']} 研判中，锁未过期；请稍后再试")
            # 锁过期：允许接管，但留痕
            _audit(conn, user_id, "steal_expired_lock", "clue", clue_id,
                   detail=f"prev_owner={row['lock_owner']}")
        conn.execute(
            "UPDATE risk_clues SET lock_owner=?, lock_token=?, locked_at=?, "
            "status=CASE WHEN status='open' THEN 'in_review' ELSE status END, "
            "version=version+1 WHERE clue_id=?",
            (user_id, token, _now(), clue_id),
        )
        _audit(conn, user_id, "acquire_lock", "clue", clue_id)
        conn.execute("COMMIT")
        return token
    except Exception:
        conn.execute("ROLLBACK")
        raise


def release_lock(conn, clue_id: str, user_id: str, token: str) -> None:
    conn.execute(
        "UPDATE risk_clues SET lock_owner=NULL, lock_token=NULL, locked_at=NULL, "
        "version=version+1 WHERE clue_id=? AND lock_owner=?",
        (clue_id, user_id),
    )
    _audit(conn, user_id, "release_lock", "clue", clue_id)


def _snapshot_versions(conn, clue: sqlite3.Row) -> dict:
    """记录研判时刻与线索相关的全部业务数据版本。"""
    snap: dict = {"at": _now(), "medicine": {}, "settlement": {}, "voucher": {},
                  "raw_records": {}}
    med_id = clue["medicine_id"]
    person_id = clue["person_id"]
    if med_id:
        m = conn.execute(
            "SELECT current_version FROM medicines WHERE medicine_id=?",
            (med_id,)).fetchone()
        if m:
            snap["medicine"][med_id] = m["current_version"]
        for r in conn.execute(
            "SELECT settlement_id, current_version FROM settlements WHERE medicine_id=?",
            (med_id,),
        ):
            snap["settlement"][r["settlement_id"]] = r["current_version"]
        for r in conn.execute(
            "SELECT voucher_id, current_version FROM vouchers WHERE medicine_id=?",
            (med_id,),
        ):
            snap["voucher"][r["voucher_id"]] = r["current_version"]
    if person_id:
        for r in conn.execute(
            "SELECT settlement_id, current_version FROM settlements WHERE person_id=?",
            (person_id,),
        ):
            snap["settlement"][r["settlement_id"]] = r["current_version"]
    # 线索命中的原始记录号
    explanation = json.loads(clue["explanation"])
    record_ids = _collect_record_ids(explanation["inputs"])
    for rid in record_ids:
        h = conn.execute(
            "SELECT content_hash FROM raw_records WHERE record_id=?", (rid,)
        ).fetchone()
        if h:
            snap["raw_records"][rid] = h["content_hash"]
    return snap


def _collect_record_ids(inputs) -> list[str]:
    ids: list[str] = []
    if isinstance(inputs, dict):
        for k, v in inputs.items():
            if k == "record_id" and isinstance(v, str):
                ids.append(v)
            else:
                ids += _collect_record_ids(v)
    elif isinstance(inputs, list):
        for item in inputs:
            ids += _collect_record_ids(item)
    return ids


def add_decision(conn, clue_id: str, user_id: str, action: str, rationale: str,
                 lock_token: str, case_id: str | None = None) -> str:
    role = _role(conn, user_id)
    if role != "case_worker":
        raise PermissionDenied("只有案件人员可以作出研判决定")
    if action not in _ACTION_STATUS:
        raise AuditError(f"未知研判动作: {action}")
    if not rationale or not rationale.strip():
        raise AuditError("研判必须填写理由，不得空白决定")
    conn.execute("BEGIN IMMEDIATE")
    try:
        clue = conn.execute(
            "SELECT * FROM risk_clues WHERE clue_id=?", (clue_id,)).fetchone()
        if not clue:
            raise AuditError("线索不存在")
        if clue["lock_owner"] != user_id or clue["lock_token"] != lock_token:
            raise ConflictError("未持有该线索的有效研判锁，决定被拒绝")
        if case_id:
            case = conn.execute("SELECT status FROM cases WHERE case_id=?",
                                (case_id,)).fetchone()
            if not case:
                raise AuditError("关联案件不存在")
            if case["status"] != "open":
                raise SealedCaseError("案件已封存/移送，不能再写入研判")

        data_versions = _snapshot_versions(conn, clue)
        data_versions["clue_version_before"] = clue["version"]
        decision_id = _new_id("dec")
        conn.execute(
            """INSERT INTO decisions(decision_id, clue_id, case_id, reviewer,
               action, rationale, data_versions) VALUES (?,?,?,?,?,?,?)""",
            (decision_id, clue_id, case_id, user_id, action, rationale,
             json.dumps(data_versions, ensure_ascii=False, sort_keys=True)),
        )
        conn.execute(
            "UPDATE risk_clues SET status=?, lock_owner=NULL, lock_token=NULL, "
            "locked_at=NULL, version=version+1 WHERE clue_id=?",
            (_ACTION_STATUS[action], clue_id),
        )
        if case_id:
            conn.execute(
                "INSERT OR IGNORE INTO case_clues(case_id, clue_id) VALUES (?,?)",
                (case_id, clue_id),
            )
        _audit(conn, user_id, f"decision:{action}", "clue", clue_id,
               detail=json.dumps({"decision_id": decision_id,
                                  "case_id": case_id}, ensure_ascii=False),
               role=role)
        conn.execute("COMMIT")
        return decision_id
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# 证据封存
# ---------------------------------------------------------------------------
def _build_manifest(conn, case_id: str) -> dict:
    clues = [r["clue_id"] for r in conn.execute(
        "SELECT clue_id FROM case_clues WHERE case_id=?", (case_id,))]
    manifest = {"case_id": case_id, "at": _now(), "clues": {}, "decisions": [],
                "raw_records": {}}
    raw_ids: set[str] = set()
    for clue_id in clues:
        clue = conn.execute("SELECT * FROM risk_clues WHERE clue_id=?",
                            (clue_id,)).fetchone()
        manifest["clues"][clue_id] = {
            "clue_type": clue["clue_type"], "status": clue["status"],
            "severity": clue["severity"], "title": clue["title"],
            "explanation_hash": canonical_hash(json.loads(clue["explanation"])),
            "evidence_hash": clue["evidence_hash"], "version": clue["version"],
        }
        raw_ids.update(_collect_record_ids(json.loads(clue["explanation"])))
    for d in conn.execute(
        "SELECT * FROM decisions WHERE case_id=? ORDER BY decided_at", (case_id,),
    ):
        versions = json.loads(d["data_versions"])
        manifest["decisions"].append({
            "decision_id": d["decision_id"], "clue_id": d["clue_id"],
            "reviewer": d["reviewer"], "action": d["action"],
            "rationale": d["rationale"], "decided_at": d["decided_at"],
            "data_versions": versions,
        })
        raw_ids.update(versions.get("raw_records", {}).keys())
    for rid in sorted(raw_ids):
        r = conn.execute(
            "SELECT source_type, source_org, content_hash, observed_at, received_at "
            "FROM raw_records WHERE record_id=?", (rid,)).fetchone()
        if r:
            manifest["raw_records"][rid] = dict(r)
    return manifest


def seal_case(conn, case_id: str, user_id: str, note: str | None = None) -> dict:
    role = _role(conn, user_id)
    if role not in ("sealing_officer", "case_worker"):
        raise PermissionDenied("无权封存证据")
    conn.execute("BEGIN IMMEDIATE")
    try:
        case = conn.execute("SELECT status FROM cases WHERE case_id=?",
                            (case_id,)).fetchone()
        if not case:
            raise AuditError("案件不存在")
        if case["status"] in ("sealed", "transferred"):
            raise SealedCaseError("案件已封存，不能重复封存（哈希链请以新案续接）")
        manifest = _build_manifest(conn, case_id)
        manifest_hash = canonical_hash(manifest)
        prev = conn.execute(
            "SELECT manifest_hash FROM seals WHERE case_id=? ORDER BY sealed_at DESC LIMIT 1",
            (case_id,)).fetchone()
        seal_id = _new_id("seal")
        conn.execute(
            """INSERT INTO seals(seal_id, case_id, sealed_by, manifest,
               manifest_hash, prev_seal_hash, note) VALUES (?,?,?,?,?,?,?)""",
            (seal_id, case_id, user_id,
             json.dumps(manifest, ensure_ascii=False, sort_keys=True),
             manifest_hash, prev["manifest_hash"] if prev else None, note),
        )
        conn.execute(
            "UPDATE cases SET status='sealed', sealed_at=? WHERE case_id=?",
            (_now(), case_id),
        )
        _audit(conn, user_id, "seal_case", "case", case_id,
               detail=json.dumps({"seal_id": seal_id, "manifest_hash": manifest_hash}),
               role=role)
        conn.execute("COMMIT")
        return {"seal_id": seal_id, "manifest_hash": manifest_hash,
                "clues": len(manifest["clues"]),
                "decisions": len(manifest["decisions"]),
                "raw_records": len(manifest["raw_records"])}
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# 交接回执
# ---------------------------------------------------------------------------
def handoff(conn, case_id: str, user_id: str, to_org: str,
            from_org: str = "宜宾市医保经办稽核部门") -> dict:
    role = _role(conn, user_id)
    if role not in ("case_worker", "sealing_officer"):
        raise PermissionDenied("无权发起交接")
    case = conn.execute("SELECT status FROM cases WHERE case_id=?",
                        (case_id,)).fetchone()
    if not case:
        raise AuditError("案件不存在")
    seal = conn.execute(
        "SELECT seal_id FROM seals WHERE case_id=? ORDER BY sealed_at DESC LIMIT 1",
        (case_id,)).fetchone()
    if not seal:
        raise AuditError("案件尚未封存，不能移送交接")
    handoff_id = _new_id("ho")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """INSERT INTO handoffs(handoff_id, case_id, seal_id, from_org, to_org,
               handler) VALUES (?,?,?,?,?,?)""",
            (handoff_id, case_id, seal["seal_id"], from_org, to_org, user_id),
        )
        _audit(conn, user_id, "handoff", "case", case_id,
               detail=json.dumps({"handoff_id": handoff_id, "to_org": to_org}),
               role=role)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"handoff_id": handoff_id, "seal_id": seal["seal_id"], "to_org": to_org}


def receive_handoff(conn, handoff_id: str, receipt_org: str, receiver: str,
                    receipt_no: str, remark: str | None = None) -> None:
    """接收方在原交接记录上追加回执（不改写任何既有字段）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM handoffs WHERE handoff_id=?",
                           (handoff_id,)).fetchone()
        if not row:
            raise AuditError("交接记录不存在")
        if row["status"] != "pending":
            raise AuditError("该交接已出具回执，不能重复接收")
        conn.execute(
            """UPDATE handoffs SET status='received', receipt_org=?, receiver=?,
               receipt_no=?, received_at=?, remark=? WHERE handoff_id=?""",
            (receipt_org, receiver, receipt_no, _now(), remark, handoff_id),
        )
        conn.execute(
            "UPDATE cases SET status='transferred', transferred_at=? WHERE case_id=?",
            (_now(), row["case_id"]),
        )
        _audit(conn, receiver, "receive_handoff", "handoff", handoff_id,
               detail=json.dumps({"receipt_no": receipt_no, "org": receipt_org}))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# 证据链还原 & 完整性校验
# ---------------------------------------------------------------------------
def evidence_chain(conn, clue_id: str) -> dict:
    """从一条线索还原：命中依据 → 各次研判及当时数据版本 → 封存 → 交接回执。"""
    clue = conn.execute("SELECT * FROM risk_clues WHERE clue_id=?",
                        (clue_id,)).fetchone()
    if not clue:
        raise AuditError("线索不存在")
    chain: dict = {
        "clue": {
            "clue_id": clue["clue_id"], "clue_type": clue["clue_type"],
            "severity": clue["severity"], "status": clue["status"],
            "title": clue["title"], "explanation": json.loads(clue["explanation"]),
            "evidence_hash": clue["evidence_hash"],
            "created_by_rule": clue["created_by_rule"], "created_at": clue["created_at"],
        },
        "decisions": [],
        "seals": [],
        "handoffs": [],
    }
    for d in conn.execute(
        "SELECT * FROM decisions WHERE clue_id=? ORDER BY decided_at", (clue_id,),
    ):
        chain["decisions"].append({
            "decision_id": d["decision_id"], "case_id": d["case_id"],
            "reviewer": d["reviewer"], "action": d["action"],
            "rationale": d["rationale"], "decided_at": d["decided_at"],
            "data_versions": json.loads(d["data_versions"]),
        })
    case_ids = {d["case_id"] for d in conn.execute(
        "SELECT DISTINCT case_id FROM decisions WHERE clue_id=? AND case_id IS NOT NULL",
        (clue_id,))}
    for case_id in case_ids:
        for s in conn.execute(
            "SELECT * FROM seals WHERE case_id=? ORDER BY sealed_at", (case_id,),
        ):
            chain["seals"].append({
                "seal_id": s["seal_id"], "case_id": case_id,
                "sealed_by": s["sealed_by"], "sealed_at": s["sealed_at"],
                "manifest_hash": s["manifest_hash"],
                "prev_seal_hash": s["prev_seal_hash"],
            })
        for h in conn.execute(
            "SELECT * FROM handoffs WHERE case_id=? ORDER BY created_at", (case_id,),
        ):
            chain["handoffs"].append(dict(h))
    return chain


def verify_integrity(conn, case_id: str | None = None) -> dict:
    """校验原始报文哈希、封存清单哈希是否与库内现状一致。"""
    problems: list[dict] = []
    sql = "SELECT record_id, content_hash, payload FROM raw_records"
    for r in conn.execute(sql):
        if canonical_hash(json.loads(r["payload"])) != r["content_hash"]:
            problems.append({"type": "raw_payload_tampered", "record_id": r["record_id"]})
    seal_sql = "SELECT seal_id, case_id, manifest, manifest_hash FROM seals"
    args: tuple = ()
    if case_id:
        seal_sql += " WHERE case_id=?"
        args = (case_id,)
    for s in conn.execute(seal_sql, args):
        if canonical_hash(json.loads(s["manifest"])) != s["manifest_hash"]:
            problems.append({"type": "seal_manifest_tampered", "seal_id": s["seal_id"]})
    return {"ok": not problems, "problems": problems}
