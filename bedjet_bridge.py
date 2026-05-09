#!/usr/bin/env python3
"""BedJet V3 BLE-to-MQTT bridge for Home Assistant."""

import asyncio
import json
import os
import sdnotify
_sd = sdnotify.SystemdNotifier()
import logging
import signal
import sys
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
import paho.mqtt.client as mqtt

# --- Config ---
BEDJET_MAC = os.environ.get("BEDJET_MAC", "AA:BB:CC:DD:EE:FF")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "bedjet")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

# BLE UUIDs
SVC_UUID = "00001000-bed0-0080-aa55-4265644a6574"
STATUS_UUID = "00002000-bed0-0080-aa55-4265644a6574"
CMD_UUID = "00002004-bed0-0080-aa55-4265644a6574"

# MQTT topics
TOPIC_PREFIX = "homeassistant"
DEVICE_ID = "bedjet_v3"
CLIMATE_TOPIC = f"bedjet/{DEVICE_ID}"

# BedJet protocol constants
MODES = {0: "off", 1: "heat", 2: "turbo", 3: "heat", 4: "cool", 5: "dry", 6: "off"}
MODE_NAMES = {0: "STANDBY", 1: "HEAT", 2: "TURBO", 3: "EXT_HEAT", 4: "COOL", 5: "DRY", 6: "WAIT"}
BUTTON_OFF = 0x01
BUTTON_COOL = 0x02
BUTTON_HEAT = 0x03
BUTTON_TURBO = 0x04
BUTTON_DRY = 0x05
BUTTON_EXT_HEAT = 0x06

HA_MODE_TO_BUTTON = {
    "off": BUTTON_OFF,
    "cool": BUTTON_COOL,
    "heat": BUTTON_HEAT,
    "dry": BUTTON_DRY,
    "fan_only": BUTTON_COOL,  # cool mode is essentially fan
}

TEMP_MIN = 19.0
TEMP_MAX = 43.0

log = logging.getLogger("bedjet_bridge")


@dataclass
class BedJetState:
    mode: int = 0
    mode_name: str = "STANDBY"
    current_temp: float = 0.0
    target_temp: float = 0.0
    ambient_temp: float = 0.0
    fan_pct: int = 0
    time_remaining_h: int = 0
    time_remaining_m: int = 0
    time_remaining_s: int = 0


def parse_status(data: bytes) -> BedJetState:
    """Parse a V3 status notification packet."""
    if len(data) < 11:
        return BedJetState()
    s = BedJetState()
    s.time_remaining_h = data[4]
    s.time_remaining_m = data[5]
    s.time_remaining_s = data[6]
    s.current_temp = data[7] / 2.0
    s.target_temp = data[8] / 2.0
    s.mode = data[9]
    s.mode_name = MODE_NAMES.get(s.mode, f"UNKNOWN({s.mode})")
    s.fan_pct = (data[10] + 1) * 5
    if len(data) > 17:
        s.ambient_temp = data[17] / 2.0
    return s


class BedJetBridge:
    def __init__(self):
        self.state = BedJetState()
        self.ble_client: BleakClient | None = None
        self.mqtt_client: mqtt.Client | None = None
        self.connected_ble = False
        self.connected_mqtt = False
        self.running = True
        self._cmd_queue: asyncio.Queue = asyncio.Queue()

    # --- MQTT ---
    def setup_mqtt(self):
        self.mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="bedjet_bridge")
        self.mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        self.mqtt_client.will_set(f"{CLIMATE_TOPIC}/available", "offline", retain=True)
        self.mqtt_client.on_connect = self._on_mqtt_connect
        self.mqtt_client.on_message = self._on_mqtt_message
        self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
        self.mqtt_client.connect_async(MQTT_HOST, MQTT_PORT)
        self.mqtt_client.loop_start()

    def _on_mqtt_connect(self, client, userdata, flags, rc, properties):
        log.info(f"MQTT connected: {rc}")
        self.connected_mqtt = True
        self._publish_discovery()
        client.subscribe(f"{CLIMATE_TOPIC}/mode/set")
        client.subscribe(f"{CLIMATE_TOPIC}/temp/set")
        client.subscribe(f"{CLIMATE_TOPIC}/fan/set")
        self._publish_availability("online")

    def _on_mqtt_disconnect(self, client, userdata, flags, rc, properties):
        log.warning(f"MQTT disconnected: {rc}")
        self.connected_mqtt = False

    def _on_mqtt_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace")
        log.info(f"MQTT cmd: {topic} = {payload}")
        try:
            self._cmd_queue.put_nowait((topic, payload))
        except asyncio.QueueFull:
            log.warning("Command queue full, dropping")

    def _publish_discovery(self):
        """Publish MQTT discovery configs for HA auto-creation."""
        device_info = {
            "identifiers": [DEVICE_ID],
            "name": "BedJet V3",
            "manufacturer": "BedJet",
            "model": "BedJet 3",
            "sw_version": "BLE Bridge",
        }

        # Climate entity
        climate_config = {
            "name": None,
            "unique_id": f"{DEVICE_ID}_climate",
            "device": device_info,
            "modes": ["off", "heat", "cool", "dry"],
            "min_temp": TEMP_MIN,
            "max_temp": TEMP_MAX,
            "temp_step": 0.5,
            "fan_modes": ["5", "10", "15", "20", "25", "30", "35", "40", "45", "50",
                          "55", "60", "65", "70", "75", "80", "85", "90", "95", "100"],
            "mode_command_topic": f"{CLIMATE_TOPIC}/mode/set",
            "mode_state_topic": f"{CLIMATE_TOPIC}/mode/state",
            "temperature_command_topic": f"{CLIMATE_TOPIC}/temp/set",
            "temperature_state_topic": f"{CLIMATE_TOPIC}/temp/state",
            "current_temperature_topic": f"{CLIMATE_TOPIC}/current_temp/state",
            "fan_mode_command_topic": f"{CLIMATE_TOPIC}/fan/set",
            "fan_mode_state_topic": f"{CLIMATE_TOPIC}/fan/state",
            "availability_topic": f"{CLIMATE_TOPIC}/available",
            "temperature_unit": "C",
        }
        self.mqtt_client.publish(
            f"{TOPIC_PREFIX}/climate/{DEVICE_ID}/config",
            json.dumps(climate_config), retain=True,
        )

        # Ambient temp sensor
        sensor_config = {
            "name": "Ambient Temperature",
            "unique_id": f"{DEVICE_ID}_ambient_temp",
            "device": device_info,
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "state_topic": f"{CLIMATE_TOPIC}/ambient_temp/state",
            "availability_topic": f"{CLIMATE_TOPIC}/available",
        }
        self.mqtt_client.publish(
            f"{TOPIC_PREFIX}/sensor/{DEVICE_ID}_ambient/config",
            json.dumps(sensor_config), retain=True,
        )

        # Mode text sensor (shows TURBO, EXT_HEAT etc)
        mode_sensor_config = {
            "name": "Mode",
            "unique_id": f"{DEVICE_ID}_mode_detail",
            "device": device_info,
            "state_topic": f"{CLIMATE_TOPIC}/mode_detail/state",
            "availability_topic": f"{CLIMATE_TOPIC}/available",
            "icon": "mdi:bed",
        }
        self.mqtt_client.publish(
            f"{TOPIC_PREFIX}/sensor/{DEVICE_ID}_mode/config",
            json.dumps(mode_sensor_config), retain=True,
        )

        # Timer sensor
        timer_config = {
            "name": "Time Remaining",
            "unique_id": f"{DEVICE_ID}_timer",
            "device": device_info,
            "state_topic": f"{CLIMATE_TOPIC}/timer/state",
            "availability_topic": f"{CLIMATE_TOPIC}/available",
            "icon": "mdi:timer-outline",
        }
        self.mqtt_client.publish(
            f"{TOPIC_PREFIX}/sensor/{DEVICE_ID}_timer/config",
            json.dumps(timer_config), retain=True,
        )

        log.info("Published MQTT discovery configs")
        _sd.notify("READY=1")

    def _publish_availability(self, status: str):
        self.mqtt_client.publish(f"{CLIMATE_TOPIC}/available", status, retain=True)

    def publish_state(self):
        """Publish current BedJet state to MQTT."""
        if not self.connected_mqtt:
            return
        s = self.state
        ha_mode = MODES.get(s.mode, "off")
        self.mqtt_client.publish(f"{CLIMATE_TOPIC}/mode/state", ha_mode, retain=True)
        self.mqtt_client.publish(f"{CLIMATE_TOPIC}/temp/state", str(s.target_temp), retain=True)
        self.mqtt_client.publish(f"{CLIMATE_TOPIC}/current_temp/state", str(s.current_temp), retain=True)
        self.mqtt_client.publish(f"{CLIMATE_TOPIC}/fan/state", str(s.fan_pct), retain=True)
        self.mqtt_client.publish(f"{CLIMATE_TOPIC}/ambient_temp/state", str(s.ambient_temp), retain=True)
        self.mqtt_client.publish(f"{CLIMATE_TOPIC}/mode_detail/state", s.mode_name, retain=True)
        timer_str = f"{s.time_remaining_h}h{s.time_remaining_m:02d}m" if s.time_remaining_h or s.time_remaining_m else "idle"
        self.mqtt_client.publish(f"{CLIMATE_TOPIC}/timer/state", timer_str, retain=True)

    # --- BLE ---
    def _on_status(self, sender, data: bytearray):
        self.state = parse_status(bytes(data))
        self.publish_state()

    async def send_command(self, cmd_bytes: bytes):
        """Send a command to the BedJet."""
        if self.ble_client and self.ble_client.is_connected:
            log.info(f"BLE write: {cmd_bytes.hex()}")
            await self.ble_client.write_gatt_char(CMD_UUID, cmd_bytes, response=False)

    async def process_mqtt_commands(self):
        """Process MQTT commands and translate to BLE writes."""
        while self.running:
            try:
                topic, payload = await asyncio.wait_for(self._cmd_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                if topic.endswith("/mode/set"):
                    button = HA_MODE_TO_BUTTON.get(payload, BUTTON_OFF)
                    await self.send_command(bytes([0x01, button]))
                elif topic.endswith("/temp/set"):
                    temp_c = float(payload)
                    temp_c = max(TEMP_MIN, min(TEMP_MAX, temp_c))
                    temp_step = int(temp_c * 2)
                    await self.send_command(bytes([0x03, temp_step]))
                elif topic.endswith("/fan/set"):
                    pct = int(payload)
                    pct = max(5, min(100, pct))
                    fan_step = (pct // 5) - 1
                    await self.send_command(bytes([0x07, fan_step]))
            except Exception as e:
                log.error(f"Error processing command: {e}")

    async def ble_loop(self):
        """Main BLE connection loop with auto-reconnect."""
        while self.running:
            try:
                log.info(f"Scanning for BedJet at {BEDJET_MAC}...")
                device = await BleakScanner.find_device_by_address(BEDJET_MAC, timeout=15.0)
                if device is None:
                    log.warning(f"BedJet not found during scan, retrying...")
                    await asyncio.sleep(10)
                    continue
                log.info(f"Connecting to BedJet at {BEDJET_MAC}...")
                async with BleakClient(device, timeout=20.0) as client:
                    self.ble_client = client
                    self.connected_ble = True
                    log.info(f"BLE connected, MTU={client.mtu_size}")
                    self._publish_availability("online")

                    await client.start_notify(STATUS_UUID, self._on_status)
                    log.info("Subscribed to status notifications")

                    # Stay connected until disconnect or shutdown
                    while client.is_connected and self.running:
                        _sd.notify("WATCHDOG=1")
                        await asyncio.sleep(1)

                    await client.stop_notify(STATUS_UUID)

            except BleakError as e:
                log.warning(f"BLE error: {e}")
            except Exception as e:
                log.error(f"Unexpected BLE error: {e}")
            finally:
                self.ble_client = None
                self.connected_ble = False
                self._publish_availability("offline")

            if self.running:
                log.info("Reconnecting in 10s...")
                await asyncio.sleep(10)

    async def run(self):
        self.setup_mqtt()

        # Wait for MQTT
        for _ in range(30):
            if self.connected_mqtt:
                break
            await asyncio.sleep(0.5)
        if not self.connected_mqtt:
            log.error("MQTT connection failed")
            return

        # Run BLE loop and command processor concurrently
        await asyncio.gather(
            self.ble_loop(),
            self.process_mqtt_commands(),
        )

    def shutdown(self):
        log.info("Shutting down...")
        self.running = False
        self._publish_availability("offline")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    bridge = BedJetBridge()

    loop = asyncio.new_event_loop()

    def handle_signal(sig):
        bridge.shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    try:
        loop.run_until_complete(bridge.run())
    except KeyboardInterrupt:
        bridge.shutdown()
    finally:
        if bridge.mqtt_client:
            bridge.mqtt_client.disconnect()
            bridge.mqtt_client.loop_stop()
        loop.close()


if __name__ == "__main__":
    main()


