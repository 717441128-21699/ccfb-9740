"""平台响应解析：HTML v1/v2 与 JSON v1 两种已知结构，v3/未知结构抛 ParseError。

解析器只做结构识别与字段提取，不做任何“猜默认值”：
拿不到必填字段（编码/通用名/剂型/规格/厂家/状态）或在售价格非法时直接抛错，
由引擎记 parse 告警并跳过，绝不写入空数据。
"""
from __future__ import annotations

import json as _json


class ParseError(Exception):
    def __init__(self, message: str, excerpt: str = ""):
        super().__init__(message)
        self.excerpt = excerpt[:500]


REQUIRED = ("item_code", "generic_name", "dosage_form", "spec_text",
            "manufacturer", "status")


def _validate(item: dict) -> dict:
    for k in REQUIRED:
        v = item.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ParseError(f"必填字段缺失或为空: {k}", str(item))
    if item["status"] == "listed":
        price = item.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            raise ParseError("在售挂网价格非法（必须为正数）", str(item))
    return item


# ---------------- JSON ----------------
def parse_json_list(payload: str | dict) -> tuple[list[dict], int, int]:
    """返回 (条目摘要列表, 当前页, 总页数)。摘要只含列表页可见字段。"""
    data = _json.loads(payload) if isinstance(payload, str) else payload
    if isinstance(data, dict) and "records" in data:  # v1
        rows, total, page, size = (data["records"], data["total"],
                                   data["page"], data["pageSize"])
        pages = (total + size - 1) // size
        return [_json_summary_v1(r) for r in rows], page, pages
    if isinstance(data, dict) and "data" in data and "pager" in data["data"]:
        raise ParseError("接口结构已改版（v3 未知结构），无法解析列表",
                         str(data)[:300])
    raise ParseError("无法识别的 JSON 列表结构", str(data)[:300])


def _json_summary_v1(r: dict) -> dict:
    return {"item_code": r.get("itemCode"), "generic_name": r.get("genericName"),
            "status": r.get("status"), "source_updated_at": r.get("updatedAt"),
            "version": r.get("version"), "price": r.get("price")}


def parse_json_detail(payload: str | dict) -> dict:
    data = _json.loads(payload) if isinstance(payload, str) else payload
    if "itemCode" in data:  # v1
        return _validate({
            "item_code": data["itemCode"], "generic_name": data["genericName"],
            "dosage_form": data["dosageForm"], "spec_text": data["spec"],
            "manufacturer": data["manufacturer"], "price": data["price"],
            "status": data["status"], "source_updated_at": data["updatedAt"],
            "version": data["version"]})
    if "data" in data or "id" in data:
        raise ParseError("接口结构已改版（v3 未知结构），无法解析详情",
                         str(data)[:300])
    raise ParseError("无法识别的 JSON 详情结构", str(data)[:300])


# ---------------- HTML（基于 BeautifulSoup 风格的极简解析，避免新增依赖） ----------------
from html.parser import HTMLParser


class _TagParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.cls: list[str] = []
        self.cur_cls = ""
        self.buf: list[str] = []
        self.rows: list[dict] = []
        self._row: dict | None = None
        self._col = None
        self.links: list[dict] = []
        self._a = None
        self.attrs_by_tag: list[dict] = []
        self.page_info: dict = {}
        self.detail: dict = {}
        self._detail_cls = None

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        self.stack.append(tag)
        self.attrs_by_tag.append(ad)
        classes = (ad.get("class") or "").split()
        self.cur_cls = " ".join(classes)
        self.cls.append(self.cur_cls)
        if tag == "tr" and "drug-row" in classes:
            self._row = {"item_code": ad.get("data-code"), "_cols": {}}
        if tag == "td" and self._row is not None:
            self._col = ad.get("class", "")
            self.buf = []
        if tag == "span" and "page-info" in classes:
            self.page_info = {"page": int(ad.get("data-page", 1)),
                              "pages": int(ad.get("data-pages", 1))}
        if tag == "span" and ad.get("id") == "page-info":
            self.page_info = {"page": int(ad.get("data-page", 1)),
                              "pages": int(ad.get("data-pages", 1))}
        if tag == "div" and ad.get("id") == "catalog":
            self.page_info = {"page": int(ad.get("data-page", 1)),
                              "pages": int(ad.get("data-pages", 1))}
        if tag == "div" and "item-card" in classes:
            self._row = {"item_code": None, "_cols": {}}
        if tag in ("span", "p", "div") and self._row is not None and classes:
            self._col = " ".join(classes)
            self.buf = []
        if tag == "a" and "go-detail" in classes:
            self._row = self._row or {"item_code": None, "_cols": {}}
            self._row["item_code"] = ad.get("data-code")
        if tag in ("div", "article") and (
                "f-" in (ad.get("class") or "") or
                "drug-info" in (ad.get("class") or "") or
                ad.get("id") == "detail"):
            if ad.get("data-code") or ad.get("data-id"):
                self.detail["item_code"] = ad.get("data-code") or ad.get("data-id")
        if tag in ("div", "p") and any(
                c.startswith(("f-", "generic-name", "dose-form", "pack-spec",
                              "vendor", "sale-price", "listing-state", "ts",
                              "rev")) for c in classes):
            self._detail_cls = " ".join(classes)
            self.buf = []
        if tag in ("div", "section") and ("nova-grid" in classes or
                                          "neo-panel" in classes):
            raise ParseError("页面已改版（v3 未知结构），无法解析", "")

    def handle_data(self, data):
        t = data.strip()
        if not t:
            return
        if self._row is not None and self._col:
            self._row["_cols"].setdefault(self._col, []).append(t)
        if self._detail_cls:
            self.buf.append(t)

    def handle_endtag(self, tag):
        if tag == "td" and self._row is not None and self._col:
            self._col = None
            self.buf = []
        if tag in ("span", "p", "div") and self._row is not None and self._col \
                and self.buf:
            self._col = None
            self.buf = []
        if tag in ("div", "p") and self._detail_cls and self.buf:
            text = " ".join(self.buf).strip()
            self._put_detail(self._detail_cls, text)
            self._detail_cls = None
            self.buf = []
        if tag == "tr" and self._row is not None and self._row.get("item_code"):
            self.rows.append(self._row)
            self._row = None
        if tag == "div" and self._row is not None and self._row.get("item_code") \
                and self._row["_cols"]:
            self.rows.append(self._row)
            self._row = None
        if self.stack:
            self.stack.pop()
            self.cls.pop()
            self.cur_cls = self.cls[-1] if self.cls else ""

    def _put_detail(self, cls: str, text: str) -> None:
        m = {"f-generic": "generic_name", "generic-name": "generic_name",
             "f-form": "dosage_form", "dose-form": "dosage_form",
             "f-spec": "spec_text", "pack-spec": "spec_text",
             "f-maker": "manufacturer", "vendor": "manufacturer",
             "f-status": "status", "listing-state": "status",
             "f-updated": "source_updated_at", "ts": "source_updated_at"}
        if cls in m:
            self.detail[m[cls]] = text
        elif cls in ("f-price", "sale-price"):
            self.detail["price"] = float(text.replace("¥", "").strip())
        elif cls in ("f-version", "rev"):
            self.detail["version"] = int(text)


def parse_html_list(payload: str) -> tuple[list[dict], int, int]:
    p = _TagParser()
    try:
        p.feed(payload)
    except ParseError:
        raise
    if not p.rows and "nova-grid" in payload:
        raise ParseError("页面已改版（v3 未知结构），无法解析列表",
                         payload[:300])
    if not p.rows:
        raise ParseError("列表未解析到任何数据行（页面结构可能已变更）",
                         payload[:300])
    out: list[dict] = []
    for r in p.rows:
        cols = r["_cols"]
        def first(*names):
            for n in names:
                if n in cols and cols[n]:
                    return cols[n][0]
            return None
        if "col-code" in cols:  # v1 表格
            out.append({
                "item_code": r["item_code"],
                "generic_name": first("col-name"),
                "status": first("col-status"),
                "source_updated_at": first("col-updated"),
                "price": _safe_float(first("col-price")),
                "version": None})
        else:  # v2 卡片
            out.append({
                "item_code": r["item_code"],
                "generic_name": first("gn"),
                "status": first("st"),
                "source_updated_at": first("ut"),
                "price": _safe_float(first("pr")),
                "version": None})
    if any(o["item_code"] is None for o in out):
        raise ParseError("列表行缺少药品编码", payload[:300])
    page = p.page_info.get("page", 1)
    pages = p.page_info.get("pages", 1)
    return out, page, pages


def _safe_float(v):
    try:
        return float(str(v).replace("¥", "").strip())
    except (TypeError, ValueError):
        return None


def parse_html_detail(payload: str) -> dict:
    p = _TagParser()
    try:
        p.feed(payload)
    except ParseError:
        raise
    d = p.detail
    if "neo-panel" in payload or not d:
        raise ParseError("详情页已改版（v3 未知结构），无法解析",
                         payload[:300])
    return _validate(d)
