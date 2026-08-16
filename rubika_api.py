import asyncio
import json
import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


class RubikaAPIError(RuntimeError):
    pass


def _response_snippet(raw: str) -> str:
    return " ".join(raw.split())[:300]


class RubikaAPI:
    def __init__(self, token: str):
        self.base = f"https://botapi.rubika.ir/v3/{token}"
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        timeout = aiohttp.ClientTimeout(total=25)
        self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def call(self, method: str, payload: dict | None = None) -> dict:
        if not self.session:
            raise RuntimeError("Rubika client is not started")
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with self.session.post(
                    f"{self.base}/{method}", json=payload or {}
                ) as response:
                    raw = await response.text()
                    status = response.status
                    if not raw.strip():
                        raise RubikaAPIError(
                            f"Rubika empty body for {method} HTTP {status}"
                        )
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise RubikaAPIError(
                            f"Rubika non-JSON {method} HTTP {status}: "
                            f"{_response_snippet(raw)}"
                        ) from exc
                    if status >= 500:
                        raise RubikaAPIError(f"Rubika HTTP {status}: {data}")
                    if status >= 400 or not isinstance(data, dict):
                        raise RubikaAPIError(f"Rubika rejected {method}: {data}")
                    if str(data.get("status", "")).lower() in {"error", "failed"}:
                        raise RubikaAPIError(f"Rubika rejected {method}: {data}")
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError, RubikaAPIError) as exc:
                last_error = exc
                log.warning(
                    "Rubika %s attempt %s failed: %s",
                    method,
                    attempt + 1,
                    exc,
                )
                if attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
        raise RubikaAPIError(str(last_error))

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        chat_keypad: dict | None = None,
        inline_keypad: dict | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}
        if chat_keypad:
            payload.update(chat_keypad=chat_keypad, chat_keypad_type="New")
        if inline_keypad:
            payload["inline_keypad"] = inline_keypad
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        try:
            return await self.call("sendMessage", payload)
        except RubikaAPIError:
            if not inline_keypad and not chat_keypad:
                raise
            fallback: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text[:4000],
            }
            if reply_to_message_id:
                fallback["reply_to_message_id"] = reply_to_message_id
            if inline_keypad and chat_keypad:
                log.warning("sendMessage keypad failed; retrying without inline keypad")
                fallback.update(chat_keypad=chat_keypad, chat_keypad_type="New")
                try:
                    return await self.call("sendMessage", fallback)
                except RubikaAPIError:
                    log.warning("sendMessage chat keypad failed; retrying text-only")
                    fallback.pop("chat_keypad", None)
                    fallback.pop("chat_keypad_type", None)
                    return await self.call("sendMessage", fallback)
            log.warning("sendMessage keypad failed; retrying text-only")
            return await self.call("sendMessage", fallback)

    async def get_updates(self, offset_id: str | None = None) -> dict:
        payload: dict[str, Any] = {"limit": 100}
        if offset_id:
            payload["offset_id"] = offset_id
        return await self.call("getUpdates", payload)

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_chat(self, chat_id: str) -> dict:
        return await self.call("getChat", {"chat_id": chat_id})

    async def forward_message(self, from_chat_id: str, message_id: str, to_chat_id: str) -> dict:
        return await self.call(
            "forwardMessage",
            {
                "from_chat_id": from_chat_id,
                "message_id": message_id,
                "to_chat_id": to_chat_id,
            },
        )

    async def update_endpoint(self, url: str, endpoint_type: str) -> dict:
        return await self.call(
            "updateBotEndpoints",
            {"url": url, "type": endpoint_type},
        )

    async def set_bot_description(self, description: str) -> dict:
        """به‌روزرسانی توضیحات ربات (که در صفحه معرفی ربات نمایش داده می‌شود).

        از متد updateBotAttributes استفاده می‌شود که در Rubika Bot API برای
        به‌روزرسانی توضیحات/شروع‌نامه ربات موجود است.
        """
        return await self.call(
            "updateBotAttributes",
            {"bot_description": str(description)[:2000]},
        )


def normalize_event(payload: dict) -> dict | None:
    """Normalize official receiveUpdate and receiveInlineMessage payloads."""
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("inline_message"), dict):
        msg = payload["inline_message"]
        aux = msg.get("aux_data") or {}
        return {
            "event_id": f"inline:{msg.get('chat_id')}:{msg.get('message_id')}:{aux.get('button_id')}",
            "chat_id": str(msg.get("chat_id") or ""),
            "sender_id": str(msg.get("sender_id") or ""),
            "message_id": str(msg.get("message_id") or ""),
            "text": str(msg.get("text") or ""),
            "button_id": str(aux.get("button_id") or ""),
            "file": msg.get("file"),
        }
    update = payload.get("update") if isinstance(payload.get("update"), dict) else payload
    msg = update.get("new_message") if isinstance(update, dict) else None
    if not isinstance(msg, dict):
        return None
    aux = msg.get("aux_data") or {}
    chat_id = str(update.get("chat_id") or "")
    message_id = str(msg.get("message_id") or "")
    return {
        "event_id": f"update:{chat_id}:{message_id}:{update.get('type')}",
        "chat_id": chat_id,
        "sender_id": str(msg.get("sender_id") or ""),
        "message_id": message_id,
        "text": str(msg.get("text") or ""),
        "button_id": str(aux.get("button_id") or ""),
        "file": msg.get("file"),
    }
