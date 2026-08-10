import asyncio
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bridge_router import BridgeRouter
import mqtt_service as mqtt_service_module
from config import AppConfig
from meshtastic_codec import channel_hash, crypt_payload
from mqtt_service import MqttService
from runtime_state import ChatApiError, RuntimeState, StatusApiServer
from status_client import fetch_messages, send_chat_message
from telegram_bridge import LocalChatDispatcher, RouteBinding, handle_message

try:
    from test_config import valid_config
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).parent))
    from test_config import valid_config


def make_binding(*, send_result=True):
    route = AppConfig.from_dict(valid_config()).routes[0]
    service = SimpleNamespace(
        route=route,
        route_id="route-1",
        send_message=Mock(return_value=send_result),
    )
    return RouteBinding(route, service)


class ChatRuntimeStateTests(unittest.TestCase):
    def test_message_history_keeps_only_latest_200_items(self):
        state = RuntimeState()
        state.register_route("route-1", "主要路由", "localhost:1883")

        for index in range(205):
            state.record_message(
                route_id="route-1",
                source="meshtastic",
                sender="!00000001",
                text=f"message-{index + 1}",
                destinations=("telegram",),
            )

        history = state.messages_after()
        self.assertEqual(len(history["messages"]), 200)
        self.assertEqual(history["messages"][0]["id"], 6)
        self.assertEqual(history["latest_id"], 205)
        self.assertEqual(
            [message["id"] for message in state.messages_after(203)["messages"]],
            [204, 205],
        )

    def test_new_runtime_state_starts_with_empty_history(self):
        first = RuntimeState()
        first.record_message(
            route_id="route-1",
            source="telegram",
            sender="TG:1",
            text="hello",
            destinations=(),
        )

        self.assertEqual(RuntimeState().messages_after()["messages"], [])


class ChatApiTests(unittest.TestCase):
    def test_authenticated_client_reads_and_sends_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            discovery = Path(directory) / "status.json"
            state = RuntimeState()
            state.record_message(
                route_id="route-1",
                source="meshtastic",
                sender="!00000001",
                text="received",
                destinations=("telegram",),
            )
            callback = Mock(return_value={"sent": ["telegram"], "errors": {}})
            server = StatusApiServer(state, discovery, send_callback=callback)
            server.start()
            try:
                messages = fetch_messages(discovery)
                result = send_chat_message(
                    discovery,
                    route_id="route-1",
                    text="outgoing",
                    target="telegram",
                )
            finally:
                server.stop()

        self.assertEqual(messages["messages"][0]["text"], "received")
        self.assertEqual(result["sent"], ["telegram"])
        callback.assert_called_once_with(
            {"route_id": "route-1", "text": "outgoing", "target": "telegram"}
        )

    def test_messages_endpoint_rejects_missing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            discovery = Path(directory) / "status.json"
            server = StatusApiServer(RuntimeState(), discovery)
            location = server.start()
            try:
                with self.assertRaises(urllib.error.HTTPError) as captured:
                    urllib.request.urlopen(  # nosec B310 - loopback test server
                        f"http://127.0.0.1:{location.port}/messages",
                        timeout=1,
                    )
                captured.exception.close()
            finally:
                server.stop()

        self.assertEqual(captured.exception.code, 401)


class LocalChatDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.state = RuntimeState()
        self.state.register_route("route-1", "主要路由", "localhost:1883")

    def test_meshtastic_send_adds_ui_prefix_and_records_message(self):
        binding = make_binding()
        dispatcher = LocalChatDispatcher((binding,), self.state)

        result = dispatcher(
            {"route_id": "route-1", "text": "hello", "target": "meshtastic"}
        )

        binding.mqtt_service.send_message.assert_called_once_with("[Bridge UI]: hello")
        self.assertEqual(result, {"sent": ["meshtastic"], "errors": {}})
        recorded = self.state.messages_after()["messages"][0]
        self.assertEqual(recorded["source"], "bridge_ui")
        self.assertEqual(recorded["text"], "hello")

    def test_custom_display_name_updates_prefix_and_monitor_sender(self):
        binding = make_binding()
        dispatcher = LocalChatDispatcher((binding,), self.state, "基地台")

        dispatcher(
            {"route_id": "route-1", "text": "hello", "target": "meshtastic"}
        )

        binding.mqtt_service.send_message.assert_called_once_with("[基地台]: hello")
        recorded = self.state.messages_after()["messages"][0]
        self.assertEqual(recorded["sender"], "基地台")

    def test_both_targets_report_telegram_not_ready_as_partial_failure(self):
        binding = make_binding()
        dispatcher = LocalChatDispatcher((binding,), self.state)

        result = dispatcher(
            {"route_id": "route-1", "text": "hello", "target": "both"}
        )

        self.assertEqual(result["sent"], ["meshtastic"])
        self.assertEqual(result["errors"], {"telegram": "Telegram 尚未就緒"})
        self.assertEqual(
            self.state.messages_after()["messages"][0]["destinations"],
            ["meshtastic"],
        )

    def test_telegram_send_uses_selected_route(self):
        binding = make_binding()
        dispatcher = LocalChatDispatcher((binding,), self.state)
        loop = Mock()
        loop.is_closed.return_value = False
        dispatcher.bind(loop, Mock())
        completed = Mock()
        completed.result.return_value = None

        def accept_coroutine(coroutine, selected_loop):
            coroutine.close()
            self.assertIs(selected_loop, loop)
            return completed

        with patch(
            "telegram_bridge.asyncio.run_coroutine_threadsafe",
            side_effect=accept_coroutine,
        ):
            result = dispatcher(
                {"route_id": "route-1", "text": "hello", "target": "telegram"}
            )

        self.assertEqual(result, {"sent": ["telegram"], "errors": {}})
        completed.result.assert_called_once_with(timeout=15)

    def test_rejects_unknown_route_and_oversized_mesh_payload(self):
        dispatcher = LocalChatDispatcher((make_binding(),), self.state)

        with self.assertRaisesRegex(ChatApiError, "找不到指定路由"):
            dispatcher({"route_id": "missing", "text": "hello", "target": "both"})
        with self.assertRaisesRegex(ChatApiError, "233 bytes"):
            dispatcher(
                {"route_id": "route-1", "text": "測" * 74, "target": "meshtastic"}
            )

    def test_rejects_oversized_discord_only_message(self):
        route = replace(make_binding().route, discord_channel_id="123456789012345678")
        binding = RouteBinding(route, make_binding().mqtt_service)
        dispatcher = LocalChatDispatcher((binding,), self.state)

        with self.assertRaisesRegex(ChatApiError, "Discord 2000 字元"):
            dispatcher(
                {"route_id": "route-1", "text": "x" * 2000, "target": "discord"}
            )


class BridgeRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.state = RuntimeState()
        self.state.register_route("route-1", "主要路由", "localhost:1883")
        self.binding = make_binding()
        self.router = BridgeRouter((self.binding,), self.state)

    def test_meshtastic_message_is_scheduled_to_telegram_and_counted(self):
        loop = Mock()
        loop.is_closed.return_value = False
        self.router.bind_telegram(loop, Mock())
        completed = Mock()

        def accept_coroutine(coroutine, selected_loop):
            coroutine.close()
            self.assertIs(selected_loop, loop)
            return completed

        with patch(
            "bridge_router.asyncio.run_coroutine_threadsafe",
            side_effect=accept_coroutine,
        ):
            self.router.forward_meshtastic("route-1", "mesh text")

        callback = completed.add_done_callback.call_args.args[0]
        completed.result.return_value = None
        callback(completed)
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["statistics"]["mesh_to_telegram_success"], 1)
        self.assertIsNotNone(snapshot["last_forwarded_at"])

    def test_unknown_meshtastic_route_is_dropped(self):
        self.router.forward_meshtastic("missing", "mesh text")

        self.assertEqual(
            self.state.snapshot()["statistics"]["other_dropped"],
            1,
        )

    async def test_telegram_message_uses_router_and_records_monitor_event(self):
        result = await self.router.forward_telegram(
            self.binding,
            user_id=123,
            username="alice",
            text="hello",
        )

        self.assertTrue(result)
        self.binding.mqtt_service.send_message.assert_called_once_with("[TG:123]: hello")
        recorded = self.state.messages_after()["messages"][0]
        self.assertEqual(recorded["source"], "telegram")
        self.assertEqual(recorded["sender"], "@alice")
        self.assertEqual(recorded["destinations"], ["meshtastic"])

    def test_meshtastic_still_reaches_discord_when_telegram_is_unavailable(self):
        route = replace(self.binding.route, discord_channel_id="123456789012345678")
        binding = RouteBinding(route, self.binding.mqtt_service)
        router = BridgeRouter((binding,), self.state)
        completed = Future()
        completed.set_result(None)
        discord_sender = Mock(return_value=completed)
        router.bind_discord(discord_sender)

        router.forward_meshtastic("route-1", "mesh text")

        discord_sender.assert_called_once_with("123456789012345678", "mesh text")
        self.assertEqual(
            self.state.snapshot()["statistics"]["mesh_to_discord_success"],
            1,
        )

    async def test_telegram_fans_out_to_mesh_and_discord(self):
        route = replace(self.binding.route, discord_channel_id="123456789012345678")
        binding = RouteBinding(route, self.binding.mqtt_service)
        router = BridgeRouter((binding,), self.state)
        completed = Future()
        completed.set_result(None)
        discord_sender = Mock(return_value=completed)
        router.bind_discord(discord_sender)

        await router.forward_telegram(
            binding,
            user_id=123,
            username="alice",
            text="hello",
        )

        discord_sender.assert_called_once_with(
            "123456789012345678", "[TG:123]: hello"
        )
        recorded = self.state.messages_after()["messages"][0]
        self.assertEqual(recorded["destinations"], ["meshtastic", "discord"])
        self.assertEqual(
            self.state.snapshot()["statistics"]["telegram_to_mesh_success"],
            1,
        )

    async def test_discord_fans_out_to_mesh_and_telegram(self):
        loop = Mock()
        loop.is_closed.return_value = False
        self.router.bind_telegram(loop, Mock())
        completed = Future()
        completed.set_result(None)

        def accept_coroutine(coroutine, selected_loop):
            coroutine.close()
            self.assertIs(selected_loop, loop)
            return completed

        with patch(
            "bridge_router.asyncio.run_coroutine_threadsafe",
            side_effect=accept_coroutine,
        ):
            await self.router.forward_discord(
                "route-1", 456, "bob", "hello from discord"
            )

        self.binding.mqtt_service.send_message.assert_called_once_with(
            "[DC:@bob]: hello from discord"
        )
        recorded = self.state.messages_after()["messages"][0]
        self.assertEqual(recorded["source"], "discord")
        self.assertEqual(recorded["sender"], "@bob")
        self.assertEqual(recorded["destinations"], ["meshtastic", "telegram"])


class TelegramMonitoringTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorized_message_is_monitored_even_when_mesh_drops_it(self):
        state = RuntimeState()
        binding = make_binding(send_result=False)
        state.register_route("route-1", "主要路由", "localhost:1883")
        message = SimpleNamespace(text="too long", reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=binding.route.target_chat_id),
            effective_message=message,
            effective_user=SimpleNamespace(id=123, username="alice"),
        )
        context = SimpleNamespace(bot_data={"route_bindings": (binding,)})

        await handle_message(update, context, runtime_state=state)

        recorded = state.messages_after()["messages"][0]
        self.assertEqual(recorded["source"], "telegram")
        self.assertEqual(recorded["sender"], "@alice")
        self.assertEqual(recorded["text"], "too long")
        self.assertEqual(recorded["destinations"], [])

    async def test_handler_delegates_authorized_message_to_router(self):
        state = RuntimeState()
        binding = make_binding()
        router = Mock()
        router.forward_telegram = AsyncMock(return_value=True)
        message = SimpleNamespace(text="hello", reply_text=AsyncMock())
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=binding.route.target_chat_id),
            effective_message=message,
            effective_user=SimpleNamespace(id=123, username="alice"),
        )
        context = SimpleNamespace(
            bot_data={"route_bindings": (binding,), "router": router}
        )

        await handle_message(update, context, runtime_state=state)

        router.forward_telegram.assert_awaited_once_with(
            binding,
            user_id=123,
            username="alice",
            text="hello",
        )


class MeshtasticMonitoringTests(unittest.TestCase):
    def test_authenticated_mesh_message_is_added_to_monitor(self):
        config = AppConfig.from_dict(valid_config())
        state = RuntimeState()
        service = MqttService(config, runtime_state=state)
        service.set_telegram_callback(Mock())
        envelope = mqtt_service_module.mqtt_pb2.ServiceEnvelope()
        packet = envelope.packet
        packet.id = 77
        setattr(packet, "from", 0x12345678)
        packet.channel = channel_hash(service.channel, service.key)
        data = mqtt_service_module.mesh_pb2.Data()
        data.portnum = mqtt_service_module.portnums_pb2.TEXT_MESSAGE_APP
        data.payload = "mesh hello".encode("utf-8")
        packet.encrypted = crypt_payload(
            data.SerializeToString(),
            service.key,
            packet.id,
            getattr(packet, "from"),
        )

        service.on_message(
            None,
            None,
            SimpleNamespace(payload=envelope.SerializeToString()),
        )

        recorded = state.messages_after()["messages"][0]
        self.assertEqual(recorded["source"], "meshtastic")
        self.assertEqual(recorded["sender"], "!12345678")
        self.assertEqual(recorded["text"], "mesh hello")


if __name__ == "__main__":
    unittest.main()
