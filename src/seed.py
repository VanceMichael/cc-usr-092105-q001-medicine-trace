"""演示场景种子数据：宜宾医保稽核"回流药跨省"案件。

所有名称、编号均为虚构，仅用于说明字段关系（见 docs/domain.md）。
时间线要点：
- BOX8101 在宜宾定点医院结算后，多日后在广州被扫码、网售并在受损包装下扣押；
  广州扫码/网售/扣押材料故意延迟入库，模拟跨省迟到数据。
- BOX8102 全程正常，用于对照公众核验与"无异常不报"。
- BOX8103 冷链票号称全程成都，实物扫码却出现在昆明且温度越限。
- 参保人 P-51128 同日 40 分钟内在宜宾、重庆两地结算同类药；
  P-90001 同日异地就诊但经核实为转诊（演示人工排除后机器不翻案）。
"""

from __future__ import annotations

from .store import Store

# 初始批次（宜宾本地可见材料）
BASE_TRACES = [
    {"business_key": "T-BOX8101-01", "business_time": "2026-08-25T09:00:00+08:00",
     "source_doc_no": "TRACE-NMPC-1001",
     "payload": {"box_code": "BOX8101A202608250001", "drug_name": "示例冷藏单抗注射液",
                 "spec": "400mg/支", "manufacturer": "苏州示例生物制药有限公司",
                 "event_type": "produce", "event_time": "2026-08-25T09:00:00+08:00",
                 "region": "江苏省苏州市", "org_code": "MFR-SZ-01",
                 "org_name": "苏州示例生物制药有限公司"}},
    {"business_key": "T-BOX8101-02", "business_time": "2026-08-26T10:00:00+08:00",
     "payload": {"box_code": "BOX8101A202608250001", "event_type": "outbound",
                 "event_time": "2026-08-26T10:00:00+08:00",
                 "region": "江苏省苏州市", "org_code": "DIST-SZ-09",
                 "org_name": "苏州示例医药物流中心"}},
    {"business_key": "T-BOX8101-03", "business_time": "2026-08-28T08:30:00+08:00",
     "payload": {"box_code": "BOX8101A202608250001", "event_type": "inbound",
                 "event_time": "2026-08-28T08:30:00+08:00",
                 "region": "四川省宜宾市", "org_code": "H-YB-001",
                 "org_name": "宜宾市第一人民医院"}},
    {"business_key": "T-BOX8101-04", "business_time": "2026-09-01T10:15:00+08:00",
     "payload": {"box_code": "BOX8101A202608250001", "event_type": "dispense",
                 "event_time": "2026-09-01T10:15:00+08:00",
                 "region": "四川省宜宾市", "org_code": "H-YB-001",
                 "org_name": "宜宾市第一人民医院"}},
    # ---- 正常对照药盒 ----
    {"business_key": "T-BOX8102-01", "business_time": "2026-08-20T09:00:00+08:00",
     "payload": {"box_code": "BOX8102B202608200007", "drug_name": "示例降压素片",
                 "spec": "5mg×14片", "manufacturer": "华北示例药业股份有限公司",
                 "event_type": "produce", "event_time": "2026-08-20T09:00:00+08:00",
                 "region": "河北省石家庄市", "org_code": "MFR-HB-02",
                 "org_name": "华北示例药业股份有限公司"}},
    {"business_key": "T-BOX8102-02", "business_time": "2026-08-25T08:00:00+08:00",
     "payload": {"box_code": "BOX8102B202608200007", "event_type": "inbound",
                 "event_time": "2026-08-25T08:00:00+08:00",
                 "region": "四川省宜宾市", "org_code": "PH-YB-18",
                 "org_name": "宜宾市翠屏区示例社区卫生服务中心"}},
    {"business_key": "T-BOX8102-03", "business_time": "2026-09-02T09:30:00+08:00",
     "payload": {"box_code": "BOX8102B202608200007", "event_type": "dispense",
                 "event_time": "2026-09-02T09:30:00+08:00",
                 "region": "四川省宜宾市", "org_code": "PH-YB-18",
                 "org_name": "宜宾市翠屏区示例社区卫生服务中心"}},
    # ---- 冷链争议药盒：票在成都、实物在昆明 ----
    {"business_key": "T-BOX8103-01", "business_time": "2026-08-18T09:00:00+08:00",
     "payload": {"box_code": "BOX8103C202608180003", "drug_name": "示例冷藏单抗注射液",
                 "spec": "100mg/支", "manufacturer": "苏州示例生物制药有限公司",
                 "event_type": "produce", "event_time": "2026-08-18T09:00:00+08:00",
                 "region": "江苏省苏州市", "org_code": "MFR-SZ-01",
                 "org_name": "苏州示例生物制药有限公司"}},
    {"business_key": "T-BOX8103-02", "business_time": "2026-08-22T08:00:00+08:00",
     "payload": {"box_code": "BOX8103C202608180003", "event_type": "inbound",
                 "event_time": "2026-08-22T08:00:00+08:00",
                 "region": "四川省成都市", "org_code": "H-CD-31",
                 "org_name": "成都市示例区人民医院"}},
    {"business_key": "T-BOX8103-03", "business_time": "2026-09-02T14:00:00+08:00",
     "payload": {"box_code": "BOX8103C202608180003", "event_type": "scan",
                 "event_time": "2026-09-02T14:00:00+08:00", "temp_c": 11.5,
                 "region": "云南省昆明市", "org_code": "WH-KM-02",
                 "org_name": "昆明某医药冷库（扫码点）"}},
]

BASE_SETTLEMENTS = [
    {"business_key": "S-20260901-7781", "business_time": "2026-09-01T10:20:00+08:00",
     "source_doc_no": "YB-JS-202609-7781",
     "payload": {"settlement_no": "S-20260901-7781", "insured_id": "P-51128",
                 "org_code": "H-YB-001", "org_name": "宜宾市第一人民医院",
                 "region": "四川省宜宾市", "settle_time": "2026-09-01T10:20:00+08:00",
                 "diagnosis": "示例病种甲", "amount_total": 3260.00, "fund_pay": 2412.40,
                 "items": [{"box_code": "BOX8101A202608250001",
                            "drug_name": "示例冷藏单抗注射液", "qty": 1, "price": 3260.00}]}},
    {"business_key": "S-20260903-2102", "business_time": "2026-09-03T09:00:00+08:00",
     "source_doc_no": "YB-JS-202609-2102",
     "payload": {"settlement_no": "S-20260903-2102", "insured_id": "P-51128",
                 "org_code": "H-YB-001", "org_name": "宜宾市第一医院",  # 故意缺字，随后更正
                 "region": "四川省宜宾市", "settle_time": "2026-09-03T09:00:00+08:00",
                 "diagnosis": "示例慢病乙", "amount_total": 418.20, "fund_pay": 301.10,
                 "items": [{"drug_name": "示例降糖素片", "qty": 2, "price": 209.10}]}},
    {"business_key": "S-20260903-2144", "business_time": "2026-09-03T09:40:00+08:00",
     "source_doc_no": "CQ-JS-202609-88402",
     "payload": {"settlement_no": "S-20260903-2144", "insured_id": "P-51128",
                 "org_code": "H-CQ-YC-07", "org_name": "重庆市永川区示例中医院",
                 "region": "重庆市永川区", "settle_time": "2026-09-03T09:40:00+08:00",
                 "diagnosis": "示例慢病乙", "amount_total": 421.00, "fund_pay": 305.20,
                 "items": [{"drug_name": "示例降糖素片", "qty": 2, "price": 210.50}]}},
    {"business_key": "S-20260902-3301", "business_time": "2026-09-02T09:35:00+08:00",
     "payload": {"settlement_no": "S-20260902-3301", "insured_id": "P-60001",
                 "org_code": "PH-YB-18",
                 "org_name": "宜宾市翠屏区示例社区卫生服务中心",
                 "region": "四川省宜宾市", "settle_time": "2026-09-02T09:35:00+08:00",
                 "diagnosis": "高血压", "amount_total": 36.00, "fund_pay": 25.20,
                 "items": [{"box_code": "BOX8102B202608200007",
                            "drug_name": "示例降压素片", "qty": 1, "price": 36.00}]}},
    {"business_key": "S-20260903-0901", "business_time": "2026-09-03T09:00:00+08:00",
     "payload": {"settlement_no": "S-20260903-0901", "insured_id": "P-90001",
                 "org_code": "H-YB-001", "org_name": "宜宾市第一人民医院",
                 "region": "四川省宜宾市", "settle_time": "2026-09-03T09:00:00+08:00",
                 "diagnosis": "胸痛待查", "amount_total": 128.00, "fund_pay": 91.00,
                 "items": [{"drug_name": "示例急救用药", "qty": 1, "price": 128.00}]}},
    {"business_key": "S-20260903-0955", "business_time": "2026-09-03T09:55:00+08:00",
     "payload": {"settlement_no": "S-20260903-0955", "insured_id": "P-90001",
                 "org_code": "H-ZG-12", "org_name": "自贡市示例区人民医院",
                 "region": "四川省自贡市", "settle_time": "2026-09-03T09:55:00+08:00",
                 "diagnosis": "胸痛待查（转诊）", "amount_total": 96.00, "fund_pay": 67.20,
                 "items": [{"drug_name": "示例急救用药", "qty": 1, "price": 96.00}]}},
    {"business_key": "S-20260903-7710", "business_time": "2026-09-03T09:10:00+08:00",
     "payload": {"settlement_no": "S-20260903-7710", "insured_id": "P-60002",
                 "org_code": "H-CD-31", "org_name": "成都市示例区人民医院",
                 "region": "四川省成都市", "settle_time": "2026-09-03T09:10:00+08:00",
                 "diagnosis": "示例病种甲", "amount_total": 1190.00, "fund_pay": 880.60,
                 "items": [{"box_code": "BOX8103C202608180003",
                            "drug_name": "示例冷藏单抗注射液", "qty": 1, "price": 1190.00}]}},
]

BASE_VOUCHERS = [
    {"business_key": "V-INV-202608-0091", "business_time": "2026-08-26T11:00:00+08:00",
     "source_doc_no": "FP-202608-0091",
     "payload": {"voucher_type": "invoice", "doc_no": "FP-202608-0091",
                 "seller": "苏州示例医药物流中心", "buyer": "宜宾市第一人民医院",
                 "box_codes": ["BOX8101A202608250001"], "amount": 3260.00,
                 "issue_time": "2026-08-26T11:00:00+08:00",
                 "note": "增值税发票（示例）"}},
    {"business_key": "V-PAY-202609-0033", "business_time": "2026-09-05T15:00:00+08:00",
     "source_doc_no": "PAY-202609-0033",
     "payload": {"voucher_type": "payment", "doc_no": "PAY-202609-0033",
                 "payer": "宜宾市医保经办示例账户", "payee": "宜宾市第一人民医院",
                 "settlement_nos": ["S-20260901-7781"], "amount": 2412.40,
                 "pay_time": "2026-09-05T15:00:00+08:00"}},
    {"business_key": "V-COLD-8103", "business_time": "2026-09-02T20:30:00+08:00",
     "source_doc_no": "LL-CD-20260902-17",
     "payload": {"voucher_type": "cold_chain", "doc_no": "LL-CD-20260902-17",
                 "box_code": "BOX8103C202608180003",
                 "carrier": "成都示例冷链物流有限公司",
                 "segments": [{"start": "2026-09-02T06:00:00+08:00",
                               "end": "2026-09-02T20:00:00+08:00",
                               "region": "四川省成都市", "temp_min_c": 2.0,
                               "temp_max_c": 8.0}],
                 "issue_time": "2026-09-02T20:30:00+08:00"}},
]

BASE_COOPERATION = [
    {"business_key": "COOP-GZ-YB-01", "business_time": "2026-09-13T10:00:00+08:00",
     "source_doc_no": "穗公经侦协〔2026〕示例118号",
     "payload": {"doc_no": "穗公经侦协〔2026〕示例118号",
                 "from_org": "广州市公安局示例分局", "to_org": "宜宾市医疗保障局",
                 "subject": "BOX8101A202608250001 等药品回流协查",
                 "issued_at": "2026-09-13T10:00:00+08:00",
                 "content": "我局在仓库查扣一批外包装受损药品，追溯码有涂改痕迹，请协助核查医保结算去向。"}},
]

# 迟到批次：广州方向材料（跨平台数据数日后才汇到）
LATE_TRACES = [
    {"business_key": "T-BOX8101-05", "business_time": "2026-09-08T22:10:00+08:00",
     "source_doc_no": "TRACE-GD-773201",
     "payload": {"box_code": "BOX8101A202608250001", "event_type": "scan",
                 "event_time": "2026-09-08T22:10:00+08:00",
                 "region": "广东省广州市", "org_code": "WH-GZ-BY-06",
                 "org_name": "广州市白云区示例仓库（入库扫码）"}},
]

LATE_ONLINE = [
    {"business_key": "OL-33901", "business_time": "2026-09-10T20:31:00+08:00",
     "source_doc_no": "CYBER-GZ-20260910-33901",
     "payload": {"listing_id": "LST-20260910-33901",
                 "platform": "某网络交易平台（店铺已取证）",
                 "box_code": "BOX8101A202608250001",
                 "post_time": "2026-09-10T20:31:00+08:00", "region": "广东省广州市",
                 "price": 2600.00,
                 "seller_handle_masked": "商家账号*甲（已按程序调取实名）",
                 "snapshot_no": "EV-SAFE-20260910-5521"}},
]

LATE_SEIZURE = [
    {"business_key": "SZ-GZ-0912", "business_time": "2026-09-12T16:00:00+08:00",
     "source_doc_no": "穗市监扣〔2026〕示例0912号",
     "payload": {"seizure_no": "SZ-GZ-0912",
                 "warehouse_name": "广州市白云区示例仓库",
                 "warehouse_region": "广东省广州市",
                 "seized_at": "2026-09-12T16:00:00+08:00",
                 "officer_org": "广州市市场监管综合执法示例支队",
                 "witness": "属地派出所示例民警",
                 "boxes": [{"box_code": "BOX8101A202608250001",
                            "pkg_damaged": True, "code_altered": True,
                            "photo_no": "IMG-SZ-0912-01至07",
                            "appearance": "外包装受潮破损，追溯码贴纸有刮擦涂改痕迹"}],
                 "storage": "扣押专柜封存，温控登记随案"}},
]

# 迟到的结算补单：业务发生在 09-03 08:50，跨省平台 09-14 才汇到，
# 此时结算来源水位线已推进到 09-03 09:55，按迟到数据登记并触发复评。
LATE_SETTLEMENTS = [
    {"business_key": "S-20260903-0850", "business_time": "2026-09-03T08:50:00+08:00",
     "source_doc_no": "CD-JS-202609-5571",
     "payload": {"settlement_no": "S-20260903-0850", "insured_id": "P-51128",
                 "org_code": "PH-CD-WH-22", "org_name": "成都市武侯区示例药店",
                 "region": "四川省成都市", "settle_time": "2026-09-03T08:50:00+08:00",
                 "diagnosis": "示例慢病乙", "amount_total": 205.00, "fund_pay": 148.00,
                 "items": [{"drug_name": "示例降糖素片", "qty": 2, "price": 102.50}]}},
]


def seed_base(store: Store) -> dict:
    """灌入宜宾本地初始材料。"""
    out = {}
    out["trace"] = store.ingest_batch(
        "trace_event", "国家/四川药品追溯平台（示例）", BASE_TRACES,
        batch_id="B-INIT-TRACE")
    out["settlement"] = store.ingest_batch(
        "settlement", "宜宾市医保结算系统（示例）", BASE_SETTLEMENTS,
        batch_id="B-INIT-SETTLE")
    out["voucher"] = store.ingest_batch(
        "voucher", "票据/资金台账归集（示例）", BASE_VOUCHERS,
        batch_id="B-INIT-VOUCHER")
    out["cooperation"] = store.ingest_batch(
        "cooperation", "广州公安协查来函（示例）", BASE_COOPERATION,
        batch_id="B-INIT-COOP")
    return out


# 对初始报文的更正：机构名称缺字，来源单位出具勘误；只追加第 2 版，不覆盖第 1 版
CORRECTIONS = [
    {"business_key": "S-20260903-2102", "business_time": "2026-09-06T11:00:00+08:00",
     "correction_reason": "宜宾市第一人民医院出具《结算报文勘误说明》，"
                          "机构名称缺『人民』二字，其余字段不变（YB-JY-202609-041）",
     "payload": {"settlement_no": "S-20260903-2102", "insured_id": "P-51128",
                 "org_code": "H-YB-001", "org_name": "宜宾市第一人民医院",
                 "region": "四川省宜宾市", "settle_time": "2026-09-03T09:00:00+08:00",
                 "diagnosis": "示例慢病乙", "amount_total": 418.20, "fund_pay": 301.10,
                 "items": [{"drug_name": "示例降糖素片", "qty": 2, "price": 209.10}]}},
]


def seed_late(store: Store) -> dict:
    """模拟数日后跨省迟到数据到达。"""
    return {
        "trace": store.ingest_batch("trace_event", "广东药品追溯节点（示例）",
                                    LATE_TRACES, batch_id="B-LATE-GD-TRACE"),
        "online_sale": store.ingest_batch("online_sale",
                                          "广州网监取证材料（示例）",
                                          LATE_ONLINE, batch_id="B-LATE-GD-ONLINE"),
        "seizure": store.ingest_batch("seizure", "广州市监扣押登记（示例）",
                                      LATE_SEIZURE, batch_id="B-LATE-GD-SEIZURE"),
        "settlement": store.ingest_batch(
            "settlement", "跨省结算平台补单（示例）", LATE_SETTLEMENTS,
            batch_id="B-LATE-CD-SETTLE"),
        "correction": store.ingest_batch(
            "settlement", "宜宾市医保结算系统（示例）", CORRECTIONS,
            batch_id="B-LATE-CORRECTION"),
    }
