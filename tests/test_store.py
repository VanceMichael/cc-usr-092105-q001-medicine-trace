"""存储层不变量：仅追加、版本化更正、重复上传、迟到、哈希链防篡改。"""

import json
import unittest

from src.seed import seed_base, seed_late
from src.store import DuplicateRecord, Store, canonical_digest


def _box_payload(region="四川省宜宾市", t="2026-09-01T10:00:00+08:00"):
    return {"box_code": "BOX-T1", "event_type": "scan", "event_time": t,
            "region": region, "org_name": "示例机构"}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()

    def tearDown(self):
        self.store.close()

    def test_append_only_correction_creates_new_version(self):
        r1 = {"business_key": "K1", "business_time": "2026-09-01T10:00:00+08:00",
              "payload": {"org_name": "错误名称"}}
        self.store.ingest_batch("trace_event", "单位甲", [r1], batch_id="B1")
        # 更正必须说明原因，否则该条拒收（不影响同批其他记录）
        r_bad = self.store.ingest_batch(
            "trace_event", "单位甲",
            [{"business_key": "K1", "payload": {"org_name": "正确名称"}}],
            batch_id="B2")
        self.assertEqual(r_bad["rejected"], 1)
        self.assertIn("correction_reason", r_bad["errors"][0]["error"])
        self.store.ingest_batch(
            "trace_event", "单位甲",
            [{"business_key": "K1",
              "payload": {"org_name": "正确名称"},
              "correction_reason": "来源单位勘误机构名称"}],
            batch_id="B3")
        hist = self.store.history("trace_event", "K1")
        versions = [h for h in hist if h["version"] > 0]
        self.assertEqual([h["version"] for h in versions], [1, 2])
        self.assertIsNone(versions[0]["supersedes"])
        self.assertEqual(versions[1]["supersedes"], versions[0]["id"])
        # 旧版逐字保留，当前投影指向新版
        self.assertEqual(json.loads(versions[0]["payload_json"])["org_name"],
                         "错误名称")
        self.assertEqual(self.store.current("trace_event", "K1")["payload"]
                         ["org_name"], "正确名称")

    def test_duplicate_upload_is_marked_not_stored_as_version(self):
        rec = {"business_key": "K2", "payload": _box_payload()}
        r1 = self.store.ingest_batch("trace_event", "单位甲", [rec], batch_id="D1")
        self.assertEqual(r1["stored"], 1)
        r2 = self.store.ingest_batch("trace_event", "单位甲", [rec], batch_id="D2")
        self.assertEqual(r2["duplicates"], 1)
        hist = [h for h in self.store.history("trace_event", "K2")
                if h["version"] > 0]
        self.assertEqual(len(hist), 1)  # 不产生第 2 版
        marks = self.store.history("trace_event", "K2")
        self.assertEqual(marks[-1]["duplicate_of"], marks[0]["id"])

    def test_late_data_detected_by_business_time(self):
        stats = seed_base(self.store)
        self.assertEqual(stats["settlement"]["late"], 0)
        late = seed_late(self.store)
        # 08:50 的结算晚于水位线（09:55）之后才送达
        self.assertEqual(late["settlement"]["late"], 1)
        self.assertIn("S-20260903-0850", late["settlement"]["late_keys"])
        # 广州材料业务时间更新，不算迟到
        self.assertEqual(late["trace"]["late"], 0)

    def test_hash_chain_detects_tampering(self):
        seed_base(self.store)
        self.assertTrue(self.store.verify_chain()["ok"])
        # 绕过业务接口直接篡改库文件内容，必须被链校验发现
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT id FROM source_record WHERE version=1 LIMIT 1").fetchone()
            self.store._conn.execute(
                "UPDATE source_record SET payload_json=? WHERE id=?",
                (json.dumps({"hacked": True}, ensure_ascii=False), row["id"]))
            self.store._conn.commit()
        result = self.store.verify_chain()
        self.assertFalse(result["ok"])
        self.assertEqual(result["broken_at"], row["id"])

    def test_unknown_source_kind_rejected(self):
        with self.assertRaises(ValueError):
            self.store.ingest_batch("not_a_kind", "单位", [
                {"business_key": "x", "payload": {}}])

    def test_bad_record_does_not_kill_batch(self):
        good = {"business_key": "OK", "payload": _box_payload()}
        bad = {"business_key": "BAD", "payload": None}
        stats = self.store.ingest_batch("trace_event", "单位", [good, bad],
                                        batch_id="MIX")
        self.assertEqual(stats["stored"], 1)
        self.assertEqual(stats["rejected"], 1)
        self.assertIsNotNone(self.store.current("trace_event", "OK"))


if __name__ == "__main__":
    unittest.main()
