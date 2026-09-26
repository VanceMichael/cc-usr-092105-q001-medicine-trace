"""分级访问控制与公众单盒核验。

角色模型（真实部署中由统一身份网关注入，这里以请求头模拟）：
- supervisor   医保稽核负责人：可建案、封存、发起跨省交接，跨案查看
- investigator 稽核人员：仅能操作其参与的案件，可录入、比对、研判
- viewer       只读协查人员：仅能查看被授权案件
- public       匿名公众：只能核验单盒药品的合法流转摘要

公众摘要严格白名单：只输出追溯链与"是否存在合规结算/配发"的结论性信息，
不输出参保人、金额、案件、扣押、网售侦查与风险线索的任何内容。
"""

from __future__ import annotations

from .rules import province_of
from .store import Store


# 追溯事件类型 -> 对公众展示的环节名
PUBLIC_EVENT_LABELS = {
    "produce": "生产赋码",
    "outbound": "出厂/批发出库",
    "inbound": "经营/使用单位入库",
    "dispense": "医疗机构药房发药",
    "scan": "流通扫码",
}


class PublicVerifier:
    def __init__(self, store: Store):
        self.store = store

    def verify(self, box_code: str) -> dict:
        traces = [v for v in self.store.all_current("trace_event")
                  if v["payload"].get("box_code") == box_code]
        if not traces:
            # 不存在与"查不到"统一回复，避免被用来枚举盒码
            return {"box_code_masked": self._mask(box_code),
                    "found": False,
                    "summary": "未查询到该追溯码的流通记录，如系实物购得，"
                               "请向售药机构或当地药监、医保部门核验",
                    "chain": []}
        traces.sort(key=lambda v: v["payload"].get("event_time") or "")
        # 合法流转链以"医疗机构发药给患者"为终点：发药之后的任何扫码
        # （可能产生于侦查环节）不进入公众视图
        last_dispense = -1
        for i, v in enumerate(traces):
            if v["payload"].get("event_type") == "dispense":
                last_dispense = i
        if last_dispense >= 0:
            traces = traces[:last_dispense + 1]
        first = traces[0]["payload"]
        chain = []
        for v in traces:
            p = v["payload"]
            chain.append({
                "step": PUBLIC_EVENT_LABELS.get(p.get("event_type"),
                                                p.get("event_type")),
                "time": p.get("event_time"),
                "region": p.get("region"),
                "org": p.get("org_name"),
                "record_version": v["version"],
                "record_digest_prefix": v["digest"][:12],
            })
        # 合规配发：存在该盒的医保结算记录即说明曾由定点机构合法配发
        dispense = None
        for sv in self.store.all_current("settlement"):
            for item in sv["payload"].get("items", []):
                if item.get("box_code") == box_code:
                    p = sv["payload"]
                    dispense = {"org": p.get("org_name"),
                                "region": p.get("region"),
                                "date": (p.get("settle_time") or "")[:10]}
        legit = self._assess(traces, dispense)
        return {
            "box_code_masked": self._mask(box_code),
            "found": True,
            "drug_generic_name": first.get("drug_name"),
            "spec": first.get("spec"),
            "manufacturer": first.get("manufacturer"),
            "summary": legit,
            "dispensed_by": dispense,   # 只有机构与日期，绝无参保人/金额
            "chain": chain,
            "notice": "本摘要仅反映全国追溯平台与医保结算的公开级信息，"
                      "不包含任何监管办案信息；如实物外包装破损或追溯码无法"
                      "辨认，请勿购买使用并拨打 12393/12315 反映。",
        }

    @staticmethod
    def _mask(code: str) -> str:
        return code[:6] + "****" + code[-4:] if len(code) > 12 else "****"

    def _assess(self, traces, dispense) -> str:
        types = [v["payload"].get("event_type") for v in traces]
        provinces = {province_of(v["payload"].get("region")) for v in traces}
        if "produce" in types and dispense:
            chain_desc = "、".join(sorted(
                {PUBLIC_EVENT_LABELS.get(t, t) for t in types}))
            return (f"追溯链完整（{chain_desc}），并于 {dispense['date']} "
                    f"在 {dispense['region']} 的定点医药机构完成结算配发，"
                    f"流转环节涉及 {'、'.join(sorted(provinces))}。"
                    "在公开信息范围内未发现阻断性异常。")
        if dispense:
            return ("该药品有定点机构结算配发记录，但追溯链环节不完整，"
                    "建议向配发机构进一步核验。")
        return ("追溯平台存在流通记录，但未见医保结算配发信息；"
                "如实物与本摘要不符，请暂停使用并向监管部门反映。")
