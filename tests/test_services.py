"""研判协作：乐观锁、人工定性不被机器覆盖、封存核验、交接回执与谱系还原。"""

import json
import threading
import unittest

from src.seed import seed_base, seed_late
from src.services import AccessDenied, AuditService, ConflictError
from src.store import Store

BOX = "BOX8101A202608250001"


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        seed_base(self.store)
        seed_late(self.store)
        self.svc = AuditService(self.store)
        res = self.svc.run_batch(note="全量比对")
        self.leads = {x["rule_code"] + "|" + x["subject_id"]: x["lead_no"]
                      for x in res["created"]}

    def tearDown(self):
        self.store.close()

    def test_optimistic_lock_blocks_stale_decision(self):
        no = self.leads["REPEAT-RX-01|P-51128"]
        lead = self.svc.get_lead(no)
        self.assertEqual(lead["latest_version"], 1)
        # 甲先研判
        self.svc.decide(no, actor="zhang.jg", action="start_review",
                        expected_version=1, comment="我先看")
        # 乙拿着过期的 v1 并发提交，必须失败
        with self.assertRaises(ConflictError):
            self.svc.decide(no, actor="li.jg", action="dismiss",
                            expected_version=1, comment="我认为没问题")
        # 乙重读后可以提交；决定只追加（此时修订号已为 2）
        self.svc.decide(no, actor="li.jg", action="annotate",
                        expected_version=2, comment="补充重庆侧就诊情况")
        actions = [(d["action"], d["actor"])
                   for d in self.svc.get_lead(no)["decisions"]]
        self.assertEqual(actions, [("start_review", "zhang.jg"),
                                   ("annotate", "li.jg")])

    def test_concurrent_decisions_one_wins(self):
        no = self.leads["REPEAT-RX-01|P-51128"]
        barrier = threading.Barrier(4)
        errors = []

        def worker(actor):
            barrier.wait()
            try:
                self.svc.decide(no, actor=actor, action="annotate",
                                expected_version=1, comment=f"{actor} 意见")
            except ConflictError as e:
                errors.append(str(e))
        threads = [threading.Thread(target=worker, args=(f"u{i}",))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lead = self.svc.get_lead(no)
        # 只有 1 个成功，其余 3 个收到版本冲突；决定序号连续
        self.assertEqual(len(errors), 3)
        self.assertEqual([d["seq"] for d in lead["decisions"]], [1])

    def test_human_dismissal_not_overwritten_by_rerun(self):
        no = self.leads["REPEAT-RX-01|P-90001"]
        self.svc.decide(no, actor="supervisor.chen", action="dismiss",
                        expected_version=1,
                        comment="经核实系胸痛转诊，异地结算具有连续性，排除")
        self.assertEqual(self.svc.get_lead(no)["status"], "dismissed")
        res = self.svc.run_batch(trigger="scheduled", note="定时复评")
        upd = [x for x in res["updated"] if x["lead_no"] == no]
        self.assertTrue(upd)  # 机器复评意见仍追加版本
        self.assertEqual(self.svc.get_lead(no)["status"], "dismissed")
        lead = self.svc.get_lead(no)
        self.assertEqual(lead["latest_version"], 2)
        self.assertTrue(any(d["action"] == "dismiss" for d in lead["decisions"]))

    def test_seal_manifest_detects_tampering(self):
        no = self.leads[f"REAPPEAR-01|{BOX}"]
        self.svc.create_case("YB-2026-X01", "宜宾回流药跨省专案",
                             "四川省宜宾市", "supervisor.chen")
        self.svc.add_member("YB-2026-X01", "zhang.jg", "investigator")
        self.svc.link_lead("YB-2026-X01", no, "zhang.jg")
        seal = self.svc.seal_case("YB-2026-X01", "supervisor.chen")
        self.assertGreater(seal["record_count"], 3)
        ok = self.svc.verify_seal("YB-2026-X01")
        self.assertTrue(ok["intact"])
        # 篡改任一被封存原始记录
        with self.store._lock:
            rid = seal["manifest"][0]["record_id"]
            self.store._conn.execute(
                "UPDATE source_record SET payload_digest=? WHERE id=?",
                ("0" * 64, rid))
            self.store._conn.commit()
        bad = self.svc.verify_seal("YB-2026-X01")
        self.assertFalse(bad["intact"])
        self.assertIn(rid, bad["tampered_records"])

    def test_case_member_access_control(self):
        self.svc.create_case("YB-2026-X02", "无权限测试案",
                             "四川省宜宾市", "supervisor.chen")
        self.svc.add_member("YB-2026-X02", "zhang.jg", "investigator")
        # 非成员不能挂线索
        with self.assertRaises(AccessDenied):
            self.svc.link_lead("YB-2026-X02",
                               self.leads["COLD-01|BOX8103C202608180003"],
                               "outsider.xx")
        # viewer 只读，不能挂线索
        self.svc.add_member("YB-2026-X02", "view.wang", "viewer")
        with self.assertRaises(AccessDenied):
            self.svc.link_lead("YB-2026-X02",
                               self.leads["COLD-01|BOX8103C202608180003"],
                               "view.wang")
        # investigator 可以
        self.svc.link_lead("YB-2026-X02",
                           self.leads["COLD-01|BOX8103C202608180003"],
                           "zhang.jg")

    def test_transfer_requires_seal_and_receipt_checks_digest(self):
        no = self.leads[f"REAPPEAR-01|{BOX}"]
        self.svc.create_case("YB-2026-X03", "交接测试案",
                             "四川省宜宾市", "supervisor.chen")
        self.svc.link_lead("YB-2026-X03", no, "supervisor.chen")
        with self.assertRaises(ValueError):
            self.svc.transfer_case("YB-2026-X03", "supervisor.chen",
                                   "广州市公安局示例分局", "广东省广州市")
        self.svc.seal_case("YB-2026-X03", "supervisor.chen")
        tr = self.svc.transfer_case("YB-2026-X03", "supervisor.chen",
                                    "广州市公安局示例分局", "广东省广州市",
                                    note="协查穗公经侦协〔2026〕示例118号")
        receipt = self.svc.receive_transfer(
            tr["transfer_no"], receiver="gz.officer.liu",
            receiver_org="广州市公安局示例分局", note="签收到位")
        self.assertTrue(receipt["package_intact"])
        self.assertEqual(receipt["conclusion"], "封存包装载内容与封存哈希一致")

    def test_lineage_reconstructs_cut_decisions_and_receipt(self):
        no = self.leads[f"REAPPEAR-01|{BOX}"]
        self.svc.create_case("YB-2026-X04", "谱系还原案",
                             "四川省宜宾市", "supervisor.chen")
        self.svc.add_member("YB-2026-X04", "zhang.jg", "investigator")
        self.svc.link_lead("YB-2026-X04", no, "zhang.jg")
        self.svc.decide(no, actor="zhang.jg", action="confirm_risk",
                        expected_version=2,
                        comment="结算地宜宾与扣押地广州矛盾客观成立，风险确认，"
                                "违法定性以后续程序为准")
        self.svc.seal_case("YB-2026-X04", "supervisor.chen")
        tr = self.svc.transfer_case("YB-2026-X04", "supervisor.chen",
                                    "广州市公安局示例分局", "广东省广州市")
        self.svc.receive_transfer(tr["transfer_no"], "gz.officer.liu",
                                  "广州市公安局示例分局")
        lin = self.svc.lineage(no)
        self.assertEqual(lin["status"], "confirmed_risk")
        self.assertEqual(len(lin["versions"]), 1)
        v = lin["versions"][0]
        self.assertTrue(v["data_cut"]["digest"])
        self.assertTrue(v["data_cut"]["max_record_id"] >= 1)
        kinds = {e["source_kind"] for e in v["evidence"]}
        self.assertEqual(kinds, {"trace_event", "settlement", "online_sale",
                                 "seizure"})
        case = lin["cases"][0]
        self.assertEqual(len(case["seals"]), 1)
        self.assertEqual(case["transfers"][0]["receipts"][0]["package_intact"],
                         1)
        # 每个决定都留有 lock_version，可还原"当时基于哪个线索版本"
        self.assertEqual(
            {d["action"] for d in lin["decisions"]},
            {"claim", "confirm_risk"})

    def test_sealed_case_reseal_is_idempotent(self):
        no = self.leads[f"REAPPEAR-01|{BOX}"]
        self.svc.create_case("YB-2026-X05", "重复封存案",
                             "四川省宜宾市", "supervisor.chen")
        self.svc.link_lead("YB-2026-X05", no, "supervisor.chen")
        s1 = self.svc.seal_case("YB-2026-X05", "supervisor.chen")
        s2 = self.svc.seal_case("YB-2026-X05", "supervisor.chen")
        self.assertEqual(s1["package_digest"], s2["package_digest"])


if __name__ == "__main__":
    unittest.main()
