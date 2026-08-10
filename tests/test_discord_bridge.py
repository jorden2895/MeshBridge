import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from discord_bridge import DiscordBridge


class DiscordBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_plain_text_from_configured_channel(self):
        handler = AsyncMock()
        bridge = DiscordBridge(
            "token",
            {"123456789012345678": "route-1"},
            handler,
        )
        message = SimpleNamespace(
            author=SimpleNamespace(
                bot=False,
                id=456,
                name="bob",
            ),
            channel=SimpleNamespace(id=123456789012345678),
            content="hello",
        )

        await bridge.client.on_message(message)

        handler.assert_awaited_once_with("route-1", 456, "bob", "hello")

    async def test_ignores_non_text_bot_and_unconfigured_channel_messages(self):
        handler = AsyncMock()
        bridge = DiscordBridge(
            "token",
            {"123456789012345678": "route-1"},
            handler,
        )
        messages = (
            SimpleNamespace(
                author=SimpleNamespace(bot=False),
                channel=SimpleNamespace(id=123456789012345678),
                content="",
            ),
            SimpleNamespace(
                author=SimpleNamespace(bot=True),
                channel=SimpleNamespace(id=123456789012345678),
                content="bot text",
            ),
            SimpleNamespace(
                author=SimpleNamespace(bot=False),
                channel=SimpleNamespace(id=999),
                content="other channel",
            ),
        )

        for message in messages:
            await bridge.client.on_message(message)

        handler.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
