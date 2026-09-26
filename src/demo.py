"""端到端演示：从扣押药品到跨省移送的完整稽核事实链。

运行：python -m src.demo
全程使用虚构数据，仅打印业务过程，不落盘。
"""

from __future__ import annotations

import json

from .access import PublicVerifier
from .seed import seed_base, seed_late
from .services import AuditService
from .store import Store

BOX = "BOX8101A202608250001"
NORMAL = "BOX8102B202608200007"


def show(title: str, obj=None) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    if obj is not None:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def main() -> None:
    store = Store()
    svc = AuditService(store)

    show("① 按原始来源逐字入库（追溯扫码/医保结算/票账货款/协查函）")
    stats = seed_base(store)
    for kind, s in stats.items():
        print(f"  {kind:12s} 入库 {s['stored']} 条，重复 {s['duplicates']}，"
              f"拒收 {s['rejected']}，迟到 {s['late']}")

    show("② 第一次批量交叉比对（此时广州材料尚未到达）")
    r1 = svc.run_batch(note="初次全量比对")
    for x in r1["created"]:
        print(f"  新线索 {x['lead_no']} [{x['rule_code']}] 主体={x['subject_id']}")
    print(f"  数据切点 {r1['data_cut']['cut_no']}，"
          f"覆盖记录上限 id={r1['data_cut']['max_record_id']}")

    show("③ 跨省迟到数据到达：广州扫码、网络销售、仓库扣押、成都补单")
    late = seed_late(store)
    for kind, s in late.items():
        flag = f"（迟到 {s['late']} 条）" if s["late"] else ""
        print(f"  {kind:12s} 入库 {s['stored']} 条{flag}")
    hist = store.history("settlement", "S-20260903-2102")
    print("\n  结算报文 S-20260903-2102 的版本链（更正只追加，不覆盖）：")
    for h in hist:
        if h["version"] == 0:
            continue
        reason = f" 更正原因：{h['correction_reason'][:28]}…" \
            if h["correction_reason"] else ""
        print(f"    v{h['version']} 入库时间 {h['received_at'][:19]} "
              f"机构={json.loads(h['payload_json'])['org_name']}{reason}")

    show("④ 迟到数据触发复评：线索追加版本，绝不覆盖首版")
    r2 = svc.run_batch(trigger="late_data", note="跨省迟到数据复评")
    print(f"  新线索 {len(r2['created'])} 条，既有线索追加复评版本 "
          f"{len(r2['updated'])} 条")
    box_lead = next(x for x in svc.list_leads(rule_code="REAPPEAR-01")
                    if x["subject_id"] == BOX)
    lead = svc.get_lead(box_lead["lead_no"])
    print(f"\n  线索 {lead['lead_no']}《{lead['title']}》"
          f" 风险等级={lead['severity']} 状态={lead['status']}")
    print("  解释：")
    for line in lead["explanation"].splitlines():
        print("    " + line)
    print("  证据（精确到来源记录版本与摘要）：")
    for e in lead["versions"][-1]["evidence"]:
        print(f"    - {e['source_kind']:12s} {e['business_key']} "
              f"v{e['version']} #{e['record_id']} {e['note']}")

    show("⑤ 人工研判：风险确认只代表『风险成立』，违法定性留给法定程序")
    svc.create_case("YB-2026-HL01", "宜宾回流药跨省专案",
                    "四川省宜宾市", "supervisor.chen")
    svc.add_member("YB-2026-HL01", "zhang.jg", "investigator")
    svc.link_lead("YB-2026-HL01", lead["lead_no"], "zhang.jg")
    dec = svc.decide(lead["lead_no"], actor="zhang.jg", action="confirm_risk",
                     expected_version=2,
                     comment="结算地宜宾与广州扣押/网售事实矛盾客观成立，"
                             "建议移送；是否违法以司法程序认定为准")
    print(f"  决定 seq={dec['seq']} action={dec['action']}")
    print(f"  系统附注：{dec['note']}")

    show("⑥ 证据封存：清单 + 封存哈希")
    seal = svc.seal_case("YB-2026-HL01", "supervisor.chen")
    print(f"  封存包 {seal['package_no']}，记录 {seal['record_count']} 条")
    print(f"  封存哈希 {seal['package_digest']}")
    print("  封存后核验：", svc.verify_seal("YB-2026-HL01")["intact"])

    show("⑦ 跨省交接，广州签收并复核封存哈希，回执入卷")
    tr = svc.transfer_case("YB-2026-HL01", "supervisor.chen",
                           "广州市公安局示例分局", "广东省广州市",
                           note="依据协查函穗公经侦协〔2026〕示例118号移送")
    receipt = svc.receive_transfer(tr["transfer_no"], "gz.officer.liu",
                                   "广州市公安局示例分局",
                                   note="封存包完整，予以签收")
    print(f"  交接单 {tr['transfer_no']} -> {tr['to_org']}")
    print(f"  回执核验：{receipt['conclusion']}（完整={receipt['package_intact']}）")

    show("⑧ 从一条线索还原：数据切点 / 人工决定 / 封存 / 交接回执")
    lineage = svc.lineage(lead["lead_no"])
    v = lineage["versions"][0]
    print(f"  机器版本 v{v['version_no']} 作业={v['job_id']} "
          f"规则版本={v['rule_version']}")
    print(f"  数据切点={v['data_cut']['cut_no']} "
          f"max_record_id={v['data_cut']['max_record_id']} "
          f"切点摘要={v['data_cut']['digest'][:20]}…")
    for d in lineage["decisions"]:
        print(f"  人工决定 seq={d['seq']} {d['action']} by={d['actor']} "
              f"基于修订号 r{d['lock_version']}：{d['comment']}")
    c = lineage["cases"][0]
    print(f"  案件 {c['case_no']} 封存 {len(c['seals'])} 次，"
          f"交接 {len(c['transfers'])} 次，"
          f"回执完整={c['transfers'][0]['receipts'][0]['package_intact']}")

    show("⑨ 公众视角：只能核验单盒合法流转摘要（脱敏，无任何办案信息）")
    pub = PublicVerifier(store)
    for code, tag in ((NORMAL, "正常药盒"), (BOX, "涉案药盒")):
        out = pub.verify(code)
        print(f"\n  【{tag}】{out['box_code_masked']} found={out['found']}")
        print(f"  摘要：{out['summary']}")
        print(f"  配发：{out['dispensed_by']}")
        print(f"  链长：{len(out['chain'])} 个公开环节（止于发药给患者）")

    show("⑩ 哈希链总核验")
    print(" ", store.verify_chain())
    store.close()


if __name__ == "__main__":
    main()
