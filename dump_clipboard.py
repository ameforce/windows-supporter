from __future__ import annotations

import os
import sys
import time


def _safe_mkdir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass


def _format_name(fmt: int, win32clipboard, win32con) -> str:
    std = {
        getattr(win32con, "CF_TEXT", -1): "CF_TEXT",
        getattr(win32con, "CF_UNICODETEXT", -1): "CF_UNICODETEXT",
        getattr(win32con, "CF_DIB", -1): "CF_DIB",
        getattr(win32con, "CF_HDROP", -1): "CF_HDROP",
        getattr(win32con, "CF_OEMTEXT", -1): "CF_OEMTEXT",
        getattr(win32con, "CF_RTF", -1): "CF_RTF",
    }
    if fmt in std:
        return std[fmt]
    try:
        name = win32clipboard.GetClipboardFormatName(fmt)
        if name:
            return str(name)
    except Exception:
        pass
    return f"FMT_{fmt}"


def main() -> int:
    try:
        import win32clipboard
        import win32con
    except Exception as e:
        print(f"pywin32 import 실패: {e}")
        return 1

    out_root = os.path.join(os.getcwd(), "clipboard_dumps")
    _safe_mkdir(out_root)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(out_root, f"dump-{stamp}")
    _safe_mkdir(out_dir)

    try:
        win32clipboard.OpenClipboard()
    except Exception as e:
        print(f"OpenClipboard 실패: {e}")
        return 1

    formats = []
    try:
        fmt = 0
        while True:
            fmt = win32clipboard.EnumClipboardFormats(fmt)
            if not fmt:
                break
            formats.append(fmt)
    except Exception:
        pass

    log_lines = []
    log_lines.append(f"dump_dir={out_dir}")
    log_lines.append(f"format_count={len(formats)}")

    max_bytes = 2 * 1024 * 1024

    for fmt in formats:
        name = _format_name(fmt, win32clipboard, win32con)
        safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_", ".", " ") else "_" for ch in name).strip()
        if not safe_name:
            safe_name = f"FMT_{fmt}"

        try:
            data = win32clipboard.GetClipboardData(fmt)
        except Exception as e:
            log_lines.append(f"{fmt}:{name}:GetClipboardData 실패:{e}")
            continue

        if isinstance(data, str):
            path = os.path.join(out_dir, f"{safe_name}.txt")
            try:
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(data)
                log_lines.append(f"{fmt}:{name}:text:{len(data)}chars -> {os.path.basename(path)}")
            except Exception as e:
                log_lines.append(f"{fmt}:{name}:write 실패:{e}")
            continue

        if isinstance(data, (bytes, bytearray)):
            b = bytes(data)
            if len(b) > max_bytes:
                b = b[:max_bytes]
                suffix = ".trunc.bin"
            else:
                suffix = ".bin"
            path = os.path.join(out_dir, f"{safe_name}{suffix}")
            try:
                with open(path, "wb") as f:
                    f.write(b)
                log_lines.append(f"{fmt}:{name}:bytes:{len(data)} -> {os.path.basename(path)}")
            except Exception as e:
                log_lines.append(f"{fmt}:{name}:write 실패:{e}")
            continue

        try:
            s = repr(data)
        except Exception:
            s = "<unrepr>"
        path = os.path.join(out_dir, f"{safe_name}.repr.txt")
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(s)
            log_lines.append(f"{fmt}:{name}:repr -> {os.path.basename(path)}")
        except Exception as e:
            log_lines.append(f"{fmt}:{name}:write 실패:{e}")

    try:
        win32clipboard.CloseClipboard()
    except Exception:
        pass

    log_path = os.path.join(out_dir, "dump.log.txt")
    try:
        with open(log_path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(log_lines))
    except Exception:
        pass

    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
