import copy
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import bridge_runtime as runtime_module
import single_instance as instance_module
from app_controller import AppController
from bridge_runtime import BridgeRuntime
from config import AppConfig, ConfigError
from config_store import load_config_data, migrate_v2_to_v3
from test_config import valid_config
from update_service import ReleaseAsset, ReleaseInfo, UpdateError, download_portable_release


def v3_config(*, telegram=True, discord=False):
    raw = valid_config()
    raw["config_version"] = 2
    if not telegram:
        raw["telegram"] = {"bot_token": "", "target_chat_id": None}
    if discord:
        raw["discord"] = {"enabled": True, "bot_token": "discord-token"}
        raw["routes"] = [{
            "name": "主要路由",
            "enabled": True,
            "telegram_enabled": telegram,
            "discord_enabled": True,
            "channel_name": "Test",
            "channel_key": "AQ==",
            "target_chat_id": -100123 if telegram else None,
            "topic_id": None,
            "discord_channel_id": "123456789012345678",
        }]
    return migrate_v2_to_v3(raw)


class ConfigMigrationTests(unittest.TestCase):
    def test_v2_is_backed_up_and_migrated_only_once(self):
        original = valid_config()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            migrated, changed = load_config_data(path)
            loaded_again, changed_again = load_config_data(path)
            backup = json.loads((path.parent / "config.v2.backup.json").read_text(encoding="utf-8"))

        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(backup, original)
        self.assertEqual(migrated, loaded_again)
        self.assertEqual(migrated["config_version"], 3)
        self.assertNotIn("status_api", migrated["features"])
        self.assertNotIn("enabled", migrated["discord"])

    def test_failed_migration_preserves_original(self):
        original = valid_config()
        original["mqtt"]["port"] = 70000
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaises(ConfigError):
                load_config_data(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((path.parent / "config.v2.backup.json").exists())

    def test_future_config_version_is_rejected_without_rewrite(self):
        original = v3_config()
        original["config_version"] = 4
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaisesRegex(ConfigError, "不支援"):
                load_config_data(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse((path.parent / "config.v2.backup.json").exists())

    def test_malformed_v2_section_raises_config_error(self):
        original = valid_config()
        original["telegram"] = "bad"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "telegram"):
                load_config_data(path)


class RuntimeLifecycleTests(unittest.TestCase):
    def test_discord_only_runtime_skips_telegram_and_cleans_up(self):
        config = AppConfig.from_dict(v3_config(telegram=False, discord=True))
        mqtt = Mock()
        mqtt.route_id = "route-1"
        discord = Mock()
        discord.schedule_text = Mock()
        with patch.object(runtime_module, "MqttService", return_value=mqtt), patch.object(
            runtime_module, "DiscordBridge", return_value=discord
        ), patch.object(runtime_module, "create_application") as create_application:
            runtime = BridgeRuntime(config)
            runtime.start()
            self.assertTrue(runtime.running)
            runtime.stop()

        create_application.assert_not_called()
        mqtt.start.assert_called_once()
        mqtt.stop.assert_called_once()
        discord.start.assert_called_once()
        discord.stop.assert_called_once()

    def test_partial_mqtt_start_failure_stops_started_routes(self):
        raw = v3_config()
        second = copy.deepcopy(raw["routes"][0])
        second.update(name="第二路由", channel_name="Second", target_chat_id=-100124)
        raw["routes"].append(second)
        config = AppConfig.from_dict(raw)
        first, second_service = Mock(), Mock()
        first.route_id, second_service.route_id = "route-1", "route-2"
        second_service.start.side_effect = OSError("offline")
        application = Mock()
        application.bot_data = {"request_stop": Mock()}
        with patch.object(runtime_module, "MqttService", side_effect=[first, second_service]), patch.object(
            runtime_module, "create_application", return_value=application
        ), patch.object(runtime_module, "start_bot"), patch.object(
            BridgeRuntime, "_wait_telegram_ready"
        ):
            runtime = BridgeRuntime(config)
            with self.assertRaises(Exception):
                runtime.start()

        first.stop.assert_called()
        second_service.stop.assert_called()
        self.assertFalse(runtime.running)

    def test_mqtt_is_started_before_platform_adapters(self):
        config = AppConfig.from_dict(v3_config(telegram=True, discord=True))
        order = []
        mqtt = Mock()
        mqtt.route_id = "route-1"
        mqtt.start.side_effect = lambda: order.append("mqtt")
        application = Mock()
        application.bot_data = {"request_stop": Mock()}
        with patch.object(runtime_module, "MqttService", return_value=mqtt), patch.object(
            BridgeRuntime, "_start_telegram", side_effect=lambda: order.append("telegram")
        ), patch.object(
            BridgeRuntime, "_start_discord", side_effect=lambda: order.append("discord")
        ):
            runtime = BridgeRuntime(config)
            runtime.start()
            runtime.stop()

        self.assertEqual(order, ["mqtt", "telegram", "discord"])

    def test_unexpected_telegram_exit_stops_runtime(self):
        config = AppConfig.from_dict(v3_config())
        mqtt = Mock()
        mqtt.route_id = "route-1"
        application = Mock()
        application.bot_data = {"request_stop": Mock()}
        release_bot = runtime_module.threading.Event()

        def exit_bot(_application):
            release_bot.wait(1)

        with patch.object(runtime_module, "MqttService", return_value=mqtt), patch.object(
            runtime_module, "create_application", return_value=application
        ), patch.object(runtime_module, "start_bot", side_effect=exit_bot), patch.object(
            BridgeRuntime, "_wait_telegram_ready"
        ):
            runtime = BridgeRuntime(config)
            runtime.start()
            release_bot.set()
            deadline = time.time() + 2
            while runtime.running and time.time() < deadline:
                time.sleep(0.02)

        self.assertFalse(runtime.running)
        mqtt.stop.assert_called()
        self.assertEqual(runtime.snapshot()["bridge"]["status"], "stopped")

    def test_intentional_stop_marks_discord_stopped(self):
        config = AppConfig.from_dict(v3_config(telegram=False, discord=True))
        mqtt = Mock()
        mqtt.route_id = "route-1"
        discord = Mock()
        discord.schedule_text = Mock()
        with patch.object(runtime_module, "MqttService", return_value=mqtt), patch.object(
            runtime_module, "DiscordBridge", return_value=discord
        ):
            runtime = BridgeRuntime(config)
            runtime.start()
            runtime.stop()

        self.assertEqual(runtime.snapshot()["discord"]["status"], "stopped")


class ControllerRollbackTests(unittest.TestCase):
    def test_failed_apply_restores_old_file_and_runtime(self):
        old_raw = v3_config()
        new_raw = copy.deepcopy(old_raw)
        new_raw["node"]["long_name"] = "New Bridge"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(old_raw), encoding="utf-8")
            controller = AppController(path)
            controller.load()
            old_runtime = Mock(running=True)
            controller.runtime = old_runtime
            failed_runtime = Mock()
            failed_runtime.start.side_effect = OSError("cannot connect")
            restored_runtime = Mock()
            with patch("app_controller.BridgeRuntime", side_effect=[failed_runtime, restored_runtime]), patch(
                "app_controller.sync_autostart"
            ):
                controller.apply_async(new_raw)
                result = None
                deadline = time.time() + 2
                while time.time() < deadline:
                    kind, payload = controller.events.get(timeout=1)
                    if kind == "operation" and not payload["running"]:
                        result = payload
                        break

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), old_raw)
            self.assertFalse(result["ok"])
            self.assertIn("已還原舊設定", result["error"])
            failed_runtime.stop.assert_called_once()
            restored_runtime.start.assert_called_once()

    def test_autostart_rollback_failure_does_not_leave_rejected_runtime(self):
        old_raw = v3_config()
        new_raw = copy.deepcopy(old_raw)
        new_raw["node"]["long_name"] = "Rejected Bridge"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(old_raw), encoding="utf-8")
            controller = AppController(path)
            controller.load()
            controller.runtime = Mock(running=True)
            rejected_runtime = Mock()
            restored_runtime = Mock()
            with patch(
                "app_controller.BridgeRuntime",
                side_effect=[rejected_runtime, restored_runtime],
            ), patch(
                "app_controller.sync_autostart",
                side_effect=[OSError("denied"), OSError("denied")],
            ):
                controller.apply_async(new_raw)
                result = None
                deadline = time.time() + 2
                while time.time() < deadline:
                    kind, payload = controller.events.get(timeout=1)
                    if kind == "operation" and not payload["running"]:
                        result = payload
                        break

        self.assertFalse(result["ok"])
        self.assertIs(controller.runtime, restored_runtime)
        self.assertEqual(controller.config.node.long_name, old_raw["node"]["long_name"])
        restored_runtime.start.assert_called_once()


class SingleInstanceTests(unittest.TestCase):
    def test_second_instance_notifies_primary(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        commands = []
        with patch.object(instance_module, "PORT", port):
            primary = instance_module.SingleInstance(commands.append)
            secondary = instance_module.SingleInstance(Mock())
            self.assertTrue(primary.acquire())
            self.assertFalse(secondary.acquire("show:settings"))
            deadline = time.time() + 2
            while not commands and time.time() < deadline:
                time.sleep(0.02)
            primary.close()
        self.assertEqual(commands, ["show:settings"])

    def test_unrelated_listener_does_not_make_application_exit(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)

        def reject_probe():
            client, _ = listener.accept()
            with client:
                client.recv(1024)
                client.sendall(b"not-meshbridge\n")

        thread = instance_module.threading.Thread(target=reject_probe, daemon=True)
        thread.start()
        with patch.object(instance_module, "PORT", port):
            instance = instance_module.SingleInstance(Mock())
            self.assertTrue(instance.acquire())
        listener.close()
        thread.join(timeout=1)


class SingleAssetUpdateTests(unittest.TestCase):
    def test_portable_update_downloads_only_meshbridge_executable(self):
        release = ReleaseInfo(
            "v3.0.1",
            "https://github.com/example/release",
            (
                ReleaseAsset("MeshBridge.exe", "https://github.com/a", "sha256:abc"),
                ReleaseAsset("MeshBridgeSettings.exe", "https://github.com/b", "sha256:def"),
            ),
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "update_service.download_asset", side_effect=lambda asset, destination: destination
        ) as download:
            files = download_portable_release(release, Path(directory))
        self.assertEqual([path.name for path in files], ["MeshBridge.exe"])
        self.assertEqual(download.call_count, 1)

    def test_portable_update_rejects_release_without_main_executable(self):
        release = ReleaseInfo("v3.0.1", "https://github.com/example/release", ())
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(UpdateError):
            download_portable_release(release, Path(directory))


if __name__ == "__main__":
    unittest.main()
