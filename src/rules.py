"""风险线索规则（纯逻辑，不触碰定性）。

规则只产出"风险提示"：severity 表示关注程度，任何规则都不得输出
违法/违规结论。每条发现附带：
- 触发所用输入证据（record_id + version + digest，可回溯到精确版本）
- 人话解释与时间线
- 规则代码、规则版本与实际生效参数
研判人员可以据此复核、排除或移交，决定权在人。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

PROVINCE_RE = re.compile(r"^(.*?(?:省|市|自治区|特别行政区))")


def province_of(region: str | None) -> str:
    """从 '四川省宜宾市' 归一化出省级行政区。"""
    if not region:
        return "未知"
    m = PROVINCE_RE.match(region.strip())
    return m.group(1) if m else region.strip()[:3]


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ref(view: dict, note: str) -> dict:
    return {"source_kind": view["source_kind"], "business_key": view["business_key"],
            "record_id": view["record_id"], "version": view["version"],
            "digest": view["digest"], "note": note}


@dataclass(frozen=True)
class Rule:
    code: str
    version: str
    name: str
    default_params: dict[str, Any]
    evaluate: Any  # callable(store, params, scope) -> list[Finding]


@dataclass
class Finding:
    rule_code: str
    rule_version: str
    severity: str            # low / medium / high —— 仅为关注程度
    subject_type: str        # box / insured / batch
    subject_id: str
    title: str
    explanation: str
    evidence: list[dict] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 规则一：同一药盒在不同地区再次出现
# ---------------------------------------------------------------------------

def _box_indexes(store) -> tuple[dict, dict, dict, dict]:
    """按盒码归并 trace / settlement / online_sale / seizure 当前版本。"""
    traces: dict[str, list[dict]] = {}
    settles: dict[str, list[dict]] = {}
    for v in store.all_current("trace_event"):
        code = v["payload"].get("box_code")
        if code:
            traces.setdefault(code, []).append(v)
    for v in store.all_current("settlement"):
        for item in v["payload"].get("items", []):
            code = item.get("box_code")
            if code:
                settles.setdefault(code, []).append(v)
    online: dict[str, list[dict]] = {}
    for v in store.all_current("online_sale"):
        code = v["payload"].get("box_code")
        if code:
            online.setdefault(code, []).append(v)
    seized: dict[str, list[dict]] = {}
    for v in store.all_current("seizure"):
        for box in v["payload"].get("boxes", []):
            code = box.get("box_code")
            if code:
                seized.setdefault(code, []).append((v, box))
    return traces, settles, online, seized


def eval_reappearance(store, params, scope) -> list[Finding]:
    gap_hours = params["min_settle_to_reappear_hours"]
    findings: list[Finding] = []
    traces, settles, online, seized = _box_indexes(store)
    boxes = set(traces) | set(settles) | set(online) | set(seized)
    if scope.get("box_codes"):
        boxes &= set(scope["box_codes"])

    for code in sorted(boxes):
        evs = sorted(traces.get(code, []),
                     key=lambda v: v["payload"].get("event_time") or "")
        timeline = [{"t": e["payload"].get("event_time"),
                     "region": e["payload"].get("region"),
                     "event_type": e["payload"].get("event_type"),
                     "org": e["payload"].get("org_name")} for e in evs]

        settle_hits = [{"settlement_no": sv["payload"].get("settlement_no"),
                        "region": sv["payload"].get("region"),
                        "settle_time": sv["payload"].get("settle_time"),
                        "org": sv["payload"].get("org_name")}
                       for sv in settles.get(code, [])]
        earliest_settle = min((parse_ts(s["settle_time"])
                               for s in settle_hits if parse_ts(s["settle_time"])),
                              default=None)

        later_hits = []
        post_provinces = {province_of(s["region"]) for s in settle_hits}
        if earliest_settle is not None:
            # 仅考察"医保结算之后"的再现，避免把厂家到医院的正常跨省流通误报
            for ev in evs:
                ep = ev["payload"]
                et = parse_ts(ep.get("event_time"))
                if et is None or et <= earliest_settle:
                    continue
                post_provinces.add(province_of(ep.get("region")))
                if province_of(ep.get("region")) not in \
                        {province_of(s["region"]) for s in settle_hits}:
                    later_hits.append({
                        "after_hours": round(
                            (et - earliest_settle).total_seconds() / 3600, 1),
                        "channel": "trace", "event": timeline_entry(ev)})
            for ov in online.get(code, []):
                op = ov["payload"]
                ot = parse_ts(op.get("post_time"))
                if ot is None or ot <= earliest_settle:
                    continue
                post_provinces.add(province_of(op.get("region")))
                if province_of(op.get("region")) not in \
                        {province_of(s["region"]) for s in settle_hits}:
                    later_hits.append({
                        "after_hours": round(
                            (ot - earliest_settle).total_seconds() / 3600, 1),
                        "channel": "online_sale",
                        "event": {"t": op.get("post_time"),
                                  "region": op.get("region"),
                                  "org": op.get("platform")}})
            for sv, box in seized.get(code, []):
                sp = sv["payload"]
                st_ = parse_ts(sp.get("seized_at"))
                if st_ is None or st_ <= earliest_settle:
                    continue
                post_provinces.add(province_of(sp.get("warehouse_region")))
                if province_of(sp.get("warehouse_region")) not in \
                        {province_of(s["region"]) for s in settle_hits}:
                    later_hits.append({
                        "after_hours": round(
                            (st_ - earliest_settle).total_seconds() / 3600, 1),
                        "channel": "seizure",
                        "event": {"t": sp.get("seized_at"),
                                  "region": sp.get("warehouse_region"),
                                  "org": sp.get("warehouse_name")}})

        cross_province = earliest_settle is not None and len(post_provinces) >= 2
        if not (cross_province or later_hits):
            continue

        evidence = [_ref(e, f"扫码事件：{e['payload'].get('event_type')} "
                            f"@ {e['payload'].get('region')}") for e in evs]
        evidence += [_ref(sv, "医保结算") for sv in settles.get(code, [])]
        evidence += [_ref(ov, "网络销售") for ov in online.get(code, [])]
        for sv, box in seized.get(code, []):
            evidence.append(_ref(sv, "扣押登记（包装受损/码面涂改情况以原件为准）"))

        damaged = any(b.get("pkg_damaged") or b.get("code_altered")
                      for _, b in seized.get(code, []))
        sold_online = any(h["channel"] == "online_sale" for h in later_hits)
        severity = "high" if (later_hits and (damaged or sold_online)) else (
            "medium" if later_hits else "low")

        reasons = []
        if cross_province:
            reasons.append(f"医保结算后实物出现在 {len(post_provinces)} 个省级区域："
                           f"{'、'.join(sorted(post_provinces))}")
        if later_hits:
            within_gap = sum(1 for h in later_hits if h["after_hours"] >= gap_hours)
            reasons.append(f"结算后异地再现 {len(later_hits)} 次"
                           f"（其中间隔 ≥{gap_hours} 小时 {within_gap} 次）")
        title = f"同一药盒 {code} 跨区域再现"
        explanation = (
            f"药盒 {code} 的追溯/结算材料存在以下时间线特征：{'；'.join(reasons)}。"
            "该线索只说明实物流转与结算地点存在需要核查的矛盾，"
            "是否构成骗保或回流药品违法，须由办案人员结合原件证据判断，系统不作认定。\n"
            f"时间线：{_short_timeline(timeline, settle_hits, online.get(code, []))}"
        )
        findings.append(Finding(
            rule_code="REAPPEAR-01", rule_version="1.0", severity=severity,
            subject_type="box", subject_id=code, title=title,
            explanation=explanation, evidence=evidence,
            facts={"post_settlement_provinces": sorted(post_provinces),
                   "timeline": timeline, "settlements": settle_hits,
                   "later_hits": later_hits,
                   "pkg_damaged_or_code_altered": damaged,
                   "online_sale": sold_online},
            params=params))
    return findings


def timeline_entry(view: dict) -> dict:
    p = view["payload"]
    return {"t": p.get("event_time"), "region": p.get("region"),
            "event_type": p.get("event_type"), "org": p.get("org_name")}


def _short_timeline(timeline, settles, online_views) -> str:
    parts = [f"{x['t']} {x.get('org') or ''}[{x['event_type']}]@{x['region']}"
             for x in timeline if x["t"]]
    parts += [f"{s['settle_time']} 医保结算@{s['region']}" for s in settles]
    parts += [f"{o['payload'].get('post_time')} 网售@{o['payload'].get('region')}"
              for o in online_views]
    return "；".join(sorted(p for p in parts if p))


# ---------------------------------------------------------------------------
# 规则二：同一参保人同一天短间隔重复开药
# ---------------------------------------------------------------------------

def eval_repeat_prescription(store, params, scope) -> list[Finding]:
    gap = params["min_gap_minutes"]
    distinct_org = params["distinct_org_only"]
    findings: list[Finding] = []
    by_person: dict[str, list[dict]] = {}
    for v in store.all_current("settlement"):
        pid = v["payload"].get("insured_id")
        if pid and parse_ts(v["payload"].get("settle_time")):
            by_person.setdefault(pid, []).append(v)
    if scope.get("insured_ids"):
        by_person = {k: x for k, x in by_person.items()
                     if k in set(scope["insured_ids"])}

    for pid, views in sorted(by_person.items()):
        views = sorted(views, key=lambda v: v["payload"]["settle_time"])
        hits = []
        for a, b in zip(views, views[1:]):
            pa, pb = a["payload"], b["payload"]
            ta, tb = parse_ts(pa["settle_time"]), parse_ts(pb["settle_time"])
            if ta.date() != tb.date():
                continue
            if distinct_org and pa.get("org_code") == pb.get("org_code"):
                continue
            minutes = (tb - ta).total_seconds() / 60
            if minutes <= gap:
                drugs_a = {i.get("drug_name") for i in pa.get("items", [])}
                drugs_b = {i.get("drug_name") for i in pb.get("items", [])}
                hits.append({
                    "minutes": round(minutes),
                    "a": {"settlement_no": pa.get("settlement_no"),
                          "time": pa.get("settle_time"),
                          "org": pa.get("org_name"), "region": pa.get("region")},
                    "b": {"settlement_no": pb.get("settlement_no"),
                          "time": pb.get("settle_time"),
                          "org": pb.get("org_name"), "region": pb.get("region")},
                    "cross_province": province_of(pa.get("region")) !=
                                      province_of(pb.get("region")),
                    "same_drug": bool(drugs_a & drugs_b),
                })
        if not hits:
            continue
        used = {h["a"]["settlement_no"] for h in hits} | \
               {h["b"]["settlement_no"] for h in hits}
        evidence = [_ref(v, f"结算：{v['payload'].get('settle_time')} "
                            f"@ {v['payload'].get('org_name')}")
                    for v in views if v["payload"].get("settlement_no") in used]
        cross = sum(1 for h in hits if h["cross_province"])
        same_drug = any(h["same_drug"] for h in hits)
        severity = "high" if cross and same_drug else ("medium" if cross else "low")
        explanation = (
            f"参保人代号 {pid} 在同一日内有 {len(hits)} 对结算间隔不超过 "
            f"{gap} 分钟" + ("且发生在不同医疗机构" if distinct_org else "") + "，"
            f"其中跨省 {cross} 对、含同类药品 {'是' if same_drug else '否'}。"
            "短间隔重复结算可能源于代购药、倒药或重复就诊等多种情形，"
            "也可能是急重病合理就医，系统仅提示人工核查，不作违法定性。"
        )
        findings.append(Finding(
            rule_code="REPEAT-RX-01", rule_version="1.0", severity=severity,
            subject_type="insured", subject_id=pid,
            title=f"参保人 {pid} 同日短间隔重复结算",
            explanation=explanation, evidence=evidence,
            facts={"hits": hits, "pair_count": len(hits)}, params=params))
    return findings


# ---------------------------------------------------------------------------
# 规则三：冷链材料与真实流转不符
# ---------------------------------------------------------------------------

def eval_cold_chain(store, params, scope) -> list[Finding]:
    findings: list[Finding] = []
    traces: dict[str, list[dict]] = {}
    for v in store.all_current("trace_event"):
        code = v["payload"].get("box_code")
        if code:
            traces.setdefault(code, []).append(v)

    for v in store.all_current("voucher"):
        p = v["payload"]
        if p.get("voucher_type") != "cold_chain":
            continue
        code = p.get("box_code")
        if scope.get("box_codes") and code not in set(scope["box_codes"]):
            continue
        segments = p.get("segments", [])
        starts = [parse_ts(s.get("start")) for s in segments]
        ends = [parse_ts(s.get("end")) for s in segments]
        spans = [t for t in starts + ends if t is not None]
        span_start, span_end = (min(spans), max(spans)) if spans else (None, None)
        mismatches = []
        for ev in sorted(traces.get(code, []),
                         key=lambda x: x["payload"].get("event_time") or ""):
            ep = ev["payload"]
            et = parse_ts(ep.get("event_time"))
            # 冷链运单只对其承运时段窗口内的流转负责，窗口外的生产/入库不比对
            if et is None or span_start is None or et < span_start or \
                    et > span_end:
                continue
            cover = None
            for seg in segments:
                s, e = parse_ts(seg.get("start")), parse_ts(seg.get("end"))
                if s and e and s <= et <= e:
                    cover = seg
                    break
            if cover is None:
                mismatches.append({
                    "type": "uncovered_movement",
                    "detail": f"{ep.get('event_time')} 在 {ep.get('region')} "
                              f"发生 {ep.get('event_type')}，无任何冷链承运时段覆盖",
                    "event": timeline_entry(ev)})
            elif province_of(cover.get("region")) != province_of(ep.get("region")):
                mismatches.append({
                    "type": "region_conflict",
                    "detail": f"{ep.get('event_time')} 冷链单记载位于 "
                              f"{cover.get('region')}，实物扫码出现在 "
                              f"{ep.get('region')}",
                    "event": timeline_entry(ev)})
            reading = ep.get("temp_c")
            if reading is not None and cover is not None:
                lo = cover.get("temp_min_c")
                hi = cover.get("temp_max_c")
                if (lo is not None and reading < lo) or \
                   (hi is not None and reading > hi):
                    mismatches.append({
                        "type": "temp_excursion",
                        "detail": f"{ep.get('event_time')} 实测 {reading}℃ "
                                  f"超出冷链单约定 [{lo},{hi}]℃",
                        "event": timeline_entry(ev)})
        if not mismatches:
            continue
        evidence = [_ref(v, "冷链票账材料")]
        for ev in traces.get(code, []):
            if any(m["event"].get("t") == ev["payload"].get("event_time")
                   for m in mismatches):
                evidence.append(_ref(ev, "真实流转扫码"))
        types = {m["type"] for m in mismatches}
        severity = "high" if "region_conflict" in types else "medium"
        explanation = (
            f"药盒 {code} 的冷链票账材料与追溯扫码记录比对发现 "
            f"{len(mismatches)} 处不符（{'、'.join(sorted(types))}）：\n"
            + "\n".join(f"- {m['detail']}" for m in mismatches)
            + "\n不符可能是单据补录、转运换装或材料造假，系统仅提示矛盾点，"
              "不得仅凭本条线索认定任何主体违法。"
        )
        findings.append(Finding(
            rule_code="COLD-01", rule_version="1.0", severity=severity,
            subject_type="box", subject_id=code,
            title=f"药盒 {code} 冷链材料与真实流转不符",
            explanation=explanation, evidence=evidence,
            facts={"mismatches": mismatches, "voucher_doc": p.get("doc_no")},
            params=params))
    return findings


RULES: dict[str, Rule] = {
    "REAPPEAR-01": Rule(
        code="REAPPEAR-01", version="1.0",
        name="同一药盒在不同地区再次出现",
        default_params={"min_settle_to_reappear_hours": 24},
        evaluate=eval_reappearance),
    "REPEAT-RX-01": Rule(
        code="REPEAT-RX-01", version="1.0",
        name="同一天短间隔重复开药",
        default_params={"min_gap_minutes": 60, "distinct_org_only": True},
        evaluate=eval_repeat_prescription),
    "COLD-01": Rule(
        code="COLD-01", version="1.0",
        name="冷链材料与真实流转不符",
        default_params={},
        evaluate=eval_cold_chain),
}
