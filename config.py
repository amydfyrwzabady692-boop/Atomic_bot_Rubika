import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


@dataclass(frozen=True)
class Settings:
    token: str
    mode: str
    webhook_secret: str
    admin_id: str
    admin_chat_id: str
    database_url: str
    web_port: int
    callback_base: str
    payment_ttl_minutes: int

    @classmethod
    def load(cls) -> "Settings":
        mode = os.getenv("RUBIKA_MODE", "polling").strip().lower()
        if mode not in {"polling", "webhook"}:
            raise RuntimeError("RUBIKA_MODE must be polling or webhook")
        db_url = os.getenv("DATABASE_URL", "").strip() or (
            f"postgresql://{_required('DB_USER')}:{_required('DB_PASSWORD')}"
            f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}"
            f"/{os.getenv('DB_NAME', 'atomic_rubika')}"
        )
        callback = _required("PAYMENT_CALLBACK_BASE").rstrip("/")
        if not callback.startswith("https://"):
            raise RuntimeError("PAYMENT_CALLBACK_BASE must be HTTPS")
        return cls(
            token=_required("RUBIKA_BOT_TOKEN"),
            mode=mode,
            webhook_secret=_required("RUBIKA_WEBHOOK_SECRET"),
            admin_id=_required("RUBIKA_ADMIN_ID"),
            admin_chat_id=_required("RUBIKA_ADMIN_CHAT_ID"),
            database_url=db_url,
            web_port=int(os.getenv("WEB_PORT", "8081")),
            callback_base=callback,
            payment_ttl_minutes=max(5, min(60, int(os.getenv("ORDER_PAYMENT_TTL_MINUTES", "15")))),
        )
