import unittest
import sys
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import mqtt_service as mqtt_service_module
from config import AppConfig
from mqtt_service import MqttService, MqttServiceError
from telegram.warnings import PTBUserWarning
from telegram_bridge import create_application, handle_message, help_command, start

from test_config import valid_config


class MqttServiceTests(unittest.TestCase):
    def encrypted_message(self, service, *, packet_id, sender_id, text):
        envelope = mqtt_service_module.mqtt_pb2.ServiceEnvelope()
        packet = envelope.packet
        packet.id = packet_id
        setattr(packet, "from", sender_id)
        data_packet = mqtt_service_module.mesh_pb2.Data()
        data_packet.portnum = mqtt_service_module.portnums_pb2.TEXT_MESSAGE_APP
        data_packet.payload = text.encode("utf-8")
        packet.channel = mqtt_service_module.channel_hash(service.channel, service.key)
        packet.encrypted = mqtt_service_module.crypt_payload(
            data_packet.SerializeToString(), service.key, packet.id, sender_id
        )
        return SimpleNamespace(payload=envelope.SerializeToString())

    def test_node_identity_uses_eight_hex_digits_everywhere(self):
        for node_id, expected in (
            (1, "!00000001"),
            (0x01234567, "!01234567"),
            (0xFFFFFFFF, "!ffffffff"),
        ):
            with self.subTest(node_id=node_id):
                raw = valid_config()
                raw["node"]["id"] = node_id
                service = MqttService(AppConfig.from_dict(raw))

                self.assertEqual(service.node_name, expected)
                self.assertTrue(service.publish_topic.endswith(f"/{expected}"))

    def test_gateway_and_node_info_use_formatted_node_identity(self):
        raw = valid_config()
        raw["node"]["id"] = 1
        service = MqttService(AppConfig.from_dict(raw))
        service.client.is_connected = Mock(return_value=True)
        service.client.publish = Mock(return_value=SimpleNamespace(rc=0))

        data = mqtt_service_module.mesh_pb2.Data()
        data.portnum = mqtt_service_module.portnums_pb2.NODEINFO_APP
        service._publish_packet(data)
        published = service.client.publish.call_args.args[1]
        envelope = mqtt_service_module.mqtt_pb2.ServiceEnvelope()
        envelope.ParseFromString(published)

        self.assertEqual(envelope.gateway_id, "!00000001")
        self.assertEqual(getattr(envelope.packet, "from"), 1)

    def test_node_info_payload_uses_configured_names(self):
        raw = valid_config()
        raw["node"]["long_name"] = "台灣橋接器"
        raw["node"]["short_name"] = "台橋"
        service = MqttService(AppConfig.from_dict(raw))
        service._mqtt_connected = True
        service.client.is_connected = Mock(return_value=True)
        service.client.publish = Mock(return_value=SimpleNamespace(rc=0))
        service._schedule_node_info = Mock()

        service._send_node_info()

        published = service.client.publish.call_args.args[1]
        envelope = mqtt_service_module.mqtt_pb2.ServiceEnvelope()
        envelope.ParseFromString(published)
        decrypted = mqtt_service_module.crypt_payload(
            envelope.packet.encrypted,
            service.key,
            envelope.packet.id,
            getattr(envelope.packet, "from"),
        )
        data = mqtt_service_module.mesh_pb2.Data()
        data.ParseFromString(decrypted)
        user = mqtt_service_module.mesh_pb2.User()
        user.ParseFromString(data.payload)
        self.assertEqual(user.long_name, "台灣橋接器")
        self.assertEqual(user.short_name, "台橋")
        self.assertEqual(user.id, service.node_name)

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

    def test_reconnect_cancels_previous_node_info_timer(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service.client.subscribe = Mock(return_value=(0, 1))
        first_timer = Mock()
        second_timer = Mock()

        with patch("mqtt_service.threading.Timer", side_effect=[first_timer, second_timer]):
            service.on_connect(service.client, None, None, 0, None)
            service.on_connect(service.client, None, None, 0, None)

        first_timer.cancel.assert_called_once()
        second_timer.start.assert_called_once()

    def test_node_info_does_not_publish_after_stop(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service._stopping = True
        service._mqtt_connected = False
        service._publish_packet = Mock()

        service._send_node_info()

        service._publish_packet.assert_not_called()

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
        packet.channel = mqtt_service_module.channel_hash(service.channel, service.key)
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

    def test_rejects_plaintext_decoded_mqtt_packet(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service.telegram_callback = Mock()
        envelope = mqtt_service_module.mqtt_pb2.ServiceEnvelope()
        packet = envelope.packet
        packet.id = 7
        setattr(packet, "from", 0x12345678)
        packet.decoded.portnum = mqtt_service_module.portnums_pb2.TEXT_MESSAGE_APP
        packet.decoded.payload = b"injected"

        service.on_message(None, None, SimpleNamespace(payload=envelope.SerializeToString()))

        service.telegram_callback.assert_not_called()

    def test_rejects_packet_that_cannot_be_decrypted(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service.telegram_callback = Mock()
        envelope = mqtt_service_module.mqtt_pb2.ServiceEnvelope()
        packet = envelope.packet
        packet.id = 8
        setattr(packet, "from", 0x12345678)
        packet.channel = mqtt_service_module.channel_hash(service.channel, service.key)
        packet.encrypted = b"\xff"

        service.on_message(None, None, SimpleNamespace(payload=envelope.SerializeToString()))

        service.telegram_callback.assert_not_called()

    def test_rejects_encrypted_packet_with_wrong_channel_hash(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service.telegram_callback = Mock()
        message = self.encrypted_message(
            service, packet_id=9, sender_id=0x12345678, text="wrong channel"
        )
        envelope = mqtt_service_module.mqtt_pb2.ServiceEnvelope()
        envelope.ParseFromString(message.payload)
        envelope.packet.channel = (envelope.packet.channel + 1) % 256

        service.on_message(None, None, SimpleNamespace(payload=envelope.SerializeToString()))

        service.telegram_callback.assert_not_called()

    def test_deduplicates_by_sender_and_packet_id_not_message_text(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service.telegram_callback = Mock()
        first = self.encrypted_message(
            service, packet_id=100, sender_id=0x12345678, text="same"
        )
        second = self.encrypted_message(
            service, packet_id=101, sender_id=0x12345678, text="same"
        )

        service.on_message(None, None, first)
        service.on_message(None, None, second)
        service.on_message(None, None, second)

        self.assertEqual(service.telegram_callback.call_count, 2)


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

    async def _invoke_conflict_handler(self):
        mqtt_service = Mock()
        fake_application = Mock()
        fake_application.bot_data = {}
        builder = Mock()
        builder.token.return_value = builder
        builder.post_init.return_value = builder
        builder.post_shutdown.return_value = builder
        builder.build.return_value = fake_application
        with patch.object(create_application.__globals__["Application"], "builder", return_value=builder):
            create_application("token", -100123, mqtt_service)
        error_handler = fake_application.add_error_handler.call_args.args[0]
        from telegram.error import Conflict

        await error_handler(None, SimpleNamespace(error=Conflict("duplicate polling")))
        fake_application.stop_running.assert_called_once()

    def test_polling_conflict_stops_application(self):
        import asyncio

        asyncio.run(self._invoke_conflict_handler())

    def test_limit_counts_utf8_bytes_not_characters(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service._publish_packet = Mock()

        service.send_message("測" * 78)

        service._publish_packet.assert_not_called()


class TelegramHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_are_silent_for_unauthorized_chat(self):
        message = SimpleNamespace(reply_html=AsyncMock(), reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=999),
            effective_message=message,
            effective_user=SimpleNamespace(id=123, mention_html=lambda: "user", username="user"),
        )
        context = SimpleNamespace(bot_data={"target_chat_id": -100123})

        await start(update, context)
        await help_command(update, context)

        message.reply_html.assert_not_awaited()
        message.reply_text.assert_not_awaited()

    async def test_silently_ignores_unauthorized_chat(self):
        message = SimpleNamespace(text="hello", reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=999),
            effective_message=message,
            effective_user=SimpleNamespace(id=123),
        )
        context = SimpleNamespace(bot_data={"target_chat_id": -100123})
        mqtt_service = Mock()

        await handle_message(update, context, mqtt_service=mqtt_service)

        message.reply_text.assert_not_awaited()
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
