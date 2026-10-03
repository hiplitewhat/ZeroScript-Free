# SPDX-License-Identifier: GPL-3.0-or-later
# synapse/screenshot.py
# -----------------------------------------------------------------------------
# Local screen capture for the Synapse MCP server. Runs on the same machine as
# the relay, so it can see the Delta console, the relay terminal, the browser,
# anything on the desktop.
#
# Tries, in order:
#   1. mss      (fast, cross-platform, captures the whole desktop or a monitor)
#   2. Pillow   (ImageGrab, simpler Windows-only backend)
#
# Returns PNG bytes + an error string. Never raises.
# -----------------------------------------------------------------------------
from __future__ import annotations

import base64
import io

try:
    import android_screenshot as _android  # local sibling (Termux/Android)
    HAS_ANDROID = True
except Exception:
    HAS_ANDROID = False

try:
    import mss  # type: ignore
    HAS_MSS = True
except Exception:
    HAS_MSS = False

try:
    from PIL import Image, ImageGrab  # type: ignore
    HAS_PIL = True
except Exception:
    HAS_PIL = False


def _png_bytes(monitor=None) -> tuple[bytes | None, str | None]:
    """Capture `monitor` (default: the whole desktop) and return PNG bytes."""
    # Android/Termux: desktop modules can't see the screen. Drive ScreenshotTile
    # (cvzi/screenshottile) via its broadcast intent and read the new file.
    if HAS_ANDROID and _android.is_android():
        data, err = _android.capture_png_b64()
        if err:
            return None, err
        import base64 as _b64
        return _b64.b64decode(data), None
    if HAS_MSS:
        try:
            import mss as _mss
            with _mss.mss() as sct:
                if monitor is None:
                    monitor = sct.monitors[0]  # [0] = union of every screen
                shot = sct.grab(monitor)
                if HAS_PIL:
                    # bgra -> raw rgb via PIL, stable on every OS
                    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                    buf = io.BytesIO()
                    img.save(buf, "PNG")
                    return buf.getvalue(), None
                return shot.png, None  # mss <= screenshot().png if PIL absent
        except Exception as exc:
            err = f"mss capture failed: {exc}"
    if HAS_PIL:
        try:
            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue(), None
        except Exception as exc:
            err = f"Pillow capture failed: {exc}"
            return None, err
    err = "screenshot requires 'mss' or Pillow:  pip install mss pillow"
    return None, err


def capture_png_b64(monitor=None) -> tuple[str | None, str | None]:
    """Return (base64 PNG, error). Either data or error is set."""
    data, err = _png_bytes(monitor)
    if err:
        return None, err
    if data is None:
        return None, "screenshot produced no data"
    return base64.b64encode(data).decode("ascii"), None


def main() -> int:
    """CLI: save a PNG to the current directory and print the path."""
    import os
    import tempfile
    data, err = _png_bytes()
    if err or data is None:
        print("ERROR:", err or "no data")
        return 1
    out = os.path.join(tempfile.gettempdir(), "synapse_screenshot.png")
    with open(out, "wb") as f:
        f.write(data)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
