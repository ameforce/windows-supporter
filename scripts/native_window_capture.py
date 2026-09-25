"""Capture an owned Tk window without depending on a visible remote desktop."""
from pathlib import Path
import ctypes
import os
import win32con
import win32gui
import win32process
import win32ui
from scripts.qa_ai_usage_native_visual import _write_rgb_png


def capture_window(window, path: Path) -> str:
    hwnd = win32gui.GetAncestor(window.winfo_id(), 2)
    if win32process.GetWindowThreadProcessId(hwnd)[1] != os.getpid():
        raise RuntimeError("Capture target is not an owned QA window")
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right-left, bottom-top
    if width <= 0 or height <= 0:
        raise RuntimeError("Capture target has empty dimensions")
    source_handle = win32gui.GetWindowDC(hwnd)
    source = win32ui.CreateDCFromHandle(source_handle)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source, width, height)
    previous = memory.SelectObject(bitmap)
    try:
        print_window = ctypes.windll.user32.PrintWindow
        print_window.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        print_window.restype = ctypes.c_int
        if not print_window(hwnd, memory.GetSafeHdc(), 2):
            raise RuntimeError("PrintWindow could not render the QA window")
        raw = bitmap.GetBitmapBits(True)
        if len(set(raw[::4])) < 2:
            raise RuntimeError("PrintWindow returned a blank QA image")
        rgb = bytearray(width*height*3)
        rgb[0::3], rgb[1::3], rgb[2::3] = raw[2::4], raw[1::4], raw[0::4]
        _write_rgb_png(path, width, height, bytes(rgb))
    finally:
        memory.SelectObject(previous)
        win32gui.DeleteObject(bitmap.GetHandle())
        memory.DeleteDC()
        win32gui.ReleaseDC(hwnd, source_handle)
    return "win32-printwindow-owned-window"
