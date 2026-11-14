"""启动四个仿真挂网平台（各占一个端口，仅监听 127.0.0.1）。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from sim.server import create_app

ROOT = Path(__file__).resolve().parent
PORTS = {"P01": 8101, "P02": 8102, "P03": 8103, "P04": 8104}


def main() -> None:
    cfgs = json.loads((ROOT / "configs" / "platforms.json").read_text(
        encoding="utf-8"))
    from werkzeug.serving import make_server
    servers = []
    for cfg in cfgs:
        app = create_app(cfg)
        srv = make_server("127.0.0.1", PORTS[cfg["code"]], app, threaded=True)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        servers.append(srv)
        print(f'仿真平台 {cfg["code"]} {cfg["name"]} -> '
              f'http://127.0.0.1:{PORTS[cfg["code"]]}')
    print("四个仿真平台已启动，Ctrl+C 退出")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        for srv in servers:
            srv.shutdown()


if __name__ == "__main__":
    main()
