"""规则引擎：三类风险线索的触发、可解释性与"绝不自动定性违法"。"""

import unittest

from src.rules import RULES, province_of
from src.seed import seed_base, seed_late
from src.services import AuditService
from src.store import Store

BOX = "BOX8101A202608250001"
COLD = "BOX8103C202608180003"
NORMAL = "BOX8102B202608200007"


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        seed_base(self.store)
        self.svc = AuditService(self.store)

    def tearDown(self):
        self.store.close()

    def test_province_normalization(self):
        self.assertEqual(province_of("四川省宜宾市"), "四川省")
        self.assertEqual(province_of("广东省广州市白云区"), "广东省")
        self.assertEqual(province_of("重庆市永川区"), "重庆市")

    def test_initial_run_flags_local_patterns_only(self):
        res = self.svc.run_batch()
        codes = {x["rule_code"] for x in res["created"]}
        # 初始数据下：重复开药、冷链不符可识别；跨省再现尚未暴露
        self.assertIn("REPEAT-RX-01", codes)
        self.assertIn("COLD-01", codes)
        reappear = [x for x in res["created"] if x["rule_code"] == "REAPPEAR-01"]
        self.assertEqual(reappear, [])

    def test_late_data_run_creates_reappearance_lead(self):
        self.svc.run_batch(note="初次比对")
        seed_late(self.store)
        res = self.svc.run_batch(trigger="late_data", note="广州迟到数据复评")
        lead_keys = {(x["rule_code"], x["subject_id"]) for x in res["created"]}
        self.assertIn(("REAPPEAR-01", BOX), lead_keys)
        lead = next(self.svc.get_lead(x["lead_no"])
                    for x in res["created"] if x["subject_id"] == BOX
                    and x["rule_code"] == "REAPPEAR-01")
        self.assertEqual(lead["severity"], "high")
        facts = lead["versions"][-1]
        explanation = lead["explanation"]
        # 解释必须可读、可复核，且明确不作违法认定
        self.assertIn("系统不作认定", explanation)
        self.assertTrue(any(e["source_kind"] == "seizure"
                            for e in facts["evidence"]))
        self.assertTrue(any(e["source_kind"] == "online_sale"
                            for e in facts["evidence"]))
        # 证据全部带精确版本与摘要
        for e in facts["evidence"]:
            self.assertTrue(e["record_id"] and e["digest"] and e["version"] >= 1)

    def test_normal_box_never_flagged(self):
        seed_late(self.store)
        self.svc.run_batch()
        flagged = {x["subject_id"] for x in self.svc.list_leads()}
        self.assertNotIn(NORMAL, flagged)

    def test_repeat_prescription_detects_cross_region_pair(self):
        seed_late(self.store)
        res = self.svc.run_batch()
        leads = [self.svc.get_lead(x["lead_no"]) for x in res["created"]
                 if x["rule_code"] == "REPEAT-RX-01"]
        p51128 = next(l for l in leads if l["subject_id"] == "P-51128")
        self.assertEqual(p51128["severity"], "high")  # 跨省且同类药
        pairs = p51128["versions"][-1]["evidence"]
        self.assertGreaterEqual(len(pairs), 3)  # 08:50/09:00/09:40 三笔互为依据
        # P-90001 同省不同机构 55 分钟，也提示，但为低风险
        p90001 = next(l for l in leads if l["subject_id"] == "P-90001")
        self.assertEqual(p90001["severity"], "low")
        self.assertIn("不作违法定性", p90001["explanation"])

    def test_cold_chain_mismatch_detail(self):
        res = self.svc.run_batch()
        cold = next(self.svc.get_lead(x["lead_no"]) for x in res["created"]
                    if x["rule_code"] == "COLD-01")
        self.assertEqual(cold["subject_id"], COLD)
        self.assertEqual(cold["severity"], "high")  # 区域冲突直接 high
        text = cold["explanation"]
        self.assertIn("昆明", text)
        self.assertIn("成都", text)
        self.assertIn("11.5", text)  # 温度越限写进解释

    def test_risk_is_never_legal_determination(self):
        seed_late(self.store)
        self.svc.run_batch()
        for lead in self.svc.list_leads():
            full = self.svc.get_lead(lead["lead_no"])
            self.assertNotIn("违法认定", full["title"])
            allowed = {"open", "reviewing"}
            self.assertIn(full["status"], allowed)

    def test_rule_versions_and_params_recorded(self):
        res = self.svc.run_batch(params={"REPEAT-RX-01": {"min_gap_minutes": 30}})
        self.assertEqual(RULES["REPEAT-RX-01"].version, "1.0")
        for created in res["created"]:
            lead = self.svc.get_lead(created["lead_no"])
            v = lead["versions"][-1]
            self.assertIn("rule_version", v)
        # 参数收紧到 30 分钟后，P-90001 的 55 分钟不再触发
        subjects = {x["subject_id"] for x in res["created"]
                    if x["rule_code"] == "REPEAT-RX-01"}
        self.assertNotIn("P-90001", subjects)

    def test_rerun_appends_lead_version_with_new_cut(self):
        r1 = self.svc.run_batch(note="第一次")
        lead_no = next(x["lead_no"] for x in r1["created"]
                       if x["rule_code"] == "REPEAT-RX-01"
                       and x["subject_id"] == "P-51128")
        seed_late(self.store)
        r2 = self.svc.run_batch(trigger="late_data", note="第二次")
        upd = next(x for x in r2["updated"] if x["lead_no"] == lead_no)
        self.assertEqual(upd["new_version"], 2)
        lead = self.svc.get_lead(lead_no)
        self.assertEqual([v["version_no"] for v in lead["versions"]], [1, 2])
        self.assertNotEqual(lead["versions"][0]["data_cut"]["cut_no"],
                            lead["versions"][1]["data_cut"]["cut_no"])


if __name__ == "__main__":
    unittest.main()
