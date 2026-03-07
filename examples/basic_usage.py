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

    action = await ads.send_action_advert(
        recipient_telegram_id=100000001,
        placement="after_result",
        language="uk",
    )
    # custom emoji from creative.entities/captionEntities are forwarded automatically.
    print(action)

    broadcast = await ads.send_broadcast_advert(
        recipient_telegram_ids=[100000001, 100000002],
        placement="daily_digest",
        language="en",
    )
    print(broadcast)

    await ads.close()
    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
