"""Night mode Telegram reporter — message-only (no polling commands)

Night mode shares the same Telegram bot/chat as kitty.
To avoid conflicts, night mode only sends messages and does NOT start polling.
"""
from typing import Any

from telegram import Bot

from kitty_night.config import night_settings
from kitty_night.utils import logger


class NightTelegramReporter:
    """Night mode Telegram message sender"""

    def __init__(self) -> None:
        self._bot: Bot | None = None

    def build(self) -> "NightTelegramReporter":
        if night_settings.telegram_bot_token and night_settings.telegram_chat_id:
            self._bot = Bot(token=night_settings.telegram_bot_token)
        else:
            logger.warning("[Night:Telegram] Bot token or chat_id not configured — messages disabled")
        return self

    async def send(self, message: str) -> None:
        if self._bot is None:
            return
        try:
            await self._bot.send_message(
                chat_id=night_settings.telegram_chat_id,
                text=message,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"[Night:Telegram] Send failed: {e}")

    async def report_trade(
        self, action: str, symbol: str, quantity: int, price: float, reason: str, name: str = "",
    ) -> None:
        emoji = "🟢" if action == "BUY" else "🔴"
        price_str = f"${price:,.2f}" if price > 0 else "market"
        label = f"{name}({symbol})" if name else symbol
        msg = (
            f"{emoji} *Night {action}*\n"
            f"`{label}` × {quantity} shares @ {price_str}\n"
            f"_{reason}_"
        )
        await self.send(msg)

    async def report_error(self, error: str) -> None:
        await self.send(f"⚠️ *Night Mode Error*\n```\n{error[:500]}\n```")
