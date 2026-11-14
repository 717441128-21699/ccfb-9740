"""端到端测试（不接触任何真实网站，全部指向 127.0.0.1 仿真站）。

覆盖：
  1. 全量采集 → 与仿真真值逐条对账，差异必须为 0；
  2. 采集进程被强杀（taskkill /F）→ 重启自动续跑，不重复不漏抓，再次对账差异为 0；
  3. 仿真站推进 4 拍调价/撤网 → 增量采集 → 对账差异为 0，变更历史逐事件匹配；
  4. 页面改版(v3) → 必须产生 parse 告警，且不写入空数据；
  5. 登录过期 → 自动重登成功；
  6. 单平台宕机 → 其他平台任务不受影响；
  7. 重复跑全量 → 变更历史不产生重复记录（幂等）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collector.db import DB  # noqa: E402
from reconcile.reconcile import (print_report, run_reconcile)  # noqa: E402
from sim.server import create_app  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

PORTS = {"P01": 8101, "P02": 8102, "P03": 8103, "P04": 8104}
ADMIN = {"X-Admin-Key": "sim-admin-key"}
DB_PATH = ROOT / "data" / "test_collector.db"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")


class SimHarness:
    def __init__(self):
        self.cfgs = json.loads((ROOT / "configs" / "platforms.json").read_text(
            encoding="utf-8"))
        # app 只建一次，server 重启时复用同一 app（保留全部调价/撤网状态）
        self.apps = [create_app(cfg) for cfg in self.cfgs]
        self.servers = []
        self.threads = []

    def start(self):
        for cfg, app in zip(self.cfgs, self.apps):
            self._serve(cfg["code"], app)
        time.sleep(0.5)

    def _serve(self, code: str, app):
        srv = make_server("127.0.0.1", PORTS[code], app, threaded=True)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.servers.append(srv)
        self.threads.append(t)

    def stop(self):
        for s in self.servers:
            s.shutdown()

    def restart(self, code: str):
        """模拟平台宕机后恢复：新建 server 但复用原 app 状态。"""
        idx = next(i for i, c in enumerate(self.cfgs) if c["code"] == code)
        self.servers[idx].shutdown()
        time.sleep(0.3)
        self.servers[idx] = None
        srv = make_server("127.0.0.1", PORTS[code], self.apps[idx],
                          threaded=True)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.servers[idx] = srv
        time.sleep(0.5)

    def admin(self, code: str, path: str, body: dict | None = None,
              method: str | None = None):
        url = f"http://127.0.0.1:{PORTS[code]}{path}"
        if method is None:
            method = "POST" if (body is not None or path.startswith("/admin/")) \
                else "GET"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=ADMIN,
                                     method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def tick_all(self):
        return {c["code"]: self.admin(c["code"], "/admin/tick", {})
                for c in self.cfgs}


def run_collector_subprocess(*args: str, kill_after: float | None = None,
                             env_db: Path = DB_PATH):
    env = dict(os.environ)
    env["COLLECTOR_DB"] = str(env_db)
    cmd = [PY, str(ROOT / "run_collect.py"), *args, "--db", str(env_db)]
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    if kill_after is None:
        out, _ = proc.communicate(timeout=180)
        return proc.returncode, out
    time.sleep(kill_after)
    proc.kill()  # Windows 下等价强杀，模拟进程被杀
    try:
        out = proc.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        out = ""
    return -1, out


def assert_zero_diff(db: DB, harness: SimHarness, label: str,
                     check_events: bool = True):
    reports, norm = run_reconcile(harness.cfgs, db,
                                  check_events=check_events)
    rc = print_report(reports, norm)
    assert rc == 0, f"{label} 对账存在差异"
    print(f"✅ {label}: 对账差异为 0\n")


def main() -> int:
    if DB_PATH.exists():
        DB_PATH.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    harness = SimHarness()
    harness.start()
    db = DB(DB_PATH)
    try:
        # 0. 健康检查
        for code in PORTS:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORTS[code]}/health", timeout=5) as r:
                assert json.loads(r.read())["ok"]
        print("✅ 四个仿真平台健康检查通过")

        # 1. 全量采集（子进程），中途强杀一次
        print("\n=== 场景1：全量采集 + 进程被杀续跑 ===")
        rc, out1 = run_collector_subprocess("sync-configs")
        assert rc == 0, out1
        rc, out_kill = run_collector_subprocess("full", kill_after=2.0)
        print("[被杀进程输出尾部]\n", "\n".join(out_kill.splitlines()[-8:]))
        assert rc == -1
        running = [j for j in db.list_jobs() if j["status"] == "running"]
        assert running, "被杀后应保留 running 断点任务"
        print(f"✅ 进程已强杀，残留 running 任务 {len(running)} 个，断点已落库")

        # 重启续跑（CLI 会自动恢复 running 任务）
        rc, out2 = run_collector_subprocess("full")
        assert rc == 0, out2
        assert not [j for j in db.list_jobs() if j["status"] == "running"], \
            "续跑后不应再有 running 任务"
        # 此时尚未推进任何事件，只对字段与归一化；事件对账在 tick 之后
        assert_zero_diff(db, harness, "全量+杀进程续跑", check_events=False)

        # 幂等：再跑一次全量，变更数不应变化
        changes_before = len(db.list_changes())
        rc, _ = run_collector_subprocess("full")
        assert rc == 0
        assert len(db.list_changes()) == changes_before, "重复全量产生了重复变更"
        print("✅ 重复全量幂等，无重复变更")

        # 2. 推进两拍事件 + 增量
        print("\n=== 场景2：调价/撤网事件 + 增量采集 ===")
        harness.tick_all()
        harness.tick_all()
        rc, _ = run_collector_subprocess("inc")
        assert rc == 0
        # 事件已开始产生，但还没全部推进，字段对账必须为 0
        assert_zero_diff(db, harness, "增量(第1-2拍)", check_events=False)

        # 3. 再推进两拍 + 增量（全部 8 个事件/省到位后做完整事件对账）
        harness.tick_all()
        harness.tick_all()
        rc, _ = run_collector_subprocess("inc")
        assert rc == 0
        assert_zero_diff(db, harness, "增量(第3-4拍)")

        # 变更历史数量：每省 9 个事件（7 调价 + 2 撤网，见 generator 调度）
        for code in PORTS:
            n = len(db.list_changes(code))
            assert n == 9, f"{code} 变更数={n}，期望 9"
        print("✅ 每省 9 个变更事件（7 调价 + 2 撤网）全部落库且与真值一致")

        # 4. 页面/接口改版 → parse 告警，且不写空数据
        print("\n=== 场景3：页面改版解析告警 ===")
        harness.admin("P01", "/admin/layout", {"channel": "html", "version": "v3"})
        harness.admin("P02", "/admin/layout", {"channel": "api", "version": "v3"})
        before_raw = len(db.list_raw())
        rc, out_v3 = run_collector_subprocess("full", "--platform", "P01")
        run_collector_subprocess("full", "--platform", "P02")
        alerts = db.list_alerts()
        assert any(a["platform_code"] == "P01" and a["kind"] == "parse"
                   for a in alerts), "P01 改版未产生 parse 告警"
        assert any(a["platform_code"] == "P02" and a["kind"] == "parse"
                   for a in alerts), "P02 改版未产生 parse 告警"
        assert len(db.list_raw()) == before_raw, "改版后写入了数据（可能是空数据）"
        print("✅ 改版触发 parse 告警，任务中止且未写入任何数据")
        # 恢复布局，后续平台宕机场景需要 P01/P02 可用
        harness.admin("P01", "/admin/layout", {"channel": "html", "version": "v1"})
        harness.admin("P02", "/admin/layout", {"channel": "api", "version": "v1"})

        # 5. 登录过期 → 自动重登
        print("\n=== 场景4：登录过期自动重登 ===")
        harness.admin("P01", "/admin/expire")
        harness.admin("P03", "/admin/expire")
        rc, out_login = run_collector_subprocess("inc", "--platform", "P01")
        assert rc == 0, out_login
        run_collector_subprocess("inc", "--platform", "P03")
        assert_zero_diff(db, harness, "登录过期重登后")
        print("✅ P01/P03 会话过期后自动重登并采集成功")

        # 6. 单平台宕机：停掉 P04，其他平台不受影响；P04 记故障告警
        print("\n=== 场景5：单平台宕机隔离 ===")
        idx = next(i for i, c in enumerate(harness.cfgs) if c["code"] == "P04")
        harness.servers[idx].shutdown()
        time.sleep(0.3)
        rc, _ = run_collector_subprocess("inc", "--platform", "P01")
        assert rc == 0, "P04 宕机不应影响 P01 采集"
        rc, _ = run_collector_subprocess("inc", "--platform", "P04")
        assert rc == 0  # 进程不崩溃，平台故障在库内记告警
        down_alerts = [a for a in db.list_alerts()
                       if a["platform_code"] == "P04"
                       and a["kind"] == "platform_down"]
        assert down_alerts, "P04 宕机未产生 platform_down 告警"
        failed_jobs = [j for j in db.list_jobs()
                       if j["platform_code"] == "P04" and j["status"] == "failed"]
        assert failed_jobs, "P04 宕机任务应标记 failed"
        # 恢复 P04（复用原 app，状态不丢）
        harness.restart("P04")
        run_collector_subprocess("inc", "--platform", "P04")
        assert_zero_diff(db, harness, "P04 恢复后")
        print("✅ P04 宕机被隔离并告警，恢复后续跑成功，其他平台不受影响")

        # 7. 人工确认数量
        pending = db.list_mappings("pending")
        assert len(pending) == 4, f"待确认={len(pending)}，期望 4"
        print("\n✅ 4 个难解析规格全部停在人工确认，未被错误自动归一")

        print("\n" + "=" * 60)
        print("全部端到端场景通过，最终对账差异为 0 🎉")
        return 0
    finally:
        harness.stop()


if __name__ == "__main__":
    raise SystemExit(main())
