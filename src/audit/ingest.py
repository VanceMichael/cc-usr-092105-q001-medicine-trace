"""原始来源入库。

六类来源（药品赋码与扫码、参保人就医结算、票账货款、仓库扣押、
网络销售、办案协作）走同一个入口：

1. 原始报文按来源与内容哈希幂等落 ``raw_records``，重复上传直接跳过，不改旧数据；
2. 业务事实由原始报文派生：新增追加，更正产生新版本号，历史版本永不覆盖；
3. 业务时间早于该来源已收最新数据的报文标记为"迟到数据"，照常入库并打标。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from .db import AuditError, canonical_hash

SOURCE_TYPES = ("coding", "settlement", "voucher", "seizure", "online", "cooperation")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _require(payload: dict, fields: tuple[str, ...]) -> None:
    missing = [f for f in fields if payload.get(f) in (None, "")]
    if missing:
        raise AuditError(f"报文缺少必要字段: {', '.join(missing)}")


def ingest_batch(
    conn: sqlite3.Connection,
    source_type: str,
    source_org: str,
    records: list[dict],
    *,
    submitted_by: str | None = None,
    note: str | None = None,
    batch_id: str | None = None,
) -> dict:
    """入库一批同一来源的原始报文，返回处理统计。

    每条 record 至少包含 ``observed_at``（报文所述业务时间），其余字段为
    各来源专属内容。整批在一个立即事务内串行提交，重复上传安全。
    """
    if source_type not in SOURCE_TYPES:
        raise AuditError(f"未知来源类型: {source_type}")
    if not records:
        raise AuditError("空批次不允许入库")

    batch_id = batch_id or _new_id("b")
    stats = {"batch_id": batch_id, "received": len(records), "deduped": 0,
             "ingested": 0, "late": 0, "corrections": []}

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 批次行先落库，raw_records.batch_id 外键才有着落；统计字段处理完再更新
        conn.execute(
            """INSERT INTO raw_batches
               (batch_id, source_type, source_org, submitted_by,
                record_count, deduped_count, note)
               VALUES (?,?,?,?,0,0,?)""",
            (batch_id, source_type, source_org, submitted_by, note),
        )
        for payload in records:
            _require(payload, ("observed_at",))
            content_hash = canonical_hash(payload)
            existed = conn.execute(
                "SELECT 1 FROM raw_records WHERE source_type=? AND content_hash=?",
                (source_type, content_hash),
            ).fetchone()
            if existed:
                stats["deduped"] += 1
                continue

            # 迟到判定：同一来源通道已收到业务时间更晚的数据
            row = conn.execute(
                "SELECT MAX(observed_at) AS m FROM raw_records WHERE source_type=?",
                (source_type,),
            ).fetchone()
            is_late = 1 if row["m"] and payload["observed_at"] < row["m"] else 0
            if is_late:
                stats["late"] += 1

            record_id = _new_id("r")
            conn.execute(
                """INSERT INTO raw_records
                   (record_id, batch_id, source_type, source_org, source_record_key,
                    content_hash, payload, observed_at, is_late)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (record_id, batch_id, source_type, source_org,
                 payload.get("record_key"), content_hash,
                 _dump(payload), payload["observed_at"], is_late),
            )
            _derive(conn, source_type, payload, record_id, stats)
            stats["ingested"] += 1

        conn.execute(
            """UPDATE raw_batches SET record_count=?, deduped_count=?
               WHERE batch_id=?""",
            (stats["ingested"], stats["deduped"], batch_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return stats


def _dump(value) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# 派生字表
# ---------------------------------------------------------------------------
_MEDICINE_FIELDS = ("code_status", "product_name", "spec", "manufacturer",
                    "batch_no", "produced_at", "is_cold_chain")
_PERSON_FIELDS = ("surname", "masked_id_no", "region_code")
_SETTLE_FIELDS = ("med_inst_code", "med_inst_name", "region_code", "diagnosis",
                  "prescribed_at", "settled_at", "quantity", "amount", "fund_type",
                  "is_cross_region", "status")
_VOUCHER_FIELDS = ("voucher_type", "doc_no", "party_org", "amount", "region_code",
                   "issued_at", "temp_min", "temp_max", "temp_recorded",
                   "cold_window_start", "cold_window_end", "status")


def _derive(conn, source_type, p: dict, record_id: str, stats: dict) -> None:
    if source_type == "coding":
        kind = p.get("kind", "scan")
        if kind == "medicine":
            _upsert_medicine(conn, p, record_id, stats)
        else:
            _add_scan(conn, p, record_id)
    elif source_type == "settlement":
        _add_settlement(conn, p, record_id, stats)
    elif source_type == "voucher":
        _add_voucher(conn, p, record_id, stats)
    elif source_type == "seizure":
        _add_seizure(conn, p, record_id)
    elif source_type == "online":
        _add_online(conn, p, record_id)
    elif source_type == "cooperation":
        _add_coop(conn, p, record_id)


def _changed(conn, hist_table: str, id_col: str, ident: str,
             version: int, fields: tuple[str, ...], payload: dict) -> bool:
    """指定历史版本与新报文在关注字段上是否存在差异。"""
    row = conn.execute(
        f"SELECT * FROM {hist_table} WHERE {id_col}=? AND version=?",
        (ident, version),
    ).fetchone()
    return any(_norm(row[f]) != _norm(payload.get(f)) for f in fields)


def _norm(v):
    if v is None:
        return None
    return v


def _upsert_medicine(conn, p: dict, record_id: str, stats: dict) -> None:
    _require(p, ("medicine_id",))
    values = {f: p.get(f) for f in _MEDICINE_FIELDS}
    values["code_status"] = values["code_status"] or "normal"
    values["is_cold_chain"] = int(values["is_cold_chain"] or 0)
    row = conn.execute("SELECT current_version FROM medicines WHERE medicine_id=?",
                       (p["medicine_id"],)).fetchone()
    if row is None:
        version = 1
        conn.execute(
            """INSERT INTO medicines(medicine_id, code_status, product_name, spec,
               manufacturer, batch_no, produced_at, is_cold_chain,
               current_version, first_record_id)
               VALUES (?,?,?,?,?,?,?,?,1,?)""",
            (p["medicine_id"], values["code_status"], values["product_name"],
             values["spec"], values["manufacturer"], values["batch_no"],
             values["produced_at"], values["is_cold_chain"], record_id),
        )
    else:
        if not _changed(conn, "medicine_versions", "medicine_id", p["medicine_id"],
                        row["current_version"], tuple(_MEDICINE_FIELDS), p):
            return  # 内容相同的派生请求（理论已被原始去重拦截）
        version = row["current_version"] + 1
        conn.execute(
            "UPDATE medicines SET code_status=?, product_name=?, spec=?, "
            "manufacturer=?, batch_no=?, produced_at=?, is_cold_chain=?, "
            "current_version=?, updated_at=? WHERE medicine_id=?",
            (values["code_status"], values["product_name"], values["spec"],
             values["manufacturer"], values["batch_no"], values["produced_at"],
             values["is_cold_chain"], version, _now(), p["medicine_id"]),
        )
        stats["corrections"].append(
            {"entity": "medicine", "id": p["medicine_id"],
             "old_version": row["current_version"], "new_version": version})
    conn.execute(
        """INSERT INTO medicine_versions(medicine_id, version, record_id,
           code_status, product_name, spec, manufacturer, batch_no, produced_at,
           is_cold_chain, change_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (p["medicine_id"], version, record_id, values["code_status"],
         values["product_name"], values["spec"], values["manufacturer"],
         values["batch_no"], values["produced_at"], values["is_cold_chain"],
         p.get("change_reason")),
    )


def _add_scan(conn, p: dict, record_id: str) -> None:
    _require(p, ("medicine_id", "event_type", "org_code", "region_code", "event_time"))
    conn.execute(
        """INSERT OR IGNORE INTO scan_events(event_id, medicine_id, seq, event_type,
           org_code, org_name, region_code, event_time, record_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (p.get("event_id") or _new_id("ev"), p["medicine_id"],
         p.get("seq", 0), p["event_type"], p["org_code"], p.get("org_name"),
         p["region_code"], p["event_time"], record_id),
    )


def _upsert_person(conn, p: dict, record_id: str) -> str:
    person = p["person"]
    _require(person, ("person_id",))
    row = conn.execute("SELECT current_version FROM persons WHERE person_id=?",
                       (person["person_id"],)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO persons(person_id, id_hash, current_version, first_record_id) "
            "VALUES (?,?,1,?)",
            (person["person_id"], person.get("id_hash"), record_id),
        )
        version = 1
    else:
        cur = conn.execute(
            "SELECT * FROM person_versions WHERE person_id=? AND version=?",
            (person["person_id"], row["current_version"])).fetchone()
        diff = any(cur[f] != person.get(f) for f in _PERSON_FIELDS)
        version = row["current_version"] + (1 if diff else 0)
        if not diff:
            return person["person_id"]
    conn.execute(
        """INSERT INTO person_versions(person_id, version, record_id,
           surname, masked_id_no, region_code) VALUES (?,?,?,?,?,?)""",
        (person["person_id"], version, record_id, person.get("surname"),
         person.get("masked_id_no"), person.get("region_code")),
    )
    conn.execute("UPDATE persons SET current_version=? WHERE person_id=?",
                 (version, person["person_id"]))
    return person["person_id"]


def _add_settlement(conn, p: dict, record_id: str, stats: dict) -> None:
    _require(p, ("settlement_id", "person", "medicine_id", "med_inst_code",
                 "region_code", "prescribed_at", "settled_at"))
    _upsert_medicine_ref(conn, p["medicine_id"], record_id)
    _upsert_person(conn, p, record_id)
    values = {f: p.get(f) for f in _SETTLE_FIELDS}
    values["is_cross_region"] = int(values["is_cross_region"] or 0)
    values["status"] = values["status"] or "valid"
    values["quantity"] = p.get("quantity", 1)
    row = conn.execute(
        "SELECT current_version FROM settlements WHERE settlement_id=?",
        (p["settlement_id"],)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO settlements(settlement_id, person_id, medicine_id, "
            "current_version, first_record_id) VALUES (?,?,?,1,?)",
            (p["settlement_id"], p["person"]["person_id"], p["medicine_id"], record_id),
        )
        version = 1
    else:
        cur = conn.execute(
            "SELECT * FROM settlement_versions WHERE settlement_id=? AND version=?",
            (p["settlement_id"], row["current_version"])).fetchone()
        diff = any(_norm(cur[f]) != _norm(values[f]) for f in _SETTLE_FIELDS)
        if not diff:
            return
        version = row["current_version"] + 1
        conn.execute("UPDATE settlements SET current_version=? WHERE settlement_id=?",
                     (version, p["settlement_id"]))
        stats["corrections"].append(
            {"entity": "settlement", "id": p["settlement_id"],
             "old_version": row["current_version"], "new_version": version})
    conn.execute(
        """INSERT INTO settlement_versions(settlement_id, version, record_id,
           med_inst_code, med_inst_name, region_code, diagnosis, prescribed_at,
           settled_at, quantity, amount, fund_type, is_cross_region, status,
           change_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p["settlement_id"], version, record_id, values["med_inst_code"],
         values["med_inst_name"], values["region_code"], values["diagnosis"],
         values["prescribed_at"], values["settled_at"],
         p.get("quantity", 1), values["amount"], values["fund_type"],
         values["is_cross_region"], values["status"], p.get("change_reason")),
    )


def _upsert_medicine_ref(conn, medicine_id: str, record_id: str) -> None:
    """结算报文引用到尚未建档的追溯码时，先占位建档，等待赋码来源补全。"""
    row = conn.execute("SELECT 1 FROM medicines WHERE medicine_id=?",
                       (medicine_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO medicines(medicine_id, current_version, first_record_id) "
            "VALUES (?,1,?)",
            (medicine_id, record_id),
        )
        conn.execute(
            """INSERT INTO medicine_versions(medicine_id, version, record_id,
               code_status) VALUES (?,1,?,'normal')""",
            (medicine_id, record_id),
        )


def _add_voucher(conn, p: dict, record_id: str, stats: dict) -> None:
    _require(p, ("voucher_id", "voucher_type"))
    values = {f: p.get(f) for f in _VOUCHER_FIELDS}
    values["status"] = values["status"] or "valid"
    row = conn.execute("SELECT current_version FROM vouchers WHERE voucher_id=?",
                       (p["voucher_id"],)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO vouchers(voucher_id, medicine_id, settlement_id, "
            "current_version, first_record_id) VALUES (?,?,?,1,?)",
            (p["voucher_id"], p.get("medicine_id"), p.get("settlement_id"), record_id),
        )
        version = 1
    else:
        cur = conn.execute(
            "SELECT * FROM voucher_versions WHERE voucher_id=? AND version=?",
            (p["voucher_id"], row["current_version"])).fetchone()
        diff = any(_norm(cur[f]) != _norm(values[f]) for f in _VOUCHER_FIELDS)
        if not diff:
            return
        version = row["current_version"] + 1
        conn.execute("UPDATE vouchers SET current_version=? WHERE voucher_id=?",
                     (version, p["voucher_id"]))
        stats["corrections"].append(
            {"entity": "voucher", "id": p["voucher_id"],
             "old_version": row["current_version"], "new_version": version})
    conn.execute(
        """INSERT INTO voucher_versions(voucher_id, version, record_id, voucher_type,
           doc_no, party_org, amount, region_code, issued_at, temp_min, temp_max,
           temp_recorded, cold_window_start, cold_window_end, status, change_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (p["voucher_id"], version, record_id, values["voucher_type"], values["doc_no"],
         values["party_org"], values["amount"], values["region_code"], values["issued_at"],
         values["temp_min"], values["temp_max"], values["temp_recorded"],
         values["cold_window_start"], values["cold_window_end"],
         values["status"], p.get("change_reason")),
    )


def _add_seizure(conn, p: dict, record_id: str) -> None:
    _require(p, ("seizure_id", "warehouse_org", "region_code", "seized_at", "items"))
    conn.execute(
        """INSERT OR IGNORE INTO seizures(seizure_id, case_id, warehouse_org,
           region_code, seized_at, officer, record_id, note)
           VALUES (?,?,?,?,?,?,?,?)""",
        (p["seizure_id"], p.get("case_id"), p["warehouse_org"], p["region_code"],
         p["seized_at"], p.get("officer"), record_id, p.get("note")),
    )
    for item in p["items"]:
        _require(item, ("medicine_id",))
        _upsert_medicine_ref(conn, item["medicine_id"], record_id)
        conn.execute(
            """INSERT OR IGNORE INTO seizure_items(seizure_id, medicine_id,
               pkg_condition, observed_code, qty) VALUES (?,?,?,?,?)""",
            (p["seizure_id"], item["medicine_id"],
             item.get("pkg_condition", "intact"), item.get("observed_code"),
             item.get("qty", 1)),
        )


def _add_online(conn, p: dict, record_id: str) -> None:
    _require(p, ("online_id", "medicine_id", "platform", "region_code", "sold_at"))
    conn.execute(
        """INSERT OR IGNORE INTO online_sales(online_id, medicine_id, platform,
           shop_name, region_code, listing_time, sold_at, buyer_region, price, record_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (p["online_id"], p["medicine_id"], p["platform"], p.get("shop_name"),
         p["region_code"], p.get("listing_time"), p["sold_at"],
         p.get("buyer_region"), p.get("price"), record_id),
    )


def _add_coop(conn, p: dict, record_id: str) -> None:
    _require(p, ("coop_id", "from_org", "to_org", "channel", "payload_summary"))
    conn.execute(
        """INSERT OR IGNORE INTO cooperations(coop_id, case_id, from_org, to_org,
           channel, request_at, response_at, payload_summary, record_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (p["coop_id"], p.get("case_id"), p["from_org"], p["to_org"], p["channel"],
         p.get("request_at"), p.get("response_at"), p["payload_summary"], record_id),
    )
