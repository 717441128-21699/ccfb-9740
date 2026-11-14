"""药品归一化：规格文本结构化解析 + 四要素（通用名/剂型/规格/厂家）标准键。

支持的规格写法示例（必须认成同一规格）：
  0.25g*24片          250mg×2板×12片        24片/盒(250mg)
  0.25g(250mg)*24片   500mg*3板*20片        0.9%*250ml*1瓶
  1000mg/5ml*5支      1g/5ml*5支            5% 500ml*1瓶
无法可靠解析（如“盒装”“家庭装”“100片/瓶”无明确含量）→ 低置信度，人工确认。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---- 剂型标准词 ----
FORM_CANON = {
    "硬胶囊": "胶囊剂", "胶囊": "胶囊剂", "胶囊制剂": "胶囊剂",
    "素片": "片剂", "口服片": "片剂",
    "冲剂": "颗粒剂", "颗粒": "颗粒剂",
    # 注意：注射液与注射剂（粉针/注射用）是两种剂型，不能互相归并
    "大容量注射剂": "注射液",
    "粉针剂": "注射剂",
}

# ---- 厂家归一：标准厂家表 + 核心字号包含匹配（别名/全称统一到同一标准名） ----
_MAKER_DROP = ["股份有限公司", "有限责任公司", "集团", "公司", "总厂", "药厂"]
_MAKER_STANDARD = [
    "石药集团欧意药业", "广州白云山医药集团", "齐鲁制药",
    "中美天津史克制药", "阿斯利康制药", "辉瑞制药",
    "四川科伦药业", "华润双鹤药业", "北京万辉双鹤药业",
    "哈药集团三精制药", "石药银湖制药", "广州白云山和记黄埔中药",
    "东北制药集团", "东北制药集团沈阳第一制药", "江中药业",
    "太极集团重庆涪陵制药",
]
_MAKER_ALIAS = {
    "石药欧意药业": "石药集团欧意药业",
    "白云山制药总厂": "广州白云山医药集团",
    "白云山制药": "广州白云山医药集团",
    "天津史克": "中美天津史克制药",
    "阿斯利康(无锡)": "阿斯利康制药",
    "阿斯利康": "阿斯利康制药",
    "辉瑞大连工厂": "辉瑞制药",
    "科伦药业": "四川科伦药业",
    "双鹤药业": "华润双鹤药业",
    "哈药三精": "哈药集团三精制药",
    "哈药三精制药": "哈药集团三精制药",
    "石药银湖": "石药银湖制药",
}

# ---- 通用名归一：去括号说明、去多余空格、去剂型后缀修饰 ----
_GENERIC_CANON = {
    "阿莫西林": "阿莫西林胶囊",  # 括号里的“胶囊”还原
    "二甲双胍片": "盐酸二甲双胍片",
}
# 括号/空格中的剂型修饰词 → 标准通用名应带的剂型后缀
# 分散片是独立的通用名词干，不能简化成普通“片”
_GENERIC_FORM_SUFFIX = [
    ("(分散片)", "分散片"), ("（分散片）", "分散片"),
    ("(胶囊)", "胶囊"), ("（胶囊）", "胶囊"),
    ("(薄膜衣片)", "片"), ("（薄膜衣片）", "片"),
    ("(片)", "片"), ("（片）", "片"),
    ("(颗粒剂)", "颗粒"), ("（颗粒剂）", "颗粒"),
    ("片剂", "片"), ("肠溶胶囊", "肠溶胶囊"),
]


def normalize_generic(generic: str) -> str:
    g = generic.strip()
    g = re.sub(r"\s+", "", g)
    suffix = ""
    for mark, suf in _GENERIC_FORM_SUFFIX:
        if mark in g:
            g = g.replace(mark, "")
            suffix = suf
    # “0.9%氯化钠注射液” → “氯化钠注射液”
    g = re.sub(r"^[0-9.]+%", "", g)
    if suffix and not g.endswith(suffix):
        g = g + suffix
    return _GENERIC_CANON.get(g, g)


@dataclass
class ParsedSpec:
    confidence: float
    reason: str
    strength_mg: float | None = None
    strength_pct: float | None = None
    volume_ml: float | None = None
    count: int | None = None
    unit: str | None = None
    packing_note: str | None = None
    canonical: str = ""


_UNIT_CANON = {"片": "片", "粒": "粒", "袋": "袋", "支": "支", "瓶": "瓶",
               "丸": "丸", "贴": "贴"}
_NUM = r"(\d+(?:\.\d+)?)"


def _to_mg(num: float, unit: str) -> float:
    return num * 1000.0 if unit == "g" else num


def _find_strengths(text: str) -> tuple[float | None, float | None, float | None]:
    """返回 (strength_mg, strength_pct, volume_ml)。"""
    pct = None
    m = re.search(rf"{_NUM}\s*%", text)
    if m:
        pct = float(m.group(1))
    vol = None
    mv = re.search(rf"{_NUM}\s*ml", text, re.I)
    if mv:
        vol = float(mv.group(1))
    mg = None
    # 形如 0.25g(250mg)：取括号内 mg；否则取第一个 g/mg
    mpar = re.search(r"[（(]\s*" + _NUM + r"\s*mg\s*[）)]", text, re.I)
    if mpar:
        mg = float(mpar.group(1))
    else:
        m1 = re.search(_NUM + r"\s*(mg|g)(?![a-z])", text, re.I)
        if m1:
            mg = _to_mg(float(m1.group(1)), m1.group(2).lower())
        else:
            # 1g/5ml、1000mg:5ml 这类斜杠/冒号写法
            m2 = re.search(_NUM + r"\s*(mg|g)\s*[/：:]\s*" + _NUM + r"\s*ml",
                           text, re.I)
            if m2:
                mg = _to_mg(float(m2.group(1)), m2.group(2).lower())
                if vol is None:
                    vol = float(m2.group(3))
    return mg, pct, vol


def parse_spec(raw: str, dosage_form: str) -> ParsedSpec:
    text = raw.strip().replace("×", "*").replace("X", "*").replace("x", "*")
    text = re.sub(r"\s+", "", text)
    mg, pct, vol = _find_strengths(text)

    # 计数：*24片 / *2板*12片 / *3板*20片 / 24片/盒 / 2板×12片/盒
    count = None
    packing = None
    board_matches = re.findall(rf"(\d+)\s*板\s*\*\s*{_NUM}\s*([片粒袋支瓶])", text)
    if board_matches:
        count = sum(int(b) * int(n) for b, n, _ in board_matches)
        packing = "板装"
        unit = _UNIT_CANON.get(board_matches[0][2])
    else:
        mcnt = re.search(r"\*" + _NUM + r"\s*([片粒袋支瓶丸贴])", text)
        if not mcnt:
            mcnt = re.search(r"^" + _NUM + r"\s*([片粒袋支瓶丸贴])\s*/", text)
        if mcnt:
            count = int(float(mcnt.group(1)))
            unit_raw = mcnt.group(2)
        else:
            # 100片/瓶 这类有数量但无含量
            m_any = re.search(_NUM + r"\s*([片粒袋支瓶丸贴])\s*/", text)
            if m_any and mg is None and pct is None:
                return ParsedSpec(
                    confidence=0.3,
                    reason="仅识别到包装数量，缺少有效成分含量，无法可靠归一",
                    unit=_UNIT_CANON.get(m_any.group(2)))
    if count is not None:
        unit = _UNIT_CANON.get(mcnt.group(2)) if "mcnt" in locals() else None
    else:
        unit = None
    # 斜杠/冒号体系里单独出现的单位
    if unit is None:
        mu = re.search(r"[*/]\s*(\d+)\s*([片粒袋支瓶丸贴])", text)
        if mu:
            count = int(mu.group(1))
            unit = _UNIT_CANON.get(mu.group(2))
    # 单件包装：250ml/瓶、250ml/瓶(0.9%) 这类没有显式数量，按 1 计
    if count is None:
        ms = re.search(r"/\s*([片粒袋支瓶丸贴])(?:[（(]|$)", text)
        if ms:
            count = 1
            unit = _UNIT_CANON.get(ms.group(1))

    if pct is not None:
        if vol is None:
            return ParsedSpec(0.35, "百分比规格缺少装量体积，无法可靠归一")
        if count is None:
            return ParsedSpec(0.4, "百分比规格缺少包装数量，信息不完整")
        canon = f"{pct:g}%*{vol:g}ml*{count}{unit}"
        return ParsedSpec(0.98, "百分比大容量制剂，解析完整",
                          strength_pct=pct, volume_ml=vol, count=count,
                          unit=unit, canonical=canon)

    if mg is None:
        return ParsedSpec(0.25, "未识别到有效成分含量（mg/g），需人工确认")

    if vol is not None:
        if count is None:
            return ParsedSpec(0.45, "注射液缺少包装数量，信息不完整")
        canon = f"{mg:g}mg/{vol:g}ml*{count}{unit}"
        return ParsedSpec(0.97, "含量+装量+包装完整解析",
                          strength_mg=mg, volume_ml=vol, count=count,
                          unit=unit, canonical=canon)

    if count is None:
        return ParsedSpec(0.4, "识别到含量但缺少包装数量，信息不完整",
                          strength_mg=mg)
    canon = f"{mg:g}mg*{count}{unit}"
    if packing:
        canon += f"({packing})"
    return ParsedSpec(0.99 if packing else 0.95,
                      "固体制剂规格完整解析",
                      strength_mg=mg, count=count, unit=unit,
                      packing_note=packing, canonical=canon)


def normalize_form(form: str) -> str:
    f = form.strip()
    return FORM_CANON.get(f, f)


def normalize_manufacturer(maker: str) -> str:
    key = maker.strip().replace("（", "(").replace("）", ")")
    for drop in _MAKER_DROP:
        key = key.replace(drop, "")
    key = re.sub(r"(有限)+$", "", key)  # “...有限公司”剥离公司后残留的“有限”
    if key in _MAKER_ALIAS:
        return _MAKER_ALIAS[key]
    # 标准表前缀/包含匹配：处理“齐鲁制药厂”“广州白云山医药”等截断写法
    # 长字号优先，避免“东北制药集团”误伤“东北制药集团沈阳第一制药”
    for std in sorted(_MAKER_STANDARD, key=len, reverse=True):
        if key in std or std in key:
            return std
    return key


def normalize_item(generic: str, form: str, spec_text: str,
                   manufacturer: str) -> dict:
    """四要素归一，返回标准键 + 置信度 + 原因。"""
    parsed = parse_spec(spec_text, form)
    ng = normalize_generic(generic)
    nf = normalize_form(form)
    nm = normalize_manufacturer(manufacturer)
    confident = parsed.confidence >= 0.9
    reason = parsed.reason
    if not confident:
        reason = f"规格解析置信度低：{parsed.reason}"
    return {
        "generic_name": ng,
        "dosage_form": nf,
        "spec_canonical": parsed.canonical or f"UNRESOLVED::{spec_text}",
        "manufacturer": nm,
        "confidence": parsed.confidence,
        "reason": reason,
        "parsed": parsed,
    }
