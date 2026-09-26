"""分级访问：公众只能看到单盒合法流转摘要；内部接口按角色鉴权。"""

import json
import urllib.request
import unittest

from src.access import PublicVerifier
from src.api import create_app
from src.seed import seed_base, seed_late
from src.services import AuditService
from src.store import Store

BOX = "BOX8101A202608250001"
NORMAL = "BOX8102B202608200007"


class PublicVerifierTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        seed_base(self.store)
        seed_late(self.store)
        self.v = PublicVerifier(self.store)

    def tearDown(self):
        self.store.close()

    def test_normal_box_summary(self):
        out = self.v.verify(NORMAL)
        self.assertTrue(out["found"])
        self.assertIn("结算配发", out["summary"])
        self.assertEqual(out["dispensed_by"]["org"],
                         "宜宾市翠屏区示例社区卫生服务中心")
        self.assertNotIn("insured", json.dumps(out, ensure_ascii=False))
        self.assertNotIn("P-60001", json.dumps(out, ensure_ascii=False))
        self.assertNotIn("3260", json.dumps(out, ensure_ascii=False))

    def test_seized_box_public_view_hides_case_data(self):
        out = self.v.verify(BOX)
        blob = json.dumps(out, ensure_ascii=False)
        # 即使是涉案盒，公众端也只给追溯链摘要：无扣押、网售、案件、参保人信息
        for forbidden in ("扣押", "seizure", "网络销售", "online_sale",
                          "回流", "风险", "线索", "P-51128", "2600",
                          "广州白云区示例仓库", "码面涂改"):
            self.assertNotIn(forbidden, blob)
        # 盒码脱敏
        self.assertIn("****", out["box_code_masked"])
        self.assertNotIn(BOX, blob)

    def test_unknown_box_uniform_reply(self):
        out = self.v.verify("NOT-EXIST-9999")
        self.assertFalse(out["found"])
        self.assertTrue(out["chain"] == [])


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store()
        seed_base(cls.store)
        seed_late(cls.store)
        cls.svc = AuditService(cls.store)
        cls.server = create_app(cls.store)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        import threading
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        # 共享库先产生一批线索，后续作业幂等复评
        cls.svc.run_batch(note="测试基线比对")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.store.close()

    def _req(self, method, path, body=None, user=None, role=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if user:
            req.add_header("X-User-Id", user)
        if role:
            req.add_header("X-Role", role)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_health_and_public_verify_anonymous(self):
        code, out = self._req("GET", "/api/health")
        self.assertEqual(code, 200)
        code, out = self._req("GET", f"/api/public/verify/{NORMAL}")
        self.assertEqual(code, 200)
        self.assertTrue(out["found"])
        self.assertNotIn("P-60001", json.dumps(out, ensure_ascii=False))

    def test_leads_requires_internal_role(self):
        code, out = self._req("GET", "/api/leads")
        self.assertEqual(code, 403)
        code, out = self._req("GET", "/api/leads", user="wang", role="viewer")
        self.assertEqual(code, 200)

    def test_ingest_requires_writer(self):
        body = {"source_org": "测试单位", "records": []}
        code, _ = self._req("POST", "/api/ingest/trace_event", body,
                            user="wang", role="viewer")
        self.assertEqual(code, 403)
        code, out = self._req("POST", "/api/ingest/trace_event", body,
                              user="zhang.jg", role="investigator")
        self.assertEqual(code, 200)

    def test_full_case_workflow_over_http(self):
        # 线索由基线比对产生，按规则+主体从列表取回
        code, listed = self._req(
            "GET", "/api/leads?rule_code=REAPPEAR-01",
            user="zhang.jg", role="investigator")
        self.assertEqual(code, 200)
        box_lead = next(x["lead_no"] for x in listed
                        if x["subject_id"] == BOX)

        # 调查员不能建案
        code, _ = self._req("POST", "/api/cases",
                            {"case_no": "YB-HTTP-01", "title": "接口专案",
                             "region": "四川省宜宾市"},
                            user="zhang.jg", role="investigator")
        self.assertEqual(code, 403)

        # 负责人建案、加成员、挂线索、封存、交接
        code, _ = self._req("POST", "/api/cases",
                            {"case_no": "YB-HTTP-01", "title": "接口专案",
                             "region": "四川省宜宾市"},
                            user="supervisor.chen", role="supervisor")
        self.assertEqual(code, 200)
        code, _ = self._req("POST", "/api/cases/YB-HTTP-01/members",
                            {"user_id": "zhang.jg", "role": "investigator"},
                            user="supervisor.chen", role="supervisor")
        self.assertEqual(code, 200)
        code, _ = self._req("POST", "/api/cases/YB-HTTP-01/links",
                            {"lead_no": box_lead},
                            user="zhang.jg", role="investigator")
        self.assertEqual(code, 200)
        code, seal = self._req("POST", "/api/cases/YB-HTTP-01/seal", {},
                               user="zhang.jg", role="investigator")
        self.assertEqual(code, 200)
        code, tr = self._req("POST", "/api/cases/YB-HTTP-01/transfer",
                             {"to_org": "广州市公安局示例分局",
                              "to_region": "广东省广州市"},
                             user="supervisor.chen", role="supervisor")
        self.assertEqual(code, 200)
        code, receipt = self._req(
            "POST", f"/api/transfers/{tr['transfer_no']}/receive",
            {"receiver_org": "广州市公安局示例分局", "note": "在线签收"},
            user="gz.officer.liu", role="investigator")
        self.assertEqual(code, 200)
        self.assertTrue(receipt["package_intact"])

        # 谱系可查
        code, lin = self._req("GET", f"/api/leads/{box_lead}/lineage",
                              user="wang", role="viewer")
        self.assertEqual(code, 200)
        self.assertEqual(lin["cases"][0]["transfers"][0]
                         ["receipts"][0]["package_intact"], 1)

    def test_decision_conflict_over_http(self):
        code, listed = self._req(
            "GET", "/api/leads?rule_code=REPEAT-RX-01",
            user="zhang.jg", role="investigator")
        no = next(x["lead_no"] for x in listed
                  if x["subject_id"] == "P-90001")
        code, lead = self._req("GET", f"/api/leads/{no}",
                               user="zhang.jg", role="investigator")
        rev = lead["revision"]
        code, _ = self._req("POST", f"/api/leads/{no}/decisions",
                            {"action": "annotate", "expected_version": rev,
                             "comment": "甲先写"},
                            user="zhang.jg", role="investigator")
        self.assertEqual(code, 200)
        code, err = self._req("POST", f"/api/leads/{no}/decisions",
                              {"action": "annotate", "expected_version": rev,
                               "comment": "乙用旧修订号"},
                              user="li.jg", role="investigator")
        self.assertEqual(code, 409)
        self.assertEqual(err["error"], "version_conflict")


if __name__ == "__main__":
    unittest.main()
