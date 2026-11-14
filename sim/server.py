"""本地仿真省级挂网平台（Flask），仅监听 127.0.0.1，绝不访问真实网站。

四种模式：
  html_login  (P01): 表单登录 + HTML 列表/详情
  api_open    (P02): 免登录 JSON 分页接口
  mixed_login (P03): API 登录 + JSON 列表 + HTML 详情
  html_open   (P04): 免登录 HTML 列表/详情

公共能力：令牌桶限流(429+Retry-After)、会话过期、布局改版开关、
/admin/tick 推进调价/撤网事件、/admin/state 导出真值。
"""
from __future__ import annotations

import html
import secrets
import threading
import time
from collections import deque
from functools import wraps

from flask import Flask, jsonify, redirect, request, url_for

from sim.generator import apply_tick, build_platform_state

ADMIN_KEY = "sim-admin-key"
SESSION_TTL_S = 600


def create_app(platform_cfg: dict) -> Flask:
    p = platform_cfg["code"]
    mode = platform_cfg["mode"]
    page_size = platform_cfg["page_size"]
    app = Flask(__name__)
    state = build_platform_state(p)
    lock = threading.RLock()
    sessions: dict[str, float] = {}
    # 限流：滑动窗口
    hits: deque[float] = deque()
    rw, rmax = platform_cfg["rate_window_s"], platform_cfg["rate_max"]
    layout = {"html": "v1", "api": "v1"}

    def rate_limited() -> bool:
        path = request.path
        if path.startswith("/admin") or path == "/login" or path == "/api/login":
            return False
        now = time.monotonic()
        hits.append(now)
        while hits and now - hits[0] > rw:
            hits.popleft()
        return len(hits) > rmax

    app.before_request(lambda: (_ for _ in ()).throw(
        _RateLimited()) if rate_limited() else None)

    def check_session() -> bool:
        token = request.cookies.get("sim_session") or \
            request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if token and sessions.get(token, 0) > time.time():
            return True
        return False

    def auth_required(fn):
        @wraps(fn)
        def w(*a, **kw):
            if not check_session():
                if mode == "mixed_login":
                    return jsonify({"error": "unauthorized"}), 401
                return redirect(url_for("login_page"))
            return fn(*a, **kw)
        return w

    # ---------------- 登录 ----------------
    @app.get("/login")
    def login_page():
        return (
            "<html><body><h1>%s 登录</h1>"
            "<form method='post' action='/login'>"
            "<input name='username'><input name='password' type='password'>"
            "<button>登录</button></form></body></html>" % html.escape(p)
        )

    @app.post("/login")
    def login_form():
        a = platform_cfg["auth"]
        if request.form.get("username") == a["username"] and \
                request.form.get("password") == a["password"]:
            token = secrets.token_hex(16)
            sessions[token] = time.time() + SESSION_TTL_S
            resp = redirect("/list")
            resp.set_cookie("sim_session", token)
            return resp
        return "bad credentials", 401

    @app.post("/api/login")
    def login_api():
        a = platform_cfg["auth"]
        data = request.get_json(silent=True) or {}
        if data.get("username") == a["username"] and \
                data.get("password") == a["password"]:
            token = secrets.token_hex(16)
            sessions[token] = time.time() + SESSION_TTL_S
            return jsonify({"access_token": token, "expires_in": SESSION_TTL_S})
        return jsonify({"error": "bad credentials"}), 401

    # ---------------- 数据访问 ----------------
    def page_items(page: int):
        with lock:
            total = len(state["items"])
            start = (page - 1) * page_size
            return state["items"][start:start + page_size], total

    def one_item(code: str):
        with lock:
            return next((i for i in state["items"] if i["item_code"] == code), None)

    def to_api_v1(it: dict) -> dict:
        return {"itemCode": it["item_code"], "genericName": it["generic_name"],
                "dosageForm": it["dosage_form"], "spec": it["spec_text"],
                "manufacturer": it["manufacturer"], "price": it["price"],
                "status": it["status"], "updatedAt": it["updated_at"],
                "version": it["version"]}

    def to_api_v3(it: dict) -> dict:  # 改版后未知结构
        return {"id": it["item_code"], "name": it["generic_name"],
                "form": it["dosage_form"], "spec_text": it["spec_text"],
                "maker": it["manufacturer"],
                "price": {"amount": it["price"]},
                "state": {"code": it["status"]},
                "update_time": it["updated_at"]}

    def api_list():
        page = max(1, request.args.get("page", 1, type=int))
        rows, total = page_items(page)
        if layout["api"] == "v3":
            return jsonify({"data": {"list": [to_api_v3(i) for i in rows],
                                     "pager": {"total": total, "size": page_size,
                                               "index": page}}})
        return jsonify({"records": [to_api_v1(i) for i in rows], "total": total,
                        "page": page, "pageSize": page_size})

    def api_detail(code: str):
        it = one_item(code)
        if it is None:
            return jsonify({"error": "not found"}), 404
        if layout["api"] == "v3":
            return jsonify({"data": to_api_v3(it)})
        return jsonify(to_api_v1(it))

    if mode == "api_open":
        app.add_url_rule("/api/list", view_func=api_list)
        app.add_url_rule("/api/detail", view_func=
                         lambda: api_detail(request.args.get("code", "")))

    if mode == "mixed_login":
        @app.get("/api/items")
        @auth_required
        def mixed_list():
            return api_list()

        @app.get("/item")
        @auth_required
        def mixed_detail():
            return html_detail(request.args.get("code", ""))

    # ---------------- HTML 渲染 ----------------
    def html_list_v1(rows, total, page, list_path):
        tr = "".join(
            "<tr class='drug-row' data-code='%s'>"
            "<td class='col-code'>%s</td><td class='col-name'>%s</td>"
            "<td class='col-price'>%s</td><td class='col-status'>%s</td>"
            "<td class='col-updated'>%s</td>"
            "<td><a class='detail-link' href='%s?code=%s'>详情</a></td></tr>"
            % (html.escape(i["item_code"]), html.escape(i["item_code"]),
               html.escape(i["generic_name"]), f'{i["price"]:.2f}',
               html.escape(i["status"]), html.escape(i["updated_at"]),
               list_path and detail_path_of(), html.escape(i["item_code"]))
            for i in rows)
        pages = (total + page_size - 1) // page_size
        return ("<html><body><table id='drug-table'><thead><tr>"
                "<th>编码</th><th>通用名</th><th>价格</th><th>状态</th>"
                f"<th>更新时间</th><th>操作</th></tr></thead><tbody>{tr}</tbody></table>"
                f"<span id='page-info' data-page='{page}' data-pages='{pages}'>"
                f"{page}/{pages}</span></body></html>")

    def detail_path_of() -> str:
        return "/detail" if mode == "html_login" else "/drug"

    def html_list_v2(rows, total, page):
        cards = "".join(
            "<div class='item-card'><span class='pk'>%s</span>"
            "<span class='gn'>%s</span><span class='pr'>¥%s</span>"
            "<span class='st'>%s</span><span class='ut'>%s</span>"
            "<a class='go-detail' data-code='%s'>查看</a></div>"
            % (html.escape(i["item_code"]), html.escape(i["generic_name"]),
               f'{i["price"]:.2f}', html.escape(i["status"]),
               html.escape(i["updated_at"]), html.escape(i["item_code"]))
            for i in rows)
        pages = (total + page_size - 1) // page_size
        return ("<html><body><div id='catalog' data-total='%d' data-page='%d' "
                "data-pages='%d'>%s</div></body></html>"
                % (total, page, pages, cards))

    def html_list_v3(rows, total, page):  # 改版后：解析器不认识的结构
        return ("<html><body><section class='nova-grid'><table><thead><tr>"
                "<th>SKU</th></tr></thead><tbody>" +
                "".join(f"<tr data-sku='{html.escape(i['item_code'])}'><td>"
                        f"{html.escape(i['generic_name'])}</td></tr>" for i in rows)
                + "</tbody></table></section></body></html>")

    def html_detail_v1(it):
        return ("<html><body><div id='detail' data-code='%s'>"
                "<div class='f-generic'>%s</div><div class='f-form'>%s</div>"
                "<div class='f-spec'>%s</div><div class='f-maker'>%s</div>"
                "<div class='f-price'>%s</div><div class='f-status'>%s</div>"
                "<div class='f-updated'>%s</div><div class='f-version'>%s</div>"
                "</div></body></html>"
                % (html.escape(it["item_code"]), html.escape(it["generic_name"]),
                   html.escape(it["dosage_form"]), html.escape(it["spec_text"]),
                   html.escape(it["manufacturer"]), f'{it["price"]:.2f}',
                   html.escape(it["status"]), html.escape(it["updated_at"]),
                   it["version"]))

    def html_detail_v2(it):
        return ("<html><body><article class='drug-info' data-id='%s'>"
                "<p class='generic-name'>%s</p><p class='dose-form'>%s</p>"
                "<p class='pack-spec'>%s</p><p class='vendor'>%s</p>"
                "<p class='sale-price'>%s</p><p class='listing-state'>%s</p>"
                "<p class='ts'>%s</p><p class='rev'>%s</p>"
                "</article></body></html>"
                % (html.escape(it["item_code"]), html.escape(it["generic_name"]),
                   html.escape(it["dosage_form"]), html.escape(it["spec_text"]),
                   html.escape(it["manufacturer"]), f'{it["price"]:.2f}',
                   html.escape(it["status"]), html.escape(it["updated_at"]),
                   it["version"]))

    def html_detail_v3(it):
        return ("<html><body><div class='neo-panel'><h3>%s</h3>"
                "<table><tr><td>%s</td></tr></table></div></body></html>"
                % (html.escape(it["item_code"]), html.escape(it["spec_text"])))

    def html_list():
        page = max(1, request.args.get("page", 1, type=int))
        rows, total = page_items(page)
        v = layout["html"]
        if v == "v3":
            return html_list_v3(rows, total, page)
        if v == "v2":
            return html_list_v2(rows, total, page)
        return html_list_v1(rows, total, page, request.path)

    def html_detail(code: str):
        it = one_item(code)
        if it is None:
            return "not found", 404
        v = layout["html"]
        if v == "v3":
            return html_detail_v3(it)
        if v == "v2":
            return html_detail_v2(it)
        return html_detail_v1(it)

    if mode in ("html_login",):
        app.add_url_rule("/list", view_func=auth_required(html_list))
        app.add_url_rule("/detail", view_func=auth_required(
            lambda: html_detail(request.args.get("code", ""))))
    if mode == "html_open":
        app.add_url_rule("/drugs", view_func=html_list)
        app.add_url_rule("/drug", view_func=
                         lambda: html_detail(request.args.get("code", "")))

    # ---------------- 管理接口（仅本机 + 密钥） ----------------
    def admin_ok() -> bool:
        return request.headers.get("X-Admin-Key") == ADMIN_KEY and \
            request.remote_addr in ("127.0.0.1", "::1")

    @app.post("/admin/tick")
    def admin_tick():
        if not admin_ok():
            return "forbidden", 403
        with lock:
            return jsonify(apply_tick(state))

    @app.get("/admin/state")
    def admin_state():
        if not admin_ok():
            return "forbidden", 403
        with lock:
            return jsonify(state)

    @app.get("/admin/events")
    def admin_events():
        if not admin_ok():
            return "forbidden", 403
        with lock:
            return jsonify(state["event_log"])

    @app.post("/admin/layout")
    def admin_layout():
        if not admin_ok():
            return "forbidden", 403
        data = request.get_json(silent=True) or {}
        channel = data.get("channel", "html")
        layout[channel] = data.get("version", "v1")
        return jsonify(layout)

    @app.post("/admin/expire")
    def admin_expire():
        if not admin_ok():
            return "forbidden", 403
        sessions.clear()
        return jsonify({"expired": True})

    @app.errorhandler(_RateLimited)
    def on_rl(_e):
        resp = jsonify({"error": "rate_limited"})
        resp.status_code = 429
        resp.headers["Retry-After"] = str(rw)
        return resp

    @app.get("/health")
    def health():
        return jsonify({"platform": p, "ok": True})

    return app


class _RateLimited(Exception):
    pass


def run(platform_cfg: dict, port: int) -> None:
    app = create_app(platform_cfg)
    app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)
