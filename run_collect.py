"""命令行入口：
  python run_collect.py sync-configs          同步平台配置到库
  python run_collect.py full [--platform P01] 全量采集
  python run_collect.py inc  [--platform P01] 增量采集
  python run_collect.py list                   查看平台/任务概况
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from collector.db import DB
from collector.engine import STOP_FLAG, PlatformRunner

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "collector.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["sync-configs", "full", "inc", "list"])
    ap.add_argument("--platform")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    db = DB(args.db)
    if args.command == "sync-configs":
        for cfg in json.loads((ROOT / "configs" / "platforms.json").read_text(
                encoding="utf-8")):
            db.upsert_platform(cfg)
        print("平台配置已同步:", [c["code"] for c in db.list_platforms()])
        return 0

    if args.command == "list":
        for p in db.list_platforms():
            wm = db.get_watermark(p["code"])
            print(f'{p["code"]} {p["name"]} enabled={p["enabled"]} '
                  f'水位={wm and wm["last_source_updated_at"]}')
        for j in db.list_jobs()[:10]:
            print("job", j["job_id"], j["platform_code"], j["mode"],
                  j["status"], f'详情{j["detail_done"]}/{j["detail_total"]}')
        return 0

    if STOP_FLAG.exists():
        STOP_FLAG.unlink()
    mode = "full" if args.command == "full" else "incremental"
    platforms = db.list_platforms(only_enabled=True)
    if args.platform:
        platforms = [p for p in platforms if p["code"] == args.platform]
    if not platforms:
        print("没有匹配的启用平台", file=sys.stderr)
        return 2
    rc = 0
    for p in platforms:
        runner = PlatformRunner(db, p["config"], log=lambda m: print(m))
        runner.run(mode)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
