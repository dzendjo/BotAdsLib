# BotAdsLib

Library for showing ads in Telegram bots built on `aiogram`, integrated with Bot Contractor API.

## Features

- fetches the next ad via `POST /api/ads/next`;
- supports ad localization (`language`) for `action` and `broadcast`;
- supports Telegram custom emoji entities (Bot API 9.4);
- applies the creative's link preview settings (disabled preview, banner above text);
- sends `action` ads to a single user;
- sends `broadcast` ads to a list of users (up to 100 recipients per batch);
- automatically submits broadcast results to `POST /api/ads/impressions/batch`;
- caches media through a service channel (`cache_channel_id`) and reuses `file_id`;
- reports the advertised bot's funnel (start → first video → premium) via `POST /api/ads/funnel`.

## Installation

```bash
pip install -e .
```

Or install dependencies only:

```bash
pip install -r requirements.txt
```

## Quick Start

```python
import asyncio

from aiogram import Bot
from botadslib import BotAdsClient


async def main() -> None:
    bot = Bot(token="<BOT_TOKEN>")

    ads = BotAdsClient(
        bot=bot,
        api_base_url="https://app.example.com",
        bot_hash="<BOT_DELIVERY_HASH>",
        cache_channel_id=-1001234567890,
        media_cache_path=".botads-media-cache.json",
    )

    # ACTION
    action_result = await ads.send_action_advert(
        recipient_telegram_id=100000001,
        placement="after_result",
        language="uk",
    )
    # API may fallback to base creative if locale is unavailable:
    print(action_result.ad_item.language if action_result.ad_item else None)
    print(action_result)

    # BROADCAST (up to 100 IDs)
    broadcast_result = await ads.send_broadcast_advert(
        recipient_telegram_ids=[100000001, 100000002, 100000003],
        placement="daily_digest",
        language="en",
    )
    print(broadcast_result)

    await ads.close()
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
```

## Initialization

Required parameters:

- `bot`: `aiogram.Bot` instance;
- `api_base_url`: ad platform base URL;
- `bot_hash`: value for `x-bot-hash`;
- `cache_channel_id`: channel ID where bot caches media to get `file_id`;
- `media_cache_path` (optional): JSON file for persistent `storageUrl -> file_id` cache between restarts.

Configuration validation:

- the library validates configuration on first use (and in `async with BotAdsClient(...)`);
- checks that `bot_hash` is valid (`x-bot-hash`);
- checks that `cache_channel_id` is an accessible channel;
- checks that the bot is channel admin and can post messages.

Media is posted to the cache channel by its storage URL. If Telegram cannot fetch that URL
(for example "failed to get HTTP URL content"), the library downloads the file itself and
uploads it (up to 50 MB). If that also fails, it raises `CacheChannelAccessError` with
Telegram's own error text.

## How `send_broadcast_advert` Works

1. Calls `POST /api/ads/next` with `deliveryType="broadcast"`.
2. If `item == null`, returns `has_ad=False`.
3. If ad exists, sends it to each recipient.
4. Builds `delivered_recipient_ids` and `failed_recipient_ids`.
5. Automatically calls `POST /api/ads/impressions/batch`.
6. If batch submit fails, raises `BroadcastBatchSubmitError` (so you can retry).
7. If API returns `409 Previous broadcast batch is pending`, wait for the previous
   batch to complete (or expire) before requesting a new one.

## Language Support

- `get_action_ad(...)`, `send_action_advert(...)`, `get_broadcast_ad(...)`,
  `send_broadcast_advert(...)` accept optional `language="uk" | "en" | ...`;
- language is sent in body + `x-ad-language` header + `language` query parameter;
- use only 2-letter codes (e.g. `uk`, `en`, `pl`);
- check `ad_item.language` to see which localization API actually returned
  (`None` means base creative fallback).

## Custom Emoji Support (Bot API 9.4)

- entities are rendered via Bot API entities, not parse_mode;
- `custom_emoji_id` is supported directly;
- compatibility fallback is supported for API payloads with `customEmojiId`;
- if entity type arrives as `customEmoji`, library normalizes it to `custom_emoji`.
- for media captions, library retries once with UTF-16 entity offsets if Telegram rejects entity offsets.
- for inline buttons in `creative.replyMarkup`, compatibility mapping is supported:
  `iconCustomEmojiId -> icon_custom_emoji_id`, `buttonStyle/buttonColor -> style`.

## Link Preview

- `creative.sendOptions.link_preview_options` (available as `creative.send_options`) is passed to
  `send_message` for text ads: a disabled preview, or a custom preview URL shown large above the text;
- fields the creative leaves unset are sent as `false`, so the bot's own `DefaultBotProperties`
  (e.g. `link_preview_is_disabled=True`) can't change how an approved ad looks;
- creatives without link preview settings keep the bot's defaults, as before.

## Recommended Broadcast Strategy for Multi-language Audiences

1. Group recipients by base Telegram language (`language_code[:2]`).
2. Split each language group into chunks of max 100 users.
3. Call `send_broadcast_advert(..., language=<group_language>)` chunk by chunk.
4. Do not request a new chunk while previous batch is pending for the same bot.

Library helper:

```python
recipient_chunks = BotAdsClient.segment_broadcast_recipients_by_language(
    recipients=[
        (100000001, "uk"),
        (100000002, "uk"),
        (100000003, "en-US"),
    ],
    fallback_language="en",
    max_chunk_size=100,
)

for language, chunk in recipient_chunks:
    await ads.send_broadcast_advert(
        recipient_telegram_ids=chunk,
        placement="daily_digest",
        language=language,
    )
```

## Click Tracking

Use `track_click(campaign_id, ad_id)` to call `GET /api/ads/click`. The bot can only count clicks for ads it shows (it must be among the ad's target bots).

## Funnel Tracking (for the advertised bot)

When your bot is the one being advertised, report the funnel so the platform can compare ads:
**start → first video → premium**. Every ad has its own `start` value, so `/start <value>` tells which ad brought the user.

```python
from aiogram.filters import Command, CommandObject

@router.message(Command("start"))
async def start(message: Message, command: CommandObject, is_new_user: bool):
    if command.args:
        await ads.report_start(
            telegram_user_id=message.from_user.id,
            start_param=command.args,
            is_new_user=is_new_user,  # True only if this update created the user record
        )

# after the user's first video is delivered
await ads.report_first_video(telegram_user_id=user_id)

# after the user's first premium purchase
await ads.report_premium(telegram_user_id=user_id)
```

- Attribution is first touch: the first ad a user started from keeps them.
- Only new users move down the funnel; a returning user's `/start` is counted separately.
- Repeated `report_first_video` / `report_premium` calls are ignored, so calling them once per user is enough but not required.
- The report is accepted only if the ad's creative links to this bot (`t.me/<bot>`); otherwise `BotAdsAPIError` with status 403.
- Don't block the user on these calls: run them in the background and log `BotAdsError`.

## Echo Bot Example with `/broadcast`

Example file: `examples/echo_bot.py`.

What it does:

- for any text message: sends echo, then tries an `action` ad;
- `/ads` command:
  - fetches active campaigns from `GET /api/ads/campaigns`;
  - prints all campaign fields in chat;
  - prints `Launch broadcast - /broadcast` at the end;
- `/broadcast` command:
  - checks if a broadcast ad is available;
  - if not, replies `No available campaigns for your bot...`;
  - if yes, sends ad preview and shows campaign parameters (`Campaign`, `Price`, `Planned recipients`);
  - requests ad with user's base Telegram language and shows requested/applied locale;
  - shows inline button `Start broadcast`;
  - asks for final confirmation via reply keyboard (`Yes` / `No`);
  - uses 2 recipients: command author + fake ID (expected 1 success and 1 fail);
  - for media ads, caches media in the service channel first, then sends via `copy_message`;
  - applies `sleep(0.05)` (50ms) between sends.
  - technical errors are sent to admin chat if `BOT_ADMIN_CHAT_ID` is set.

Run:

Use `.env.example` as a reference for required variables.

```bash
export BOT_TOKEN="<BOT_TOKEN>"
export ADS_API_BASE_URL="https://app.example.com"
export ADS_BOT_HASH="<BOT_DELIVERY_HASH>"
export ADS_CACHE_CHANNEL_ID="-1001234567890"
export ADS_MEDIA_CACHE_PATH=".botads-media-cache.json"
# optional: receive technical errors in admin chat
export BOT_ADMIN_CHAT_ID="<YOUR_ADMIN_CHAT_ID>"

python3 -m examples.echo_bot
```
