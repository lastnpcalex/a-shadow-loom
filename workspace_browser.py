"""Read-only, bounded workspace inventory and exact-root Git summaries."""
from pathlib import Path
from contextlib import contextmanager
import os
import subprocess
import threading


class WorkspaceBrowseError(ValueError):
    pass


def _private(part: str) -> bool:
    name = part.lower()
    return name in {'.git', '.aws', '.ssh', '.codex', '.agents', '.venv', 'node_modules'} or name.startswith('.env') or name.endswith(('.pem', '.key'))


@contextmanager
def _directory_scan(root: Path, path: Path):
    """Pin the directory traversal rather than check a path then reopen it."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        info = kernel.GetFileInformationByHandleEx
        info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        info.restype = wintypes.BOOL
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL

        class Attributes(ctypes.Structure):
            _fields_ = [('attributes', wintypes.DWORD), ('tag', wintypes.DWORD)]

        handles = []
        # Share reads only: ancestors cannot be renamed, replaced, or opened
        # for reparse-point mutation while scandir uses the pinned path.
        target = root / path
        try:
            for directory in [*reversed(target.parents), target]:
                handle = create(str(directory), 0x80000000, 1, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise WorkspaceBrowseError('Directory cannot be safely opened')
                handles.append(handle)
                attributes = Attributes()
                if not info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
                    raise WorkspaceBrowseError('Directory identity is unavailable')
                # Name-surrogate reparse points include symlinks and junctions;
                # cloud-provider metadata reparse points are not redirected paths.
                if not attributes.attributes & 0x10 or attributes.tag & 0x20000000:
                    raise WorkspaceBrowseError('Linked directories cannot be browsed')
            with os.scandir(target) as scan:
                yield scan
        finally:
            for handle in reversed(handles):
                close(handle)
    else:
        handles = []
        try:
            cursor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            handles.append(cursor)
            for part in [*root.parts[1:], *path.parts]:
                cursor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=cursor)
                handles.append(cursor)
            with os.scandir(cursor) as scan:
                yield scan
        finally:
            for handle in reversed(handles):
                os.close(handle)


def list_workspace_directory(workspace: str, relative: str = '', limit: int = 500) -> dict:
    root = Path(workspace).resolve()
    path = Path(relative)
    if path.anchor or any(part == '..' or part.endswith(('.', ' ')) or ':' in part or _private(part) for part in path.parts):
        raise WorkspaceBrowseError('Choose a public relative workspace directory')
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_dir():
        raise WorkspaceBrowseError('Directory is outside the workspace or unavailable')
    if any(_private(part) for part in target.relative_to(root).parts):
        raise WorkspaceBrowseError('Choose a public relative workspace directory')
    # Do not walk symlinks/junctions, even when their destination is inside root.
    cursor = root
    for part in path.parts:
        cursor = cursor / part
        if cursor.is_symlink() or cursor.is_junction():
            raise WorkspaceBrowseError('Linked directories cannot be browsed')
    entries = []
    complete = True
    with _directory_scan(root, path) as scan:
        for index, entry in enumerate(scan):
            if index >= limit:
                complete = False
                break
            candidate = target / entry.name
            stat = entry.stat(follow_symlinks=False)
            if _private(entry.name) or entry.is_symlink() or getattr(stat, 'st_reparse_tag', 0) & 0x20000000:
                continue
            directory = entry.is_dir(follow_symlinks=False)
            entries.append({'name': entry.name, 'path': candidate.relative_to(root).as_posix(),
                            'directory': directory, 'size': None if directory else stat.st_size})
    entries.sort(key=lambda row: (not row['directory'], row['name'].lower()))
    return {'path': target.relative_to(root).as_posix(), 'entries': entries, 'complete': complete}


def _read_git(command: list[str], cap: int = 32000) -> tuple[str, bool]:
    """Bound both pipes in memory; terminate and reap on overflow or timeout."""
    kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs) as process:
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()

        def drain(pipe, output):
            while chunk := pipe.read(4096):
                available = cap - len(output)
                output.extend(chunk[:available])
                if len(chunk) > available:
                    overflow.set()
                    process.kill()
                    break

        threads = [threading.Thread(target=drain, args=(pipe, output), daemon=True)
                   for pipe, output in zip((process.stdout, process.stderr), buffers)]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        finally:
            for thread in threads:
                thread.join()
        if process.returncode and not overflow.is_set():
            raise WorkspaceBrowseError('Git information is unavailable for this workspace')
        return buffers[0].decode('utf-8', errors='replace'), not overflow.is_set()


def git_workspace_summary(workspace: str) -> dict:
    root = Path(workspace).resolve()

    def read(*args):
        return _read_git(['git', '-C', str(root), '--no-optional-locks',
                          '-c', 'core.fsmonitor=false', *args])

    try:
        top, complete = read('rev-parse', '--show-toplevel')
        if not complete:
            raise WorkspaceBrowseError('Git root exceeds the inspection limit')
        top = top.strip()
    except WorkspaceBrowseError:
        return {'repository': False, 'detail': 'This workspace has no Git repository.'}
    if Path(top).resolve() != root:
        return {'repository': False, 'detail': 'This workspace has no independent Git repository; its parent repository is not shown.'}
    status, status_complete = read('status', '--porcelain=v1', '--untracked-files=normal')
    diff, diff_complete = read('diff', '--stat', '--no-ext-diff', '--no-textconv')
    try:
        history, history_complete = read('log', '-8', '--format=%h %s', '--no-decorate')
    except WorkspaceBrowseError:
        history = 'No commits yet.'
        history_complete = True
    cap = 32000
    return {'repository': True, 'root': str(root), 'status': status[:cap],
            'diff_stat': diff[:cap], 'history': history[:cap],
            'complete': status_complete and diff_complete and history_complete}
