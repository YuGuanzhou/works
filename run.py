import argparse
import json
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.request import build_opener, ProxyHandler

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="工作无忧 Python · 本地求职助手")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开管理页面")
    parser.add_argument("--data-dir", type=Path, help="本地数据目录")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    os.environ["JOBS_DATA_DIR"] = str(args.data_dir or root / "data")
    from jobs_app.main import create_app
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", args.port)) == 0:
            try:
                with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{args.port}/api/health", timeout=2) as response:
                    existing = json.load(response)
                if existing.get("app") == "get-jobs-python":
                    from jobs_app import __version__
                    if existing.get("version") != __version__:
                        print(f"旧版 {existing.get('version')} 仍在运行，请先关闭旧版后台，再启动 {__version__}。")
                        return 1
                    print(f"工作无忧已运行：http://127.0.0.1:{args.port}")
                    if not args.no_browser:
                        webbrowser.open(f"http://127.0.0.1:{args.port}")
                    return 0
            except Exception:
                pass
            print(f"端口 {args.port} 已被占用，请使用 --port 指定另一个端口。")
            return 1
    if not args.no_browser:
        timer = threading.Timer(1.5, webbrowser.open, args=(f"http://127.0.0.1:{args.port}",))
        timer.daemon = True
        timer.start()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
