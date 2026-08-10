# MeshBridge MQTT service

import paho.mqtt.client as mqtt
import logging
import time
import random
import threading
from collections import deque

from config import AppConfig, RouteConfig
from meshtastic_codec import channel_hash, crypt_payload
from runtime_state import RuntimeState

try:
    from meshtastic.protobuf import mesh_pb2, mqtt_pb2, portnums_pb2
    from meshtastic import BROADCAST_NUM
except ImportError as exc:
    raise RuntimeError("meshtastic is not installed; run: pip install -r requirements.txt") from exc

# logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
MAX_MESHTASTIC_PAYLOAD_BYTES = mesh_pb2.Constants.DATA_PAYLOAD_LEN
RECONNECT_FAILURE_LOG_INTERVAL_SECONDS = 30
NODE_INFO_INITIAL_DELAY_SECONDS = 2
NODE_INFO_INTERVAL_SECONDS = 15 * 60

class MqttServiceError(RuntimeError):
    """Raised when the MQTT service cannot start or publish a message."""

class MqttService:
    def __init__(
        self,
        config: AppConfig,
        route: RouteConfig | None = None,
        runtime_state: RuntimeState | None = None,
        route_id: str = "route-1",
    ):
        self.telegram_callback = None
        self.fatal_callback = None
        # Store tuples of ((from_node_id, packet_id), timestamp) for MQTT redelivery deduplication.
        self.recent_packets = deque(maxlen=200)
        self.dedup_window_seconds = 60  # Deduplication time window in seconds

        # MQTT settings from config
        self.broker = config.mqtt.broker
        self.port = config.mqtt.port
        self.username = config.mqtt.username
        self.password = config.mqtt.password
        self.root_topic = config.mqtt.root_topic
        selected_route = route or config.routes[0]
        self.route = selected_route
        self.route_id = route_id
        self.runtime_state = runtime_state
        self.channel = selected_route.channel_name
        self.key = selected_route.channel_key

        # Node settings
        self.node_id = config.node.node_id
        self.long_name = config.node.long_name
        self.short_name = config.node.short_name
        self.node_name = f"!{self.node_id:08x}"

        # MQTT topics
        self.subscribe_topic = f"{self.root_topic}{self.channel}/#"
        self.publish_topic = f"{self.root_topic}{self.channel}/{self.node_name}"
        
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self.client.on_connect = self.on_connect
        self.client.on_connect_fail = self.on_connect_fail
        self.client.on_message = self.on_message
        self.client.on_disconnect = self.on_disconnect
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

        self.global_message_id = random.randint(1, 0xFFFFFFFF)
        self._message_id_lock = threading.Lock()
        self._connect_event = threading.Event()
        self._connect_error = None
        self._node_info_timer = None
        self._node_info_lock = threading.Lock()
        self._stopping = False
        self._has_connected_once = False
        self._mqtt_connected = False
        self._last_reconnect_failure_log = 0.0
        if self.runtime_state is not None:
            self.runtime_state.register_route(
                self.route_id,
                self.route.name,
                f"{self.broker}:{self.port}",
            )

    def _set_status(self, status: str, error: str | None = None) -> None:
        if self.runtime_state is not None:
            self.runtime_state.set_mqtt(self.route_id, status, error)

    def _increment(self, key: str) -> None:
        if self.runtime_state is not None:
            self.runtime_state.increment(key)

    def set_telegram_callback(self, callback):
        self.telegram_callback = callback

    def set_fatal_callback(self, callback):
        self.fatal_callback = callback

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            logger.info("正在訂閱 MQTT 主題：%s", self.subscribe_topic)
            result, _ = client.subscribe(self.subscribe_topic)
            if result != mqtt.MQTT_ERR_SUCCESS:
                self._connect_error = f"failed to subscribe to {self.subscribe_topic}: MQTT error {result}"
                self._mqtt_connected = False
                self._set_status("error", self._connect_error)
                logger.error("MQTT 主題訂閱失敗：錯誤代碼 %s", result)
                self._connect_event.set()
                if self.fatal_callback is not None:
                    self.fatal_callback(self._connect_error)
                return
            if self._has_connected_once:
                logger.info("MQTT 已重新連線：%s:%s", self.broker, self.port)
            else:
                logger.info("MQTT 連線成功：%s:%s", self.broker, self.port)
            self._has_connected_once = True
            self._mqtt_connected = True
            self._set_status("connected")
            self._last_reconnect_failure_log = 0.0
            self._connect_error = None
            # A reconnect replaces the pending NodeInfo broadcast instead of leaking timers.
            self._schedule_node_info(NODE_INFO_INITIAL_DELAY_SECONDS)
        else:
            self._mqtt_connected = False
            self._connect_error = f"broker rejected connection: {reason_code}"
            self._set_status("error", self._connect_error)
            logger.error("MQTT 連線遭 Broker 拒絕：%s", reason_code)
        self._connect_event.set()

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        was_connected = self._mqtt_connected
        self._mqtt_connected = False
        if not self._stopping:
            if was_connected:
                self._set_status("reconnecting", str(reason_code))
                logger.warning(
                    "MQTT 連線中斷：%s；程式將自動嘗試重新連線。", reason_code
                )

    def on_connect_fail(self, client, userdata):
        """Reports reconnect failures without flooding the log."""
        if self._stopping or not self._has_connected_once:
            return
        now = time.monotonic()
        if now - self._last_reconnect_failure_log < RECONNECT_FAILURE_LOG_INTERVAL_SECONDS:
            return
        self._last_reconnect_failure_log = now
        self._set_status("reconnecting")
        logger.warning("MQTT 重新連線尚未成功，程式將繼續嘗試。")

    def on_message(self, client, userdata, msg):
        try:
            se = mqtt_pb2.ServiceEnvelope()
            se.ParseFromString(msg.payload)
            mp = se.packet
            
            # Encrypted MQTT topics must never trust a caller-supplied decoded field.
            expected_channel = channel_hash(self.channel, self.key)
            if (
                not mp.HasField("encrypted")
                or mp.channel != expected_channel
                or not self.decode_encrypted(mp)
            ):
                self._increment("decrypt_failed")
                logger.warning("Ignoring unauthenticated MQTT packet ID %s.", getattr(mp, "id", 0))
                return

            # Process only decoded text messages from other nodes
            if mp.decoded.portnum == portnums_pb2.TEXT_MESSAGE_APP and getattr(mp, "from") != self.node_id:
                sender_node_id = getattr(mp, "from")
                payload_bytes = mp.decoded.payload
                
                # MQTT retries keep the same packet ID; repeated text with a new ID is legitimate.
                unique_key = (sender_node_id, getattr(mp, "id", 0))
                current_time = time.time()

                # 1. Purge old packets from the cache
                while self.recent_packets and self.recent_packets[0][1] < current_time - self.dedup_window_seconds:
                    self.recent_packets.popleft()

                # 2. Check if the unique key is in the recent cache
                if any(unique_key == p_key for p_key, ts in self.recent_packets):
                    self._increment("duplicate_packets")
                    logger.info(
                        "Ignoring duplicate MQTT packet %s from node !%08x.",
                        getattr(mp, "id", 0),
                        sender_node_id,
                    )
                    return
                
                # 3. Add the new packet to the cache
                self.recent_packets.append((unique_key, current_time))
                
                # --- End Deduplication Check ---

                text_payload = payload_bytes.decode("utf-8")

                # Check if the message originated from the Telegram bridge to prevent a loop
                if text_payload.strip().startswith("[TG:"):
                    return

                sender_id_hex = f"!{sender_node_id:08x}"
                from_name = f"Node {sender_id_hex}"
                
                message_to_forward = f"[{from_name}]: {text_payload}"
                packet_id = getattr(mp, "id", 0)
                logger.info(
                    "Received Meshtastic text packet from %s (packet_id=%s, payload_bytes=%s)",
                    sender_id_hex,
                    packet_id,
                    len(payload_bytes),
                )
                logger.debug(
                    "DEBUG ONLY (privacy risk): full Meshtastic message to forward: %s",
                    message_to_forward,
                )
                
                if self.runtime_state is not None:
                    self.runtime_state.record_message(
                        route_id=self.route_id,
                        source="meshtastic",
                        sender=sender_id_hex,
                        text=text_payload,
                        destinations=("telegram",),
                    )

                if self.telegram_callback is None:
                    self._increment("other_dropped")
                    logger.error("Telegram callback is not configured; dropping received message.")
                    return
                self.telegram_callback(message_to_forward)

        except Exception as e:
            logger.error(f"Error processing incoming MQTT message: {e}", exc_info=True)

    def _schedule_node_info(self, delay: float) -> None:
        if self._stopping:
            return
        if self._node_info_timer is not None:
            self._node_info_timer.cancel()
        self._node_info_timer = threading.Timer(delay, self._send_node_info)
        self._node_info_timer.daemon = True
        self._node_info_timer.start()

    def _send_node_info(self):
        """Builds and broadcasts our node's User packet (NodeInfo)."""
        if self._stopping or not self._mqtt_connected or not self.client.is_connected():
            logger.debug("Skipping NodeInfo broadcast because MQTT is not connected.")
            return
        with self._node_info_lock:
            logger.info(
                "Broadcasting NodeInfo: long_name='%s', short_name='%s'",
                self.long_name,
                self.short_name,
            )
            user_packet = mesh_pb2.User()
            user_packet.id = self.node_name
            user_packet.long_name = self.long_name
            user_packet.short_name = self.short_name

            data_packet = mesh_pb2.Data()
            data_packet.portnum = portnums_pb2.NODEINFO_APP
            data_packet.payload = user_packet.SerializeToString()

            try:
                self._publish_packet(data_packet)
            except MqttServiceError as exc:
                logger.warning("NodeInfo 發布失敗：%s", exc)
            finally:
                self._schedule_node_info(NODE_INFO_INTERVAL_SECONDS)

    def send_message(self, text, destination_id=BROADCAST_NUM) -> bool:
        """Encodes and sends a text message to the Meshtastic network via MQTT."""
        payload = text.encode("utf-8")
        if len(payload) > MAX_MESHTASTIC_PAYLOAD_BYTES:
            self._increment("oversized_dropped")
            logger.warning(
                "Dropping oversized Meshtastic message (%s bytes; maximum is %s).",
                len(payload),
                MAX_MESHTASTIC_PAYLOAD_BYTES,
            )
            return False

        logger.info("Sending %s-byte text message to Meshtastic destination %s", len(payload), destination_id)

        data_packet = mesh_pb2.Data()
        data_packet.portnum = portnums_pb2.TEXT_MESSAGE_APP
        data_packet.payload = payload
        
        try:
            self._publish_packet(data_packet, destination_id=destination_id)
        except MqttServiceError:
            self._increment("disconnected_dropped")
            raise
        if self.runtime_state is not None:
            self.runtime_state.mark_forwarded()
        return True

    def _publish_packet(self, data_packet, destination_id=BROADCAST_NUM):
        """Helper to wrap a Data packet in a MeshPacket and publish it."""
        if not self.client.is_connected():
            raise MqttServiceError("MQTT client is not connected")

        mesh_packet = mesh_pb2.MeshPacket()
        setattr(mesh_packet, "from", self.node_id)
        mesh_packet.to = destination_id
        with self._message_id_lock:
            mesh_packet.id = self.global_message_id
            self.global_message_id = self.global_message_id % 0xFFFFFFFF + 1
        mesh_packet.hop_limit = 3
        mesh_packet.channel = channel_hash(self.channel, self.key)

        try:
            mesh_packet.encrypted = crypt_payload(
                data_packet.SerializeToString(), self.key, mesh_packet.id, self.node_id
            )
        except Exception as e:
            raise MqttServiceError(f"failed to encrypt packet: {e}") from e

        service_envelope = mqtt_pb2.ServiceEnvelope()
        service_envelope.packet.CopyFrom(mesh_packet)
        service_envelope.channel_id = self.channel
        service_envelope.gateway_id = self.node_name

        payload = service_envelope.SerializeToString()
        result = self.client.publish(self.publish_topic, payload)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise MqttServiceError(f"MQTT publish failed with error code {result.rc}")
        logger.info(f"Packet with PortNum {data_packet.portnum} published to MQTT.")


    def decode_encrypted(self, mp) -> bool:
        """Decrypts an encrypted Meshtastic packet."""
        try:
            decrypted_bytes = crypt_payload(
                mp.encrypted, self.key, mp.id, getattr(mp, "from")
            )
            
            data = mesh_pb2.Data()
            data.ParseFromString(decrypted_bytes)
            mp.decoded.CopyFrom(data)
            logger.debug("Successfully decrypted packet.")
            return True

        except Exception as e:
            logger.warning(f"Failed to decrypt packet ID {mp.id}: {e}")
            mp.ClearField("decoded")
            return False

    def start(self):
        """Connects to the MQTT broker and starts the client loop."""
        self._connect_event.clear()
        self._connect_error = None
        self._stopping = False
        self._set_status("connecting")
        try:
            if self.username:
                self.client.username_pw_set(self.username, self.password or None)
            logger.info("正在連線 MQTT Broker：%s:%s", self.broker, self.port)
            self.client.connect(self.broker, self.port, 60)
            self.client.loop_start()
            if not self._connect_event.wait(timeout=10):
                raise MqttServiceError("timed out waiting for MQTT connection")
            if self._connect_error:
                raise MqttServiceError(self._connect_error)
            if not self.client.is_connected():
                raise MqttServiceError("MQTT connection did not become ready")
            logger.info("MQTT 服務已就緒。")
        except Exception as e:
            self._cleanup_failed_start()
            if isinstance(e, MqttServiceError):
                raise
            raise MqttServiceError(f"failed to start MQTT client: {e}") from e

    def _cleanup_failed_start(self) -> None:
        self._stopping = True
        if self._node_info_timer is not None:
            self._node_info_timer.cancel()
            self._node_info_timer = None
        try:
            try:
                if self.client.is_connected():
                    self.client.disconnect()
            except Exception:
                logger.exception("清理失敗的 MQTT 連線時發生錯誤。")
        finally:
            self.client.loop_stop()
        self._mqtt_connected = False
        self._connect_event.clear()
        error = self._connect_error or "MQTT startup failed"
        self._set_status("error", error)

    def stop(self):
        """Stops the MQTT client loop and disconnects."""
        logger.info("正在停止 MQTT 服務。")
        self._stopping = True
        if self._node_info_timer is not None:
            self._node_info_timer.cancel()
            self._node_info_timer = None
        try:
            if self.client.is_connected():
                self.client.disconnect()
        finally:
            self.client.loop_stop()
        self._mqtt_connected = False
        self._set_status("stopped")
        logger.info("MQTT 服務已停止。")
