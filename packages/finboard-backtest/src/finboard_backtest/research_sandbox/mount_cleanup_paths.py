"""按已验证的挂载目录身份读取并删除可重建输入。"""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path


class UnsafeMountPathError(OSError):
    """路径不是普通文件或目录, 或在检查期间移位。"""


def plain_stat(path: Path, *, directory: bool = False, dir_fd: int | None = None) -> os.stat_result:
    info = os.stat(path.name if dir_fd is not None else path, dir_fd=dir_fd, follow_symlinks=False)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or (not directory and info.st_nlink != 1)
    ):
        raise UnsafeMountPathError(str(path))
    return info


@contextmanager
def pin_mount(mount: Path) -> Iterator[tuple[int | None, int | None]]:
    """持有物理目录链, POSIX 返回挂载及上一级目录的 fd。"""
    directories = (*reversed(mount.parents), mount)
    with ExitStack() as stack:
        if sys.platform == "win32":
            # 持有不共享 DELETE 的目录句柄, 禁止祖先目录或挂载本身被替换。
            for path in directories:
                stack.enter_context(_windows_open(path, directory=True))
            yield None, None
            return
        fd = parent_fd = None
        for path in directories:
            parent_fd = fd
            fd = os.open(
                path.name if parent_fd is not None else path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            stack.callback(os.close, fd)
        yield fd, parent_fd


def unlink_input(path: Path, dir_fd: int | None) -> int:
    if sys.platform == "win32":
        return _windows_unlink(path)
    assert dir_fd is not None
    if not os.path.samestat(os.fstat(dir_fd), plain_stat(path.parent, directory=True)):
        raise UnsafeMountPathError(str(path.parent))
    size = plain_stat(path, dir_fd=dir_fd).st_size
    # unlinkat 不解析最后一段符号链接, 目录 fd 也不会被替换的路径重定向。
    os.unlink(path.name, dir_fd=dir_fd)
    return size


class _WindowsFileInfo(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("access_time", wintypes.FILETIME),
        ("write_time", wintypes.FILETIME),
        ("volume_serial", wintypes.DWORD),
        ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD),
        ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD),
        ("index_low", wintypes.DWORD),
    ]


@lru_cache(maxsize=1)
def _kernel32() -> ctypes.CDLL:
    if sys.platform != "win32":
        raise OSError("Win32 文件句柄仅在 Windows 上可用")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    api.CreateFileW.restype = wintypes.HANDLE
    api.GetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_WindowsFileInfo),
    )
    api.GetFileInformationByHandle.restype = wintypes.BOOL
    api.SetFileInformationByHandle.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    api.SetFileInformationByHandle.restype = wintypes.BOOL
    api.CloseHandle.argtypes = (wintypes.HANDLE,)
    api.CloseHandle.restype = wintypes.BOOL
    return api


@contextmanager
def _windows_open(path: Path, *, directory: bool) -> Iterator[tuple[int, _WindowsFileInfo]]:
    api = _kernel32()
    # CreateFileW 从绝对路径打开 reparse point 本身, 随后核验句柄属性。
    name = str(path)
    if not name.startswith("\\\\?\\"):
        name = "\\\\?\\UNC\\" + name[2:] if name.startswith("\\\\") else "\\\\?\\" + name
    file_list_directory = 0x0001
    delete = 0x00010000
    share_read_and_write = 0x00000003
    open_existing = 3
    backup_semantics_and_open_reparse_point = 0x02200000
    handle = api.CreateFileW(
        name,
        file_list_directory if directory else delete,
        share_read_and_write if directory else 0,
        None,
        open_existing,
        backup_semantics_and_open_reparse_point,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise _windows_error(path)
    try:
        info = _WindowsFileInfo()
        if not api.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise _windows_error(path)
        if (
            info.attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or bool(info.attributes & stat.FILE_ATTRIBUTE_DIRECTORY) != directory
            or (not directory and info.links != 1)
        ):
            raise UnsafeMountPathError(str(path))
        yield handle, info
    finally:
        api.CloseHandle(handle)


def _windows_unlink(path: Path) -> int:
    with _windows_open(path, directory=False) as (handle, info):
        file_disposition_info = 4
        delete_on_close = ctypes.c_ubyte(1)
        if not _kernel32().SetFileInformationByHandle(
            handle,
            file_disposition_info,
            ctypes.byref(delete_on_close),
            ctypes.sizeof(delete_on_close),
        ):
            raise _windows_error(path)
        return int(info.size_high) << 32 | int(info.size_low)


def _windows_error(path: Path) -> OSError:
    if sys.platform != "win32":
        return OSError("Win32 文件句柄仅在 Windows 上可用", str(path))
    code = ctypes.get_last_error()
    return OSError(0, ctypes.FormatError(code), str(path), code)
