"""宜宾"回流药"跨省证据归并 —— 端到端场景演示。

运行：PYTHONPATH=. python3 scripts/demo_yibin.py

数据均为虚构样例，不对应任何真实个人、企业或业务记录。
剧情：一批外包装受损、追溯码被涂改的药品在宜宾某仓库被扣押；
稽核人员归并赋码扫码、就医结算、票账货款、扣押、网售与协作六类来源，
系统产出三类"只提示、不定性"的风险线索，人工研判后封存移送并取得回执。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.audit import cases, query, rules
from src.audit.db import ConflictError, PermissionDenied, connect, init_db
from src.audit.ingest import ingest_batch

# 地区码（演示用）：5115 宜宾；4401 广州；3301 杭州
YB, GZ, HZ = "5115", "4401", "3301"
STAFF_A, STAFF_B, SEALER = "u_yibin_01", "u_yibin_02", "u_seal_01"


def build_scenario(conn):
    # 1) 药品赋码与扫码（含一盒冷链胰岛素 M1、一盒普通药 M2） ----------------
    ingest_batch(conn, "coding", "全国药品追溯平台", [
        {"record_key": "med-M1", "observed_at": "2026-08-01T08:00:00Z",
         "kind": "medicine", "medicine_id": "8115-M1-0001",
         "product_name": "演示用重组人胰岛素注射液", "spec": "300IU/3ml",
         "manufacturer": "演示生物制药厂", "batch_no": "B2607A",
         "is_cold_chain": 1},
        {"record_key": "med-M2", "observed_at": "2026-08-01T08:05:00Z",
         "kind": "medicine", "medicine_id": "8115-M2-0002",
         "product_name": "演示用降压片", "spec": "7片/盒",
         "manufacturer": "演示药业", "batch_no": "B2607C"},
    ], note="赋码建档", batch_id="b_coding_med")

    scan_records = [
        ("ev1", "8115-M1-0001", "produce", "MF01", "演示制药厂成品库", HZ, "2026-08-01T09:00:00Z"),
        ("ev2", "8115-M1-0001", "warehouse", "WH-ZJ", "浙江中心仓", HZ, "2026-08-02T02:00:00Z"),
        ("ev3", "8115-M1-0001", "distribute", "LOG01", "川浙冷链干线", HZ, "2026-08-02T06:00:00Z"),
        ("ev4", "8115-M1-0001", "hospital_in", "H511501", "宜宾市第一演示医院", YB, "2026-08-03T08:30:00Z"),
        ("ev5", "8115-M1-0001", "dispense", "H511501", "宜宾市第一演示医院药房", YB, "2026-08-05T10:05:00Z"),
        ("ev6", "8115-M2-0002", "produce", "MF02", "演示药业成品库", HZ, "2026-08-01T09:10:00Z"),
        ("ev7", "8115-M2-0002", "distribute", "LOG02", "普通公路货运", HZ, "2026-08-02T10:00:00Z"),
        ("ev8", "8115-M2-0002", "hospital_in", "H511502", "宜宾市第二演示医院", YB, "2026-08-03T09:00:00Z"),
        ("ev9", "8115-M2-0002", "dispense", "H511502", "宜宾市第二演示医院药房", YB, "2026-08-05T11:40:00Z"),
    ]
    ingest_batch(conn, "coding", "全国药品追溯平台", [
        {"record_key": k, "observed_at": t, "kind": "scan",
         "event_id": k, "medicine_id": m, "event_type": et,
         "org_code": oc, "org_name": on, "region_code": rg, "event_time": t, "seq": i}
        for i, (k, m, et, oc, on, rg, t) in enumerate(scan_records, 1)
    ], note="流通扫码", batch_id="b_coding_scan")

    # 2) 参保人就医结算 -------------------------------------------------------
    person = {"person_id": "P-7721", "surname": "王",
              "masked_id_no": "5115**********0017", "region_code": YB,
              "id_hash": "hash-of-id-7721"}
    ingest_batch(conn, "settlement", "四川医保结算平台", [
        {"record_key": "st1", "observed_at": "2026-08-05T10:05:00Z",
         "settlement_id": "ST-1001", "person": person, "medicine_id": "8115-M1-0001",
         "med_inst_code": "H511501", "med_inst_name": "宜宾市第一演示医院",
         "region_code": YB, "diagnosis": "2型糖尿病",
         "prescribed_at": "2026-08-05T09:50:00Z", "settled_at": "2026-08-05T10:05:00Z",
         "quantity": 2, "amount": 186.40, "fund_type": "统筹", "is_cross_region": 0},
        # 同一天 80 分钟后在另一家医院再次开药（重复开药线索）
        {"record_key": "st2", "observed_at": "2026-08-05T11:40:00Z",
         "settlement_id": "ST-1002", "person": person, "medicine_id": "8115-M2-0002",
         "med_inst_code": "H511502", "med_inst_name": "宜宾市第二演示医院",
         "region_code": YB, "diagnosis": "高血压",
         "prescribed_at": "2026-08-05T11:10:00Z", "settled_at": "2026-08-05T11:40:00Z",
         "quantity": 3, "amount": 92.10, "fund_type": "个账", "is_cross_region": 0},
    ], note="参保人结算", batch_id="b_settle")

    # 迟到数据：该参保人 7 月跨省就医在广州结算另一盒药，异地通道数据晚送达 ----
    late = ingest_batch(conn, "settlement", "国家异地就医结算通道", [
        {"record_key": "st0-late", "observed_at": "2026-07-20T09:00:00Z",
         "settlement_id": "ST-0900", "person": person, "medicine_id": "8115-M0-0099",
         "med_inst_code": "H440199", "med_inst_name": "广州某演示医院",
         "region_code": GZ, "diagnosis": "2型糖尿病（异地随诊）",
         "prescribed_at": "2026-07-20T08:40:00Z", "settled_at": "2026-07-20T09:00:00Z",
         "quantity": 1, "amount": 95.00, "fund_type": "统筹", "is_cross_region": 1},
    ], note="异地结算迟到数据", batch_id="b_settle_late")
    assert late["late"] == 1

    # 更正：ST-1001 金额最初错报，医保平台追加更正版本（旧版保留） -----------
    corr = ingest_batch(conn, "settlement", "四川医保结算平台", [
        {"record_key": "st1-corr", "observed_at": "2026-08-06T09:00:00Z",
         "settlement_id": "ST-1001", "person": person, "medicine_id": "8115-M1-0001",
         "med_inst_code": "H511501", "med_inst_name": "宜宾市第一演示医院",
         "region_code": YB, "diagnosis": "2型糖尿病",
         "prescribed_at": "2026-08-05T09:50:00Z", "settled_at": "2026-08-05T10:05:00Z",
         "quantity": 2, "amount": 168.40, "fund_type": "统筹", "is_cross_region": 0,
         "status": "corrected", "change_reason": "单价录入错误，按发票金额更正"},
    ], note="结算金额更正", batch_id="b_settle_corr")
    assert corr["corrections"][0]["entity"] == "settlement"

    # 3) 票账货款 + 冷链材料（与真实流转矛盾） --------------------------------
    ingest_batch(conn, "voucher", "扣押现场随货材料", [
        {"record_key": "v-inv", "observed_at": "2026-08-04T00:00:00Z",
         "voucher_id": "V-INV-77", "voucher_type": "invoice", "doc_no": "FP-DEMO-77",
         "party_org": "宜宾某演示商行", "amount": 22600.0, "region_code": YB,
         "issued_at": "2026-08-04T00:00:00Z", "medicine_id": "8115-M1-0001"},
        # 材料声称全程 2~8℃、冷链时段 8/2 04:00 至 8/3 08:00；
        # 但第三方探头记录 12.6℃，且真实配送扫码 8/2 06:00 勉强在窗内、
        # 入院 8/3 08:30 已落在声称时段之外
        {"record_key": "v-cold", "observed_at": "2026-08-03T09:00:00Z",
         "voucher_id": "V-COLD-77", "voucher_type": "cold_chain",
         "doc_no": "LL-DEMO-77", "party_org": "演示冷链运输公司",
         "region_code": YB, "issued_at": "2026-08-03T09:00:00Z",
         "medicine_id": "8115-M1-0001",
         "temp_min": 2.0, "temp_max": 8.0, "temp_recorded": 12.6,
         "cold_window_start": "2026-08-02T04:00:00Z",
         "cold_window_end": "2026-08-03T08:00:00Z"},
    ], note="发票与冷链温控单", batch_id="b_voucher")

    # 4) 仓库扣押登记（包装受损、码面涂改） -----------------------------------
    ingest_batch(conn, "seizure", "宜宾市市场监管/医保联合执法", [
        {"record_key": "sz1", "observed_at": "2026-09-10T15:00:00Z",
         "seizure_id": "SZ-2026-031", "warehouse_org": "宜宾市翠屏区某演示仓库",
         "region_code": YB, "seized_at": "2026-09-10T15:00:00Z",
         "officer": "执法员A", "note": "外包装受损、追溯码被刮擦涂改",
         "items": [
             {"medicine_id": "8115-M1-0001", "pkg_condition": "altered_code",
              "observed_code": "8115-M1-00?1", "qty": 12},
             {"medicine_id": "8115-M2-0002", "pkg_condition": "damaged", "qty": 5},
         ]},
    ], note="仓库扣押", batch_id="b_seizure")

    # 5) 网络销售：结算后药盒在广州网店再次出现 -------------------------------
    ingest_batch(conn, "online", "网络交易监测协作", [
        {"record_key": "on1", "observed_at": "2026-09-05T20:00:00Z",
         "online_id": "ON-501", "medicine_id": "8115-M1-0001",
         "platform": "演示网购平台", "shop_name": "广州某某演示专营店",
         "region_code": GZ, "listing_time": "2026-09-01T12:00:00Z",
         "sold_at": "2026-09-05T20:00:00Z", "buyer_region": YB, "price": 120.0},
    ], note="网售监测", batch_id="b_online")

    # 6) 办案协作：跨省协查、物流、监控、资金 ---------------------------------
    ingest_batch(conn, "cooperation", "宜宾市医保稽核部门", [
        {"record_key": "cp1", "observed_at": "2026-09-12T10:00:00Z",
         "coop_id": "CP-01", "from_org": "宜宾医保稽核", "to_org": "广州医保稽核",
         "channel": "cross_province", "request_at": "2026-09-12T10:00:00Z",
         "payload_summary": "请协助核查网店实际经营者及涉案药品来源"},
        {"record_key": "cp2", "observed_at": "2026-09-13T10:00:00Z",
         "coop_id": "CP-02", "from_org": "广州医保稽核", "to_org": "宜宾医保稽核",
         "channel": "logistics", "request_at": "2026-09-12T10:00:00Z",
         "response_at": "2026-09-13T10:00:00Z",
         "payload_summary": "快递面单显示广州发宜宾，寄件人指向某回收药贩"},
        {"record_key": "cp3", "observed_at": "2026-09-13T11:00:00Z",
         "coop_id": "CP-03", "from_org": "宜宾公安", "to_org": "宜宾医保稽核",
         "channel": "surveillance", "request_at": "2026-09-11T09:00:00Z",
         "response_at": "2026-09-13T11:00:00Z",
         "payload_summary": "仓库监控拍到有人用酒精棉片刮擦追溯码后重新装箱"},
        {"record_key": "cp4", "observed_at": "2026-09-14T09:00:00Z",
         "coop_id": "CP-04", "from_org": "宜宾医保稽核", "to_org": "银行协查",
         "channel": "fund", "request_at": "2026-09-12T14:00:00Z",
         "response_at": "2026-09-14T09:00:00Z",
         "payload_summary": "收款账户与参保人就医结算资金的回流关系待核"},
    ], note="跨省办案协作", batch_id="b_coop")


def show(title):
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def main() -> None:
    db_dir = tempfile.mkdtemp(prefix="yibin_audit_")
    conn = connect(Path(db_dir) / "audit.db")
    init_db(conn)

    show("一、六类来源按原始报文入库（重复上传幂等演示）")
    build_scenario(conn)
    # 重复上传同一批次：内容哈希一致，全部判重，零新增
    dup = ingest_batch(conn, "online", "网络交易监测协作", [
        {"record_key": "on1", "observed_at": "2026-09-05T20:00:00Z",
         "online_id": "ON-501", "medicine_id": "8115-M1-0001",
         "platform": "演示网购平台", "shop_name": "广州某某演示专营店",
         "region_code": GZ, "listing_time": "2026-09-01T12:00:00Z",
         "sold_at": "2026-09-05T20:00:00Z", "buyer_region": YB, "price": 120.0},
    ], batch_id="b_online_dup")
    print(f"重复上传网售批次：接收 {dup['received']} 条，判重 {dup['deduped']} 条，"
          f"新增 {dup['ingested']} 条")

    show("二、批量交叉比对：生成三类可解释风险线索（系统不定性违法）")
    run = rules.run_all(conn, repeat_window_min=180)
    print(f"本次新生成线索 {run['created']} 条：")
    for c in query.list_clues(conn, STAFF_A):
        print(f"  [{c['severity']:6}] {c['clue_type']:24} {c['title']}")
        print(f"      依据：{c['explanation']['rationale']}")
    # 重跑规则：业务身份去重，不重复造线索
    again = rules.run_all(conn, repeat_window_min=180)
    print(f"更正/补数后重跑规则，新生成线索 {again['created']} 条（去重生效）")

    clues = {c["clue_type"]: c["clue_id"]
             for c in query.list_clues(conn, STAFF_A)}

    show("三、多人并发研判：甲乙同时打开同一条跨省回流线索")
    token_a = cases.acquire_lock(conn, clues["cross_region_reappear"], STAFF_A)
    print(f"稽核员甲取得研判锁 token={token_a[:8]}…")
    try:
        cases.acquire_lock(conn, clues["cross_region_reappear"], STAFF_B)
    except ConflictError as e:
        print(f"稽核员乙尝试同时研判被拒绝：{e}")

    show("四、人工决定（风险提示 ≠ 违法认定，结论只能人工写）")
    case_id = cases.create_case(
        conn, STAFF_A, "宜宾9·10跨省回流药案",
        clue_ids=list(clues.values()))
    print(f"立案：{case_id}")
    dec1 = cases.add_decision(
        conn, clues["cross_region_reappear"], STAFF_A,
        "confirm_for_transfer",
        "药盒宜宾结算后于广州网售并回流宜宾扣押，监控拍到涂改追溯码，"
        "跨省协查与物流面单相互印证，建议随案移送公安；系统线索仅作提示，定性以机关认定为准。",
        token_a, case_id=case_id)
    print(f"决定1（跨省回流，建议移送）：{dec1}")

    token_b = cases.acquire_lock(conn, clues["repeat_prescription"], STAFF_B)
    dec2 = cases.add_decision(
        conn, clues["repeat_prescription"], STAFF_B, "request_coop",
        "同日两家医院间隔80分钟开药，需调处方与就诊记录核实是否真实诊疗，暂不定性。",
        token_b, case_id=case_id)
    print(f"决定2（重复开药，补证协查）：{dec2}")

    token_c = cases.acquire_lock(conn, clues["cold_chain_mismatch"], STAFF_A)
    dec3 = cases.add_decision(
        conn, clues["cold_chain_mismatch"], STAFF_A, "escalate",
        "探头温度与温控单声称区间矛盾、入院时间超出声称冷链时段，疑似补造材料，移交药监核验。",
        token_c, case_id=case_id)
    print(f"决定3（冷链材料不符，升级核查）：{dec3}")

    show("五、证据封存（版本清单 + 哈希链），封存后冻结")
    seal = cases.seal_case(conn, case_id, SEALER, note="移送前首次封存")
    print(f"封存 {seal['seal_id']}：线索 {seal['clues']} 条、决定 {seal['decisions']} 条、"
          f"原始记录 {seal['raw_records']} 份；清单哈希 {seal['manifest_hash'][:16]}…")
    try:
        cases.attach_clue(conn, STAFF_A, case_id, list(clues.values())[0])
    except Exception as e:
        print(f"封存后再归并线索被拒绝：{e}")

    show("六、交接移送与接收回执")
    ho = cases.handoff(conn, case_id, STAFF_A, to_org="宜宾市公安局食品药品犯罪侦查支队")
    print(f"发起交接：{ho['handoff_id']} → 公安")
    cases.receive_handoff(conn, ho["handoff_id"], "宜宾市公安局食药侦支队",
                          "民警B",  "YBSA-2026-0918", "清单与封存哈希核对无误，予以接收")
    print("接收方已追加回执（回执号 YBSA-2026-0918），案件状态转为 transferred")

    show("七、从一条线索还原完整证据链（当时数据版本+人工决定+交接回执）")
    chain = cases.evidence_chain(conn, clues["cross_region_reappear"])
    print(json.dumps(chain, ensure_ascii=False, indent=2, default=str)[:2600], "…")

    show("八、分级查看")
    pub = query.public_verify(conn, "8115-M1-0001")
    print("公众核验单盒摘要：")
    print(json.dumps({k: v for k, v in pub.items()
                      if k not in ("legal_chain",)}, ensure_ascii=False, indent=2))
    try:
        query.medicine_dossier(conn, "u_public", "8115-M1-0001")
    except PermissionDenied as e:
        print(f"\n公众尝试查看案件档案被拒绝：{e}")
    integrity = cases.verify_integrity(conn)
    print(f"\n证据完整性校验：{'通过' if integrity['ok'] else integrity['problems']}")
    print(f"数据库文件：{Path(db_dir)/'audit.db'}")


if __name__ == "__main__":
    main()
