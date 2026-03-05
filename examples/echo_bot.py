import asyncio
import html
import logging
import os
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

# Allow running this file directly without installing the package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from botadslib import BotAdsClient, BotAdsError
from botadslib.models import AdItem


FAKE_BROADCAST_RECIPIENT_ID = 99999999999999
ACTION_PLACEMENT = "after_echo"
BROADCAST_PLACEMENT = "manual_broadcast"
BROADCAST_START_CALLBACK = "broadcast_start"


@dataclass(slots=True)
class PendingBroadcast:
    ad_item: AdItem
    recipients: list[str]
    stage: str = "ready"


router = Router()
_ads_client: BotAdsClient | None = None
_admin_chat_id: int | None = None
_pending_broadcasts: dict[int, PendingBroadcast] = {}


class AwaitingBroadcastConfirm(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False
        pending = _pending_broadcasts.get(message.from_user.id)
        return bool(pending and pending.stage == "confirm")


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def get_ads_client() -> BotAdsClient:
    if _ads_client is None:
        raise RuntimeError("BotAdsClient is not initialized")
    return _ads_client


def get_optional_int_env(name: str) -> int | None:
    value = os.getenv(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


async def notify_admin(bot: Bot, *, context: str, error: Exception | str) -> None:
    details = f"[BotAds] {context}\n{error}"
    logging.error(details)
    if _admin_chat_id is None:
        return
    try:
        await bot.send_message(chat_id=_admin_chat_id, text=details)
    except Exception:  # noqa: BLE001
        logging.exception("Failed to send admin notification to BOT_ADMIN_CHAT_ID")


def _chunk_text(text: str, max_size: int = 3800) -> list[str]:
    if len(text) <= max_size:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_size:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, max_size)
        if split_at <= 0:
            split_at = max_size
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


def _readable(value: Any, *, empty: str = "not set") -> str:
    if value is None:
        return empty
    if isinstance(value, str) and not value.strip():
        return empty
    return str(value)


def _to_number(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return default
    return default


def _status_icon(status: Any) -> str:
    value = str(status or "").strip().lower()
    if value in {"active", "approved", "completed"}:
        return "✅"
    if value in {"pending", "processing"}:
        return "⏳"
    if value in {"rejected", "inactive", "paused", "expired", "failed", "disabled"}:
        return "❌"
    return "ℹ️"


def _campaign_status_ua(status: Any) -> str:
    value = str(status or "").strip().lower()
    mapping = {
        "active": "активна",
        "inactive": "неактивна",
        "paused": "на паузі",
        "draft": "чернетка",
        "completed": "завершена",
    }
    return mapping.get(value, value or "невідомо")


def _format_money(value: Any) -> str:
    if value is None or value == "":
        return "$0"
    try:
        amount = Decimal(str(value))
        amount = amount.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        text = f"{amount:.6f}".rstrip("0").rstrip(".")
        if text in {"", "-0"}:
            text = "0"
        return f"${text}"
    except (InvalidOperation, ValueError):
        return f"${value}"


def _format_pricing(pricing: dict[str, Any]) -> str:
    if pricing.get("cpm") not in (None, ""):
        return f"CPM ${pricing.get('cpm')}"
    if pricing.get("cpc") not in (None, ""):
        return f"CPC ${pricing.get('cpc')}"
    return "CPM $0"


def _render_compact_campaign(campaign: dict[str, Any]) -> str:
    name = html.escape(_readable(campaign.get("name"), empty="Unnamed campaign"))
    status = campaign.get("status")
    status_icon = _status_icon(status)
    status_text = _campaign_status_ua(status)

    ads = campaign.get("ads") if isinstance(campaign.get("ads"), list) else []
    lines = [f"📣 <b>{name}</b> — {status_icon} {status_text} ({len(ads)} оголошення)"]

    for ad in ads:
        if not isinstance(ad, dict):
            continue
        ad_status_icon = _status_icon(ad.get("status"))
        ad_name = html.escape(_readable(ad.get("name"), empty="ad"))
        stats = ad.get("stats") if isinstance(ad.get("stats"), dict) else {}
        pricing = ad.get("pricing") if isinstance(ad.get("pricing"), dict) else {}
        approval = ad.get("botApproval") if isinstance(ad.get("botApproval"), dict) else {}
        bot_icon = _status_icon(approval.get("status"))

        impressions = _to_number(stats.get("impressions"), default=0)
        spent = _format_money(stats.get("spent"))
        pricing_text = _format_pricing(pricing)

        lines.append(
            f"{ad_status_icon} <b>{ad_name}</b>: "
            f"{impressions}👁️ / {spent} • {pricing_text} • bot {bot_icon}"
        )

    return "\n".join(lines)


def _nested_get(data: dict[str, Any], path: tuple[str, ...]) -> Any | None:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _pick_first(data: dict[str, Any], paths: list[tuple[str, ...]]) -> Any | None:
    for path in paths:
        value = _nested_get(data, path)
        if value is not None and value != "":
            return value
    return None


def _format_price(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, dict):
        amount = value.get("amount") or value.get("value")
        currency = value.get("currency") or ""
        if amount is not None:
            return f"{amount} {currency}".strip()
    return str(value)


def _extract_campaign_name(ad_item: AdItem) -> str:
    raw = ad_item.raw
    value = _pick_first(
        raw,
        [
            ("campaignName",),
            ("campaignTitle",),
            ("campaign", "name"),
            ("campaign", "title"),
            ("campaignMeta", "name"),
            ("campaignMeta", "title"),
        ],
    )
    return str(value) if value is not None else "N/A"


def _extract_campaign_price(ad_item: AdItem) -> str:
    raw = ad_item.raw
    value = _pick_first(
        raw,
        [
            ("price",),
            ("campaignPrice",),
            ("cpm",),
            ("cpc",),
            ("cost",),
            ("campaign", "price"),
            ("campaign", "cpm"),
            ("campaign", "cpc"),
        ],
    )
    return _format_price(value)


def _extract_planned_count(ad_item: AdItem) -> str:
    if ad_item.broadcast_batch and ad_item.broadcast_batch.recipients_count:
        return str(ad_item.broadcast_batch.recipients_count)

    raw = ad_item.raw
    value = _pick_first(
        raw,
        [
            ("plannedRecipientsCount",),
            ("targetRecipientsCount",),
            ("campaign", "plannedRecipientsCount"),
            ("campaign", "targetRecipientsCount"),
        ],
    )
    return str(value) if value is not None else "N/A"


def _build_start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Start broadcast", callback_data=BROADCAST_START_CALLBACK)]
        ]
    )


def _build_confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Yes"), KeyboardButton(text="No")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def _send_broadcast(
    *,
    message: Message,
    bot: Bot,
    ads: BotAdsClient,
    pending: PendingBroadcast,
) -> None:
    ad_item = pending.ad_item
    recipients = pending.recipients

    delivered: list[str] = []
    failed: list[str] = []
    errors: dict[str, str] = {}

    if ads.ad_has_media(ad_item):
        try:
            cached_message = await ads.cache_ad_message(ad_item=ad_item)
        except Exception as exc:  # noqa: BLE001
            await notify_admin(bot, context="Failed to cache ad media in service channel", error=exc)
            await message.answer("Broadcast is temporarily unavailable.")
            return

        for recipient in recipients:
            try:
                await bot.copy_message(
                    chat_id=BotAdsClient.to_chat_id(recipient),
                    from_chat_id=ads.cache_channel_id,
                    message_id=cached_message.message_id,
                )
                delivered.append(recipient)
            except TelegramAPIError as exc:
                failed.append(recipient)
                errors[recipient] = str(exc)
            except Exception as exc:  # noqa: BLE001
                failed.append(recipient)
                errors[recipient] = str(exc)
            await asyncio.sleep(0.05)
    else:
        for recipient in recipients:
            try:
                await ads.send_ad_to_chat(
                    chat_id=BotAdsClient.to_chat_id(recipient),
                    ad_item=ad_item,
                )
                delivered.append(recipient)
            except TelegramAPIError as exc:
                failed.append(recipient)
                errors[recipient] = str(exc)
            except Exception as exc:  # noqa: BLE001
                failed.append(recipient)
                errors[recipient] = str(exc)
            await asyncio.sleep(0.05)

    assert ad_item.broadcast_batch is not None
    try:
        await ads.submit_broadcast_result(
            batch_id=ad_item.broadcast_batch.batch_id,
            delivered_recipient_telegram_ids=delivered,
            failed_recipient_telegram_ids=failed,
        )
    except BotAdsError as exc:
        await notify_admin(bot, context="Broadcast batch submit failed", error=exc)
        await message.answer("Broadcast was sent, but final confirmation failed.")
        return

    delivered_list = ", ".join(delivered) if delivered else "-"
    failed_list = ", ".join(failed) if failed else "-"
    await message.answer(
        "Broadcast completed.\n"
        f"Successful: {len(delivered)}\n"
        f"Failed: {len(failed)}\n"
        f"Successful IDs: {delivered_list}\n"
        f"Failed IDs: {failed_list}"
    )

    if errors:
        error_details = "\n".join(f"{recipient}: {error}" for recipient, error in errors.items())
        await notify_admin(bot, context="Broadcast delivery errors", error=error_details)


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(
        "Hi. This is an echo bot with ad delivery.\n"
        "- Send any text, I will echo it and then try to send an action ad.\n"
        "- Use /ads to see active campaigns.\n"
        "- Use /broadcast to run a test broadcast flow."
    )


@router.message(Command("ads"))
async def handle_ads(message: Message, bot: Bot) -> None:
    ads = get_ads_client()
    try:
        campaigns = await ads.get_campaigns(include_inactive=False)
    except BotAdsError as exc:
        await notify_admin(bot, context="Campaigns request failed", error=exc)
        await message.answer("Ads are temporarily unavailable.")
        return

    if not campaigns:
        await message.answer(
            "Немає активних кампаній для вашого бота.\n\n"
            "<b>запуск розсилки</b> - /broadcast",
            parse_mode="HTML",
        )
        return

    blocks = [
        _render_compact_campaign(campaign)
        for campaign in campaigns
        if isinstance(campaign, dict)
    ]
    text = "\n\n".join(blocks)
    for chunk in _chunk_text(text):
        await message.answer(chunk, parse_mode="HTML")

    await message.answer("<b>запуск розсилки</b> - /broadcast", parse_mode="HTML")


@router.message(Command("broadcast"))
async def handle_broadcast(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return

    ads = get_ads_client()
    recipients = [message.from_user.id, FAKE_BROADCAST_RECIPIENT_ID]

    try:
        ad_item, normalized_recipients = await ads.get_broadcast_ad(
            recipient_telegram_ids=recipients,
            placement=BROADCAST_PLACEMENT,
        )
    except BotAdsError as exc:
        await notify_admin(bot, context="Broadcast ad request failed", error=exc)
        await message.answer("Broadcast is temporarily unavailable.")
        return

    if not ad_item:
        await message.answer("No available campaigns for your bot...")
        return

    _pending_broadcasts[message.from_user.id] = PendingBroadcast(
        ad_item=ad_item,
        recipients=normalized_recipients,
        stage="ready",
    )

    try:
        await ads.send_ad_to_chat(chat_id=message.chat.id, ad_item=ad_item)
    except Exception as exc:  # noqa: BLE001
        await notify_admin(bot, context="Failed to send broadcast preview", error=exc)
        await message.answer("Broadcast preview is temporarily unavailable.")
        return

    await message.answer(
        "Broadcast campaign found:\n"
        f"Campaign: {_extract_campaign_name(ad_item)}\n"
        f"Price: {_extract_campaign_price(ad_item)}\n"
        f"Planned recipients: {_extract_planned_count(ad_item)}\n"
        "Press the button to continue.",
        reply_markup=_build_start_keyboard(),
    )


@router.callback_query(F.data == BROADCAST_START_CALLBACK)
async def handle_broadcast_start_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.from_user:
        return

    pending = _pending_broadcasts.get(callback.from_user.id)
    if pending is None:
        if callback.message:
            await callback.message.answer("No pending broadcast. Use /broadcast first.")
        return

    pending.stage = "confirm"
    if callback.message:
        await callback.message.answer(
            "Are you sure you want to start this broadcast?",
            reply_markup=_build_confirm_keyboard(),
        )


@router.message(AwaitingBroadcastConfirm(), F.text)
async def handle_broadcast_confirmation(message: Message, bot: Bot) -> None:
    if not message.from_user or not message.text:
        return

    pending = _pending_broadcasts.get(message.from_user.id)
    if pending is None or pending.stage != "confirm":
        return

    decision = message.text.strip().lower()
    if decision not in {"yes", "no"}:
        await message.answer(
            "Please use the confirmation buttons: Yes or No.",
            reply_markup=_build_confirm_keyboard(),
        )
        return

    if decision == "no":
        _pending_broadcasts.pop(message.from_user.id, None)
        await message.answer("Broadcast canceled.", reply_markup=ReplyKeyboardRemove())
        return

    _pending_broadcasts.pop(message.from_user.id, None)
    await message.answer("Starting broadcast...", reply_markup=ReplyKeyboardRemove())
    await _send_broadcast(message=message, bot=bot, ads=get_ads_client(), pending=pending)


@router.message(F.text, ~CommandStart(), ~Command("broadcast"), ~Command("ads"))
async def handle_echo_with_action_ad(message: Message, bot: Bot) -> None:
    if not message.from_user or not message.text:
        return

    await message.answer(message.text)

    ads = get_ads_client()
    try:
        result = await ads.send_action_advert(
            recipient_telegram_id=message.from_user.id,
            placement=ACTION_PLACEMENT,
        )
    except BotAdsError as exc:
        await notify_admin(bot, context="Action ad request failed", error=exc)
        return

    if result.has_ad and result.error:
        await notify_admin(bot, context="Action ad send failed", error=result.error)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)

    token = require_env("BOT_TOKEN")
    ads_api_base_url = require_env("ADS_API_BASE_URL")
    ads_bot_hash = require_env("ADS_BOT_HASH")
    ads_cache_channel_id = int(require_env("ADS_CACHE_CHANNEL_ID"))
    ads_media_cache_path = os.getenv("ADS_MEDIA_CACHE_PATH", ".botads-media-cache.json")
    admin_chat_id = get_optional_int_env("BOT_ADMIN_CHAT_ID")

    bot = Bot(token=token)
    global _ads_client
    global _admin_chat_id
    _admin_chat_id = admin_chat_id
    _ads_client = BotAdsClient(
        bot=bot,
        api_base_url=ads_api_base_url,
        bot_hash=ads_bot_hash,
        cache_channel_id=ads_cache_channel_id,
        media_cache_path=ads_media_cache_path,
    )

    dp = Dispatcher()
    dp.include_router(router)

    try:
        await dp.start_polling(bot)
    finally:
        await _ads_client.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
