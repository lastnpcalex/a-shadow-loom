"""Independent Admin credentials, replay protection and persistent browser sessions.

State is external to Git/OneDrive. On Windows the TOTP seed is DPAPI protected
for the service account. This does not isolate it from processes of that user.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import os
import secrets
import sqlite3
import struct
import time
from contextlib import contextmanager

from exo_state import state_path

SESSION_DAYS = 30
IDLE_DAYS = 7
_secured_paths = set()


def _secure_directory(path):
    """Remove inherited access on the private Windows credential directory."""
    if os.name != 'nt' or str(path) in _secured_paths:
        return
    from ctypes import wintypes
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    adv.SetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    adv.SetNamedSecurityInfoW.restype = wintypes.DWORD
    token, size = wintypes.HANDLE(), wintypes.DWORD()
    sid_text, descriptor = wintypes.LPWSTR(), ctypes.c_void_p()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        user = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, user, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0]
        if not adv.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        sddl = f'D:P(A;OICI;FA;;;{sid_text.value})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
        if not adv.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        error = adv.SetNamedSecurityInfoW(str(path), 1, 4 | 0x80000000, None, None, dacl, None)
        if error:
            raise ctypes.WinError(error)
        _secured_paths.add(str(path))
    finally:
        if sid_text: kernel.LocalFree(sid_text)
        if descriptor: kernel.LocalFree(descriptor)
        kernel.CloseHandle(token)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _protect(data: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        # Non-Windows deployments depend on owner-only directory permissions.
        return data
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buf = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not operation(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(outgoing.data)


@contextmanager
def connection():
    path = state_path("admin-auth", "credentials.sqlite3")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _secure_directory(path.parent)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    if os.name != "nt":
        os.chmod(path, 0o600)
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS owner (
              id INTEGER PRIMARY KEY CHECK(id=1), password TEXT NOT NULL,
              seed BLOB NOT NULL, counter INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS pending (
              id INTEGER PRIMARY KEY CHECK(id=1), token TEXT NOT NULL,
              password TEXT NOT NULL, seed BLOB NOT NULL, expires REAL NOT NULL,
              counter INTEGER NOT NULL DEFAULT -1, codes TEXT);
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, label TEXT NOT NULL,
              created REAL NOT NULL, seen REAL NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS recovery (digest TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS attempts (
              bucket TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL);
        """)
        yield db
    finally:
        db.close()


def configured():
    with connection() as db:
        return db.execute("SELECT 1 FROM owner").fetchone() is not None


def password_hash(password):
    salt = secrets.token_bytes(32)
    key = hashlib.scrypt(password.encode(), salt=salt, n=2**17, r=8, p=1,
                         maxmem=256*1024*1024, dklen=32)
    return salt.hex() + ":" + key.hex()


def password_matches(password, encoded):
    salt, expected = encoded.split(":")
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**17,
                            r=8, p=1, maxmem=256*1024*1024, dklen=32)
    return hmac.compare_digest(actual.hex(), expected)


def totp(seed, counter):
    raw = hmac.digest(seed, struct.pack(">Q", counter), "sha1")
    offset = raw[-1] & 15
    return str((struct.unpack(">I", raw[offset:offset+4])[0] & 0x7fffffff) % 1000000).zfill(6)


def matching_counter(seed, code, last, now):
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        return None
    for counter in range(int(now // 30)-1, int(now // 30)+2):
        if counter > last and hmac.compare_digest(totp(seed, counter), code):
            return counter
    return None


def allow_attempt(bucket, now=None):
    now = time.time() if now is None else now
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM attempts WHERE started < ?", (now-300,))
        row = db.execute("SELECT count FROM attempts WHERE bucket=?", (bucket,)).fetchone()
        count = row[0] if row else 0
        if count >= 10:
            db.rollback()
            return False
        db.execute("INSERT INTO attempts VALUES (?, ?, 1) ON CONFLICT(bucket) DO UPDATE SET count=count+1",
                   (bucket, now))
        db.commit()
        return True


def start_setup(password, now=None):
    now = time.time() if now is None else now
    hashed = password_hash(password)
    seed, token = secrets.token_bytes(20), secrets.token_urlsafe(32)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM owner").fetchone():
            raise ValueError("Admin is already configured")
        db.execute("INSERT OR REPLACE INTO pending VALUES (1, ?, ?, ?, ?, -1, NULL)",
                   (_digest(token), hashed, _protect(seed), now+600))
        db.commit()
    return token, base64.b32encode(seed).decode().rstrip("=")


def confirm_setup(token, code, now=None):
    import json
    now = time.time() if now is None else now
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM pending WHERE token=? AND expires>?", (_digest(token), now)).fetchone()
        if not row or row["codes"] is not None:
            raise ValueError("Restart setup or check the Authy code")
        counter = matching_counter(_protect(row["seed"], True), code, row["counter"], now)
        if counter is None:
            raise ValueError("Restart setup or check the Authy code")
        codes = ["ADM-" + secrets.token_hex(16).upper() for _ in range(8)]
        db.execute("UPDATE pending SET counter=?, codes=? WHERE id=1",
                   (counter, json.dumps([_digest(c) for c in codes])))
        db.commit()
    return codes


def _issue(db, label, now):
    token, ident = secrets.token_urlsafe(32), secrets.token_hex(16)
    db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
               (ident, _digest(token), label[:120], now, now, now+SESSION_DAYS*86400))
    return token


def activate_setup(token, label, now=None):
    import json
    now = time.time() if now is None else now
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM pending WHERE token=? AND expires>?", (_digest(token), now)).fetchone()
        if not row or not row["codes"] or db.execute("SELECT 1 FROM owner").fetchone():
            raise ValueError("Restart setup")
        db.execute("INSERT INTO owner VALUES (1, ?, ?, ?)", (row["password"], row["seed"], row["counter"]))
        db.executemany("INSERT INTO recovery VALUES (?)", [(c,) for c in json.loads(row["codes"])])
        db.execute("DELETE FROM pending")
        session = _issue(db, label, now)
        db.commit()
        return session


def login(password, code, label, now=None):
    now = time.time() if now is None else now
    with connection() as db:
        row = db.execute("SELECT * FROM owner").fetchone()
        if not row or not password_matches(password, row["password"]):
            return None
        # Re-read under a write transaction so simultaneous OTP/recovery uses
        # cannot both succeed. Wrong passwords cannot consume recovery codes.
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM owner").fetchone()
        counter = matching_counter(_protect(row["seed"], True), code, row["counter"], now)
        if counter is not None:
            db.execute("UPDATE owner SET counter=? WHERE id=1", (counter,))
        else:
            if not db.execute("DELETE FROM recovery WHERE digest=?", (_digest(code.strip().upper()),)).rowcount:
                db.rollback()
                return None
        token = _issue(db, label, now)
        db.commit()
        return token


def session(token, now=None):
    if len(token) != 43:
        return None
    now = time.time() if now is None else now
    with connection() as db:
        row = db.execute("SELECT * FROM sessions WHERE token=? AND expires>? AND seen>?",
                         (_digest(token), now, now-IDLE_DAYS*86400)).fetchone()
        if not row:
            return None
        if now-row["seen"] > 300:
            db.execute("UPDATE sessions SET seen=? WHERE id=?", (now, row["id"]))
            db.commit()
        return {k: row[k] for k in ("id", "label", "created", "seen", "expires")}


def sessions():
    now = time.time()
    with connection() as db:
        db.execute("DELETE FROM sessions WHERE expires<=? OR seen<=?", (now, now-IDLE_DAYS*86400))
        db.commit()
        rows = db.execute("SELECT id, label, created, seen, expires FROM sessions ORDER BY created DESC").fetchall()
        return [dict(r) for r in rows], db.execute("SELECT count(*) FROM recovery").fetchone()[0]


def revoke(ident):
    with connection() as db:
        db.execute("DELETE FROM sessions WHERE id=?", (ident,))
        db.commit()


def rotate_recovery(password, code):
    """Fresh password + OTP proof; rotation cannot be authorized by a cookie alone."""
    now = time.time()
    with connection() as db:
        row = db.execute('SELECT * FROM owner').fetchone()
        if not row or not password_matches(password, row['password']):
            return None
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM owner').fetchone()
        counter = matching_counter(_protect(row['seed'], True), code, row['counter'], now)
        if counter is None:
            db.rollback()
            return None
        codes = ['ADM-' + secrets.token_hex(16).upper() for _ in range(8)]
        db.execute('UPDATE owner SET counter=? WHERE id=1', (counter,))
        db.execute('DELETE FROM recovery')
        db.executemany('INSERT INTO recovery VALUES (?)', [(_digest(c),) for c in codes])
        db.commit()
        return codes
