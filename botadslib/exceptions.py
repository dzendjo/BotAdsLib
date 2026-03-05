from __future__ import annotations

from typing import Any


class BotAdsError(Exception):
    """Base exception for the library."""


class BotAdsConfigurationError(BotAdsError):
    """Raised when library configuration is invalid."""


class BotAdsNetworkError(BotAdsError):
    """Raised for transport-level errors while calling ad platform APIs."""


class BotAdsAPIError(BotAdsError):
    """Raised when ad platform responds with 4xx/5xx status codes."""

    def __init__(self, status_code: int, message: str, payload: Any | None = None) -> None:
        super().__init__(f"API error {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.payload = payload


class BroadcastBatchSubmitError(BotAdsError):
    """Raised when broadcast results were not submitted successfully."""

    def __init__(
        self,
        batch_id: str,
        delivered_recipient_ids: list[str],
        failed_recipient_ids: list[str],
        reason: Exception,
    ) -> None:
        super().__init__(
            "Failed to submit broadcast batch results "
            f"for batch {batch_id}: {reason}"
        )
        self.batch_id = batch_id
        self.delivered_recipient_ids = delivered_recipient_ids
        self.failed_recipient_ids = failed_recipient_ids
        self.reason = reason


class CacheChannelAccessError(BotAdsConfigurationError):
    """Raised when bot cannot use cache channel for media upload."""
