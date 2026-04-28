from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


class SecretStore:
    def __init__(self, purpose: str) -> None:
        self._purpose = str(purpose or "windows-supporter")
        return

    def protect(self, value: str) -> str:
        raw = str(value or "")
        if not raw:
            return ""
        data = raw.encode("utf-8")
        protected = self.__protect_with_win32crypt(data)
        if not protected:
            protected = self.__protect_with_ctypes(data)
        if not protected:
            return ""
        return "dpapi:" + base64.b64encode(protected).decode("ascii")

    def __protect_with_win32crypt(self, data: bytes) -> bytes:
        try:
            import win32crypt

            return bytes(win32crypt.CryptProtectData(data, self._purpose, None, None, None, 0))
        except Exception:
            return b""

    def __protect_with_ctypes(self, data: bytes) -> bytes:
        try:
            data_buffer = ctypes.create_string_buffer(data)
            data_blob = _DataBlob(
                len(data),
                ctypes.cast(data_buffer, ctypes.POINTER(ctypes.c_byte)),
            )
            out_blob = _DataBlob()
            crypt32 = ctypes.windll.crypt32
            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(_DataBlob),
                wintypes.LPCWSTR,
                ctypes.POINTER(_DataBlob),
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(_DataBlob),
            ]
            crypt32.CryptProtectData.restype = wintypes.BOOL
            ok = crypt32.CryptProtectData(
                ctypes.byref(data_blob),
                self._purpose,
                None,
                None,
                None,
                0,
                ctypes.byref(out_blob),
            )
            if not ok:
                return b""
            try:
                return ctypes.string_at(out_blob.pbData, out_blob.cbData)
            finally:
                self.__local_free(out_blob.pbData)
        except Exception:
            return b""

    def unprotect(self, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            if raw.startswith("dpapi:"):
                blob = base64.b64decode(raw[len("dpapi:"):].encode("ascii"))
                data = self.__unprotect_with_win32crypt(blob)
                if not data:
                    data = self.__unprotect_with_ctypes(blob)
                if data:
                    return data.decode("utf-8")
        except Exception:
            return ""
        return ""

    def __unprotect_with_win32crypt(self, blob: bytes) -> bytes:
        try:
            import win32crypt

            result = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
            data = result[1] if isinstance(result, tuple) else result
            if isinstance(data, str):
                return data.encode("utf-8")
            return bytes(data)
        except Exception:
            return b""

    def __unprotect_with_ctypes(self, blob: bytes) -> bytes:
        try:
            blob_buffer = ctypes.create_string_buffer(blob)
            in_blob = _DataBlob(
                len(blob),
                ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_byte)),
            )
            out_blob = _DataBlob()
            crypt32 = ctypes.windll.crypt32
            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(_DataBlob),
                ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(_DataBlob),
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(_DataBlob),
            ]
            crypt32.CryptUnprotectData.restype = wintypes.BOOL
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(in_blob),
                None,
                None,
                None,
                None,
                0,
                ctypes.byref(out_blob),
            )
            if not ok:
                return b""
            try:
                return ctypes.string_at(out_blob.pbData, out_blob.cbData)
            finally:
                self.__local_free(out_blob.pbData)
        except Exception:
            return b""

    def __local_free(self, pointer) -> None:
        if not pointer:
            return
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
            kernel32.LocalFree.restype = wintypes.HLOCAL
            kernel32.LocalFree(ctypes.cast(pointer, wintypes.HLOCAL))
        except Exception:
            pass
        return
