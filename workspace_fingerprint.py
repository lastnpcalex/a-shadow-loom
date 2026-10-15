"""Read-only file identity/change stamps for recovery-cache eligibility.

Windows st_ctime is creation time on supported Python versions, not a change
counter. Query FILE_BASIC_INFO instead. Failure disables reuse; it never makes
a file appear unchanged. No content, permissions, timestamps or Git state are
modified by these probes.
"""
from functools import lru_cache
import os
from pathlib import Path
import stat


@lru_cache(maxsize=1)
def _windows_api():
    import ctypes

    class BasicInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_int64) for name in
                    ("CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")]
        _fields_ += [("FileAttributes", ctypes.c_uint32)]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                               ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    api.CreateFileW.restype = ctypes.c_void_p
    api.GetFileInformationByHandleEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                ctypes.c_void_p, ctypes.c_uint32]
    api.GetFileInformationByHandleEx.restype = ctypes.c_int
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int
    return api, BasicInfo


def _windows_change_stamp(path: Path) -> list[int] | None:
    import ctypes

    try:
        api, BasicInfo = _windows_api()
        # A read-capable handle, but no ReadFile call: OPEN_EXISTING, never
        # create/truncate. Metadata-only access bypasses Windows share checks;
        # FILE_READ_DATA is required to reject an already-open writer.
        handle = api.CreateFileW(str(path), 0x1 | 0x80, 0x1 | 0x4, None, 3, 0x80, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            return None
        try:
            info = BasicInfo()
            if not api.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
                return None
            if info.ChangeTime <= 0 or info.LastWriteTime <= 0:
                return None
            return [info.LastWriteTime, info.ChangeTime, info.FileAttributes]
        finally:
            api.CloseHandle(handle)
    except (OSError, AttributeError):
        return None


def file_fingerprint(path: Path) -> list[int | str] | None:
    try:
        value = path.stat()
        if not stat.S_ISREG(value.st_mode) or not value.st_ino:
            return None
        stamp = (_windows_change_stamp(path) if os.name == "nt"
                 else [value.st_ctime_ns])
        if stamp is None or not all(stamp):
            return None
        return ["file-identity-v1", os.name, value.st_dev, value.st_ino,
                value.st_size, value.st_mtime_ns, value.st_mode, *stamp]
    except (OSError, ValueError):
        return None
