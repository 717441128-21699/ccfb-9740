"""仿真挂网平台数据生成器。

全部数据由固定 seed 确定性生成，不访问任何真实网站。
每个平台 12 个标准品种（同一批 canonical 药物，各省规格写法不同）
+ 1 个规格无法可靠解析的“人工确认”品种。
事件（调价/撤网）按 tick 预先编排，/admin/tick 逐拍推进，时间戳确定性递增。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, asdict

BASE_EPOCH = "2026-11-01T08:00:00"


@dataclass
class CanonDrug:
    id: str
    generic: str          # 通用名（标准）
    form: str             # 剂型（标准）
    strength_mg: float | None = None
    strength_pct: float | None = None
    volume_ml: float | None = None
    count: int | None = None
    unit: str | None = None          # 片/粒/袋/支/瓶
    packing_note: str | None = None  # 板装等备注
    manufacturer: str = ""
    base_price: float = 0.0

    def canonical_spec(self) -> str:
        if self.strength_pct is not None:
            s = f"{self.strength_pct:g}%*{self.volume_ml:g}ml"
            if self.count:
                s += f"*{self.count}{self.unit}"
            return s
        if self.volume_ml and self.strength_mg is not None:
            s = f"{self.strength_mg:g}mg/{self.volume_ml:g}ml"
            if self.count:
                s += f"*{self.count}{self.unit}"
            return s
        s = f"{self.strength_mg:g}mg"
        if self.count:
            s += f"*{self.count}{self.unit}"
        if self.packing_note:
            s += f"({self.packing_note})"
        return s


# 12 个标准品种（四省共有，规格写法各异但指向同一药物）
CANON_DRUGS: list[CanonDrug] = [
    CanonDrug("D01", "阿莫西林胶囊", "胶囊剂", strength_mg=250, count=24, unit="粒",
              manufacturer="石药集团欧意药业有限公司", base_price=12.80),
    CanonDrug("D02", "头孢克肟片", "片剂", strength_mg=100, count=12, unit="片",
              packing_note="板装", manufacturer="广州白云山医药集团股份有限公司", base_price=28.50),
    CanonDrug("D03", "布洛芬片", "片剂", strength_mg=200, count=24, unit="片",
              manufacturer="中美天津史克制药有限公司", base_price=15.00),
    CanonDrug("D04", "奥美拉唑肠溶胶囊", "胶囊剂", strength_mg=20, count=14, unit="粒",
              packing_note="板装", manufacturer="阿斯利康制药有限公司", base_price=42.00),
    CanonDrug("D05", "氯化钠注射液", "注射液", strength_pct=0.9, volume_ml=250, count=1, unit="瓶",
              manufacturer="四川科伦药业股份有限公司", base_price=3.60),
    CanonDrug("D06", "葡萄糖注射液", "注射液", strength_pct=5.0, volume_ml=500, count=1, unit="瓶",
              manufacturer="华润双鹤药业股份有限公司", base_price=4.20),
    CanonDrug("D07", "注射用头孢曲松钠", "注射剂", strength_mg=1000, count=1, unit="瓶",
              manufacturer="齐鲁制药有限公司", base_price=18.90),
    CanonDrug("D08", "阿托伐他汀钙片", "片剂", strength_mg=20, count=7, unit="片",
              manufacturer="辉瑞制药有限公司", base_price=55.00),
    CanonDrug("D09", "盐酸二甲双胍片", "片剂", strength_mg=500, count=60, unit="片",
              packing_note="板装", manufacturer="北京万辉双鹤药业有限责任公司", base_price=22.30),
    CanonDrug("D10", "阿奇霉素分散片", "片剂", strength_mg=250, count=6, unit="片",
              manufacturer="哈药集团三精制药有限公司", base_price=16.70),
    CanonDrug("D11", "维生素C注射液", "注射液", strength_mg=1000, volume_ml=5, count=5, unit="支",
              manufacturer="石药银湖制药有限公司", base_price=8.80),
    CanonDrug("D12", "板蓝根颗粒", "颗粒剂", strength_mg=10000, count=20, unit="袋",
              manufacturer="广州白云山和记黄埔中药有限公司", base_price=19.60),
]

# 每省一个规格无法可靠解析的品种（应进入人工确认，不自动归一）
HARD_ITEMS = {
    "P01": ("H01", "去痛片", "片剂", "盒装", "东北制药集团股份有限公司", 9.90),
    "P02": ("H02", "复合维生素B片", "片剂", "100片/瓶", "东北制药集团沈阳第一制药有限公司", 6.60),
    "P03": ("H03", "健胃消食片", "片剂", "家庭装", "江中药业股份有限公司", 7.70),
    "P04": ("H04", "藿香正气水", "酊剂", "10支装", "太极集团重庆涪陵制药厂有限公司", 11.10),
}

# 各省通用名/剂型/厂家的不同写法
GENERIC_ALIAS = {
    "P01": {},  # 标准写法
    "P02": {"阿莫西林胶囊": "阿莫西林(胶囊)", "布洛芬片": "布洛芬(薄膜衣片)",
            "氯化钠注射液": "0.9%氯化钠注射液"},
    "P03": {"头孢克肟片": "头孢克肟 片剂", "阿托伐他汀钙片": "阿托伐他汀钙(片)",
            "板蓝根颗粒": "板蓝根(颗粒剂)"},
    "P04": {"奥美拉唑肠溶胶囊": "奥美拉唑 肠溶胶囊", "阿奇霉素分散片": "阿奇霉素(分散片)",
            "盐酸二甲双胍片": "二甲双胍 片"},
}
FORM_ALIAS = {
    "P01": {},
    "P02": {"胶囊剂": "硬胶囊", "颗粒剂": "颗粒"},
    "P03": {"片剂": "口服片", "胶囊剂": "胶囊", "注射剂": "粉针剂"},
    "P04": {"片剂": "素片", "胶囊剂": "胶囊制剂", "注射液": "大容量注射剂",
            "颗粒剂": "冲剂"},
}
MAKER_ALIAS = {
    "P01": {},
    "P02": {"石药集团欧意药业有限公司": "石药欧意药业",
            "广州白云山医药集团股份有限公司": "白云山制药总厂",
            "齐鲁制药有限公司": "齐鲁制药厂"},
    "P03": {"中美天津史克制药有限公司": "天津史克",
            "阿斯利康制药有限公司": "阿斯利康(无锡)",
            "辉瑞制药有限公司": "辉瑞大连工厂"},
    "P04": {"四川科伦药业股份有限公司": "科伦药业",
            "华润双鹤药业股份有限公司": "双鹤药业",
            "哈药集团三精制药有限公司": "哈药三精",
            "石药银湖制药有限公司": "石药银湖"},
}


def _g(d: CanonDrug) -> float:
    return d.strength_mg / 1000.0


def _boards(count: int) -> int:
    """统一板数规则，保证各省同品种解析出的总数量一致：
    6 的倍数用 3 板，其余偶数用 2 板。"""
    if count % 6 == 0:
        return 3
    return 2


def render_spec(p: str, d: CanonDrug) -> str:
    """按各省习惯渲染规格文本，四种风格。"""
    if p == "P01":  # 克重 + 板装，如 0.25g*24片 / 250mg×2板×12片
        if d.strength_pct is not None:
            base = f"{d.strength_pct:g}%*{d.volume_ml:g}ml*{d.count}{d.unit}"
        elif d.volume_ml:
            base = f"{_g(d):g}g/{d.volume_ml:g}ml*{d.count}{d.unit}"
        elif d.packing_note == "板装":
            boards = _boards(d.count)
            per = d.count // boards
            base = f"{d.strength_mg:g}mg×{boards}板×{per}{d.unit}"
        else:
            base = f"{_g(d):g}g*{d.count}{d.unit}"
        return base
    if p == "P02":  # 毫克 + 板，如 250mg*24粒 / 500mg*3板*20片
        if d.strength_pct is not None:
            return f"{d.strength_pct:g}%*{d.volume_ml:g}ml/{d.unit}"
        if d.volume_ml:
            return f"{d.strength_mg:g}mg/{d.volume_ml:g}ml*{d.count}{d.unit}"
        if d.packing_note == "板装":
            boards = _boards(d.count)
            per = d.count // boards
            return f"{d.strength_mg:g}mg*{boards}板*{per}{d.unit}"
        return f"{d.strength_mg:g}mg*{d.count}{d.unit}"
    if p == "P03":  # 包装在前，如 24粒/盒(250mg) / 2板×12片/盒 100mg
        if d.strength_pct is not None:
            return f"{d.volume_ml:g}ml/{d.unit}({d.strength_pct:g}%)"
        if d.volume_ml:
            return f"{d.count}{d.unit}/盒({d.strength_mg:g}mg/{d.volume_ml:g}ml)"
        if d.packing_note == "板装":
            boards = _boards(d.count)
            per = d.count // boards
            return f"{boards}板×{per}{d.unit}/盒 {d.strength_mg:g}mg"
        return f"{d.count}{d.unit}/盒({d.strength_mg:g}mg)"
    # P04：双单位括号，如 0.25g(250mg)*24片 / 500mg(0.5g)*3板*20片
    if d.strength_pct is not None:
        return f"{d.strength_pct:g}% {d.volume_ml:g}ml*{d.count}{d.unit}"
    if d.volume_ml:
        return f"{d.strength_mg:g}mg:{d.volume_ml:g}ml*{d.count}{d.unit}"
    head = f"{_g(d):g}g({d.strength_mg:g}mg)"
    if d.packing_note == "板装":
        boards = _boards(d.count)
        per = d.count // boards
        return f"{head}*{boards}板*{per}{d.unit}"
    return f"{head}*{d.count}{d.unit}"


def _shift_ts(ts: str, hours: int) -> str:
    import datetime
    dt = datetime.datetime.fromisoformat(ts) + datetime.timedelta(hours=hours)
    return dt.isoformat()


def build_platform_state(p: str) -> dict:
    """生成某省平台初始状态与预置事件。"""
    items: list[dict] = []
    for idx, d in enumerate(CANON_DRUGS):
        code = f"{p}-{idx + 1:03d}"
        generic = GENERIC_ALIAS[p].get(d.generic, d.generic)
        form = FORM_ALIAS[p].get(d.form, d.form)
        maker = MAKER_ALIAS[p].get(d.manufacturer, d.manufacturer)
        items.append({
            "item_code": code,
            "canonical_id": d.id,
            "generic_name": generic,
            "dosage_form": form,
            "spec_text": render_spec(p, d),
            "manufacturer": maker,
            "price": round(d.base_price * (1 + (idx % 5) * 0.01), 2),
            "status": "listed",
            "updated_at": BASE_EPOCH,
            "version": 1,
        })
    h = HARD_ITEMS[p]
    items.append({
        "item_code": f"{p}-H01",
        "canonical_id": None,
        "generic_name": h[1],
        "dosage_form": h[2],
        "spec_text": h[3],
        "manufacturer": h[4],
        "price": h[5],
        "status": "listed",
        "updated_at": BASE_EPOCH,
        "version": 1,
    })

    # 预置 4 拍事件：调价 6 次、撤网 2 次（每省）
    events: list[list[dict]] = [
        [{"type": "price", "canonical_id": "D01", "factor": 1.08},
         {"type": "price", "canonical_id": "D08", "factor": 0.93}],
        [{"type": "price", "canonical_id": "D03", "factor": 1.12},
         {"type": "delist", "canonical_id": "D05"}],
        [{"type": "price", "canonical_id": "D07", "factor": 0.88},
         {"type": "price", "canonical_id": "D11", "factor": 1.05},
         {"type": "delist", "canonical_id": "D10"}],
        [{"type": "price", "canonical_id": "D12", "factor": 0.9},
         {"type": "price", "canonical_id": "D04", "factor": 1.15}],
    ]
    return {"platform": p, "tick": 0, "items": items, "event_log": [],
            "events_schedule": events}


def apply_tick(state: dict) -> list[dict]:
    """推进一拍，返回本拍发生的事件（含变更后真值）。"""
    tick = state["tick"]
    schedule = state["events_schedule"]
    if tick >= len(schedule):
        return []
    occurred: list[dict] = []
    ts = _shift_ts(BASE_EPOCH, (tick + 1) * 24)
    for ev in schedule[tick]:
        target = next(i for i in state["items"]
                      if i["canonical_id"] == ev["canonical_id"])
        record = {"tick": tick + 1, "type": ev["type"],
                  "item_code": target["item_code"],
                  "canonical_id": ev["canonical_id"], "changed_at": ts}
        if ev["type"] == "price":
            old = target["price"]
            new = round(old * ev["factor"], 2)
            target["price"] = new
            target["updated_at"] = ts
            target["version"] += 1
            record.update({"old_price": old, "new_price": new,
                           "status": "listed"})
        else:
            target["status"] = "delisted"
            target["updated_at"] = ts
            target["version"] += 1
            record.update({"old_price": target["price"],
                           "new_price": None, "status": "delisted"})
        occurred.append(record)
        state["event_log"].append(copy.deepcopy(record))
    state["tick"] = tick + 1
    return occurred


if __name__ == "__main__":  # 人工查看生成结果
    for p in ("P01", "P02", "P03", "P04"):
        st = build_platform_state(p)
        print(p, json.dumps(st["items"][:2], ensure_ascii=False, indent=1))
