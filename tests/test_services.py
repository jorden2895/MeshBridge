import unittest
import sys
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from config import AppConfig
from mqtt_service import MqttService, MqttServiceError
from telegram.warnings import PTBUserWarning
from telegram_bridge import create_application, handle_message

from test_config import valid_config


class MqttServiceTests(unittest.TestCase):
    def test_send_raises_when_disconnected(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service.client.is_connected = Mock(return_value=False)
        with self.assertRaisesRegex(MqttServiceError, "not connected"):
            service.send_message("hello")

    def test_accepts_payload_at_233_byte_limit(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service._publish_packet = Mock()

        service.send_message("a" * 233)

        service._publish_packet.assert_called_once()

    def test_silently_drops_payload_over_233_bytes(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service._publish_packet = Mock()

        service.send_message("a" * 234)

        service._publish_packet.assert_not_called()


class TelegramApplicationTests(unittest.TestCase):
    def test_frozen_build_suppresses_only_false_builder_warning(self):
        mqtt_service = Mock()
        fake_application = Mock()
        fake_application.bot_data = {}
        builder = Mock()
        builder.token.return_value = builder
        builder.post_init.return_value = builder
        builder.post_shutdown.return_value = builder

        def build():
            warnings.warn(
                "`Application` instances should be built via the `ApplicationBuilder`.",
                PTBUserWarning,
            )
            return fake_application

        builder.build.side_effect = build
        with patch.object(sys, "frozen", True, create=True), patch.object(
            create_application.__globals__["Application"], "builder", return_value=builder
        ):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                create_application("token", -100123, mqtt_service)

        self.assertEqual(caught, [])

    def test_limit_counts_utf8_bytes_not_characters(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service._publish_packet = Mock()

        service.send_message("測" * 78)

        service._publish_packet.assert_not_called()


class TelegramHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_unauthorized_chat_without_leaking_target_id(self):
        message = SimpleNamespace(text="hello", reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=999),
            effective_message=message,
            effective_user=SimpleNamespace(id=123),
        )
        context = SimpleNamespace(bot_data={"target_chat_id": -100123})
        mqtt_service = Mock()

        await handle_message(update, context, mqtt_service=mqtt_service)

        reply = message.reply_text.await_args.args[0]
        self.assertNotIn("-100123", reply)
        mqtt_service.send_message.assert_not_called()

    async def test_reports_mqtt_send_failure(self):
        message = SimpleNamespace(text="hello", reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-100123),
            effective_message=message,
            effective_user=SimpleNamespace(id=123),
        )
        context = SimpleNamespace(bot_data={"target_chat_id": -100123})
        mqtt_service = Mock()
        mqtt_service.send_message.side_effect = MqttServiceError("offline")

        await handle_message(update, context, mqtt_service=mqtt_service)

        message.reply_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
