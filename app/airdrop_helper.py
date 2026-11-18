"""隔空传送子进程（仅调试用；正常 App 走主线程 in-process 路径）。

用法：
    python -m app.airdrop_helper --check          # 只检查服务可用性，不弹面板
    python -m app.airdrop_helper a.jpg b.jpg      # 弹面板（可能需要键盘输入验证码）

退出码：0 正常（用户完成或超时），3 服务不可用，4 没有有效文件。
"""
from __future__ import annotations

import os
import sys


def _check(paths: list[str]) -> int:
    try:
        from AppKit import NSSharingService, NSSharingServiceNameSendViaAirDrop, NSURL
    except Exception as e:  # noqa: BLE001
        print(f"AppKit unavailable: {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    svc = NSSharingService.sharingServiceNamed_(NSSharingServiceNameSendViaAirDrop)
    if svc is None:
        print("AirDrop service missing", file=sys.stderr)
        return 3
    if paths:
        urls = [NSURL.fileURLWithPath_(p) for p in paths]
        if not svc.canPerformWithItems_(urls):
            print("cannot perform with items", file=sys.stderr)
            return 3
    print("ok")
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    paths = [p for p in args if os.path.isfile(p)]
    if "--check" in argv:
        return _check(paths)
    if not paths:
        print("no valid files", file=sys.stderr)
        return 4

    try:
        import objc
        from AppKit import (NSApplication, NSSharingService,
                            NSSharingServiceNameSendViaAirDrop, NSURL, NSTimer)
        from PyObjCTools import AppHelper
    except Exception as e:  # noqa: BLE001
        print(f"AppKit unavailable: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)          # Accessory：不进 Dock
    urls = [NSURL.fileURLWithPath_(p) for p in paths]
    svc = NSSharingService.sharingServiceNamed_(NSSharingServiceNameSendViaAirDrop)
    if svc is None or not svc.canPerformWithItems_(urls):
        print("service unavailable", file=sys.stderr)
        return 3

    state = {"ok": False}

    class Delegate(objc.lookUpClass("NSObject")):
        def sharingService_didCompleteForItems_error_(self, service, items, error):
            if error is not None:
                print(f"completed with error: {error}", file=sys.stderr)
            app.stop_(None)

        def sharingService_didFailToShareItems_error_(self, service, items, error):
            print(f"failed: {error}", file=sys.stderr)
            app.stop_(None)

    delegate = Delegate.alloc().init()

    def begin():
        # 事件循环已经在跑，此时再弹面板，窗口才能正常成为 key window
        state["ok"] = True
        svc.setDelegate_(delegate)
        svc.performWithItems_(urls)

    AppHelper.callAfter(begin)
    # 用户一直不选/不输验证码时留一个上限，别让进程挂死
    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(180.0, False, lambda t: app.stop_(None))
    app.activateIgnoringOtherApps_(True)
    app.run()                             # 正规事件循环：能处理模态面板与键盘输入
    return 0 if state["ok"] else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))
