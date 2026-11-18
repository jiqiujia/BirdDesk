"""BirdDesk 启动入口。

    python -m app.main            # 桌面窗口（pywebview）
    python -m app.main --browser  # 用默认浏览器打开
    python -m app.main --port 8765 --no-open   # 仅启动服务（调试用）
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from . import server


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(port: int) -> uvicorn.Server:
    cfg = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.5)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    return srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    port = args.port or _free_port()
    url = f"http://127.0.0.1:{port}/"

    if args.browser or args.no_open:
        srv = _serve(port)
        print(f"BirdDesk 已启动：{url}", flush=True)
        if args.browser:
            webbrowser.open(url)
        try:
            while not srv.should_exit:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return

    import webview  # pywebview

    holder: dict = {}

    def pick(start: str):
        win = holder.get("win")
        if win is None:
            return None
        kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
        if kind is None:
            kind = webview.FOLDER_DIALOG
        res = win.create_file_dialog(kind, directory=start)
        if not res:
            return None
        return res[0] if isinstance(res, (list, tuple)) else res

    server.native_pick_folder = pick
    _serve(port)
    win = webview.create_window("BirdDesk 鸟片整理", url, width=1440, height=920,
                                min_size=(980, 640), text_select=True)
    holder["win"] = win
    webview.start(private_mode=False)
    sys.exit(0)


if __name__ == "__main__":
    main()
