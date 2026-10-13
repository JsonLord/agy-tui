"""Native Windows executable, path and owned process containment helpers."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path, PureWindowsPath


def validate_windows_path(value: str):
    path = PureWindowsPath(value)
    if value.startswith(("\\\\", "//")) or (path.drive and not path.root):
        raise ValueError("Unsupported device/UNC/drive-relative path")
    for part in path.parts:
        if part == path.anchor:
            continue
        if (
            ":" in part
            or part.endswith((" ", "."))
            or re.fullmatch(
                r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.IGNORECASE
            )
        ):
            raise ValueError("Unsupported Windows alias/alternate data stream")


def native_binary(value: str) -> str:
    found = shutil.which(value)
    path = Path(found or value).resolve()
    if os.name != "nt":
        return str(path) if found or path.is_file() else value
    if path.suffix.lower() == ".exe" and path.is_file():
        return str(path)
    for base in (path.parent, path.parent / "node_modules/opencode-ai"):
        for package in ("opencode-windows-x64", "opencode-windows-arm64"):
            candidate = base / "node_modules" / package / "bin/opencode.exe"
            if candidate.is_file():
                return str(candidate.resolve())
    raise ValueError("Supply the native opencode.exe; shell shims are not executed")


def process_options() -> dict:
    return (
        {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        }
        if os.name == "nt"
        else {}
    )


class OwnedWindowsJob:
    """Kill-on-close job for only our ACP PID and descendants, never Desktop."""

    def __init__(self, pid: int):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [
                ("ProcessTime", ctypes.c_int64),
                ("JobTime", ctypes.c_int64),
                ("Flags", wintypes.DWORD),
                ("Min", ctypes.c_size_t),
                ("Max", ctypes.c_size_t),
                ("Active", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("Priority", wintypes.DWORD),
                ("Scheduling", wintypes.DWORD),
            ]

        class IO(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOps",
                    "WriteOps",
                    "OtherOps",
                    "ReadBytes",
                    "WriteBytes",
                    "OtherBytes",
                )
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("Basic", Basic),
                ("IO", IO),
                ("ProcessMemory", ctypes.c_size_t),
                ("JobMemory", ctypes.c_size_t),
                ("PeakProcess", ctypes.c_size_t),
                ("PeakJob", ctypes.c_size_t),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        limits = Extended()
        limits.Basic.Flags = 0x2000
        try:
            if (
                not handle
                or not process
                or not kernel.SetInformationJobObject(
                    handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
                )
                or not kernel.AssignProcessToJobObject(handle, process)
            ):
                raise OSError("Cannot contain owned ACP process")
            self.handle, self.kernel = handle, kernel
        except BaseException:
            if handle:
                kernel.CloseHandle(handle)
            raise
        finally:
            if process:
                kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
