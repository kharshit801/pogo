"""Windows Credential Manager: where pogo keeps the proxy password on Windows
(the counterpart of the macOS Keychain). Visible under Control Panel → Credential Manager →
Windows Credentials as "pogo:<user>@<proxy>"."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
CRED_MAX_CREDENTIAL_BLOB_SIZE = 5 * 512


class CREDENTIALW(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]


def _advapi32():
    dll = ctypes.WinDLL("advapi32", use_last_error=True)
    dll.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    dll.CredWriteW.restype = wintypes.BOOL
    dll.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    dll.CredReadW.restype = wintypes.BOOL
    dll.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    dll.CredDeleteW.restype = wintypes.BOOL
    dll.CredFree.argtypes = [ctypes.c_void_p]
    dll.CredFree.restype = None
    return dll


def target(account: str) -> str:
    return f"pogo:{account}"


def write(account: str, username: str, password: str) -> None:
    blob = password.encode("utf-16-le")
    if len(blob) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
        raise ValueError("that password is too long for Windows Credential Manager")
    buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob) if blob else None
    credential = CREDENTIALW()
    credential.Type = CRED_TYPE_GENERIC
    credential.TargetName = target(account)
    credential.UserName = username
    credential.CredentialBlobSize = len(blob)
    credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)) if buffer else None
    credential.Persist = CRED_PERSIST_LOCAL_MACHINE
    if not _advapi32().CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def read(account: str) -> str:
    dll = _advapi32()
    pointer = ctypes.POINTER(CREDENTIALW)()
    if not dll.CredReadW(target(account), CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        return ""  # usually ERROR_NOT_FOUND: nothing saved yet
    try:
        credential = pointer.contents
        if not credential.CredentialBlobSize:
            return ""
        return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize).decode("utf-16-le")
    finally:
        dll.CredFree(pointer)


def delete(account: str) -> None:
    _advapi32().CredDeleteW(target(account), CRED_TYPE_GENERIC, 0)
