"""稽核后台端到端测试。"""

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.audit import cases, query, rules
from src.audit.db import (ConflictError, PermissionDenied, SealedCaseError,
                          canonical_hash, connect, init_db)
from src.audit.ingest import ingest_batch

YB, GZ, HZ = "5115", "4401", "3301"
A, B, SEALER, PUBLIC = "u_yibin_01", "u_yibin_02", "u_seal_01", "u_public"


def med_record(mid, **kw):
    base = {"record_key": f"med-{mid}", "observed_at": "2026-08-01T08:00:00Z",
            "kind": "medicine", "medicine_id": mid, "product_name": "演示药",
            "is_cold_chain": 0}
    base.update(kw)
    return base


def scan_record(mid, etype, org, region, t, seq=1, ename=None):
    return {"record_key": f"{mid}-{etype}-{org}-{t}", "observed_at": t,
            "kind": "scan", "event_id": f"ev-{mid}-{seq}", "medicine_id": mid,
            "event_type": etype, "org_code": org, "org_name": ename or org,
            "region_code": region, "event_time": t, "seq": seq}


def settle_record(sid, person, mid, org, region, prescribed, settled, **kw):
    base = {"record_key": f"{sid}-k", "observed_at": settled,
            "settlement_id": sid, "person": person, "medicine_id": mid,
            "med_inst_code": org, "med_inst_name": f"医院{org}", "region_code": region,
            "diagnosis": "演示诊断", "prescribed_at": prescribed, "settled_at": settled,
            "quantity": 1, "amount": 100.0, "fund_type": "统筹", "is_cross_region": 0}
    base.update(kw)
    return base


PERSON = {"person_id": "P1", "surname": "李", "masked_id_no": "5115****0001",
          "region_code": YB, "id_hash": "h1"}


class AuditBackendTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.dir.name) / "t.db")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def _seed_simple_flow(self, mid="M1", settle_region=YB):
        ingest_batch(self.conn, "coding", "追溯平台", [
            med_record(mid),
            scan_record(mid, "produce", "MF", HZ, "2026-08-01T09:00:00Z", 1),
            scan_record(mid, "distribute", "LG", HZ, "2026-08-02T06:00:00Z", 2),
            scan_record(mid, "hospital_in", "H1", settle_region,
                        "2026-08-03T08:30:00Z", 3),
            scan_record(mid, "dispense", "H1", settle_region,
                        "2026-08-05T10:00:00Z", 4, "医院H1"),
        ], batch_id="b-code")
        ingest_batch(self.conn, "settlement", "医保平台", [
            settle_record("S1", PERSON, mid, "H1", settle_region,
                          "2026-08-05T09:50:00Z", "2026-08-05T10:00:00Z")
        ], batch_id="b-set")

    # ------------------------------------------------------------------
    # 入库：去重 / 迟到 / 更正追加
    # ------------------------------------------------------------------
    def test_duplicate_upload_is_idempotent(self):
        self._seed_simple_flow()
        rec = scan_record("M1", "dispense", "H1", YB, "2026-08-05T10:00:00Z",
                          4, "医院H1")
        s1 = ingest_batch(self.conn, "coding", "追溯平台", [rec], batch_id="dup1")
        s2 = ingest_batch(self.conn, "coding", "追溯平台", [rec], batch_id="dup2")
        self.assertEqual((s1["ingested"], s1["deduped"]), (0, 1))
        self.assertEqual((s2["ingested"], s2["deduped"]), (0, 1))
        n = self.conn.execute("SELECT COUNT(*) c FROM scan_events").fetchone()["c"]
        self.assertEqual(n, 4)  # 没有重复事件
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM raw_records").fetchone()["c"],
            self.conn.execute(
                "SELECT COUNT(DISTINCT content_hash) c FROM raw_records").fetchone()["c"])

    def test_late_data_flagged_but_kept(self):
        self._seed_simple_flow()
        stats = ingest_batch(self.conn, "settlement", "异地通道", [
            settle_record("S0", PERSON, "M9", "HG", GZ,
                          "2026-07-01T08:00:00Z", "2026-07-01T09:00:00Z",
                          is_cross_region=1)
        ], batch_id="late")
        self.assertEqual(stats["late"], 1)
        row = self.conn.execute(
            "SELECT is_late FROM raw_records WHERE source_record_key='S0-k'").fetchone()
        self.assertEqual(row["is_late"], 1)

    def test_correction_appends_version_never_overwrites(self):
        self._seed_simple_flow()
        corrected = settle_record("S1", PERSON, "M1", "H1", YB,
                                  "2026-08-05T09:50:00Z", "2026-08-05T10:00:00Z",
                                  amount=88.0, status="corrected",
                                  change_reason="金额更正")
        stats = ingest_batch(self.conn, "settlement", "医保平台", [corrected],
                             batch_id="corr")
        self.assertEqual(stats["corrections"][0]["new_version"], 2)
        versions = self.conn.execute(
            "SELECT version, amount, status FROM settlement_versions "
            "WHERE settlement_id='S1' ORDER BY version").fetchall()
        self.assertEqual([(v["version"], v["amount"], v["status"]) for v in versions],
                         [(1, 100.0, "valid"), (2, 88.0, "corrected")])
        cur = self.conn.execute(
            "SELECT amount, status FROM current_settlement WHERE settlement_id='S1'"
        ).fetchone()
        self.assertEqual((cur["amount"], cur["status"]), (88.0, "corrected"))

    def test_raw_payload_is_immutable_and_replayable(self):
        self._seed_simple_flow()
        # 直接篡改当前业务数据不影响原始报文；原始报文仍可回放重建依据
        self.conn.execute("UPDATE settlement_versions SET amount=0.01 WHERE version=1")
        raw = self.conn.execute(
            "SELECT payload FROM raw_records WHERE source_record_key='S1-k'").fetchone()
        self.assertEqual(json.loads(raw["payload"])["amount"], 100.0)
        self.assertFalse(self.conn.execute(
            "SELECT is_late FROM raw_records WHERE 1=0").fetchone())

    # ------------------------------------------------------------------
    # 风险线索：三类规则 + 只提示不定性
    # ------------------------------------------------------------------
    def test_cross_region_reappear_clue(self):
        self._seed_simple_flow()
        ingest_batch(self.conn, "online", "网售监测", [{
            "record_key": "on1", "observed_at": "2026-09-05T20:00:00Z",
            "online_id": "ON1", "medicine_id": "M1", "platform": "平台X",
            "region_code": GZ, "sold_at": "2026-09-05T20:00:00Z"}], batch_id="on")
        out = rules.run_all(self.conn)
        clues = query.list_clues(self.conn, A)
        self.assertEqual(out["created"], 1)
        clue = clues[0]
        self.assertEqual(clue["clue_type"], "cross_region_reappear")
        self.assertEqual(clue["status"], "open")  # 绝不由系统定性
        self.assertEqual(clue["severity"], "high")
        self.assertIn("规则不作认定", clue["explanation"]["rationale"])
        # 解释中包含每条输入的原始记录号，可回溯
        self.assertTrue(all("record_id" in json.dumps(i)
                            for i in [clue["explanation"]["inputs"][0]]))

    def test_repeat_prescription_with_threshold(self):
        self._seed_simple_flow()
        # 间隔 60 分钟 -> 命中（阈值 180）
        ingest_batch(self.conn, "settlement", "医保平台", [
            settle_record("S2", PERSON, "M2", "H2", YB,
                          "2026-08-05T11:00:00Z", "2026-08-05T11:05:00Z")
        ], batch_id="s2")
        self.assertEqual(rules.run_all(self.conn, repeat_window_min=180)["created"], 1)
        # 全新库：间隔 300 分钟、阈值 180 -> 不命中
        dir2 = tempfile.TemporaryDirectory()
        c2 = connect(Path(dir2.name) / "t2.db"); init_db(c2)
        ingest_batch(c2, "coding", "p", [med_record("M1"), med_record("M2")], batch_id="m")
        ingest_batch(c2, "settlement", "p", [
            settle_record("S1", PERSON, "M1", "H1", YB,
                          "2026-08-05T08:00:00Z", "2026-08-05T08:05:00Z"),
            settle_record("S2", PERSON, "M2", "H2", YB,
                          "2026-08-05T13:00:00Z", "2026-08-05T13:05:00Z")], batch_id="s")
        self.assertEqual(rules.run_all(c2, repeat_window_min=180)["created"], 0)
        c2.close(); dir2.cleanup()

    def test_cold_chain_temp_and_window(self):
        ingest_batch(self.conn, "coding", "追溯平台", [
            med_record("M1", is_cold_chain=1),
            scan_record("M1", "warehouse", "WH", HZ, "2026-08-02T02:00:00Z", 1),
            scan_record("M1", "distribute", "LG", HZ, "2026-08-02T06:00:00Z", 2),
            scan_record("M1", "hospital_in", "H1", YB, "2026-08-03T09:00:00Z", 3),
        ], batch_id="c")
        ingest_batch(self.conn, "voucher", "物流", [{
            "record_key": "vc", "observed_at": "2026-08-03T10:00:00Z",
            "voucher_id": "V1", "voucher_type": "cold_chain", "medicine_id": "M1",
            "temp_min": 2.0, "temp_max": 8.0, "temp_recorded": 11.0,
            "cold_window_start": "2026-08-02T04:00:00Z",
            "cold_window_end": "2026-08-03T08:00:00Z"}], batch_id="v")
        rules.run_all(self.conn)
        clues = query.list_clues(self.conn, A, clue_type="cold_chain_mismatch")
        self.assertEqual(len(clues), 1)
        contradictions = clues[0]["explanation"]["inputs"]["contradictions"]
        kinds = {c["type"] for c in contradictions}
        self.assertEqual(kinds, {"temp_out_of_range", "window_not_covered"})

    def test_rules_never_auto_adjudicate(self):
        self._seed_simple_flow()
        ingest_batch(self.conn, "online", "网售监测", [{
            "record_key": "on1", "observed_at": "2026-09-05T20:00:00Z",
            "online_id": "ON1", "medicine_id": "M1", "platform": "平台X",
            "region_code": GZ, "sold_at": "2026-09-05T20:00:00Z"}], batch_id="on")
        rules.run_all(self.conn)
        statuses = {r["status"] for r in self.conn.execute(
            "SELECT status FROM risk_clues")}
        self.assertEqual(statuses, {"open"})

    def test_rule_dedup_on_rerun_and_after_correction(self):
        self._seed_simple_flow()
        ingest_batch(self.conn, "online", "网售监测", [{
            "record_key": "on1", "observed_at": "2026-09-05T20:00:00Z",
            "online_id": "ON1", "medicine_id": "M1", "platform": "平台X",
            "region_code": GZ, "sold_at": "2026-09-05T20:00:00Z"}], batch_id="on")
        self.assertEqual(rules.run_all(self.conn)["created"], 1)
        self.assertEqual(rules.run_all(self.conn)["created"], 0)
        # 结算追加更正版本后重跑，仍不重复造线索
        ingest_batch(self.conn, "settlement", "医保平台", [
            settle_record("S1", PERSON, "M1", "H1", YB,
                          "2026-08-05T09:50:00Z", "2026-08-05T10:00:00Z",
                          amount=88.0, status="corrected")], batch_id="corr")
        self.assertEqual(rules.run_all(self.conn)["created"], 0)

    # ------------------------------------------------------------------
    # 并发研判 / 权限 / 封存 / 交接
    # ------------------------------------------------------------------
    def test_concurrent_review_lock(self):
        self._seed_simple_flow()
        rules.run_all(self.conn)  # 无网售，这里不应有线索
        ingest_batch(self.conn, "online", "m", [{
            "record_key": "o", "observed_at": "2026-09-05T20:00:00Z",
            "online_id": "ON1", "medicine_id": "M1", "platform": "P",
            "region_code": GZ, "sold_at": "2026-09-05T20:00:00Z"}], batch_id="o")
        rules.run_all(self.conn)
        clue_id = query.list_clues(self.conn, A)[0]["clue_id"]
        token = cases.acquire_lock(self.conn, clue_id, A)
        with self.assertRaises(ConflictError):
            cases.acquire_lock(self.conn, clue_id, B)
        # 未持锁不能下决定
        with self.assertRaises(ConflictError):
            cases.add_decision(self.conn, clue_id, B, "dismiss", "理由", "wrong-token")
        # 持锁人可以决定，决定后线索解锁并变更状态
        did = cases.add_decision(self.conn, clue_id, A, "dismiss",
                                 "经核实为家属正常代购，排除", token)
        self.assertTrue(did)
        self.assertEqual(self.conn.execute(
            "SELECT status, lock_owner FROM risk_clues WHERE clue_id=?",
            (clue_id,)).fetchone()["status"], "dismissed")

    def test_public_cannot_review_or_see_dossier(self):
        self._seed_simple_flow()
        with self.assertRaises(PermissionDenied):
            cases.create_case(self.conn, PUBLIC, "X")
        with self.assertRaises(PermissionDenied):
            query.medicine_dossier(self.conn, PUBLIC, "M1")
        with self.assertRaises(PermissionDenied):
            query.list_clues(self.conn, PUBLIC)
        with self.assertRaises(PermissionDenied):
            query.original_payload(self.conn, PUBLIC, "whatever")

    def test_seal_freezes_case_and_chain_reconstruction(self):
        self._seed_simple_flow()
        ingest_batch(self.conn, "online", "m", [{
            "record_key": "o", "observed_at": "2026-09-05T20:00:00Z",
            "online_id": "ON1", "medicine_id": "M1", "platform": "P",
            "region_code": GZ, "sold_at": "2026-09-05T20:00:00Z"}], batch_id="o")
        rules.run_all(self.conn)
        clue_id = query.list_clues(self.conn, A)[0]["clue_id"]
        case_id = cases.create_case(self.conn, A, "案", clue_ids=[clue_id])
        token = cases.acquire_lock(self.conn, clue_id, A)
        cases.add_decision(self.conn, clue_id, A, "confirm_for_transfer",
                           "证据相互印证，建议移送", token, case_id=case_id)
        seal = cases.seal_case(self.conn, case_id, SEALER)
        # 封存后冻结
        with self.assertRaises(SealedCaseError):
            cases.attach_clue(self.conn, A, case_id, clue_id)
        with self.assertRaises(SealedCaseError):
            cases.seal_case(self.conn, case_id, SEALER)
        # 证据链：线索 -> 决定(含当时数据版本) -> 封存
        chain = cases.evidence_chain(self.conn, clue_id)
        self.assertEqual(chain["decisions"][0]["action"], "confirm_for_transfer")
        self.assertEqual(chain["decisions"][0]["data_versions"]["settlement"]["S1"], 1)
        self.assertEqual(chain["seals"][0]["manifest_hash"], seal["manifest_hash"])
        # 交接 + 回执
        ho = cases.handoff(self.conn, case_id, A, to_org="公安")
        cases.receive_handoff(self.conn, ho["handoff_id"], "公安", "民警", "RCPT-1")
        chain2 = cases.evidence_chain(self.conn, clue_id)
        self.assertEqual(chain2["handoffs"][0]["status"], "received")
        self.assertEqual(chain2["handoffs"][0]["receipt_no"], "RCPT-1")
        # 同一 handoff 不能重复回执
        with self.assertRaises(Exception):
            cases.receive_handoff(self.conn, ho["handoff_id"], "公安", "警2", "RCPT-2")

    def test_handoff_requires_seal(self):
        case_id = cases.create_case(self.conn, A, "未封存案")
        with self.assertRaises(Exception):
            cases.handoff(self.conn, case_id, A, to_org="公安")

    def test_integrity_detects_tampering(self):
        self._seed_simple_flow()
        self.assertTrue(cases.verify_integrity(self.conn)["ok"])
        rid = self.conn.execute(
            "SELECT record_id FROM raw_records WHERE source_record_key='S1-k'"
        ).fetchone()["record_id"]
        bad = json.loads(self.conn.execute(
            "SELECT payload FROM raw_records WHERE record_id=?", (rid,)).fetchone()["payload"])
        bad["amount"] = 0.01
        self.conn.execute("UPDATE raw_records SET payload=? WHERE record_id=?",
                          (json.dumps(bad, ensure_ascii=False), rid))
        report = cases.verify_integrity(self.conn)
        self.assertFalse(report["ok"])
        self.assertEqual(report["problems"][0]["type"], "raw_payload_tampered")

    # ------------------------------------------------------------------
    # 分级查看与审计
    # ------------------------------------------------------------------
    def test_public_verify_is_minimal_and_masked(self):
        self._seed_simple_flow()
        out = query.public_verify(self.conn, "M1")
        self.assertTrue(out["found"])
        self.assertNotIn("clues", out)
        self.assertNotIn("seizures", out)
        self.assertNotIn("person", json.dumps(out, ensure_ascii=False))
        self.assertEqual(out["last_settlement"]["institution"], "医院H1")
        # 审计日志记录了公众查询
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM access_log WHERE actor='u_public' AND action='public_verify'"
        ).fetchone())

    def test_public_verify_unknown_code(self):
        out = query.public_verify(self.conn, "NO-SUCH")
        self.assertFalse(out["found"])

    def test_staff_sees_full_dossier_with_history_and_sources(self):
        self._seed_simple_flow()
        ingest_batch(self.conn, "voucher", "物流", [{
            "record_key": "v", "observed_at": "2026-08-04T00:00:00Z",
            "voucher_id": "V1", "voucher_type": "invoice", "medicine_id": "M1",
            "amount": 200.0}], batch_id="v")
        d = query.medicine_dossier(self.conn, A, "M1")
        self.assertEqual(len(d["scan_events"]), 4)
        self.assertEqual(d["first_settlement"]["institution"], "医院H1")
        self.assertEqual(d["settlements"][0]["history"][0]["amount"], 100.0)
        self.assertEqual(d["vouchers"][0]["voucher_type"], "invoice")
        rid = d["settlements"][0]["record_id"]
        payload = query.original_payload(self.conn, A, rid)
        self.assertEqual(payload["payload"]["settlement_id"], "S1")

    # ------------------------------------------------------------------
    # 扣押：受损/涂改码以平台记录为准建立关联
    # ------------------------------------------------------------------
    def test_seizure_altered_code_links_to_platform_record(self):
        self._seed_simple_flow("M1")
        ingest_batch(self.conn, "seizure", "联合执法", [{
            "record_key": "sz", "observed_at": "2026-09-10T15:00:00Z",
            "seizure_id": "SZ1", "warehouse_org": "某仓库", "region_code": YB,
            "seized_at": "2026-09-10T15:00:00Z", "items": [
                {"medicine_id": "M1", "pkg_condition": "altered_code",
                 "observed_code": "M?"}]}], batch_id="sz")
        d = query.medicine_dossier(self.conn, A, "M1")
        self.assertEqual(d["seizures"][0]["pkg_condition"], "altered_code")
        self.assertEqual(d["medicine"]["medicine_id"], "M1")  # 平台码仍是主键

    # ------------------------------------------------------------------
    # 并发：两个连接同时写入不丢批次（WAL + BEGIN IMMEDIATE 串行化）
    # ------------------------------------------------------------------
    def test_concurrent_connections_no_lost_batches(self):
        self._seed_simple_flow()
        c2 = connect(Path(self.dir.name) / "t.db")
        try:
            r1 = ingest_batch(self.conn, "online", "来源甲", [{
                "record_key": "o1", "observed_at": "2026-09-01T00:00:00Z",
                "online_id": "O1", "medicine_id": "M1", "platform": "P1",
                "region_code": GZ, "sold_at": "2026-09-01T00:00:00Z"}], batch_id="ob1")
            r2 = ingest_batch(c2, "online", "来源乙", [{
                "record_key": "o2", "observed_at": "2026-09-02T00:00:00Z",
                "online_id": "O2", "medicine_id": "M1", "platform": "P2",
                "region_code": HZ, "sold_at": "2026-09-02T00:00:00Z"}], batch_id="ob2")
            self.assertEqual((r1["ingested"], r2["ingested"]), (1, 1))
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) c FROM online_sales").fetchone()["c"], 2)
        finally:
            c2.close()


if __name__ == "__main__":
    unittest.main()
