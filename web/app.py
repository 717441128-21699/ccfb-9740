"""Web 控制台：平台配置 / 采集任务 / 药品库 / 价格变更 / 告警 五个页面。

任务在后台线程执行，stop 标志落运行时文件并通过检查点表可恢复；
进程重启后未完成的 running 任务会在下次触发时自动续跑。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from flask import (Flask, redirect, render_template, request, url_for)

from collector.db import DB
from collector.engine import PlatformRunner, Scheduler

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "collector.db"
CONFIGS = ROOT / "configs" / "platforms.json"
STOP_FLAG = ROOT / "data" / "STOP_ALL"

app = Flask(__name__)
db = DB(os.environ.get("COLLECTOR_DB", DEFAULT_DB))
_jobs_lock = threading.Lock()
_job_threads: dict[str, threading.Thread] = {}


def _load_configs() -> list[dict]:
    return json.loads(CONFIGS.read_text(encoding="utf-8"))


def _sync_configs() -> None:
    for cfg in _load_configs():
        db.upsert_platform(cfg)


@app.post("/admin/sync-config")
def sync_config():
    _sync_configs()
    return redirect(url_for("platforms_page"))


@app.get("/")
def index():
    stats = {
        "platforms": len(db.list_platforms()),
        "raw": len(db.list_raw()),
        "drugs": len(db.list_drugs()),
        "changes": len(db.list_changes()),
        "alerts": len(db.list_alerts()),
        "pending": len(db.list_mappings("pending")),
        "jobs": db.list_jobs()[:6],
    }
    return render_template("index.html", stats=stats)


# ---------- 平台配置 ----------
@app.get("/platforms")
def platforms_page():
    rows = db.list_platforms()
    return render_template("platforms.html", platforms=rows)


@app.post("/platforms/<code>/toggle")
def toggle_platform(code):
    enabled = request.form.get("enabled") == "1"
    db.set_platform_enabled(code, enabled)
    return redirect(url_for("platforms_page"))


# ---------- 采集任务 ----------
@app.get("/jobs")
def jobs_page():
    return render_template("jobs.html", jobs=db.list_jobs(),
                           platforms=db.list_platforms(),
                           logs=db.list_logs(100))


def _run_in_thread(cfg: dict, mode: str) -> None:
    def target():
        runner = PlatformRunner(db, cfg, log=lambda m: db.add_log(
            cfg["code"], None, "info", m))
        try:
            runner.run(mode)
        finally:
            with _jobs_lock:
                _job_threads.pop(cfg["code"], None)
    with _jobs_lock:
        old = _job_threads.get(cfg["code"])
        if old and old.is_alive():
            return
        t = threading.Thread(target=target, daemon=True)
        _job_threads[cfg["code"]] = t
        t.start()


@app.post("/jobs/run")
def jobs_run():
    mode = request.form.get("mode", "incremental")
    code = request.form.get("platform")
    cfgs = {c["code"]: c["config"] for c in db.list_platforms()}
    if code and code in cfgs:
        _run_in_thread(cfgs[code], mode)
    else:
        for c in db.list_platforms(only_enabled=True):
            _run_in_thread(c["config"], mode)
    return redirect(url_for("jobs_page"))


@app.post("/jobs/run-all")
def jobs_run_all():
    mode = request.form.get("mode", "full")
    for c in db.list_platforms(only_enabled=True):
        _run_in_thread(c["config"], mode)
    return redirect(url_for("jobs_page"))


@app.post("/jobs/stop")
def jobs_stop():
    STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
    STOP_FLAG.write_text("stop", encoding="utf-8")
    return redirect(url_for("jobs_page"))


# ---------- 药品库 ----------
@app.get("/drugs")
def drugs_page():
    drugs = db.list_drugs()
    mappings = db.list_mappings()
    pending = [m for m in mappings if m["status"] == "pending"]
    return render_template("drugs.html", drugs=drugs, pending=pending)


@app.post("/drugs/confirm/<int:mapping_id>")
def confirm_mapping(mapping_id: int):
    # 人工确认：按页面提交的四要素建/取标准品种并落 confirmed 映射
    mappings = db.list_mappings()
    m = next(x for x in mappings if x["id"] == mapping_id)
    from collector.normalize import normalize_item
    norm = normalize_item(m["generic_name"], m["dosage_form"],
                          m["spec_text"], m["manufacturer"])
    norm["spec_canonical"] = request.form["spec_canonical"].strip()
    norm["generic_name"] = request.form["generic_name"].strip()
    norm["dosage_form"] = request.form["dosage_form"].strip()
    norm["manufacturer"] = request.form["manufacturer"].strip()
    drug_id = db.get_or_create_drug(norm)
    db.upsert_mapping(m["platform_code"], m["item_code"], drug_id,
                      "confirmed", 1.0, "人工确认")
    return redirect(url_for("drugs_page"))


# ---------- 价格变更 ----------
@app.get("/changes")
def changes_page():
    platform = request.args.get("platform")
    changes = db.list_changes(platform)
    return render_template("changes.html", changes=changes,
                           platforms=db.list_platforms(), current=platform)


# ---------- 告警 ----------
@app.get("/alerts")
def alerts_page():
    return render_template("alerts.html", alerts=db.list_alerts(
        include_resolved=request.args.get("all") == "1"))


@app.post("/alerts/<int:alert_id>/resolve")
def resolve_alert(alert_id: int):
    db.resolve_alert(alert_id)
    return redirect(url_for("alerts_page"))


def create_app() -> Flask:
    _sync_configs()
    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=8200, debug=False)
