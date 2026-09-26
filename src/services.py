"""稽核服务层：批量交叉比对、线索版本、并发研判、封存、交接与谱系还原。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .rules import RULES, Finding
from .store import SOURCE_KINDS, Store, canonical_digest, utcnow


class ConflictError(Exception):
    """乐观锁冲突：线索在读取后已被他人更新。"""


class AccessDenied(Exception):
    """非案件成员或角色不足。"""


def _jid() -> str:
    return "J-" + uuid.uuid4().hex[:12]


def _lid() -> str:
    return "XS-" + uuid.uuid4().hex[:12]  # 线索编号


def _pid(prefix: str) -> str:
    return f"{prefix}-" + uuid.uuid4().hex[:12]


class AuditService:
    def __init__(self, store: Store):
        self.store = store

    # ================= 数据切点 =================

    def create_data_cut(self, note: str = "") -> dict:
        """冻结当前数据版本：以后任何还原都能精确回到这一刻。"""
        with self.store.tx() as conn:
            max_id = conn.execute(
                "SELECT COALESCE(MAX(id),0) AS m FROM source_record").fetchone()["m"]
            rows = conn.execute(
                "SELECT source_kind,business_key,record_id,version,digest "
                "FROM current_view ORDER BY source_kind,business_key").fetchall()
            digest = canonical_digest([dict(r) for r in rows])
            cut_no = _pid("CUT")
            cur = conn.execute(
                "INSERT INTO data_cut(cut_no,created_at,max_record_id,digest,note) "
                "VALUES(?,?,?,?,?)",
                (cut_no, utcnow(), max_id, digest, note))
            return {"data_cut_id": cur.lastrowid, "cut_no": cut_no,
                    "max_record_id": max_id, "digest": digest,
                    "current_view_count": len(rows)}

    def get_data_cut(self, data_cut_id: int) -> dict:
        with self.store._lock:
            r = self.store._conn.execute(
                "SELECT * FROM data_cut WHERE id=?", (data_cut_id,)).fetchone()
        if not r:
            raise KeyError("数据切点不存在")
        return dict(r)

    # ================= 批量交叉比对 =================

    def run_batch(self, rule_codes: list[str] | None = None,
                  params: dict[str, dict] | None = None,
                  scope: dict | None = None, trigger: str = "manual",
                  note: str = "批量交叉比对") -> dict:
        """运行一次批量比对作业。

        - 作业开始前冻结数据切点，线索永远绑定产生它时的数据版本；
        - 迟到数据到达后可再次运行（trigger='late_data'），新发现追加新版本；
        - 已经被人工 dismiss/confirm_risk/refer 的线索不会被机器状态覆盖，
          只追加一条复评版本保留机器意见。
        """
        rule_codes = rule_codes or list(RULES)
        params = params or {}
        scope = scope or {}
        job_id = _jid()
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO batch_job(job_id,rule_version,scope_json,params_json,"
                "status,created_at,started_at,trigger) VALUES(?,?,?,?,?,?,?,?)",
                (job_id, ",".join(sorted(rule_codes)),
                 json.dumps(scope, ensure_ascii=False),
                 json.dumps(params, ensure_ascii=False), "running",
                 utcnow(), utcnow(), trigger))
        cut = self.create_data_cut(note=f"作业 {job_id} 的数据切点：{note}")

        all_findings: list[Finding] = []
        rule_versions = {}
        for code in rule_codes:
            rule = RULES[code]
            p = {**rule.default_params, **params.get(code, {})}
            rule_versions[code] = rule.version
            all_findings += rule.evaluate(self.store, p, scope)

        created, updated = [], []
        with self.store.tx() as conn:
            for f in all_findings:
                self._upsert_lead(conn, f, job_id, cut["data_cut_id"],
                                  created, updated)
            stats = {"findings": len(all_findings),
                     "created": len(created), "updated": len(updated),
                     "by_rule": {c: sum(1 for f in all_findings
                                        if f.rule_code == c)
                                 for c in rule_codes}}
            conn.execute(
                "UPDATE batch_job SET status='done',finished_at=?,stats_json=? "
                "WHERE job_id=?",
                (utcnow(), json.dumps(stats, ensure_ascii=False), job_id))
        return {"job_id": job_id, "trigger": trigger, "data_cut": cut,
                "stats": stats, "created": created, "updated": updated,
                "rule_versions": rule_versions}

    def _upsert_lead(self, conn, f: Finding, job_id: str, data_cut_id: int,
                     created: list, updated: list) -> None:
        existing = conn.execute(
            "SELECT * FROM risk_lead WHERE rule_code=? AND subject_type=? "
            "AND subject_id=?",
            (f.rule_code, f.subject_type, f.subject_id)).fetchone()
        if existing is None:
            lead_no = _lid()
            cur = conn.execute(
                "INSERT INTO risk_lead(lead_no,rule_code,rule_version,severity,"
                "title,explanation,subject_type,subject_id,status,created_at,"
                "first_job_id,latest_job_id,latest_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lead_no, f.rule_code, f.rule_version, f.severity, f.title,
                 f.explanation, f.subject_type, f.subject_id, "open",
                 utcnow(), job_id, job_id, 1))
            lead_id = cur.lastrowid
            self._insert_version(conn, lead_id, 1, job_id, f, data_cut_id)
            created.append({"lead_no": lead_no, "rule_code": f.rule_code,
                            "subject_id": f.subject_id})
        else:
            lead_id = existing["id"]
            vno = existing["latest_version"] + 1
            conn.execute(
                "UPDATE risk_lead SET severity=?,title=?,explanation=?,"
                "rule_version=?,latest_job_id=?,latest_version=?,"
                "revision=revision+1 WHERE id=?",
                (f.severity, f.title, f.explanation, f.rule_version, job_id,
                 vno, lead_id))
            self._insert_version(conn, lead_id, vno, job_id, f, data_cut_id)
            updated.append({"lead_no": existing["lead_no"],
                            "new_version": vno})

    def _insert_version(self, conn, lead_id: int, vno: int, job_id: str,
                        f: Finding, data_cut_id: int) -> None:
        evidence = [{k: e[k] for k in
                     ("source_kind", "business_key", "record_id", "version",
                      "digest", "note")} for e in f.evidence]
        conn.execute(
            "INSERT INTO lead_version(lead_id,version_no,job_id,rule_version,"
            "params_json,evidence_json,data_cut_id,explanation,severity,"
            "produced_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (lead_id, vno, job_id, f.rule_version,
             json.dumps(f.params, ensure_ascii=False),
             json.dumps(evidence, ensure_ascii=False), data_cut_id,
             f.explanation, f.severity, utcnow()))

    # ================= 线索查询 =================

    def get_lead(self, lead_no: str, with_versions: bool = True) -> dict:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT * FROM risk_lead WHERE lead_no=?", (lead_no,)).fetchone()
            if not row:
                raise KeyError("线索不存在")
            lead = dict(row)
            if with_versions:
                vs = self.store._conn.execute(
                    "SELECT lv.*, dc.cut_no, dc.digest AS cut_digest, "
                    "dc.max_record_id FROM lead_version lv JOIN data_cut dc "
                    "ON dc.id=lv.data_cut_id WHERE lv.lead_id=? ORDER BY version_no",
                    (lead["id"],)).fetchall()
                lead["versions"] = [dict(v) for v in vs]
                decs = self.store._conn.execute(
                    "SELECT * FROM lead_decision WHERE lead_id=? ORDER BY seq",
                    (lead["id"],)).fetchall()
                lead["decisions"] = [dict(d) for d in decs]
        for v in lead.get("versions", []):
            v["evidence"] = json.loads(v["evidence_json"])
            v["params"] = json.loads(v["params_json"])
            v["data_cut"] = {"cut_no": v.pop("cut_no"),
                             "digest": v.pop("cut_digest"),
                             "max_record_id": v.pop("max_record_id")}
            del v["evidence_json"], v["params_json"]
        for d in lead.get("decisions", []):
            if d.get("payload_json"):
                d["payload"] = json.loads(d["payload_json"])
                del d["payload_json"]
        return lead

    def list_leads(self, status: str | None = None,
                   rule_code: str | None = None) -> list[dict]:
        sql = ("SELECT lead_no,rule_code,severity,title,subject_type,"
               "subject_id,status,latest_version,created_at FROM risk_lead")
        where, args = [], []
        if status:
            where.append("status=?"), args.append(status)
        if rule_code:
            where.append("rule_code=?"), args.append(rule_code)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1"
        sql += " ELSE 2 END, created_at"
        with self.store._lock:
            return [dict(r) for r in self.store._conn.execute(sql, args).fetchall()]

    # ================= 人工研判（并发安全） =================

    STATE_ACTIONS = {
        "start_review": "reviewing",
        "confirm_risk": "confirmed_risk",   # 仅确认"风险成立"，仍非违法认定
        "dismiss": "dismissed",
        "refer": "referred",                # 移送（违法认定由司法/行政程序作出）
    }
    TERMINAL_NOTE = {
        "dismiss": "已排除：系统线索不再自动改写该状态，复评意见仍逐版留存",
        "confirm_risk": "风险成立（人工）：不构成违法定性，定性以法定程序为准",
        "refer": "已移送：后续以受案机关结论为准",
    }

    def decide(self, lead_no: str, actor: str, action: str,
               expected_version: int | None = None, comment: str = "",
               payload: dict | None = None) -> dict:
        """追加一条人工决定。

        expected_version 为调用方读取到的 latest_version（乐观锁）：
        若期间他人已更新线索，抛出 ConflictError，调用方须先重读再决定。
        任何动作只追加，不覆盖历史决定。
        """
        if action not in ("claim", "annotate", *self.STATE_ACTIONS):
            raise ValueError(f"未知动作: {action}")
        case_id = payload.get("case_id") if payload else None
        stored_payload = {k: v for k, v in (payload or {}).items()
                          if k != "case_id"}
        with self.store.tx() as conn:
            row = conn.execute(
                "SELECT * FROM risk_lead WHERE lead_no=?", (lead_no,)).fetchone()
            if not row:
                raise KeyError("线索不存在")
            if expected_version is not None and \
                    row["revision"] != expected_version:
                raise ConflictError(
                    f"线索已被他人更新：读取时修订号 r{expected_version}，"
                    f"当前 r{row['revision']}，请重读后再提交决定")
            nxt = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM lead_decision "
                "WHERE lead_id=?", (row["id"],)).fetchone()["s"]
            conn.execute(
                "INSERT INTO lead_decision(lead_id,case_id,seq,action,actor,"
                "comment,lock_version,made_at,payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (row["id"], case_id, nxt, action, actor, comment,
                 row["revision"], utcnow(),
                 json.dumps(stored_payload, ensure_ascii=False)
                 if stored_payload else None))
            if action in self.STATE_ACTIONS:
                conn.execute("UPDATE risk_lead SET status=? WHERE id=?",
                             (self.STATE_ACTIONS[action], row["id"]))
            conn.execute("UPDATE risk_lead SET revision=revision+1 WHERE id=?",
                         (row["id"],))
            return {"lead_no": lead_no, "seq": nxt, "action": action,
                    "actor": actor, "based_on_revision": row["revision"],
                    "note": self.TERMINAL_NOTE.get(action, "")}

    # ================= 案件、成员 =================

    def create_case(self, case_no: str, title: str, region: str,
                    owner: str) -> dict:
        with self.store.tx() as conn:
            conn.execute(
                "INSERT INTO case_file(case_no,title,region,created_at,status) "
                "VALUES(?,?,?,?,'active')",
                (case_no, title, region, utcnow()))
            cid = conn.execute("SELECT id FROM case_file WHERE case_no=?",
                               (case_no,)).fetchone()["id"]
            conn.execute(
                "INSERT INTO case_member(case_id,user_id,role,added_at) "
                "VALUES(?,?,?,?)", (cid, owner, "supervisor", utcnow()))
        return {"case_no": case_no, "id": cid}

    def add_member(self, case_no: str, user_id: str, role: str) -> None:
        if role not in ("investigator", "supervisor", "viewer"):
            raise ValueError("角色必须为 investigator/supervisor/viewer")
        with self.store.tx() as conn:
            cid = self._case_id(conn, case_no)
            conn.execute(
                "INSERT INTO case_member(case_id,user_id,role,added_at) "
                "VALUES(?,?,?,?) ON CONFLICT(case_id,user_id) DO UPDATE SET role=excluded.role",
                (cid, user_id, role, utcnow()))

    def link_lead(self, case_no: str, lead_no: str, user_id: str) -> None:
        self.require_case_member(case_no, user_id)
        with self.store.tx() as conn:
            cid = self._case_id(conn, case_no)
            lid = conn.execute("SELECT id FROM risk_lead WHERE lead_no=?",
                               (lead_no,)).fetchone()["id"]
            conn.execute(
                "INSERT OR IGNORE INTO lead_link(lead_id,case_id,linked_at,linked_by) "
                "VALUES(?,?,?,?)", (lid, cid, utcnow(), user_id))
            nxt = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM lead_decision "
                "WHERE lead_id=?", (lid,)).fetchone()["s"]
            conn.execute(
                "INSERT INTO lead_decision(lead_id,case_id,seq,action,actor,"
                "comment,lock_version,made_at) VALUES(?,?,?,?,?,?,?,?)",
                (lid, cid, nxt, "claim", user_id, f"纳入案件 {case_no}",
                 conn.execute("SELECT revision FROM risk_lead WHERE id=?",
                              (lid,)).fetchone()["revision"], utcnow()))
            conn.execute("UPDATE risk_lead SET revision=revision+1 WHERE id=?",
                         (lid,))

    @staticmethod
    def _case_id(conn, case_no: str) -> int:
        r = conn.execute("SELECT id,status FROM case_file WHERE case_no=?",
                         (case_no,)).fetchone()
        if not r:
            raise KeyError("案件不存在")
        return r["id"]

    def require_case_member(self, case_no: str, user_id: str,
                            need_roles=("investigator", "supervisor")) -> int:
        """返回 case_id；非成员或角色不足时拒绝。viewer 只读。"""
        with self.store._lock:
            r = self.store._conn.execute(
                "SELECT c.id AS cid, m.role FROM case_file c "
                "LEFT JOIN case_member m ON m.case_id=c.id AND m.user_id=? "
                "WHERE c.case_no=?", (user_id, case_no)).fetchone()
        if not r:
            raise KeyError("案件不存在")
        if not r["role"] or r["role"] not in need_roles:
            raise AccessDenied(f"用户 {user_id} 无权操作案件 {case_no}")
        return r["cid"]

    # ================= 证据封存 =================

    def seal_case(self, case_no: str, actor: str) -> dict:
        """对案件全部相关来源记录做封存清单与封存哈希（只读快照，不改原件）。"""
        cid = self.require_case_member(case_no, actor)
        with self.store.tx() as conn:
            case = conn.execute("SELECT * FROM case_file WHERE id=?",
                                (cid,)).fetchone()
            if case["status"] == "sealed" and case["seal_digest"]:
                return {"case_no": case_no, "sealed": True,
                        "package_digest": case["seal_digest"],
                        "note": "案件已封存，返回既有封存结果"}
            lead_ids = [r["lead_id"] for r in conn.execute(
                "SELECT lead_id FROM lead_link WHERE case_id=?", (cid,))]
            manifest, seen = [], set()
            for lid in lead_ids:
                for r in conn.execute(
                        "SELECT evidence_json FROM lead_version WHERE lead_id=?",
                        (lid,)):
                    for e in json.loads(r["evidence_json"]):
                        key = (e["source_kind"], e["business_key"], e["record_id"])
                        if key in seen:
                            continue
                        seen.add(key)
                        rec = conn.execute(
                            "SELECT version,payload_digest FROM source_record "
                            "WHERE id=?", (e["record_id"],)).fetchone()
                        if rec:
                            manifest.append({
                                "source_kind": e["source_kind"],
                                "business_key": e["business_key"],
                                "record_id": e["record_id"],
                                "version": rec["version"],
                                "digest": rec["payload_digest"]})
            manifest.sort(key=lambda x: (x["source_kind"], x["business_key"],
                                         x["record_id"]))
            package_digest = canonical_digest(
                {"case_no": case_no, "manifest": manifest})
            package_no = _pid("SEAL")
            cur = conn.execute(
                "INSERT INTO sealed_package(case_id,package_no,created_at,"
                "created_by,manifest_json,package_digest,record_count) "
                "VALUES(?,?,?,?,?,?,?)",
                (cid, package_no, utcnow(), actor,
                 json.dumps(manifest, ensure_ascii=False), package_digest,
                 len(manifest)))
            conn.execute(
                "UPDATE case_file SET sealed_at=?,seal_digest=?,status='sealed' "
                "WHERE id=?", (utcnow(), package_digest, cid))
        return {"case_no": case_no, "package_no": package_no,
                "record_count": len(manifest), "package_digest": package_digest,
                "manifest": manifest}

    def verify_seal(self, case_no: str) -> dict:
        """复算封存哈希并逐条核对记录当前哈希（事后防篡改核验）。"""
        with self.store._lock:
            conn = self.store._conn
            case = conn.execute(
                "SELECT * FROM case_file WHERE case_no=?", (case_no,)).fetchone()
            if not case or not case["seal_digest"]:
                raise KeyError("案件未封存")
            pkg = conn.execute(
                "SELECT * FROM sealed_package WHERE case_id=? ORDER BY id DESC "
                "LIMIT 1", (case["id"],)).fetchone()
            manifest = json.loads(pkg["manifest_json"])
            bad = []
            for item in manifest:
                r = conn.execute(
                    "SELECT payload_digest FROM source_record WHERE id=?",
                    (item["record_id"],)).fetchone()
                if not r or r["payload_digest"] != item["digest"]:
                    bad.append(item["record_id"])
            recomputed = canonical_digest(
                {"case_no": case_no, "manifest": manifest})
        return {"intact": recomputed == pkg["package_digest"] and not bad,
                "package_digest": pkg["package_digest"],
                "recomputed_digest": recomputed,
                "tampered_records": bad, "record_count": len(manifest)}

    # ================= 跨省交接与回执 =================

    def transfer_case(self, case_no: str, actor: str, to_org: str,
                      to_region: str, note: str = "") -> dict:
        cid = self.require_case_member(case_no, actor, need_roles=("supervisor",))
        with self.store.tx() as conn:
            case = conn.execute("SELECT * FROM case_file WHERE id=?",
                                (cid,)).fetchone()
            if not case["seal_digest"]:
                raise ValueError("交接前必须先封存证据包")
            pkg = conn.execute(
                "SELECT id FROM sealed_package WHERE case_id=? ORDER BY id DESC "
                "LIMIT 1", (cid,)).fetchone()
            tno = _pid("TR")
            cur = conn.execute(
                "INSERT INTO transfer(case_id,transfer_no,to_org,to_region,"
                "created_at,created_by,package_id,status,request_note) "
                "VALUES(?,?,?,?,?,?,?,'sent',?)",
                (cid, tno, to_org, to_region, utcnow(), actor, pkg["id"], note))
            conn.execute("UPDATE case_file SET status='transferred' WHERE id=?",
                         (cid,))
        return {"transfer_no": tno, "to_org": to_org, "to_region": to_region}

    def receive_transfer(self, transfer_no: str, receiver: str,
                         receiver_org: str, note: str = "") -> dict:
        """接收方核对封存哈希后出具回执；结果如实记录，不替接收方掩饰不符。"""
        with self.store.tx() as conn:
            tr = conn.execute("SELECT * FROM transfer WHERE transfer_no=?",
                              (transfer_no,)).fetchone()
            if not tr:
                raise KeyError("交接单不存在")
            pkg = conn.execute(
                "SELECT * FROM sealed_package WHERE id=?",
                (tr["package_id"],)).fetchone()
            case = conn.execute("SELECT case_no FROM case_file WHERE id=?",
                                (tr["case_id"],)).fetchone()
            manifest = json.loads(pkg["manifest_json"])
            bad = []
            for item in manifest:
                r = conn.execute(
                    "SELECT payload_digest FROM source_record WHERE id=?",
                    (item["record_id"],)).fetchone()
                if not r or r["payload_digest"] != item["digest"]:
                    bad.append(item["record_id"])
            recomputed = canonical_digest(
                {"case_no": case["case_no"], "manifest": manifest})
            intact = (recomputed == pkg["package_digest"] and not bad)
            cur = conn.execute(
                "INSERT INTO transfer_receipt(transfer_id,received_at,receiver,"
                "receiver_org,package_intact,note,digest_at_receipt) "
                "VALUES(?,?,?,?,?,?,?)",
                (tr["id"], utcnow(), receiver, receiver_org, 1 if intact else 0,
                 note, recomputed))
            conn.execute("UPDATE transfer SET status='received' WHERE id=?",
                         (tr["id"],))
            return {"receipt_id": cur.lastrowid, "transfer_no": transfer_no,
                    "package_intact": intact,
                    "recomputed_digest": recomputed,
                    "expected_digest": pkg["package_digest"],
                    "tampered_records": bad,
                    "conclusion": "封存包装载内容与封存哈希一致"
                    if intact else "封存包核验未通过，须启动差异核查，不得签收采信"}

    # ================= 谱系还原：线索 -> 数据/决定/交接 =================

    def lineage(self, lead_no: str) -> dict:
        """从一条线索完整还原：每个机器版本采用的数据切点、当时的证据与
        解释、每条人工决定（含 lock_version）、所在案件封存与交接回执。"""
        lead = self.get_lead(lead_no)
        out = {"lead_no": lead_no, "rule_code": lead["rule_code"],
               "status": lead["status"], "versions": [], "decisions":
                   lead["decisions"], "cases": []}
        for v in lead["versions"]:
            out["versions"].append({
                "version_no": v["version_no"], "job_id": v["job_id"],
                "rule_version": v["rule_version"], "params": v["params"],
                "severity": v["severity"], "explanation": v["explanation"],
                "produced_at": v["produced_at"],
                "data_cut": v["data_cut"],
                "evidence": v["evidence"]})
        with self.store._lock:
            conn = self.store._conn
            for ll in conn.execute(
                    "SELECT cf.case_no,cf.title,cf.region,cf.status,cf.sealed_at,"
                    "cf.seal_digest FROM lead_link lk JOIN case_file cf "
                    "ON cf.id=lk.case_id WHERE lk.lead_id=?",
                    (lead["id"],)).fetchall():
                case_info = dict(ll)
                pkgs = conn.execute(
                    "SELECT package_no,created_at,created_by,package_digest,"
                    "record_count FROM sealed_package WHERE case_id="
                    "(SELECT id FROM case_file WHERE case_no=?)",
                    (ll["case_no"],)).fetchall()
                case_info["seals"] = [dict(p) for p in pkgs]
                trs = conn.execute(
                    "SELECT transfer_no,to_org,to_region,created_at,status "
                    "FROM transfer WHERE case_id="
                    "(SELECT id FROM case_file WHERE case_no=?)",
                    (ll["case_no"],)).fetchall()
                transfers = []
                for t in trs:
                    t = dict(t)
                    rcpts = conn.execute(
                        "SELECT received_at,receiver,receiver_org,package_intact,"
                        "digest_at_receipt,note FROM transfer_receipt r "
                        "JOIN transfer x ON x.id=r.transfer_id "
                        "WHERE x.transfer_no=?", (t["transfer_no"],)).fetchall()
                    t["receipts"] = [dict(r) for r in rcpts]
                    transfers.append(t)
                case_info["transfers"] = transfers
                out["cases"].append(case_info)
        return out
