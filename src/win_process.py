"""Fast Windows process inventory and graceful, PID-scoped shutdown."""
import ctypes as c
from ctypes import wintypes as w
import os
import re


class Entry(c.Structure):
    _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD), ('heap', c.c_size_t),
                ('module', w.DWORD), ('threads', w.DWORD), ('parent', w.DWORD),
                ('priority', w.LONG), ('flags', w.DWORD), ('name', w.WCHAR * 260)]


def inventory():
    k = c.WinDLL('kernel32', use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [w.HANDLE, c.POINTER(Entry)]
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    snap = k.CreateToolhelp32Snapshot(2, 0)
    if snap == c.c_void_p(-1).value:
        raise RuntimeError('无法检查运行中的软件，请退出 Codex 后重试。')
    result = {}
    try:
        entry = Entry(); entry.size = c.sizeof(entry)
        if not k.Process32FirstW(snap, c.byref(entry)):
            raise RuntimeError('无法读取进程列表，已停止写入。')
        while True:
            result[entry.pid] = {'pid': entry.pid, 'parent': entry.parent, 'name': entry.name, 'path': ''}
            if not k.Process32NextW(snap, c.byref(entry)):
                if c.get_last_error() != 18:
                    raise RuntimeError('进程检查没有完成，已停止写入。')
                break
    finally:
        k.CloseHandle(snap)
    session = w.DWORD()
    if not k.ProcessIdToSessionId(os.getpid(), c.byref(session)):
        raise RuntimeError('无法确认当前 Windows 会话。')
    for pid, row in list(result.items()):
        other = w.DWORD()
        if not k.ProcessIdToSessionId(pid, c.byref(other)) or other.value != session.value:
            del result[pid]
            continue
        if row['name'].lower() in ('chatgpt.exe', 'codex.exe', 'codex-desktop.exe', 'codex-app.exe', 'openai codex.exe'):
            process = k.OpenProcess(0x1000, False, pid)
            if process:
                try:
                    buffer = c.create_unicode_buffer(32768); length = w.DWORD(len(buffer))
                    if k.QueryFullProcessImageNameW(process, 0, buffer, c.byref(length)):
                        row['path'] = buffer.value
                finally:
                    k.CloseHandle(process)
    return result


def identify(rows):
    found = {}
    writers = re.compile(r'^(codex|codex-app-server|codex-exec|codex-exec-server)\.exe$', re.I)
    desktop = {pid for pid, row in rows.items() if
               row['name'].lower() in ('codex-desktop.exe', 'codex-app.exe', 'openai codex.exe') or
               ('openai.codex' in row.get('path', '').lower() and row['name'].lower() in ('chatgpt.exe', 'codex.exe'))}
    for pid in desktop:
        row = rows[pid]
        if row['parent'] not in desktop:
            found[pid] = {**row, 'kind': 'desktop', 'label': 'Codex 桌面端'}
    for pid, row in rows.items():
        if not writers.fullmatch(row['name']) or pid in desktop:
            continue
        found[pid] = {**row, 'kind': 'cli', 'label': 'Codex 后台 / CLI'}
        parent, seen = row['parent'], set()
        while parent in rows and parent not in seen:
            seen.add(parent); ancestor = rows[parent]
            if parent in desktop:
                # Add the root desktop, not every renderer/utility child.
                if ancestor['parent'] not in desktop:
                    found[parent] = {**ancestor, 'kind': 'desktop', 'label': 'Codex 桌面端'}
            elif ancestor['name'].lower() in ('code.exe', 'cursor.exe', 'trae.exe', 'trae cn.exe', 'idea64.exe', 'pycharm64.exe'):
                found[parent] = {**ancestor, 'kind': 'ide', 'label': ancestor['name'] + '（Codex 会话）'}
            parent = ancestor['parent']
    return list(found.values())


def owners():
    return identify(inventory())


def close_windows(found):
    user = c.WinDLL('user32', use_last_error=True)
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    pids = {row['pid'] for row in found if row['kind'] in ('desktop', 'ide')}
    @callback_type
    def visit(hwnd, _):
        pid = w.DWORD(); user.GetWindowThreadProcessId(hwnd, c.byref(pid))
        if pid.value in pids:
            user.PostMessageW(hwnd, 0x10, 0, 0)  # WM_CLOSE, including tray-hidden windows.
        return True
    user.EnumWindows(visit, 0)


class UniqueProcess(c.Structure):
    _fields_ = [('pid', w.DWORD), ('started', w.FILETIME)]


def graceful_shutdown(found):
    """Restart Manager flags=0: never opt in to forced shutdown."""
    k = c.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; k.OpenProcess.restype = w.HANDLE
    k.GetProcessTimes.argtypes = [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4
    k.CloseHandle.argtypes = [w.HANDLE]
    entries = []
    for row in found:
        process = k.OpenProcess(0x1000, False, row['pid'])
        if not process:
            continue
        try:
            times = [w.FILETIME() for _ in range(4)]
            if k.GetProcessTimes(process, *(c.byref(t) for t in times)):
                entries.append(UniqueProcess(row['pid'], times[0]))
        finally:
            k.CloseHandle(process)
    if not entries:
        return
    rm = c.WinDLL('rstrtmgr', use_last_error=True)
    session = w.DWORD(); key = c.create_unicode_buffer(33)
    rm.RmStartSession.argtypes = [c.POINTER(w.DWORD), w.DWORD, w.LPWSTR]
    rm.RmRegisterResources.argtypes = [w.DWORD, w.UINT, c.c_void_p, w.UINT, c.POINTER(UniqueProcess), w.UINT, c.c_void_p]
    rm.RmShutdown.argtypes = [w.DWORD, w.DWORD, c.c_void_p]
    if rm.RmStartSession(c.byref(session), 0, key):
        return
    try:
        array = (UniqueProcess * len(entries))(*entries)
        if rm.RmRegisterResources(session.value, 0, None, len(entries), array, 0, None) == 0:
            rm.RmShutdown(session.value, 0, None)
    finally:
        rm.RmEndSession(session.value)
