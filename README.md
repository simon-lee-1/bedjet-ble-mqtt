# BedJet BLE-to-MQTT Bridge

A lightweight Python bridge that connects a BedJet V3 to Home Assistant via MQTT with auto-discovery. Communicates directly over Bluetooth Low Energy — no cloud, no app, no HomeKit bridge required.

## Features

- **Direct BLE control** — lock/unlock temperature, fan speed, mode changes
- **MQTT auto-discovery** — appears as a climate entity in Home Assistant automatically
- **Real-time status** — temperature, mode, fan speed, and timer updates via BLE notifications
- **Auto-reconnect** — handles BLE disconnects and stale BlueZ cache gracefully
- **Lightweight** — single Python script, runs as a systemd service

## Requirements

- Linux host with Bluetooth adapter (tested with Intel AX200)
- Python 3.10+
- BedJet V3 within BLE range
- MQTT broker (e.g., Mosquitto in Home Assistant)

## Installation

```bash
pip install bleak paho-mqtt
```

## Configuration

Set environment variables or create a `.env` file:

```bash
export BEDJET_MAC="AA:BB:CC:DD:EE:FF"  # Your BedJet's MAC address
export MQTT_HOST="192.168.1.100"        # Your MQTT broker IP
export MQTT_PORT=1883
export MQTT_USER="bedjet"
export MQTT_PASS="your_password"
```

Find your BedJet MAC address:
```bash
bluetoothctl scan le
# Look for "BEDJET_V3"
```

## Usage

```bash
python3 bedjet_bridge.py
```

## Systemd Service

```bash
sudo cp bedjet-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bedjet-bridge
```

## Home Assistant

The bridge publishes MQTT discovery configs automatically. After starting, a climate entity (`climate.bedjet_v3`) will appear in Home Assistant with:

- Mode control (heat, cool, dry, turbo, off)
- Target temperature
- Fan speed
- Current temperature reading

## Running in a VM

If Home Assistant runs in a VM (KVM, VirtualBox), the native HA Bluetooth integration won't work. This bridge runs on the **host** machine with direct Bluetooth access and communicates with HA over MQTT — bypassing the VM's lack of BLE.

## Troubleshooting

### "Device not found" after reboot
The bridge performs a fresh BLE scan before each connection attempt to avoid stale BlueZ cache issues. If the BedJet still isn't found, ensure it's powered on and within range.

### MQTT connect/disconnect loop
Ensure only one instance of the bridge is running. Check with:
```bash
ps aux | grep bedjet_bridge
```

## License

MIT

## Related Projects & Credits

There are other approaches to integrating BedJet with Home Assistant. This project was built to solve a specific gap (BLE from a VM host over MQTT), but credit goes to these projects for prior work on BedJet reverse engineering and integration:

| Project | Approach | Notes |
|---------|----------|-------|
| [pjt0620/Home-Assistant-Bedjet](https://github.com/pjt0620/Home-Assistant-Bedjet) | BLE MQTT bridge | Reverse-engineered BLE protocol. Similar concept to this project. |
| [robert-friedland/ha-bedjet](https://github.com/robert-friedland/ha-bedjet) | HA custom integration | Direct BLE integration using HA's Bluetooth stack. Requires BLE on the HA host. |
| [asheliahut/ha-bedjet](https://github.com/asheliahut/ha-bedjet) | HA custom integration | Another custom component approach. |
| [Home Assistant native BedJet](https://www.home-assistant.io/integrations/bedjet/) | Built-in integration | Requires Bluetooth adapter accessible to HA directly. Won't work if HA is in a VM without USB passthrough. |

### Why this project?

- **VM-friendly** — runs on the host with direct BLE access, talks to HA over MQTT. No USB passthrough or Bluetooth proxy needed.
- **Stale cache handling** — performs a fresh BLE scan before each connection, avoiding the common BlueZ issue where devices aren't found after a host reboot.
- **Single file, no framework** — just Python + bleak + paho-mqtt. Easy to understand and modify.
- **MQTT auto-discovery** — zero manual HA configuration needed.
