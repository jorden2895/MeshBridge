from __future__ import annotations

import asyncio
import base64
import threading
from dataclasses import dataclass

import discord
import paho.mqtt.client as mqtt
from telegram import Bot

from config import AppConfig


CONNECTION_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class ConnectionTestResult:
    service: str
    succeeded: bool
    message: str


def _safe_error_message(exc: Exception, secrets: tuple[str, ...]) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    return message


def probe_telegram(config: AppConfig) -> str:
    async def get_identity() -> str:
        async with Bot(config.telegram.bot_token) as bot:
            user = await bot.get_me()
            return f"@{user.username}" if user.username else user.full_name
    return asyncio.run(get_identity())


def probe_mqtt(config: AppConfig, timeout: float = CONNECTION_TIMEOUT_SECONDS) -> str:
    connected = threading.Event()
    outcome: dict[str, str] = {}
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if config.mqtt.username:
        client.username_pw_set(config.mqtt.username, config.mqtt.password or None)

    def on_connect(client, userdata, flags, reason_code, properties) -> None:
        outcome["success" if reason_code == 0 else "error"] = (
            f"{config.mqtt.broker}:{config.mqtt.port}"
            if reason_code == 0
            else f"Broker 拒絕連線：{reason_code}"
        )
        connected.set()

    def on_connect_fail(client, userdata) -> None:
        outcome["error"] = "無法連線至 Broker"
        connected.set()

    client.on_connect = on_connect
    client.on_connect_fail = on_connect_fail
    try:
        client.connect(config.mqtt.broker, config.mqtt.port, 60)
        client.loop_start()
        if not connected.wait(timeout):
            raise TimeoutError("連線逾時")
        if "error" in outcome:
            raise ConnectionError(outcome["error"])
        return outcome["success"]
    finally:
        if client.is_connected():
            client.disconnect()
        client.loop_stop()


def probe_discord(config: AppConfig) -> str:
    async def get_identity() -> str:
        client = discord.Client(intents=discord.Intents.none())
        try:
            await client.login(config.discord.bot_token)
            return str(client.user)
        finally:
            await client.close()
    return asyncio.run(get_identity())


def check_connections(
    config: AppConfig,
    telegram_probe=probe_telegram,
    mqtt_probe=probe_mqtt,
    discord_probe=probe_discord,
) -> list[ConnectionTestResult]:
    secrets = (
        config.telegram.bot_token,
        config.discord.bot_token,
        config.mqtt.password,
        *(base64.b64encode(route.channel_key).decode("ascii") for route in config.routes),
    )
    probes = [("MQTT", mqtt_probe)]
    if config.telegram.enabled:
        probes.insert(0, ("Telegram", telegram_probe))
    if config.discord.enabled:
        probes.append(("Discord", discord_probe))
    results: list[ConnectionTestResult] = []
    for service, probe in probes:
        try:
            results.append(ConnectionTestResult(service, True, f"連線成功：{probe(config)}"))
        except Exception as exc:
            results.append(
                ConnectionTestResult(service, False, f"連線失敗：{_safe_error_message(exc, secrets)}")
            )
    return results
