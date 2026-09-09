"""Current-user Windows DPAPI; never silently downgrade to plaintext."""

import base64
import ctypes
import sys
from ctypes import wintypes


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def _crypt(data: bytes, decrypt=False) -> bytes:
    if sys.platform != "win32":
        raise ValueError("当前系统未接入安全密钥存储；请使用本地模型。")
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    crypto = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypto.CryptUnprotectData if decrypt else crypto.CryptProtectData
    operation.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError("无法访问当前系统账号的密钥存储，请重新输入 API Key。")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


def protect(secret: str) -> str:
    return base64.b64encode(_crypt(secret.encode())).decode()


def reveal(protected: str) -> str:
    return _crypt(base64.b64decode(protected), decrypt=True).decode()
