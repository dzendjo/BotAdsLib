from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

DeliveryType = Literal["action", "broadcast"]


@dataclass(slots=True)
class CreativeMedia:
    type: str
    storage_url: str | None = None
    file_unique_id: str | None = None
    file_size: int | None = None
    duration: int | None = None
    mime_type: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "CreativeMedia":
        return cls(
            type=str(data.get("type") or ""),
            storage_url=data.get("storageUrl"),
            file_unique_id=data.get("fileUniqueId"),
            file_size=data.get("fileSize"),
            duration=data.get("duration"),
            mime_type=data.get("mimeType"),
        )


@dataclass(slots=True)
class CreativeDelivery:
    media_type: str | None = None
    media_storage_url: str | None = None
    has_portable_media: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any] | None) -> "CreativeDelivery":
        if not data:
            return cls()
        return cls(
            media_type=data.get("mediaType"),
            media_storage_url=data.get("mediaStorageUrl"),
            has_portable_media=bool(data.get("hasPortableMedia", False)),
        )


@dataclass(slots=True)
class Creative:
    id: str
    content_type: str
    text: str | None
    caption: str | None
    entities: list[dict[str, Any]] = field(default_factory=list)
    caption_entities: list[dict[str, Any]] = field(default_factory=list)
    media: list[CreativeMedia] = field(default_factory=list)
    reply_markup: dict[str, Any] | None = None
    send_options: dict[str, Any] = field(default_factory=dict)
    delivery: CreativeDelivery = field(default_factory=CreativeDelivery)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Creative":
        media_items = data.get("media") or []
        entities = data.get("entities")
        if entities is None:
            entities = data.get("messageEntities")
        caption_entities = data.get("captionEntities")
        if caption_entities is None:
            caption_entities = data.get("caption_entities")
        reply_markup = data.get("replyMarkup")
        if reply_markup is None:
            reply_markup = data.get("reply_markup")
        send_options = data.get("sendOptions")
        if send_options is None:
            send_options = data.get("send_options")
        return cls(
            id=str(data.get("_id") or ""),
            content_type=str(data.get("contentType") or "text"),
            text=data.get("text"),
            caption=data.get("caption"),
            entities=list(entities or []),
            caption_entities=list(caption_entities or []),
            media=[CreativeMedia.from_api(m) for m in media_items if isinstance(m, dict)],
            reply_markup=reply_markup,
            send_options=dict(send_options) if isinstance(send_options, dict) else {},
            delivery=CreativeDelivery.from_api(data.get("delivery")),
        )


@dataclass(slots=True)
class BroadcastBatch:
    batch_id: str
    recipients_count: int

    @classmethod
    def from_api(cls, data: dict[str, Any] | None) -> "BroadcastBatch | None":
        if not data:
            return None
        return cls(
            batch_id=str(data.get("batchId") or ""),
            recipients_count=int(data.get("recipientsCount") or 0),
        )


@dataclass(slots=True)
class AdItem:
    campaign_id: str
    ad_id: str
    language: str | None
    click_url: str
    creative: Creative
    broadcast_batch: BroadcastBatch | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "AdItem":
        language = data.get("language")
        return cls(
            campaign_id=str(data.get("campaignId") or ""),
            ad_id=str(data.get("adId") or ""),
            language=str(language) if language else None,
            click_url=str(data.get("clickUrl") or ""),
            creative=Creative.from_api(data.get("creative") or {}),
            broadcast_batch=BroadcastBatch.from_api(data.get("broadcastBatch")),
            raw=dict(data),
        )


@dataclass(slots=True)
class CachedMediaEntry:
    storage_url: str
    media_type: str
    file_id: str


@dataclass(slots=True)
class ActionAdvertResult:
    has_ad: bool
    recipient_telegram_id: str
    ad_item: AdItem | None = None
    sent_message_id: int | None = None
    used_cached_media: bool = False
    error: str | None = None


@dataclass(slots=True)
class BroadcastAdvertResult:
    has_ad: bool
    recipient_telegram_ids: list[str]
    ad_item: AdItem | None = None
    batch_id: str | None = None
    delivered_recipient_ids: list[str] = field(default_factory=list)
    failed_recipient_ids: list[str] = field(default_factory=list)
    delivery_errors: dict[str, str] = field(default_factory=dict)
    batch_submit_response: dict[str, Any] | None = None
