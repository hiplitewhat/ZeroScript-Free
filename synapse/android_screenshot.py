# SPDX-License-Identifier: GPL-3.0-or-later
# synapse/android_screenshot.py
# -----------------------------------------------------------------------------
# Android screenshot capture for Synapse. On Android the desktop modules (mss /
# Pillow ImageGrab) can't see the screen, so we drive the ScreenshotTile app
# (https://github.com/cvzi/ScreenshotTile) instead:
#
#   1. Send its broadcast intent from Termux:
#        am broadcast -a com.github.cvzi.screenshottile.SCREENSHOT \
#            -e secret <secret> com.github.cvzi.screenshottile
#   2. ScreenshotTile saves  Pictures/Screenshots/Screenshot_<timestamp>.png
#      (default storage dir + default "Screenshot_%timestamp%" filename pattern)
#   3. We wait for the newest file to change, read it, and return PNG bytes.
#
# Config via env on the phone running the relay:
#   SYNAPSE_SCREENSHOT_SECRET   the broadcast secret from ScreenshotTile settings
#                               (REQUIRED for automation; set it in the app)
#   SYNAPSE_SCREENSHOT_DIR      path to the screenshot folder (default the two
#                               standard Pictures/DCIM candidates)
#   SYNAPSE_SCREENSHOT_PACKAGE  target package (default com.github.cvzi.screenshottile)
# -----------------------------------------------------------------------------
from __future__ import annotations

import base64
import glob
import os
import subprocess
import time
from pathlib import Path

PACKAGE = os.environ.get("SYNAPSE_SCREENSHOT_PACKAGE", "com.github.cvzi.screenshottile")
ACTION = os.environ.get("SYNAPSE_SCREENSHOT_ACTION", f"{PACKAGE}.SCREENSHOT")

DEFAULT_DIRS = [
    "/sdcard/Pictures/Screenshots",
    "/storage/emulated/0/Pictures/Screenshots",
    "/sdcard/DCIM/Screenshots",
    "/storage/emulated/0/DCIM/Screenshots",
    # App-specific fallback (Android P and lower when external public storage is
    # unavailable); also covers a non-default folder the user set.
    "/sdcard/Android/data/com.github.cvzi.screenshottile/files/Pictures/Screenshots",
    "/storage/emulated/0/Android/data/Pictures/Screenshots",
]


def is_android() -> bool:
    """True when running inside Termux / on an Android device."""
    if os.path.exists("/system/bin/am") and "ANDROID_ROOT" in os.environ:
        return True
    env = os.environ
    return bool(
        env.get("TERMUX_APP__PACKAGE_NAME")
        or env.get("PREFIX", "").startswith("/data/data/com.termux")
        or env.get("ANDROID_ROOT")
    )


def _dirs() -> list[Path]:
    out: list[Path] = []
    custom = os.environ.get("SYNAPSE_SCREENSHOT_DIR")
    if custom:
        out.append(Path(custom))
    for d in DEFAULT_DIRS:
        out.append(Path(d))
    return out


def _latest() -> tuple[Path | None, float]:
    """Return (path, mtime) of the newest screenshot picture."""
    best: Path | None = None
    best_mtime = 0.0
    for d in _dirs():
        if not d.is_dir():
            continue
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            for p in glob.glob(str(d / pattern)):
                fp = Path(p)
                try:
                    mt = fp.stat().st_mtime
                except OSError:
                    continue
                if mt > best_mtime:
                    best = fp
                    best_mtime = mt
    return best, best_mtime


def trigger(secret: str, partial: bool = False, delay: float = 3.0) -> tuple[bool, str]:
    """Send the ScreenshotTile broadcast. Returns (ok, detail)."""
    if not is_android():
        return False, "not an Android/Termux environment"
    if not secret:
        return False, "SYNAPSE_SCREENSHOT_SECRET is required (set the broadcast secret in ScreenshotTile first)"
    if not os.path.exists("/system/bin/am"):
        return False, "am not found - are you running inside Termux?"
    cmd = [
        "am", "broadcast",
        "-a", ACTION,
        "-e", "secret", secret,
    ]
    if partial:
        cmd += ["--ez", "partial", "true"]
    cmd.append(PACKAGE)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                              cwd=str(Path.home()))
        out = (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:
        return False, f"am broadcast failed: {exc}"
    ok = proc.returncode == 0
    detail = (out.strip() or f"exit {proc.returncode}")[:300]
    # The tile has a built-in delay, plus we give MediaStore a beat to flush.
    time.sleep(delay)
    return ok, detail


def _wait_for_capture(timeout: float = 12.0) -> tuple[Path | None, str]:
    """Wait for a new screenshot after triggering. Returns (path, error)."""
    before, before_m = _latest()
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        path, mt = _latest()
        if path is not None and (before is None or path != before or mt > before_m):
            # Require a short settle so the file is fully written.
            time.sleep(0.25)
            try:
                if path.stat().st_size > 0:
                    return path, ""
            except OSError:
                pass
        time.sleep(0.25)
    return None, f"no new screenshot after {timeout:.0f}s (checked {before})"


def capture_png_b64(timeout: float = 12.0) -> tuple[str | None, str | None]:
    """Trigger ScreenshotTile, read the newest file, return base64 PNG bytes."""
    secret = os.environ.get("SYNAPSE_SCREENSHOT_SECRET", "")
    ok, detail = trigger(secret, delay=2.0)
    if not ok:
        return None, detail
    path, err = _wait_for_capture(timeout=timeout)
    if err or path is None:
        return None, err or "no capture"
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, f"could not read {path}: {exc}"
    return base64.b64encode(data).decode("ascii"), None


def main() -> int:
    import tempfile
    data, err = capture_png_b64()
    if err:
        print("ERROR:", err)
        return 1
    out = os.path.join(tempfile.gettempdir(), "synapse_android_screenshot.png")
    with open(out, "wb") as f:
        f.write(base64.b64decode(data))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
