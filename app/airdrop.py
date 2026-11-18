"""隔空传送（AirDrop）。

macOS 没有 AirDrop 命令行，只能用 AppKit 的 `NSSharingService`（`com.apple.share.AirDrop.send`）：
它弹出系统面板，由用户选设备、必要时输入对方显示的验证码——这一步必须在有键盘焦点的图形会话里完成。

**为什么之前会卡住不能输入**：早先的做法是每次传送都拉一个子进程，用
`runConsoleEventLoop` 跑事件循环 + Accessory 策略。控制台事件循环只泵默认模式，
而分享面板是窗口模态的；再加上 Accessory 进程没有真正的 key window，
面板虽然能画出来，键盘事件却进不去——表现就是"窗口出来了但打不了字"。

现在的策略：
1. 应用本体（pywebview）主线程本来就跑着 `NSApplication`，所以直接在**主线程**里
   `performWithItems_`（用 `AppHelper.callAfter` 从 HTTP 线程派发过去）——
   面板属于我们自己的 App，焦点、键盘、验证码输入都正常。
2. 只有在纯命令行 / `--no-open` 调试模式下（没有 NSApp 主循环）才退回子进程；
   子进程用 `app.run()` 正规事件循环而不是 console 循环，并且默认只用于调试。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import BASE_DIR

TIMEOUT_S = 180
_keep_alive: list = []          # 防止 NSSharingService / delegate 被 GC


def available() -> bool:
    try:
        from AppKit import NSSharingService, NSSharingServiceNameSendViaAirDrop  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _in_process() -> bool:
    """当前进程是否有正在运行的 Cocoa 主循环（即桌面窗口模式）。"""
    try:
        from AppKit import NSApp
        return NSApp is not None and bool(NSApp.isRunning())
    except Exception:  # noqa: BLE001
        return False


def _can_send(paths: list[str]) -> bool:
    try:
        from AppKit import NSSharingService, NSSharingServiceNameSendViaAirDrop, NSURL
        svc = NSSharingService.sharingServiceNamed_(NSSharingServiceNameSendViaAirDrop)
        return svc is not None and bool(svc.canPerformWithItems_(
            [NSURL.fileURLWithPath_(p) for p in paths]))
    except Exception:  # noqa: BLE001
        return False


def _fire(paths: list[str]) -> bool:
    """在主线程上真正调用分享服务。"""
    from AppKit import NSApp, NSSharingService, NSSharingServiceNameSendViaAirDrop, NSURL

    urls = [NSURL.fileURLWithPath_(p) for p in paths]
    svc = NSSharingService.sharingServiceNamed_(NSSharingServiceNameSendViaAirDrop)
    if svc is None or not svc.canPerformWithItems_(urls):
        return False
    try:
        # 面板要成为 key window 才能接收键盘（输入验证码）；先把 App 提到前台
        NSApp.activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001
        pass
    _keep_alive.append((svc, urls))
    svc.performWithItems_(urls)
    return True


def send_files(paths: list[Path]) -> dict:
    """把文件交给隔空传送面板。返回 {ok, count, message, mode}。"""
    files = [str(p) for p in paths if p and Path(p).is_file()]
    if not files:
        return {"ok": False, "count": 0, "message": "没有可传送的文件", "mode": "none"}
    if not available():
        return {"ok": False, "count": 0, "mode": "none",
                "message": "当前系统不支持隔空传送，可在 Finder 里右键共享"}

    # 主线程里由 _fire 自己判断能不能发；避免在请求线程碰 AppKit
    if _in_process():
        try:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(_fire, files)
            return {"ok": True, "count": len(files), "mode": "in-process",
                    "message": "已打开隔空传送面板，请选择设备"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "count": len(files), "mode": "in-process",
                    "message": f"无法打开面板：{type(e).__name__}: {e}"}

    # 调试模式（无图形主循环）：先预检，再交给子进程
    if not _can_send(files):
        return {"ok": False, "count": len(files), "mode": "check",
                "message": "系统暂时无法发起隔空传送（AirDrop 未开启 / 附近没有可接收设备）"}
    cmd = [sys.executable, "-m", "app.airdrop_helper", *files]
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(BASE_DIR))
    try:
        proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "count": len(files), "mode": "helper",
                "message": f"无法启动传送：{type(e).__name__}"}
    try:
        out, _ = proc.communicate(timeout=2.5)
        text = (out or b"").decode("utf-8", "ignore").strip()
        if proc.returncode == 3:
            return {"ok": False, "count": len(files), "mode": "helper",
                    "message": "系统拒绝了这次传送（没有可用接收端或不在图形会话中）"}
        if proc.returncode != 0:
            return {"ok": False, "count": len(files), "mode": "helper",
                    "message": f"传送面板启动失败：{text[:120] or proc.returncode}"}
        return {"ok": True, "count": len(files), "mode": "helper", "message": "已完成"}
    except subprocess.TimeoutExpired:
        return {"ok": True, "count": len(files), "mode": "helper",
                "message": "已打开隔空传送面板（调试模式，键盘焦点可能异常）"}
