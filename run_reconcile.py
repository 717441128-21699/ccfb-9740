"""命令行对账：python run_reconcile.py"""
from __future__ import annotations

import json
from pathlib import Path

from collector.db import DB
from reconcile.reconcile import print_report, run_reconcile

ROOT = Path(__file__).resolve().parent


def main() -> int:
    configs = json.loads((ROOT / "configs" / "platforms.json").read_text(
        encoding="utf-8"))
    db = DB(ROOT / "data" / "collector.db")
    reports, norm_diffs = run_reconcile(configs, db)
    return print_report(reports, norm_diffs)


if __name__ == "__main__":
    raise SystemExit(main())
