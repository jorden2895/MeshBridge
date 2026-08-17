import asyncio
import copy
import json
import tempfile
import unittest
from concurrent.futures import Future
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from automation import AutomationEngine, format_eew_message, normalize_eew
from bridge_router import BridgeRouter, RouteBinding
from config import AppConfig, ConfigError, CURRENT_CONFIG_VERSION
from config_store import (
    load_config_data,
    migrate_v2_to_v3,
    migrate_v3_to_v4,
    migrate_v4_to_v5,
)
from main import build_parser, resolve_eew_arguments
from runtime_state import RuntimeState
from test_config import valid_config


def automation_config():
    raw = migrate_v4_to_v5(migrate_v3_to_v4(migrate_v2_to_v3(valid_config())))
    route_name = raw["routes"][0]["name"]
    raw["routes"][0]["eew_enabled"] = True
    raw["automations"] = {
        "keyword_rules": [
            {
                "name": "精確",
                "enabled": True,
                "routes": [route_name],
                "match": "exact",
                "keyword": "狀態",
                "response": "正常",
            },
            {
                "name": "包含",
                "enabled": True,
                "routes": [route_name],
                "match": "contains",
                "keyword": "狀",
                "response": "第二條",
            },
        ],
        "eew": {"dedupe_seconds": 60},
        "schedules": [
            {
                "name": "平日",
                "enabled": True,
                "routes": [route_name],
                "cron": "0 9 * * 1-5",
                "message": "早安",
            }
        ],
    }
    return raw


class AutomationConfigTests(unittest.TestCase):
    def test_parses_keyword_eew_and_five_field_cron(self):
        config = AppConfig.from_dict(automation_config())
        self.assertEqual(config.automations.keyword_rules[0].match, "exact")
        self.assertEqual(config.automations.eew.routes, (config.routes[0].name,))
        self.assertEqual(config.automations.schedules[0].cron, "0 9 * * 1-5")

    def test_rejects_unknown_route_invalid_cron_and_oversized_message(self):
        cases = []
        unknown = automation_config()
        unknown["automations"]["keyword_rules"][0]["routes"] = ["不存在"]
        cases.append(unknown)
        cron = automation_config()
        cron["automations"]["schedules"][0]["cron"] = "* * * * * *"
        cases.append(cron)
        oversized = automation_config()
        oversized["automations"]["keyword_rules"][0]["response"] = "測" * 78
        cases.append(oversized)
        for raw in cases:
            with self.subTest(raw=raw["automations"]):
                with self.assertRaises(ConfigError):
                    AppConfig.from_dict(raw)

    def test_v3_is_backed_up_and_migrated_to_v5(self):
        original = migrate_v2_to_v3(valid_config())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(original), encoding="utf-8")
            migrated, changed = load_config_data(path)
            backup = json.loads((path.parent / "config.v3.backup.json").read_text(encoding="utf-8"))
        self.assertTrue(changed)
        self.assertEqual(backup, original)
        self.assertEqual(migrated["config_version"], CURRENT_CONFIG_VERSION)
        self.assertEqual(migrated["automations"]["keyword_rules"], [])

    def test_v4_eew_destinations_move_to_route_switches(self):
        original = migrate_v3_to_v4(migrate_v2_to_v3(valid_config()))
        route_name = original["routes"][0]["name"]
        original["automations"]["eew"] = {
            "enabled": True,
            "routes": [route_name],
            "dedupe_seconds": 60,
        }
        migrated = migrate_v4_to_v5(original)
        self.assertTrue(migrated["routes"][0]["eew_enabled"])
        self.assertEqual(migrated["automations"]["eew"], {"dedupe_seconds": 60})
        self.assertEqual(AppConfig.from_dict(migrated).automations.eew.routes, (route_name,))

    def test_disabled_route_is_not_an_eew_destination(self):
        raw = automation_config()
        raw["routes"][0]["enabled"] = False
        raw["routes"].append({**raw["routes"][0], "name": "備用路由", "enabled": True, "channel_name": "Backup", "target_chat_id": "456", "eew_enabled": False})
        config = AppConfig.from_dict(raw)
        self.assertFalse(config.automations.eew.enabled)
        self.assertEqual(config.automations.eew.routes, ())


class AutomationEngineTests(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig.from_dict(automation_config())
        self.router = Mock()
        self.router.send_to_routes.return_value = {"sent": ["主要路由/meshtastic"], "errors": {}}
        self.engine = AutomationEngine(self.config.automations, self.router)

    def test_keyword_is_casefolded_trimmed_and_first_match_wins(self):
        route_name = self.config.routes[0].name
        self.assertEqual(self.engine.keyword_response(route_name, "  狀態  "), "正常")
        self.assertIsNone(self.engine.keyword_response(route_name, "沒有命中"))

    def test_eew_is_sent_once_with_ground_cow_source_label(self):
        first = self.engine.send_eew("5+", "20")
        second = self.engine.send_eew("5+", 20)
        self.assertFalse(first.get("duplicate", False))
        self.assertTrue(second["duplicate"])
        text = self.router.send_to_routes.call_args.args[1]
        self.assertIn("震度5強", text)
        self.assertIn("來源：地牛", text)
        self.assertNotIn("私人／內部參考", text)

    def test_eew_validation_and_cron_next_run(self):
        self.assertEqual(normalize_eew("6-", "0"), ("6-", 0))
        with self.assertRaises(ValueError):
            format_eew_message("8", 10)
        base = datetime(2026, 8, 17, 8, 30, tzinfo=timezone.utc)
        self.assertEqual(
            AutomationEngine.next_run("0 9 * * *", base),
            datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc),
        )

    def test_eew_without_enabled_route_fails_clearly(self):
        raw = automation_config()
        raw["routes"][0]["eew_enabled"] = False
        config = AppConfig.from_dict(raw)
        engine = AutomationEngine(config.automations, self.router)
        with self.assertRaisesRegex(RuntimeError, "尚未啟用"):
            engine.send_eew("5+", "20")

    def test_cli_accepts_ground_cow_arguments(self):
        parser = build_parser()
        args = parser.parse_args(["5+", "20"])
        self.assertEqual(resolve_eew_arguments(parser, args), ["5+", "20"])

    def test_cli_keeps_manual_eew_flag_compatibility(self):
        parser = build_parser()
        args = parser.parse_args(["--eew", "5+", "20"])
        self.assertEqual(resolve_eew_arguments(parser, args), ["5+", "20"])

    def test_cli_accepts_ground_cow_v42_named_arguments(self):
        parser = build_parser()
        args = parser.parse_args([
            "--epicenter-lat=23.85",
            "--epicenter-lon=120.82",
            "--depth=10",
            "--magnitude=7.3",
            "--max-intensity=7",
            "--local-intensity=5+",
            "--arrival-time=1717142400",
            "--remaining-time=20",
        ])
        self.assertEqual(resolve_eew_arguments(parser, args), ["5+", "20"])


class KeywordSourceReplyTests(unittest.IsolatedAsyncioTestCase):
    def binding(self, **route_changes):
        config = AppConfig.from_dict(automation_config())
        route = replace(config.routes[0], **route_changes)
        mqtt = Mock()
        mqtt.route_id = "route-1"
        mqtt.send_message.return_value = True
        return RouteBinding(route, mqtt)

    def test_meshtastic_reply_stays_on_meshtastic(self):
        binding = self.binding(telegram_enabled=False, discord_enabled=False)
        router = BridgeRouter((binding,), RuntimeState())
        router.bind_automation(Mock(keyword_response=Mock(return_value="自動回覆")))
        router.forward_meshtastic("route-1", "[Node !12345678]: 狀態")
        binding.mqtt_service.send_message.assert_called_once_with("自動回覆")

    async def test_telegram_reply_stays_on_telegram(self):
        binding = self.binding(discord_enabled=False)
        router = BridgeRouter((binding,), RuntimeState())
        router.bind_automation(Mock(keyword_response=Mock(return_value="自動回覆")))
        bot = Mock()
        bot.send_message = AsyncMock()
        router.bind_telegram(asyncio.get_running_loop(), bot)
        await router.forward_telegram(binding, user_id=1, username="user", text="狀態")
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["text"], "自動回覆")

    async def test_discord_reply_stays_on_discord(self):
        binding = self.binding(
            telegram_enabled=False,
            target_chat_id=None,
            discord_enabled=True,
            discord_channel_id="123",
        )
        router = BridgeRouter((binding,), RuntimeState())
        router.bind_automation(Mock(keyword_response=Mock(return_value="自動回覆")))

        def send(_channel, _text):
            future = Future()
            future.set_result(None)
            return future

        sender = Mock(side_effect=send)
        router.bind_discord(sender)
        await router.forward_discord("route-1", 2, "user", "狀態")
        self.assertIn(("123", "自動回覆"), [call.args for call in sender.call_args_list])


if __name__ == "__main__":
    unittest.main()
