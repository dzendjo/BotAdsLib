# BotAdsLib

Library for showing ads in Telegram bots built on `aiogram`, integrated with Bot Contractor API.

## Features

- fetches the next ad via `POST /api/ads/next`;
- sends `action` ads to a single user;
- sends `broadcast` ads to a list of users;
- automatically submits broadcast results to `POST /api/ads/impressions/batch`;
- caches media through a service channel (`cache_channel_id`) and reuses `file_id`.

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
    )
    print(action_result)

    # BROADCAST (up to 100 IDs)
    broadcast_result = await ads.send_broadcast_advert(
        recipient_telegram_ids=[100000001, 100000002, 100000003],
        placement="daily_digest",
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

If media upload to cache channel fails, the library raises a clear message to check
that the bot is admin in the cache channel.

## How `send_broadcast_advert` Works

1. Calls `POST /api/ads/next` with `deliveryType="broadcast"`.
2. If `item == null`, returns `has_ad=False`.
3. If ad exists, sends it to each recipient.
4. Builds `delivered_recipient_ids` and `failed_recipient_ids`.
5. Automatically calls `POST /api/ads/impressions/batch`.
6. If batch submit fails, raises `BroadcastBatchSubmitError` (so you can retry).

## Click Tracking

Use `track_click(campaign_id, ad_id)` to call `GET /api/ads/click`.

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
