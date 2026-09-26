"""分级查询服务。

两级可见范围：

* ``case_worker`` / ``sealing_officer``：可看一盒药的完整档案与线索证据链
  （就医结算、参保人、票账货款、扣押、网售、协作、研判、封存、交接）。
* ``public``：只能核验**单盒药品的合法流转摘要**——药品主档、正规流通环节、
  最终结算机构；不含案件、线索、参保人、扣押与网络销售明细。
  若药品结算后又出现非正规流通，仅给中性提示，不泄露办案信息。

所有查询（含公众查询）都写 ``access_log``，但日志不记录返回内容本身。
"""

from __future__ import annotations

import sqlite3

from .cases import evidence_chain
from .db import AuditError, PermissionDenied

STAFF_ROLES = ("case_worker", "sealing_officer")


def _role(conn, user_id: str) -> str:
    row = conn.execute("SELECT role, active FROM users WHERE user_id=?",
                       (user_id,)).fetchone()
    if not row or not row["active"]:
        raise AuditError(f"账号不存在或已停用: {user_id}")
    return row["role"]


def _audit(conn, actor: str, role: str, action: str, target: str,
           result: str = "ok", detail: str | None = None) -> None:
    conn.execute(
        "INSERT INTO access_log(actor, role, action, target_type, target_id, "
        "result, detail) VALUES (?,?,?,?,?,?,?)",
        (actor, role, action, "medicine", target, result, detail),
    )


# ---------------------------------------------------------------------------
# 公众：单盒合法流转摘要
# ---------------------------------------------------------------------------
def public_verify(conn, medicine_id: str) -> dict:
    """供普通查询者核验单盒药品的合法流转摘要（脱敏）。"""
    actor = "u_public"
    role = _role(conn, actor)
    med = conn.execute(
        "SELECT * FROM medicines WHERE medicine_id=?", (medicine_id,)).fetchone()
    if not med:
        _audit(conn, actor, role, "public_verify", medicine_id, result="not_found")
        return {"medicine_id": medicine_id, "found": False,
                "summary": "未查询到该追溯码的赋码信息，无法核验，请谨慎购买。"}

    # 正规流通环节（生产/仓储/配送/入院/配发/结算），不含扣押、网售
    legal_types = ("produce", "warehouse", "distribute", "hospital_in",
                   "dispense", "settlement_scan")
    chain = [{
        "event_type": e["event_type"], "org_name": e["org_name"] or e["org_code"],
        "region_code": e["region_code"], "event_time": e["event_time"],
    } for e in conn.execute(
        f"""SELECT event_type, org_code, org_name, region_code, event_time
            FROM scan_events
            WHERE medicine_id=? AND event_type IN ({','.join('?'*len(legal_types))})
            ORDER BY event_time""",
        (medicine_id, *legal_types),
    )]

    settlement = conn.execute(
        """SELECT med_inst_name, med_inst_code, region_code, settled_at
           FROM current_settlement
           WHERE medicine_id=? AND status!='voided'
           ORDER BY settled_at DESC LIMIT 1""",
        (medicine_id,),
    ).fetchone()

    # 仅返回布尔级别的风险信号，绝不返回案件、线索、扣押地、平台等明细
    later_recurrence = bool(conn.execute(
        """SELECT 1 FROM current_settlement cs
           WHERE cs.medicine_id=? AND cs.status!='voided'
             AND EXISTS (
                 SELECT 1 FROM online_sales o
                 WHERE o.medicine_id=cs.medicine_id AND o.sold_at > cs.settled_at
                 UNION ALL
                 SELECT 1 FROM seizures s JOIN seizure_items si ON si.seizure_id=s.seizure_id
                 WHERE si.medicine_id=cs.medicine_id AND s.seized_at > cs.settled_at
             ) LIMIT 1""",
        (medicine_id,),
    ).fetchone())

    result = {
        "found": True,
        "medicine_id": medicine_id,
        "product_name": med["product_name"],
        "spec": med["spec"],
        "manufacturer": med["manufacturer"],
        "batch_no": med["batch_no"],
        "cold_chain": bool(med["is_cold_chain"]),
        "legal_chain": chain,
        "last_settlement": (
            {"institution": settlement["med_inst_name"] or settlement["med_inst_code"],
             "region_code": settlement["region_code"],
             "settled_at": settlement["settled_at"]}
            if settlement else None),
    }
    if settlement and not later_recurrence:
        result["verdict"] = "该药品赋码与正规流通链可核验，已在定点机构正常结算。"
    elif settlement and later_recurrence:
        result["verdict"] = (
            "该药品曾在定点机构医保结算；结算后又出现非正规渠道流通记录，"
            "已不属于合法一手流转，如系网购请谨慎并向医保或药监部门核实。")
    else:
        result["verdict"] = "已查到赋码与流通记录，但未查到医保结算信息。"

    _audit(conn, actor, role, "public_verify", medicine_id,
           detail=f"found={result['found']},settled={settlement is not None},"
                  f"recurrence={later_recurrence}")
    return result


# ---------------------------------------------------------------------------
# 案件人员：完整档案与线索
# ---------------------------------------------------------------------------
def medicine_dossier(conn, user_id: str, medicine_id: str) -> dict:
    role = _role(conn, user_id)
    if role not in STAFF_ROLES:
        _audit(conn, user_id, role, "medicine_dossier", medicine_id,
               result="denied")
        raise PermissionDenied("普通查询者不得查看案件档案")

    med = conn.execute("SELECT * FROM medicines WHERE medicine_id=?",
                       (medicine_id,)).fetchone()
    if not med:
        _audit(conn, user_id, role, "medicine_dossier", medicine_id,
               result="not_found")
        raise AuditError("未找到该追溯码")

    dossier = {
        "medicine": dict(med),
        "versions": [dict(v) for v in conn.execute(
            "SELECT * FROM medicine_versions WHERE medicine_id=? ORDER BY version",
            (medicine_id,))],
        "scan_events": [dict(e) for e in conn.execute(
            "SELECT * FROM scan_events WHERE medicine_id=? ORDER BY event_time, seq",
            (medicine_id,))],
        "settlements": [],
        "vouchers": [],
        "seizures": [],
        "online_sales": [dict(o) for o in conn.execute(
            "SELECT * FROM online_sales WHERE medicine_id=?", (medicine_id,))],
        "clues": [],
    }
    for s in conn.execute(
        """SELECT * FROM current_settlement WHERE medicine_id=?
           ORDER BY settled_at""", (medicine_id,),
    ):
        person = conn.execute(
            """SELECT pv.surname, pv.masked_id_no, pv.region_code
               FROM persons p JOIN person_versions pv
                 ON pv.person_id=p.person_id AND pv.version=p.current_version
               WHERE p.person_id=?""",
            (s["person_id"],)).fetchone()
        item = dict(s)
        item["person"] = dict(person) if person else None
        item["history"] = [dict(h) for h in conn.execute(
            "SELECT * FROM settlement_versions WHERE settlement_id=? ORDER BY version",
            (s["settlement_id"],))]
        dossier["settlements"].append(item)

    # 显式回答"这盒药最初在哪家机构结算"（含被更正/作废过的全部历史版本）
    if dossier["settlements"]:
        first = dossier["settlements"][0]
        dossier["first_settlement"] = {
            "settlement_id": first["settlement_id"],
            "institution": first["med_inst_name"] or first["med_inst_code"],
            "med_inst_code": first["med_inst_code"],
            "region_code": first["region_code"],
            "settled_at": first["settled_at"],
            "is_cross_region": bool(first["is_cross_region"]),
            "current_version": first["current_version"],
        }

    for v in conn.execute(
        """SELECT * FROM current_voucher
           WHERE medicine_id=? OR settlement_id IN
              (SELECT settlement_id FROM settlements WHERE medicine_id=?)""",
        (medicine_id, medicine_id),
    ):
        item = dict(v)
        item["history"] = [dict(h) for h in conn.execute(
            "SELECT * FROM voucher_versions WHERE voucher_id=? ORDER BY version",
            (v["voucher_id"],))]
        dossier["vouchers"].append(item)

    for sz in conn.execute(
        """SELECT s.*, si.pkg_condition, si.observed_code, si.qty
           FROM seizures s JOIN seizure_items si ON si.seizure_id=s.seizure_id
           WHERE si.medicine_id=?""",
        (medicine_id,),
    ):
        dossier["seizures"].append(dict(sz))

    for c in conn.execute(
        "SELECT clue_id FROM risk_clues WHERE medicine_id=?", (medicine_id,),
    ):
        dossier["clues"].append(evidence_chain(conn, c["clue_id"]))

    _audit(conn, user_id, role, "medicine_dossier", medicine_id)
    return dossier


def list_clues(conn, user_id: str, *, status: str | None = None,
               clue_type: str | None = None) -> list[dict]:
    role = _role(conn, user_id)
    if role not in STAFF_ROLES:
        raise PermissionDenied("普通查询者不得查看线索清单")
    sql = "SELECT * FROM risk_clues WHERE 1=1"
    args: list = []
    if status:
        sql += " AND status=?"
        args.append(status)
    if clue_type:
        sql += " AND clue_type=?"
        args.append(clue_type)
    sql += " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at"
    rows = [dict(r) for r in conn.execute(sql, args)]
    for r in rows:
        r["explanation"] = __import__("json").loads(r["explanation"])
    _audit(conn, user_id, role, "list_clues", "-",
           detail=f"status={status},type={clue_type},n={len(rows)}")
    return rows


def original_payload(conn, user_id: str, record_id: str) -> dict:
    """调取某条派生数据背后的原始报文（仅案件人员）。"""
    role = _role(conn, user_id)
    if role not in STAFF_ROLES:
        raise PermissionDenied("普通查询者不得调取原始报文")
    import json
    row = conn.execute("SELECT * FROM raw_records WHERE record_id=?",
                       (record_id,)).fetchone()
    if not row:
        raise AuditError("原始记录不存在")
    _audit(conn, user_id, role, "original_payload", record_id)
    return {"record_id": record_id, "source_type": row["source_type"],
            "source_org": row["source_org"], "observed_at": row["observed_at"],
            "received_at": row["received_at"], "is_late": bool(row["is_late"]),
            "content_hash": row["content_hash"],
            "payload": json.loads(row["payload"])}
