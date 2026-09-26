"""风险线索引擎。

引擎只做一件事：把数据中命中规则的现象整理成**可解释的风险提示**。
它不做、也不允许做违法认定——线索状态恒从 ``open`` 开始，是否移送只能由
有权限的人工账号在研判模块中决定。

三条规则：

* ``cross_region_reappear`` 同一药盒在不同地区再次出现
* ``repeat_prescription``   同参保人同一天短间隔重复开药
* ``cold_chain_mismatch``   冷链材料与真实流转不符

每条线索保存：规则名、阈值、命中输入（含原始记录号与数据版本）、推理说明，
并对命中输入快照取哈希：输入完全相同的重复比对不会产生第二条线索。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta

from .db import canonical_hash

DEFAULT_REPEAT_WINDOW_MIN = 180  # "短间隔"默认阈值，可按办案口径调整


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def run_all(
    conn: sqlite3.Connection,
    *,
    repeat_window_min: int = DEFAULT_REPEAT_WINDOW_MIN,
    medicine_ids: list[str] | None = None,
    person_ids: list[str] | None = None,
) -> dict:
    """执行批量交叉比对，返回新生成线索的统计与编号。"""
    created: list[str] = []
    created += _rule_cross_region(conn, medicine_ids)
    created += _rule_repeat_prescription(conn, repeat_window_min, person_ids)
    created += _rule_cold_chain(conn, medicine_ids)
    return {"created": len(created), "clue_ids": created}


def _save_clue(conn, clue_type: str, severity: str, title: str,
               explanation: dict, dedup_key: str, *,
               medicine_id=None, person_id=None) -> str | None:
    evidence_hash = canonical_hash(explanation["inputs"])
    dup = conn.execute(
        "SELECT 1 FROM risk_clues WHERE clue_type=? AND dedup_key=?",
        (clue_type, dedup_key),
    ).fetchone()
    if dup:
        return None
    clue_id = _new_id("clue")
    conn.execute(
        """INSERT INTO risk_clues(clue_id, clue_type, medicine_id, person_id,
           severity, title, explanation, evidence_hash, dedup_key, created_by_rule)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (clue_id, clue_type, medicine_id, person_id, severity, title,
         json.dumps(explanation, ensure_ascii=False, sort_keys=True),
         evidence_hash, dedup_key, clue_type),
    )
    return clue_id


# ---------------------------------------------------------------------------
# 规则一：同一药盒在不同地区再次出现
# ---------------------------------------------------------------------------
def _sightings(conn, medicine_id: str) -> list[dict]:
    """汇总一盒药的所有"露面"：扫码、结算、网售、扣押。"""
    out: list[dict] = []
    for ev in conn.execute(
        """SELECT event_type, org_code, org_name, region_code, event_time, record_id
           FROM scan_events WHERE medicine_id=? ORDER BY event_time""",
        (medicine_id,),
    ):
        out.append({"kind": "scan", "event_type": ev["event_type"],
                    "org_code": ev["org_code"], "org_name": ev["org_name"],
                    "region_code": ev["region_code"], "time": ev["event_time"],
                    "record_id": ev["record_id"]})
    for st in conn.execute(
        """SELECT cs.region_code, cs.med_inst_code, cs.med_inst_name,
                  cs.prescribed_at, cs.settled_at, cs.record_id, cs.version
           FROM current_settlement cs WHERE cs.medicine_id=? AND cs.status!='voided'""",
        (medicine_id,),
    ):
        out.append({"kind": "settlement", "org_code": st["med_inst_code"],
                    "org_name": st["med_inst_name"], "region_code": st["region_code"],
                    "time": st["settled_at"], "prescribed_at": st["prescribed_at"],
                    "record_id": st["record_id"], "version": st["version"]})
    for o in conn.execute(
        """SELECT platform, shop_name, region_code, buyer_region, sold_at, record_id
           FROM online_sales WHERE medicine_id=? ORDER BY sold_at""",
        (medicine_id,),
    ):
        out.append({"kind": "online_sale", "org_code": o["platform"],
                    "org_name": o["shop_name"], "region_code": o["region_code"],
                    "buyer_region": o["buyer_region"], "time": o["sold_at"],
                    "record_id": o["record_id"]})
    for sz in conn.execute(
        """SELECT s.region_code, s.warehouse_org, s.seized_at, s.record_id,
                  si.pkg_condition
           FROM seizures s JOIN seizure_items si ON si.seizure_id=s.seizure_id
           WHERE si.medicine_id=? ORDER BY s.seized_at""",
        (medicine_id,),
    ):
        out.append({"kind": "seizure", "org_code": sz["warehouse_org"],
                    "region_code": sz["region_code"], "time": sz["seized_at"],
                    "pkg_condition": sz["pkg_condition"], "record_id": sz["record_id"]})
    return sorted(out, key=lambda x: x["time"])


def _rule_cross_region(conn, medicine_ids: list[str] | None) -> list[str]:
    created = []
    if medicine_ids is None:
        rows = conn.execute("SELECT medicine_id FROM medicines").fetchall()
        medicine_ids = [r["medicine_id"] for r in rows]
    for medicine_id in medicine_ids:
        sightings = _sightings(conn, medicine_id)
        regions = {s["region_code"] for s in sightings}
        if len(regions) < 2:
            continue
        # 关键形态：已结算（药已到参保人手中）后，又在结算地以外的地区露面
        settlements = [s for s in sightings if s["kind"] == "settlement"]
        if not settlements:
            continue
        first_settle = min(settlements, key=lambda s: s["time"])
        later = [s for s in sightings
                 if s["time"] > first_settle["time"]
                 and s["region_code"] != first_settle["region_code"]]
        if not later:
            continue
        hot = any(s["kind"] in ("online_sale", "seizure") for s in later)
        severity = "high" if hot else "medium"
        title = (f"药盒 {medicine_id} 在结算地 {first_settle['region_code']} 之外"
                 f"再次出现（{len(later)} 次）")
        rationale = (
            f"该药盒已于 {first_settle['time']} 在 {first_settle['region_code']} "
            f"结算出机构，按正常流向不应再次进入流通；其后在 "
            f"{sorted({s['region_code'] for s in later})} 出现 "
            f"{len(later)} 次露面记录。该现象提示可能存在回流或二次销售，"
            "但是否违法需结合票账货款与人工核查综合判断，规则不作认定。"
        )
        explanation = {
            "rule": "cross_region_reappear",
            "threshold": "结算后在不同地区出现 1 次及以上",
            "inputs": sightings,
            "rationale": rationale,
        }
        clue_id = _save_clue(conn, "cross_region_reappear", severity, title,
                             explanation, medicine_id, medicine_id=medicine_id)
        if clue_id:
            created.append(clue_id)
    return created


# ---------------------------------------------------------------------------
# 规则二：同参保人同一天短间隔重复开药
# ---------------------------------------------------------------------------
def _rule_repeat_prescription(conn, window_min: int,
                              person_ids: list[str] | None) -> list[str]:
    created = []
    if person_ids is None:
        rows = conn.execute("SELECT person_id FROM persons").fetchall()
        person_ids = [r["person_id"] for r in rows]
    for person_id in person_ids:
        rows = conn.execute(
            """SELECT cs.* FROM current_settlement cs
               WHERE cs.person_id=? AND cs.status!='voided'
               ORDER BY cs.prescribed_at""",
            (person_id,),
        ).fetchall()
        # 按自然日分组，检验相邻开药时间间隔
        by_day: dict[str, list] = {}
        for r in rows:
            by_day.setdefault(r["prescribed_at"][:10], []).append(r)
        for day, items in by_day.items():
            if len(items) < 2:
                continue
            gaps = []
            hit_pair = None
            for a, b in zip(items, items[1:]):
                gap = (_parse(b["prescribed_at"]) - _parse(a["prescribed_at"])).total_seconds() / 60
                gaps.append(gap)
                if gap <= window_min and hit_pair is None:
                    hit_pair = (a, b, gap)
            if hit_pair is None:
                continue
            a, b, gap = hit_pair
            inputs = [{
                "settlement_id": r["settlement_id"], "version": r["version"],
                "record_id": r["record_id"], "med_inst_code": r["med_inst_code"],
                "med_inst_name": r["med_inst_name"], "region_code": r["region_code"],
                "medicine_id": r["medicine_id"], "prescribed_at": r["prescribed_at"],
                "settled_at": r["settled_at"], "quantity": r["quantity"],
                "is_cross_region": bool(r["is_cross_region"]),
            } for r in items]
            title = f"参保人 {person_id} 于 {day} 短间隔重复开药（最小间隔 {gap:.0f} 分钟）"
            rationale = (
                f"该参保人在 {day} 共开药 {len(items)} 次，"
                f"{a['med_inst_name'] or a['med_inst_code']} 与 "
                f"{b['med_inst_name'] or b['med_inst_code']} 两次开药仅相隔 "
                f"{gap:.0f} 分钟，未超过阈值 {window_min} 分钟。"
                "可能为分解处方、多头开药或代购药，规则仅作提示，"
                "是否构成违规待遇领取需人工结合病情与处方核验。"
            )
            explanation = {
                "rule": "repeat_prescription",
                "threshold": f"同一自然日内相邻开药间隔 <= {window_min} 分钟",
                "inputs": inputs,
                "rationale": rationale,
            }
            clue_id = _save_clue(conn, "repeat_prescription", "medium", title,
                                 explanation, f"{person_id}|{day}",
                                 person_id=person_id)
            if clue_id:
                created.append(clue_id)
    return created


# ---------------------------------------------------------------------------
# 规则三：冷链材料与真实流转不符
# ---------------------------------------------------------------------------
def _rule_cold_chain(conn, medicine_ids: list[str] | None) -> list[str]:
    created = []
    if medicine_ids is None:
        rows = conn.execute(
            "SELECT medicine_id FROM medicines WHERE is_cold_chain=1").fetchall()
        medicine_ids = [r["medicine_id"] for r in rows]
    for medicine_id in medicine_ids:
        med = conn.execute(
            "SELECT is_cold_chain, current_version FROM medicines WHERE medicine_id=?",
            (medicine_id,),
        ).fetchone()
        if not med or not med["is_cold_chain"]:
            continue
        vouchers = conn.execute(
            """SELECT * FROM current_voucher
               WHERE (medicine_id=? OR settlement_id IN
                      (SELECT settlement_id FROM settlements WHERE medicine_id=?))
                 AND voucher_type='cold_chain' AND status!='voided'""",
            (medicine_id, medicine_id),
        ).fetchall()
        events = conn.execute(
            """SELECT event_type, org_code, region_code, event_time, record_id
               FROM scan_events
               WHERE medicine_id=? AND event_type IN ('warehouse','distribute','hospital_in')
               ORDER BY event_time""",
            (medicine_id,),
        ).fetchall()

        contradictions: list[dict] = []
        for v in vouchers:
            # 情形 A：实际/第三方探头温度超出材料声称区间
            if v["temp_recorded"] is not None and v["temp_min"] is not None \
                    and v["temp_max"] is not None:
                if v["temp_recorded"] < v["temp_min"] or v["temp_recorded"] > v["temp_max"]:
                    contradictions.append({
                        "type": "temp_out_of_range",
                        "voucher_id": v["voucher_id"], "version": v["version"],
                        "record_id": v["record_id"],
                        "claimed_range": [v["temp_min"], v["temp_max"]],
                        "temp_recorded": v["temp_recorded"],
                    })
            # 情形 B：材料声称的冷链时段未覆盖真实运输/入库环节
            if v["cold_window_start"] and v["cold_window_end"]:
                win_s, win_e = _parse(v["cold_window_start"]), _parse(v["cold_window_end"])
                for e in events:
                    t = _parse(e["event_time"])
                    if t < win_s or t > win_e:
                        contradictions.append({
                            "type": "window_not_covered",
                            "voucher_id": v["voucher_id"], "version": v["version"],
                            "record_id": v["record_id"],
                            "claimed_window": [v["cold_window_start"], v["cold_window_end"]],
                            "event": {"event_type": e["event_type"],
                                      "org_code": e["org_code"],
                                      "region_code": e["region_code"],
                                      "event_time": e["event_time"],
                                      "record_id": e["record_id"]},
                        })
        if not contradictions:
            continue
        title = f"药盒 {medicine_id} 的冷链证明材料与实际流转记录不符"
        rationale = (
            f"发现 {len(contradictions)} 处冷链矛盾："
            + "；".join(
                "探头温度超出声称区间" if c["type"] == "temp_out_of_range"
                else f"真实环节 {c['event']['event_type']}@{c['event']['event_time']} "
                     "落在声称冷链时段之外"
                for c in contradictions)
            + "。提示冷链材料可能补造或与真实物流不一致，规则不作违法认定，"
              "需调取物流温控原件人工核验。"
        )
        explanation = {
            "rule": "cold_chain_mismatch",
            "threshold": "实际温度超出声称区间，或真实环节时间不在声称冷链时段内",
            "inputs": {
                "medicine_id": medicine_id,
                "medicine_version": med["current_version"],
                "contradictions": contradictions,
            },
            "rationale": rationale,
        }
        clue_id = _save_clue(conn, "cold_chain_mismatch", "high", title,
                             explanation, medicine_id, medicine_id=medicine_id)
        if clue_id:
            created.append(clue_id)
    return created
