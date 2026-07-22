import unittest
import sys
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import mqtt_service as mqtt_service_module
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

    def test_reports_initial_connection_and_reconnection(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service.client.subscribe = Mock(return_value=(0, 1))

        with patch("mqtt_service.threading.Timer"), self.assertLogs(
            "mqtt_service", level="INFO"
        ) as captured:
            service.on_connect(service.client, None, None, 0, None)
            service.on_disconnect(service.client, None, None, 1, None)
            service.on_connect(service.client, None, None, 0, None)

        output = "\n".join(captured.output)
        self.assertIn("MQTT 連線成功", output)
        self.assertIn("MQTT 連線中斷", output)
        self.assertIn("MQTT 已重新連線", output)

    def test_reconnect_failure_logging_is_rate_limited(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service._has_connected_once = True

        with patch("mqtt_service.time.monotonic", side_effect=[31.0, 32.0]), self.assertLogs(
            "mqtt_service", level="WARNING"
        ) as captured:
            service.on_connect_fail(service.client, None)
            service.on_connect_fail(service.client, None)

        self.assertEqual(len(captured.output), 1)

    def test_meshtastic_info_log_excludes_message_body(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service.telegram_callback = Mock()
        message_text = "private channel message"
        payload = message_text.encode("utf-8")
        sender_node_id = 0x12345678

        envelope = mqtt_service_module.mqtt_pb2.ServiceEnvelope()
        packet = envelope.packet
        packet.id = 42
        setattr(packet, "from", sender_node_id)
        data_packet = mqtt_service_module.mesh_pb2.Data()
        data_packet.portnum = mqtt_service_module.portnums_pb2.TEXT_MESSAGE_APP
        data_packet.payload = payload
        packet.encrypted = mqtt_service_module.crypt_payload(
            data_packet.SerializeToString(), service.key, packet.id, sender_node_id
        )

        msg = SimpleNamespace(payload=envelope.SerializeToString())
        with self.assertLogs("mqtt_service", level="INFO") as captured:
            service.on_message(None, None, msg)

        output = "\n".join(captured.output)
        self.assertIn("packet_id=42", output)
        self.assertIn(f"payload_bytes={len(payload)}", output)
        self.assertIn("from !12345678", output)
        self.assertNotIn(message_text, output)
        service.telegram_callback.assert_called_once_with("[Node !12345678]: private channel message")


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
