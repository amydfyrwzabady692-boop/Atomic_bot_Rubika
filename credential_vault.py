"""ذخیره اطلاعات ورود اکانت فری‌فایر برای جم با اطلاعات.

اگر ACCOUNT_CREDENTIALS_KEY ست باشد Fernet؛ وگرنه JSON ساده.
"""
import json
import os

from cryptography.fernet import Fernet, InvalidToken


class CredentialVaultError(RuntimeError):
    pass


def _fernet_or_none():
    raw = (os.getenv("ACCOUNT_CREDENTIALS_KEY") or "").strip().encode("ascii")
    if not raw:
        return None
    try:
        return Fernet(raw)
    except (ValueError, TypeError):
        return None


def encrypt_credentials(identifier, password, note="", backup_code=""):
    payload = {
        "identifier": str(identifier or "").strip(),
        "password": str(password or ""),
        "backup_code": str(backup_code or "").strip(),
        "note": str(note or "").strip(),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    fernet = _fernet_or_none()
    if fernet is not None:
        return fernet.encrypt(raw).decode("ascii")
    return raw.decode("utf-8")


def decrypt_credentials(ciphertext):
    text = str(ciphertext or "").strip()
    if not text:
        raise CredentialVaultError("اطلاعات ورود خالی است.")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CredentialVaultError("اطلاعات ورود قابل خواندن نیست.") from exc
        return _normalize(data)
    fernet = _fernet_or_none()
    if fernet is None:
        raise CredentialVaultError(
            "این سفارش با کلید قدیمی رمز شده؛ ACCOUNT_CREDENTIALS_KEY را ست کن."
        )
    try:
        raw = fernet.decrypt(text.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CredentialVaultError("اطلاعات رمزگذاری‌شده قابل بازیابی نیست.") from exc
    return _normalize(data)


def _normalize(data):
    if not isinstance(data, dict):
        raise CredentialVaultError("فرمت اطلاعات ورود نامعتبر است.")
    return {
        "identifier": str(data.get("identifier") or ""),
        "password": str(data.get("password") or ""),
        "backup_code": str(data.get("backup_code") or ""),
        "note": str(data.get("note") or ""),
    }


def mask_identifier(identifier):
    text = str(identifier or "").strip()
    if len(text) <= 4:
        return "***"
    if "@" in text:
        name, _, domain = text.partition("@")
        keep = name[:2] if len(name) > 2 else name[:1]
        return f"{keep}***@{domain}"
    return f"{text[:2]}***{text[-2:]}"
