import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from config import AppConfig, ConfigError
from main import RedactingFormatter
from mqtt_service import MqttService, MqttServiceError
from runtime_state import RuntimeState, StatusApiServer
from status_client import StatusUnavailable, fetch_status
from telegram_bridge import RouteBinding, ThreadSafeApplicationStop, handle_message
from update_service import ReleaseInfo, is_newer, record_check, should_check
from update_monitor import UpdateMonitor

from test_config import valid_config


def multi_route_config(count=2):
    raw = valid_config()
    raw["features"] = {"multi_route_enabled": True}
    raw["routes"] = [
        {
            "name": f"路由 {index + 1}",
            "enabled": True,
            "channel_name": f"Channel{index + 1}",
            "channel_key": "AQ==",
            "target_chat_id": -1000 - index,
            "topic_id": index + 1,
        }
        for index in range(count)
    ]
    return raw


class ConfigurationRoadmapTests(unittest.TestCase):
    def test_legacy_config_remains_compatible_with_safe_defaults(self):
        config = AppConfig.from_dict(valid_config())
        self.assertEqual(len(config.routes), 1)
        self.assertTrue(config.features.statistics_enabled)
        self.assertTrue(config.features.status.enabled)
        self.assertFalse(config.features.multi_route_enabled)
        self.assertFalse(config.features.tray.enabled)
        self.assertFalse(config.features.updates.enabled)

    def test_anonymous_mqtt_and_normalized_root_topic_are_allowed(self):
        raw = valid_config()
        raw["mqtt"]["username"] = ""
        raw["mqtt"]["password"] = ""
        raw["mqtt"]["root_topic"] = " /custom/root/ "
        config = AppConfig.from_dict(raw)
        self.assertEqual(config.mqtt.username, "")
        self.assertEqual(config.mqtt.root_topic, "custom/root/")

    def test_root_topic_wildcards_are_rejected(self):
        raw = valid_config()
        raw["mqtt"]["root_topic"] = "custom/+/root"
        with self.assertRaisesRegex(ConfigError, "wildcard"):
            AppConfig.from_dict(raw)

    def test_routes_are_limited_to_five_and_one_to_one(self):
        self.assertEqual(len(AppConfig.from_dict(multi_route_config(5)).active_routes), 5)
        with self.assertRaisesRegex(ConfigError, "1 到 5"):
            AppConfig.from_dict(multi_route_config(6))
        duplicate = multi_route_config(2)
        duplicate["routes"][1]["target_chat_id"] = duplicate["routes"][0]["target_chat_id"]
        duplicate["routes"][1]["topic_id"] = duplicate["routes"][0]["topic_id"]
        with self.assertRaisesRegex(ConfigError, "聊天室"):
            AppConfig.from_dict(duplicate)


class RuntimeStatusTests(unittest.TestCase):
    def test_statistics_reset_and_errors_are_redacted(self):
        state = RuntimeState(secrets_to_redact=("secret-token",))
        state.register_route("route-1", "主要", "broker:1883")
        state.increment("duplicate_packets")
        state.set_mqtt("route-1", "error", "bad secret-token")
        snapshot = state.snapshot()
        self.assertEqual(snapshot["statistics"]["duplicate_packets"], 1)
        self.assertEqual(snapshot["routes"]["route-1"]["last_error"], "bad ***")
        self.assertEqual(
            RuntimeState().snapshot()["statistics"]["duplicate_packets"],
            0,
        )

    def test_local_status_api_requires_token_and_reports_staleness(self):
        with tempfile.TemporaryDirectory() as directory:
            discovery = Path(directory) / "status.json"
            state = RuntimeState()
            server = StatusApiServer(state, discovery)
            server.start()
            try:
                self.assertEqual(fetch_status(discovery)["pid"], server.state.snapshot()["pid"])
                state._heartbeat = 0
                with self.assertRaises(StatusUnavailable):
                    fetch_status(discovery)
            finally:
                server.stop()
            self.assertFalse(discovery.exists())

    def test_log_formatter_redacts_known_secrets(self):
        import logging

        formatter = RedactingFormatter("%(message)s", ("bot-secret",))
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "request failed at https://api.telegram.org/bot-secret/getMe",
            (),
            None,
        )
        self.assertNotIn("bot-secret", formatter.format(record))


class MqttStartupPolicyTests(unittest.TestCase):
    def test_subscription_failure_is_fatal(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service.client.subscribe = Mock(return_value=(1, 1))
        service.fatal_callback = Mock()
        service.on_connect(service.client, None, None, 0, None)
        self.assertFalse(service._mqtt_connected)
        self.assertIsNotNone(service._connect_error)
        service.fatal_callback.assert_called_once()

    def test_failed_start_always_stops_network_loop(self):
        service = MqttService(AppConfig.from_dict(valid_config()))
        service.client.connect = Mock(side_effect=OSError("offline"))
        service.client.loop_stop = Mock()
        service.client.is_connected = Mock(return_value=False)
        with self.assertRaises(MqttServiceError):
            service.start()
        service.client.loop_stop.assert_called_once()


class MultiRouteTelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_topic_selects_exact_route(self):
        config = AppConfig.from_dict(multi_route_config(2))
        services = [Mock(), Mock()]
        bindings = tuple(
            RouteBinding(route, service)
            for route, service in zip(config.routes, services)
        )
        context = SimpleNamespace(bot_data={"route_bindings": bindings})
        message = SimpleNamespace(
            text="hello",
            message_thread_id=2,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=-1001),
            effective_message=message,
            effective_user=SimpleNamespace(id=123),
        )
        await handle_message(update, context)
        services[0].send_message.assert_not_called()
        services[1].send_message.assert_called_once()


class ThreadSafeShutdownTests(unittest.TestCase):
    def test_stop_is_dispatched_to_application_event_loop(self):
        callback = Mock()
        loop = Mock()
        loop.is_closed.return_value = False
        request_stop = ThreadSafeApplicationStop(callback)
        request_stop.bind_loop(loop)

        request_stop()

        loop.call_soon_threadsafe.assert_called_once_with(callback)
        callback.assert_not_called()

    def test_early_stop_request_runs_after_loop_is_bound(self):
        callback = Mock()
        loop = Mock()
        request_stop = ThreadSafeApplicationStop(callback)
        request_stop()

        request_stop.bind_loop(loop)

        loop.call_soon.assert_called_once_with(callback)


class UpdatePolicyTests(unittest.TestCase):
    def test_only_higher_semver_is_newer(self):
        release = ReleaseInfo("v1.4.0", "https://example.invalid", ())
        self.assertTrue(is_newer(release, "1.3.9"))
        self.assertFalse(is_newer(release, "1.4.0"))

    def test_update_interval_is_persisted_without_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "updates.json"
            self.assertTrue(should_check(state_path, 24))
            record_check(state_path)
            self.assertFalse(should_check(state_path, 24))
            self.assertEqual(set(json.loads(state_path.read_text()).keys()), {"last_checked"})

    def test_background_monitor_notifies_for_new_stable_release(self):
        with tempfile.TemporaryDirectory() as directory:
            notify = Mock()
            monitor = UpdateMonitor(
                current_version="1.2.0",
                mode="notify",
                interval_hours=24,
                application_directory=Path(directory),
                notify=notify,
                stop_application=Mock(),
            )
            release = ReleaseInfo("v1.4.0", "https://example.invalid", ())
            with patch("update_monitor.fetch_latest_release", return_value=release):
                monitor._check_once()
            notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
