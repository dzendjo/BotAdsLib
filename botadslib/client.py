from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Message, MessageEntity

from .exceptions import (
    BotAdsAPIError,
    BotAdsConfigurationError,
    BotAdsError,
    BotAdsNetworkError,
    CacheChannelAccessError,
    BroadcastBatchSubmitError,
)
from .models import (
    ActionAdvertResult,
    AdItem,
    BroadcastAdvertResult,
    CachedMediaEntry,
)


class BotAdsClient:
    """
    Aiogram integration client for ad delivery platform.

    Parameters
    ----------
    bot:
        aiogram Bot instance used to send ads.
    api_base_url:
        Base URL of delivery API, e.g. ``https://app.example.com``.
    bot_hash:
        Publisher bot hash from ad platform.
    cache_channel_id:
        Telegram channel ID where media is uploaded once to resolve stable ``file_id``.
    media_cache_path:
        Optional JSON path for persisting resolved ``storage_url -> file_id`` cache.
    """

    def __init__(
        self,
        bot: Bot,
        api_base_url: str,
        bot_hash: str,
        cache_channel_id: int | str,
        *,
        session: aiohttp.ClientSession | None = None,
        next_timeout_seconds: float = 1.2,
        batch_timeout_seconds: float = 1.8,
        max_api_retries: int = 2,
        retry_backoff_seconds: float = 0.35,
        broadcast_concurrency: int = 10,
        media_cache_path: str | None = None,
    ) -> None:
        if not api_base_url:
            raise ValueError("api_base_url is required")
        if not bot_hash:
            raise ValueError("bot_hash is required")

        self.bot = bot
        self.api_base_url = api_base_url.rstrip("/")
        self.bot_hash = bot_hash
        self.cache_channel_id = cache_channel_id

        self.next_timeout_seconds = next_timeout_seconds
        self.batch_timeout_seconds = batch_timeout_seconds
        self.max_api_retries = max_api_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.broadcast_concurrency = max(1, broadcast_concurrency)
        self.media_cache_path = Path(media_cache_path).expanduser() if media_cache_path else None

        self._session = session
        self._owns_session = session is None

        self._media_cache: dict[tuple[str, str], CachedMediaEntry] = {}
        self._media_cache_lock = asyncio.Lock()
        self._configuration_lock = asyncio.Lock()
        self._configuration_validated = False
        self._load_media_cache()

    async def __aenter__(self) -> "BotAdsClient":
        await self.ensure_configuration()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    def clear_media_cache(self) -> None:
        self._media_cache.clear()
        self._persist_media_cache()

    async def ensure_configuration(self, *, force: bool = False) -> None:
        if self._configuration_validated and not force:
            return

        async with self._configuration_lock:
            if self._configuration_validated and not force:
                return

            await self._ensure_session()
            me = await self.bot.get_me()

            await self._validate_cache_channel(bot_user_id=me.id)
            await self._validate_bot_hash(bot_user_id=me.id)
            self._configuration_validated = True

    async def get_action_ad(
        self,
        *,
        recipient_telegram_id: int | str,
        placement: str,
        language: str | None = None,
    ) -> AdItem | None:
        await self.ensure_configuration()
        recipient = str(recipient_telegram_id)
        language_code = self._normalize_language_code(language)
        payload = {
            "deliveryType": "action",
            "recipientTelegramId": recipient,
            "placement": placement,
        }
        if language_code:
            payload["language"] = language_code
        response = await self._fetch_next_ad(
            payload=payload,
            delivery_type="action",
            language=language_code,
        )
        item_data = response.get("item") if isinstance(response, dict) else None
        if not item_data:
            return None
        return AdItem.from_api(item_data)

    async def get_broadcast_ad(
        self,
        *,
        recipient_telegram_ids: Sequence[int | str],
        placement: str,
        language: str | None = None,
    ) -> tuple[AdItem | None, list[str]]:
        await self.ensure_configuration()
        recipients = self._normalize_recipient_ids(recipient_telegram_ids)
        language_code = self._normalize_language_code(language)
        if not recipients:
            raise ValueError("recipient_telegram_ids must contain at least one ID")
        if len(recipients) > 100:
            raise ValueError("recipient_telegram_ids cannot contain more than 100 IDs")

        payload = {
            "deliveryType": "broadcast",
            "recipientTelegramIds": recipients,
            "placement": placement,
        }
        if language_code:
            payload["language"] = language_code
        response = await self._fetch_next_ad(
            payload=payload,
            delivery_type="broadcast",
            language=language_code,
        )
        item_data = response.get("item") if isinstance(response, dict) else None
        if not item_data:
            return None, recipients

        ad_item = AdItem.from_api(item_data)
        if not ad_item.broadcast_batch or not ad_item.broadcast_batch.batch_id:
            raise BotAdsError("broadcastBatch.batchId is missing in broadcast ad response")
        return ad_item, recipients

    async def send_ad_to_chat(
        self,
        *,
        chat_id: int | str,
        ad_item: AdItem,
    ) -> tuple[Message, bool]:
        await self.ensure_configuration()
        return await self._send_creative(chat_id=chat_id, ad_item=ad_item)

    async def cache_ad_message(self, *, ad_item: AdItem) -> Message:
        await self.ensure_configuration()
        message, _ = await self._send_creative(
            chat_id=self.cache_channel_id,
            ad_item=ad_item,
        )
        return message

    def ad_has_media(self, ad_item: AdItem) -> bool:
        media_type, storage_url = self._resolve_media(ad_item.creative)
        return bool(media_type and storage_url)

    @staticmethod
    def to_chat_id(recipient: int | str) -> int | str:
        value = str(recipient)
        if value.startswith("-") and value[1:].isdigit():
            return int(value)
        if value.isdigit():
            return int(value)
        return value

    @classmethod
    def segment_broadcast_recipients_by_language(
        cls,
        *,
        recipients: Sequence[tuple[int | str, str | None]],
        fallback_language: str = "en",
        max_chunk_size: int = 100,
    ) -> list[tuple[str, list[str]]]:
        """
        Group recipients by base language and split each group into API-safe chunks.

        Returns list of ``(language, recipient_ids_chunk)`` pairs where each chunk size
        is at most ``max_chunk_size`` (and never more than 100, per API contract).
        """
        if max_chunk_size < 1:
            raise ValueError("max_chunk_size must be greater than 0")
        if max_chunk_size > 100:
            raise ValueError("max_chunk_size cannot be greater than 100")

        normalized_fallback = cls._normalize_language_code(fallback_language)
        assert normalized_fallback is not None

        grouped: dict[str, list[str]] = {}
        seen_recipients: set[str] = set()

        for recipient, raw_language in recipients:
            recipient_id = str(recipient)
            if recipient_id in seen_recipients:
                continue
            seen_recipients.add(recipient_id)

            language = normalized_fallback
            if raw_language:
                base_language = raw_language.strip().lower().replace("_", "-").split("-", 1)[0]
                try:
                    language = cls._normalize_language_code(base_language) or normalized_fallback
                except ValueError:
                    language = normalized_fallback

            grouped.setdefault(language, []).append(recipient_id)

        chunks: list[tuple[str, list[str]]] = []
        for language, ids in grouped.items():
            for start in range(0, len(ids), max_chunk_size):
                chunks.append((language, ids[start:start + max_chunk_size]))
        return chunks

    async def send_action_advert(
        self,
        *,
        recipient_telegram_id: int | str,
        placement: str,
        language: str | None = None,
    ) -> ActionAdvertResult:
        recipient = str(recipient_telegram_id)
        ad_item = await self.get_action_ad(
            recipient_telegram_id=recipient,
            placement=placement,
            language=language,
        )
        if not ad_item:
            return ActionAdvertResult(has_ad=False, recipient_telegram_id=recipient)
        try:
            message, used_cached_media = await self.send_ad_to_chat(
                chat_id=self.to_chat_id(recipient),
                ad_item=ad_item,
            )
            return ActionAdvertResult(
                has_ad=True,
                recipient_telegram_id=recipient,
                ad_item=ad_item,
                sent_message_id=message.message_id,
                used_cached_media=used_cached_media,
            )
        except TelegramAPIError as exc:
            return ActionAdvertResult(
                has_ad=True,
                recipient_telegram_id=recipient,
                ad_item=ad_item,
                error=str(exc),
            )

    async def send_broadcast_advert(
        self,
        *,
        recipient_telegram_ids: Sequence[int | str],
        placement: str,
        language: str | None = None,
        concurrency: int | None = None,
    ) -> BroadcastAdvertResult:
        ad_item, recipients = await self.get_broadcast_ad(
            recipient_telegram_ids=recipient_telegram_ids,
            placement=placement,
            language=language,
        )
        if not ad_item:
            return BroadcastAdvertResult(has_ad=False, recipient_telegram_ids=recipients)
        assert ad_item.broadcast_batch is not None
        batch_id = ad_item.broadcast_batch.batch_id
        semaphore = asyncio.Semaphore(concurrency or self.broadcast_concurrency)

        delivered: list[str] = []
        failed: list[str] = []
        delivery_errors: dict[str, str] = {}

        async def deliver(recipient: str) -> None:
            async with semaphore:
                try:
                    await self.send_ad_to_chat(
                        chat_id=self.to_chat_id(recipient),
                        ad_item=ad_item,
                    )
                    delivered.append(recipient)
                except TelegramAPIError as exc:
                    failed.append(recipient)
                    delivery_errors[recipient] = str(exc)
                except Exception as exc:  # noqa: BLE001
                    failed.append(recipient)
                    delivery_errors[recipient] = str(exc)

        await asyncio.gather(*(deliver(recipient) for recipient in recipients))

        try:
            submit_response = await self.submit_broadcast_result(
                batch_id=batch_id,
                delivered_recipient_telegram_ids=delivered,
                failed_recipient_telegram_ids=failed,
            )
        except Exception as exc:  # noqa: BLE001
            raise BroadcastBatchSubmitError(
                batch_id=batch_id,
                delivered_recipient_ids=delivered,
                failed_recipient_ids=failed,
                reason=exc,
            ) from exc

        return BroadcastAdvertResult(
            has_ad=True,
            recipient_telegram_ids=recipients,
            ad_item=ad_item,
            batch_id=batch_id,
            delivered_recipient_ids=delivered,
            failed_recipient_ids=failed,
            delivery_errors=delivery_errors,
            batch_submit_response=submit_response,
        )

    async def submit_broadcast_result(
        self,
        *,
        batch_id: str,
        delivered_recipient_telegram_ids: Sequence[int | str],
        failed_recipient_telegram_ids: Sequence[int | str],
    ) -> dict[str, Any]:
        await self.ensure_configuration()
        if not batch_id:
            raise ValueError("batch_id is required")

        delivered = [str(recipient) for recipient in delivered_recipient_telegram_ids]
        failed = [str(recipient) for recipient in failed_recipient_telegram_ids]

        overlap = set(delivered) & set(failed)
        if overlap:
            raise ValueError(
                "delivered_recipient_telegram_ids and failed_recipient_telegram_ids overlap"
            )

        payload = {
            "batchId": batch_id,
            "deliveredRecipientTelegramIds": delivered,
            "failedRecipientTelegramIds": failed,
        }
        return await self._request_json(
            method="POST",
            path="/api/ads/impressions/batch",
            json_payload=payload,
            timeout_seconds=self.batch_timeout_seconds,
        )

    async def track_click(self, *, campaign_id: str, ad_id: str | None = None) -> dict[str, Any]:
        await self.ensure_configuration()
        params: dict[str, str] = {"campaignId": campaign_id}
        if ad_id:
            params["adId"] = ad_id

        return await self._request_json(
            method="GET",
            path="/api/ads/click",
            params=params,
            timeout_seconds=self.next_timeout_seconds,
        )

    async def report_start(
        self,
        *,
        telegram_user_id: int | str,
        start_param: str,
        is_new_user: bool,
    ) -> dict[str, Any]:
        """
        Report ``/start <start_param>`` received by this bot (funnel step 1).

        Call it from the ``/start`` handler whenever the command has an argument.
        Pass ``is_new_user=True`` only when the user record was created by this
        very update: only new users count as acquired by the ad and move down
        the funnel. The response has ``matched`` (the value belongs to an ad that
        links to this bot) and ``counted`` (first touch for this user).
        """
        return await self._report_funnel_event(
            event="start",
            telegram_user_id=telegram_user_id,
            extra={"startParam": start_param, "isNewUser": bool(is_new_user)},
        )

    async def report_first_video(self, *, telegram_user_id: int | str) -> dict[str, Any]:
        """Report the user's first delivered video (funnel step 2). Repeats are ignored."""
        return await self._report_funnel_event(event="first_video", telegram_user_id=telegram_user_id)

    async def report_premium(self, *, telegram_user_id: int | str) -> dict[str, Any]:
        """Report the user's first premium purchase (funnel step 3). Repeats are ignored."""
        return await self._report_funnel_event(event="premium", telegram_user_id=telegram_user_id)

    async def _report_funnel_event(
        self,
        *,
        event: str,
        telegram_user_id: int | str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        await self.ensure_configuration()
        payload: dict[str, Any] = {"event": event, "telegramUserId": str(telegram_user_id)}
        if extra:
            payload.update(extra)

        return await self._request_json(
            method="POST",
            path="/api/ads/funnel",
            json_payload=payload,
            timeout_seconds=self.next_timeout_seconds,
        )

    async def get_campaigns(
        self,
        *,
        include_inactive: bool = False,
        delivery_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch campaigns available for current bot hash.

        Parameters
        ----------
        include_inactive:
            If True, API may return inactive campaigns/ads.
        delivery_type:
            Optional filter: ``action`` or ``broadcast``.
        """
        await self.ensure_configuration()
        params: dict[str, str] = {
            "includeInactive": "true" if include_inactive else "false",
        }
        headers: dict[str, str] = {}

        if delivery_type:
            params["deliveryType"] = delivery_type
            headers["x-ad-delivery-type"] = delivery_type

        response = await self._request_json(
            method="GET",
            path="/api/ads/campaigns",
            params=params,
            headers=headers,
            timeout_seconds=self.next_timeout_seconds,
        )
        items = response.get("items") if isinstance(response, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    async def _fetch_next_ad(
        self,
        *,
        payload: dict[str, Any],
        delivery_type: str,
        language: str | None = None,
    ) -> dict[str, Any]:
        headers = {"x-ad-delivery-type": delivery_type}
        params = {"deliveryType": delivery_type}
        if language:
            headers["x-ad-language"] = language
            params["language"] = language
        return await self._request_json(
            method="POST",
            path="/api/ads/next",
            json_payload=payload,
            headers=headers,
            params=params,
            timeout_seconds=self.next_timeout_seconds,
        )

    async def _request_json(
        self,
        *,
        method: str,
        path: str,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        await self._ensure_session()
        assert self._session is not None

        merged_headers = {
            "Content-Type": "application/json",
            "x-bot-hash": self.bot_hash,
        }
        if headers:
            merged_headers.update(headers)

        url = f"{self.api_base_url}{path}"

        for attempt in range(self.max_api_retries + 1):
            try:
                timeout = aiohttp.ClientTimeout(total=timeout_seconds)
                async with self._session.request(
                    method=method,
                    url=url,
                    json=json_payload,
                    params=params,
                    headers=merged_headers,
                    timeout=timeout,
                ) as response:
                    text = await response.text()
                    payload: Any | None = None
                    if text:
                        try:
                            payload = json.loads(text)
                        except json.JSONDecodeError:
                            payload = text

                    if response.status >= 500 and attempt < self.max_api_retries:
                        await asyncio.sleep(self.retry_backoff_seconds * (attempt + 1))
                        continue

                    if response.status >= 400:
                        message = self._extract_error_message(payload) or text or "unknown error"
                        raise BotAdsAPIError(
                            status_code=response.status,
                            message=message,
                            payload=payload,
                        )

                    if payload is None:
                        return {}
                    if isinstance(payload, dict):
                        return payload
                    return {"raw": payload}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt >= self.max_api_retries:
                    raise BotAdsNetworkError(str(exc)) from exc
                await asyncio.sleep(self.retry_backoff_seconds * (attempt + 1))

        raise BotAdsNetworkError("Request failed after retries")

    @staticmethod
    def _extract_error_message(payload: Any) -> str | None:
        if isinstance(payload, dict):
            value = payload.get("message") or payload.get("error")
            if value:
                return str(value)
        if isinstance(payload, str):
            return payload
        return None

    async def _ensure_session(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._owns_session = True

    async def _validate_bot_hash(self, *, bot_user_id: int) -> None:
        payload = {
            "deliveryType": "action",
            "recipientTelegramId": str(bot_user_id),
            "placement": "botadslib_healthcheck",
        }
        try:
            await self._fetch_next_ad(payload=payload, delivery_type="action")
        except BotAdsAPIError as exc:
            if exc.status_code == 401:
                raise BotAdsConfigurationError(
                    "Invalid bot_hash (ad hash). Check x-bot-hash provided by the ad platform."
                ) from exc
            raise

    async def _validate_cache_channel(self, *, bot_user_id: int) -> None:
        try:
            chat = await self.bot.get_chat(self.cache_channel_id)
        except TelegramAPIError as exc:
            raise CacheChannelAccessError(
                "Cache channel is not accessible. Check cache_channel_id and ensure the bot "
                "is added to the channel as admin."
            ) from exc

        chat_type_raw = getattr(chat, "type", "")
        chat_type = str(getattr(chat_type_raw, "value", chat_type_raw)).lower()
        if chat_type != "channel":
            raise CacheChannelAccessError(
                "cache_channel_id must point to a Telegram channel."
            )

        try:
            member = await self.bot.get_chat_member(
                chat_id=self.cache_channel_id,
                user_id=bot_user_id,
            )
        except TelegramAPIError as exc:
            raise CacheChannelAccessError(
                "Cannot verify bot membership in cache channel. Check channel settings and bot rights."
            ) from exc

        status_raw = getattr(member, "status", "")
        status = str(getattr(status_raw, "value", status_raw)).lower()
        if status not in {"administrator", "creator", "owner"}:
            raise CacheChannelAccessError(
                "Bot cannot post to cache channel. Check that bot is channel admin."
            )

        can_post_messages = getattr(member, "can_post_messages", None)
        if can_post_messages is False:
            raise CacheChannelAccessError(
                "Bot does not have permission to post messages in cache channel. "
                "Check bot admin rights."
            )

    async def _send_creative(self, *, chat_id: int | str, ad_item: AdItem) -> tuple[Message, bool]:
        creative = ad_item.creative
        reply_markup = self._parse_reply_markup(creative.reply_markup)

        if creative.content_type == "text":
            text = creative.text or ""
            entities = self._parse_entities(creative.entities, text=text)
            message = await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                entities=entities,
                parse_mode=None,
                reply_markup=reply_markup,
                **self._link_preview_kwargs(creative.send_options),
            )
            return message, False

        media_type, storage_url = self._resolve_media(creative)
        if not media_type or not storage_url:
            fallback_text = creative.text
            fallback_entities = creative.entities
            if not fallback_text:
                fallback_text = creative.caption or ""
                fallback_entities = creative.caption_entities
            parsed_fallback_entities = self._parse_entities(
                fallback_entities,
                text=fallback_text or "",
            )
            fallback_message = await self.bot.send_message(
                chat_id=chat_id,
                text=fallback_text or "",
                entities=parsed_fallback_entities,
                parse_mode=None,
                reply_markup=reply_markup,
                **self._link_preview_kwargs(creative.send_options),
            )
            return fallback_message, False

        file_id, from_existing_cache = await self._get_or_cache_file_id(
            media_type=media_type,
            storage_url=storage_url,
        )

        caption_text = creative.caption or ""
        caption_entities = self._parse_entities(
            creative.caption_entities,
            text=caption_text,
        )
        try:
            message = await self._send_media(
                chat_id=chat_id,
                media_type=media_type,
                media=file_id,
                caption=creative.caption,
                caption_entities=caption_entities,
                reply_markup=reply_markup,
            )
        except TelegramAPIError as exc:
            if not self._is_entity_parse_error(exc):
                raise

            utf16_caption_entities = self._parse_entities(
                creative.caption_entities,
                text=caption_text,
                assume_codepoint_offsets=True,
            )
            if not utf16_caption_entities:
                raise

            message = await self._send_media(
                chat_id=chat_id,
                media_type=media_type,
                media=file_id,
                caption=creative.caption,
                caption_entities=utf16_caption_entities,
                reply_markup=reply_markup,
            )
        return message, from_existing_cache

    def _resolve_media(self, creative: Any) -> tuple[str | None, str | None]:
        media_type = self._normalize_media_type(
            creative.delivery.media_type or creative.content_type
        )
        if media_type == "text":
            return None, None

        storage_url = creative.delivery.media_storage_url
        if not storage_url:
            for media in creative.media:
                if media.storage_url:
                    storage_url = media.storage_url
                    break

        if not storage_url:
            return None, None

        return media_type, storage_url

    async def _get_or_cache_file_id(self, *, media_type: str, storage_url: str) -> tuple[str, bool]:
        key = (media_type, storage_url)

        cached = self._media_cache.get(key)
        if cached:
            return cached.file_id, True

        async with self._media_cache_lock:
            cached = self._media_cache.get(key)
            if cached:
                return cached.file_id, True

            try:
                cache_message = await self._send_media(
                    chat_id=self.cache_channel_id,
                    media_type=media_type,
                    media=storage_url,
                    caption=None,
                    caption_entities=None,
                    reply_markup=None,
                )
            except TelegramAPIError as exc:
                raise CacheChannelAccessError(
                    "Failed to upload ad media to cache channel. Check that cache channel exists "
                    "and bot is admin with permission to post messages."
                ) from exc
            file_id = self._extract_file_id(cache_message, media_type)
            self._media_cache[key] = CachedMediaEntry(
                storage_url=storage_url,
                media_type=media_type,
                file_id=file_id,
            )
            self._persist_media_cache()
            return file_id, False

    async def _send_media(
        self,
        *,
        chat_id: int | str,
        media_type: str,
        media: str,
        caption: str | None,
        caption_entities: list[MessageEntity] | None,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> Message:
        if media_type == "photo":
            return await self.bot.send_photo(
                chat_id=chat_id,
                photo=media,
                caption=caption,
                caption_entities=caption_entities,
                parse_mode=None,
                reply_markup=reply_markup,
            )
        if media_type == "video":
            return await self.bot.send_video(
                chat_id=chat_id,
                video=media,
                caption=caption,
                caption_entities=caption_entities,
                parse_mode=None,
                reply_markup=reply_markup,
            )
        if media_type == "audio":
            return await self.bot.send_audio(
                chat_id=chat_id,
                audio=media,
                caption=caption,
                caption_entities=caption_entities,
                parse_mode=None,
                reply_markup=reply_markup,
            )
        if media_type == "document":
            return await self.bot.send_document(
                chat_id=chat_id,
                document=media,
                caption=caption,
                caption_entities=caption_entities,
                parse_mode=None,
                reply_markup=reply_markup,
            )
        if media_type == "animation":
            return await self.bot.send_animation(
                chat_id=chat_id,
                animation=media,
                caption=caption,
                caption_entities=caption_entities,
                parse_mode=None,
                reply_markup=reply_markup,
            )
        if media_type == "voice":
            return await self.bot.send_voice(
                chat_id=chat_id,
                voice=media,
                caption=caption,
                caption_entities=caption_entities,
                parse_mode=None,
                reply_markup=reply_markup,
            )
        if media_type == "video_note":
            return await self.bot.send_video_note(
                chat_id=chat_id,
                video_note=media,
                reply_markup=reply_markup,
            )
        if media_type == "sticker":
            return await self.bot.send_sticker(
                chat_id=chat_id,
                sticker=media,
                reply_markup=reply_markup,
            )

        # Unknown media type: fallback to document for maximum compatibility.
        return await self.bot.send_document(
            chat_id=chat_id,
            document=media,
            caption=caption,
            caption_entities=caption_entities,
            parse_mode=None,
            reply_markup=reply_markup,
        )

    @staticmethod
    def _extract_file_id(message: Message, media_type: str) -> str:
        if media_type == "photo" and message.photo:
            return message.photo[-1].file_id
        if media_type == "video" and message.video:
            return message.video.file_id
        if media_type == "audio" and message.audio:
            return message.audio.file_id
        if media_type == "document" and message.document:
            return message.document.file_id
        if media_type == "animation" and message.animation:
            return message.animation.file_id
        if media_type == "voice" and message.voice:
            return message.voice.file_id
        if media_type == "video_note" and message.video_note:
            return message.video_note.file_id
        if media_type == "sticker" and message.sticker:
            return message.sticker.file_id

        if message.document:
            return message.document.file_id
        if message.video:
            return message.video.file_id
        if message.audio:
            return message.audio.file_id
        if message.animation:
            return message.animation.file_id
        if message.voice:
            return message.voice.file_id
        if message.video_note:
            return message.video_note.file_id
        if message.sticker:
            return message.sticker.file_id
        if message.photo:
            return message.photo[-1].file_id

        raise BotAdsError("Cannot extract file_id from cache channel message")

    @staticmethod
    def _parse_entities(
        raw_entities: Iterable[dict[str, Any]],
        *,
        text: str = "",
        assume_codepoint_offsets: bool = False,
    ) -> list[MessageEntity] | None:
        parsed: list[MessageEntity] = []
        for entity in raw_entities:
            if not isinstance(entity, dict):
                continue
            normalized_entity = BotAdsClient._normalize_message_entity(entity)
            if assume_codepoint_offsets:
                normalized_entity = BotAdsClient._convert_entity_offsets_to_utf16(
                    normalized_entity,
                    text=text,
                )
            try:
                parsed.append(MessageEntity.model_validate(normalized_entity))
            except Exception:  # noqa: BLE001
                continue
        return parsed or None

    @staticmethod
    def _link_preview_kwargs(send_options: dict[str, Any]) -> dict[str, Any]:
        raw_options = send_options.get("link_preview_options")
        if not isinstance(raw_options, dict):
            # No explicit options: keep the bot's own link preview defaults.
            return {}
        # Fields left unset must not fall back to the bot's DefaultBotProperties:
        # the ad has to look exactly like the creative the advertiser approved.
        try:
            options = LinkPreviewOptions(
                is_disabled=bool(raw_options.get("is_disabled")),
                url=raw_options.get("url"),
                prefer_small_media=bool(raw_options.get("prefer_small_media")),
                prefer_large_media=bool(raw_options.get("prefer_large_media")),
                show_above_text=bool(raw_options.get("show_above_text")),
            )
        except Exception:  # noqa: BLE001
            return {}
        return {"link_preview_options": options}

    @staticmethod
    def _parse_reply_markup(raw_reply_markup: dict[str, Any] | None) -> InlineKeyboardMarkup | None:
        if not raw_reply_markup:
            return None
        try:
            normalized = BotAdsClient._normalize_reply_markup(raw_reply_markup)
            return InlineKeyboardMarkup.model_validate(normalized)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _normalize_recipient_ids(
        recipient_telegram_ids: Sequence[int | str],
    ) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []

        for recipient in recipient_telegram_ids:
            value = str(recipient)
            if value not in seen:
                seen.add(value)
                normalized.append(value)

        return normalized

    @staticmethod
    def _normalize_media_type(media_type: str | None) -> str:
        normalized = (media_type or "document").strip()
        if not normalized:
            return "document"
        lowered = normalized.lower()
        mapping = {
            "videonote": "video_note",
            "video_note": "video_note",
            "gif": "animation",
        }
        return mapping.get(lowered, lowered)

    @staticmethod
    def _normalize_message_entity(entity: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(entity)

        if "custom_emoji_id" not in normalized:
            custom_emoji_id = normalized.get("customEmojiId")
            if custom_emoji_id is None:
                custom_emoji_id = normalized.get("customEmojiID")
            if custom_emoji_id is not None:
                normalized["custom_emoji_id"] = custom_emoji_id

        entity_type = normalized.get("type")
        if isinstance(entity_type, str):
            normalized["type"] = BotAdsClient._normalize_entity_type(entity_type)

        return normalized

    @staticmethod
    def _normalize_entity_type(entity_type: str) -> str:
        raw = entity_type.strip().replace("-", "_")
        if not raw:
            return raw

        if "_" in raw:
            normalized = raw.lower()
        else:
            normalized = re.sub(r"(?<!^)(?=[A-Z])", "_", raw).lower()

        aliases = {
            "customemoji": "custom_emoji",
            "textmention": "text_mention",
            "textlink": "text_link",
            "expandableblockquote": "expandable_blockquote",
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def _normalize_reply_markup(markup: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(markup)

        if "inline_keyboard" not in normalized and "inlineKeyboard" in normalized:
            normalized["inline_keyboard"] = normalized.get("inlineKeyboard")

        rows = normalized.get("inline_keyboard")
        if isinstance(rows, list):
            normalized_rows: list[list[dict[str, Any]]] = []
            for row in rows:
                if not isinstance(row, list):
                    continue
                normalized_row: list[dict[str, Any]] = []
                for button in row:
                    if not isinstance(button, dict):
                        continue
                    normalized_row.append(BotAdsClient._normalize_inline_button(button))
                if normalized_row:
                    normalized_rows.append(normalized_row)
            normalized["inline_keyboard"] = normalized_rows

        return normalized

    @staticmethod
    def _normalize_inline_button(button: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(button)

        remap = {
            "callbackData": "callback_data",
            "webApp": "web_app",
            "loginUrl": "login_url",
            "switchInlineQuery": "switch_inline_query",
            "switchInlineQueryCurrentChat": "switch_inline_query_current_chat",
            "switchInlineQueryChosenChat": "switch_inline_query_chosen_chat",
            "copyText": "copy_text",
            "callbackGame": "callback_game",
            "iconCustomEmojiId": "icon_custom_emoji_id",
            "iconCustomEmojiID": "icon_custom_emoji_id",
        }
        for source_key, target_key in remap.items():
            if target_key not in normalized and source_key in normalized:
                normalized[target_key] = normalized[source_key]

        if "style" not in normalized:
            if "buttonStyle" in normalized:
                normalized["style"] = normalized["buttonStyle"]
            elif "buttonColor" in normalized:
                normalized["style"] = normalized["buttonColor"]

        return normalized

    @staticmethod
    def _convert_entity_offsets_to_utf16(
        entity: dict[str, Any],
        *,
        text: str,
    ) -> dict[str, Any]:
        converted = dict(entity)
        offset = converted.get("offset")
        length = converted.get("length")
        if not isinstance(offset, int) or not isinstance(length, int):
            return converted
        if offset < 0 or length < 0:
            return converted

        max_index = len(text)
        start_cp = min(offset, max_index)
        end_cp = min(offset + length, max_index)
        if end_cp < start_cp:
            end_cp = start_cp

        start_utf16 = len(text[:start_cp].encode("utf-16-le")) // 2
        end_utf16 = len(text[:end_cp].encode("utf-16-le")) // 2

        converted["offset"] = start_utf16
        converted["length"] = max(0, end_utf16 - start_utf16)
        return converted

    @staticmethod
    def _is_entity_parse_error(exc: TelegramAPIError) -> bool:
        text = str(exc).lower()
        return "can't parse entities" in text or ("entity" in text and "offset" in text)

    @staticmethod
    def _normalize_language_code(language: str | None) -> str | None:
        if language is None:
            return None

        normalized = language.strip().lower()
        if not normalized:
            return None
        if not re.fullmatch(r"[a-z]{2}", normalized):
            raise ValueError("language must be a 2-letter code, for example 'uk' or 'en'")
        return normalized

    def _load_media_cache(self) -> None:
        if not self.media_cache_path or not self.media_cache_path.exists():
            return

        try:
            raw = json.loads(self.media_cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return

        if not isinstance(raw, dict):
            return

        for key, item in raw.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                continue
            media_type, _, storage_url = key.partition("|")
            if not media_type or not storage_url:
                continue
            file_id = item.get("file_id")
            if not isinstance(file_id, str) or not file_id:
                continue
            self._media_cache[(media_type, storage_url)] = CachedMediaEntry(
                storage_url=storage_url,
                media_type=media_type,
                file_id=file_id,
            )

    def _persist_media_cache(self) -> None:
        if not self.media_cache_path:
            return

        serialized: dict[str, dict[str, str]] = {}
        for (media_type, storage_url), entry in self._media_cache.items():
            key = f"{media_type}|{storage_url}"
            serialized[key] = {
                "file_id": entry.file_id,
            }

        try:
            self.media_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.media_cache_path.write_text(
                json.dumps(serialized, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            # Cache persistence should not break message delivery.
            return
