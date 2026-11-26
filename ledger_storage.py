"""Validated, atomic, single-writer persistence for the local trade ledger."""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile


def read_snapshot(path):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def decode_records(raw):
    if raw is None:
        raise ValueError("İşlem dosyası bulunamadı.")
    try:
        records = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise ValueError("İşlem dosyası geçerli JSON içermiyor.") from error
    if not isinstance(records, list) or any(
        not isinstance(record, dict) or not ("quantity" in record or "amount_g" in record)
        for record in records
    ):
        raise ValueError("Dosya bir işlem kayıtları listesi içermeli.")
    return records


def encode_records(records):
    return json.dumps(records, indent=4, ensure_ascii=False, allow_nan=False).encode("utf-8")


@contextmanager
def ledger_write_lock(path):
    """Prevent two local application processes from replacing the same file."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create.restype = wintypes.HANDLE
    wait = kernel.WaitForSingleObject
    wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait.restype = wintypes.DWORD
    release = kernel.ReleaseMutex
    release.argtypes = (wintypes.HANDLE,)
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)

    identity = os.path.normcase(os.path.realpath(path)).encode("utf-8")
    handle = create(None, False, f"Local\\PiyasaLedger-{sha256(identity).hexdigest()}")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        status = wait(handle, 0)
        if status == 0x102:
            raise OSError("İşlem dosyası başka bir uygulama penceresinde kullanılıyor.")
        if status not in (0, 0x80):
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = True
        yield
    finally:
        if acquired:
            release(handle)
        close(handle)


def replace_snapshot(path, raw):
    destination = Path(path)
    temporary = tempfile.NamedTemporaryFile(
        mode="wb", dir=destination.resolve().parent, prefix=".transactions-", delete=False
    )
    try:
        with temporary:
            temporary.write(raw)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary.name, destination)
    finally:
        Path(temporary.name).unlink(missing_ok=True)
