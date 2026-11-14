"""与仿真站真值逐条对账。

真值来源：各仿真平台的 /admin/state（仅本机+管理密钥），包含当前全量条目
与完整 event_log。对账三个层面，任一差异即非零退出：
  A. 条目集合与字段：每个平台每个 item_code 的价格/状态/版本/更新时间逐条一致；
  B. 归一正确性：12 个标准品种四省全部映射到同一组标准品种（每品种 4 来源）；
     4 个难解析条目必须停在人工确认（pending），不允许自动归一；
  C. 变更历史：仿真侧每一次 price/delist 事件都能在 price_change 表找到，
     类型、原价、新价完全一致，且不存在仿真侧没有的“幽灵变更”。
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field

ADMIN_KEY = "sim-admin-key"


def fetch_truth(base_url: str) -> dict:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/admin/state",
        headers={"X-Admin-Key": ADMIN_KEY})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


@dataclass
class ReconReport:
    platform: str
    field_diffs: list[str] = field(default_factory=list)
    event_diffs: list[str] = field(default_factory=list)
    items_truth: int = 0
    items_collected: int = 0

    @property
    def diff_count(self) -> int:
        return len(self.field_diffs) + len(self.event_diffs)


def reconcile_platform(cfg: dict, db, check_events: bool = True) -> ReconReport:
    truth = fetch_truth(cfg["base_url"])
    rep = ReconReport(cfg["code"])
    rep.items_truth = len(truth["items"])
    raw_rows = {r["item_code"]: r for r in db.list_raw(cfg["code"])}
    rep.items_collected = len(raw_rows)

    # A. 逐条字段对账
    for t in truth["items"]:
        code = t["item_code"]
        got = raw_rows.get(code)
        if got is None:
            rep.field_diffs.append(f"{code}: 采集库缺失该条目")
            continue
        for tk, gk in (("price", "price"), ("status", "status"),
                       ("version", "version"),
                       ("updated_at", "source_updated_at")):
            tv, gv = t[tk], got[gk]
            if tk == "price":
                tv, gv = round(float(tv), 2), round(float(gv), 2)
            if tv != gv:
                rep.field_diffs.append(
                    f"{code}.{tk} 真值={tv!r} 采集={gv!r}")
        if t["generic_name"] != got["generic_name"] or \
                t["spec_text"] != got["spec_text"] or \
                t["manufacturer"] != got["manufacturer"]:
            rep.field_diffs.append(f"{code}: 文本字段与真值不一致")
    extra = set(raw_rows) - {t["item_code"] for t in truth["items"]}
    for code in sorted(extra):
        rep.field_diffs.append(f"{code}: 采集库存在真值中没有的条目")

    # C. 变更事件对账
    if not check_events:
        return rep
    changes = [c for c in db.list_changes(cfg["code"])]
    change_idx = {(c["item_code"], c["change_type"],
                   c["source_changed_at"]): c for c in changes}
    for ev in truth["event_log"]:
        key = (ev["item_code"], ev["type"], ev["changed_at"])
        c = change_idx.pop(key, None)
        if c is None:
            rep.event_diffs.append(
                f"事件缺失: {ev['item_code']} {ev['type']} @ {ev['changed_at']}")
            continue
        if ev["type"] == "price":
            if round(c["old_price"], 2) != round(ev["old_price"], 2) or \
                    round(c["new_price"], 2) != round(ev["new_price"], 2):
                rep.event_diffs.append(
                    f"价格不符: {ev['item_code']} 真值 "
                    f"{ev['old_price']}->{ev['new_price']} 采集 "
                    f"{c['old_price']}->{c['new_price']}")
        else:  # delist
            if c["new_price"] is not None or \
                    round(c["old_price"], 2) != round(ev["old_price"], 2):
                rep.event_diffs.append(
                    f"撤网记录不符: {ev['item_code']} {c}")
    for key in change_idx:  # 仿真侧不存在的幽灵变更
        rep.event_diffs.append(f"幽灵变更（真值无此事件）: {key}")
    return rep


def reconcile_normalization(db) -> list[str]:
    """B. 归一正确性：标准品种 4 来源、难解析条目 pending。"""
    diffs: list[str] = []
    mappings = db.list_mappings()
    auto = [m for m in mappings if m["status"] in ("auto", "confirmed")]
    pending = [m for m in mappings if m["status"] == "pending"]

    # 每个 drug_id 应恰好汇聚 4 个平台各 1 条
    by_drug: dict[int, list[dict]] = {}
    for m in auto:
        by_drug.setdefault(m["drug_id"], []).append(m)
    if len(by_drug) != 12:
        diffs.append(f"标准品种数={len(by_drug)}，期望 12")
    for drug_id, ms in by_drug.items():
        platforms = {m["platform_code"] for m in ms}
        if len(ms) != 4 or platforms != {"P01", "P02", "P03", "P04"}:
            diffs.append(
                f"drug_id={drug_id} 来源={sorted(platforms)}（应四省各 1 条）")

    if len(pending) != 4:
        diffs.append(f"待人工确认条目={len(pending)}，期望 4")
    for m in pending:
        if not m["item_code"].endswith("H01"):
            diffs.append(f"非预期条目进入人工确认: {m['item_code']}")
    return diffs


def run_reconcile(configs: list[dict], db, check_events: bool = True):
    """check_events=False 时只对当前字段与归一化（用于事件发生前的全量阶段）。"""
    reports = [reconcile_platform(cfg, db, check_events=check_events)
               for cfg in configs]
    norm_diffs = reconcile_normalization(db)
    return reports, norm_diffs


def print_report(reports: list[ReconReport], norm_diffs: list[str]) -> int:
    total = 0
    for r in reports:
        total += r.diff_count
        print(f"[{r.platform}] 真值条目={r.items_truth} 采集条目="
              f"{r.items_collected} 差异={r.diff_count}")
        for d in r.field_diffs + r.event_diffs:
            print("   -", d)
    print(f"[归一化] 差异={len(norm_diffs)}")
    for d in norm_diffs:
        print("   -", d)
    total += len(norm_diffs)
    print("=" * 50)
    print(f"对账总差异: {total}")
    print("结果:", "通过 ✅ 差异为 0" if total == 0 else "失败 ❌")
    return 0 if total == 0 else 1
