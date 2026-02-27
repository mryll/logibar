# logibar

Waybar widgets that show battery level for Logitech wireless peripherals -- keyboard, mouse, and headset -- with event-driven updates and systemd integration.

![screenshot](screenshot.png)

## Features

- Real-time battery monitoring for Logitech Lightspeed devices
- Event-driven -- updates instantly on connect/disconnect/charge events, no polling
- Two background daemons: one for keyboard/mouse (HID++ 2.0), one for headset (custom HID protocol)
- Waybar widgets hide automatically when a device is disconnected
- Color-coded battery levels (normal/warning/critical) with Omarchy theme support
- Atomic state file writes -- no corruption on concurrent reads
- systemd user services for automatic startup

## Supported Devices

| Device | Type | Daemon |
|---|---|---|
| G915 X TKL | Keyboard | `logibar-hidpp-monitor` |
| PRO X Superlight 2 | Mouse | `logibar-hidpp-monitor` |
| PRO X 2 LIGHTSPEED | Headset | `logibar-headset-monitor` |

Adding other Logitech Lightspeed devices is straightforward -- see [Adding Devices](#adding-devices).

## Requirements

- Python 3
- [`python-hid`](https://pypi.org/project/hid/) (hidapi bindings) -- `pip install hid` or `pacman -S python-hid`
- [Waybar](https://github.com/Alexays/Waybar)
- A [Nerd Font](https://www.nerdfonts.com/) for icons

### HID device permissions

The daemons need read/write access to `/dev/hidraw*` devices. Create a udev rule:

```bash
sudo tee /etc/udev/rules.d/99-logitech-hidraw.rules << 'EOF'
# Logitech HID++ devices -- allow user access for battery monitoring
KERNEL=="hidraw*", ATTRS{idVendor}=="046d", MODE="0660", TAG+="uaccess"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Installation

### Arch Linux (AUR)

```bash
yay -S logibar
```

### From source

```bash
git clone https://github.com/mryll/logibar.git
cd logibar
make install PREFIX=~/.local
make install-systemd
```

This installs:
- 3 widget scripts + 2 daemons to `~/.local/bin/`
- 2 systemd user services (enabled and started automatically)

### Full install (includes debug tools)

```bash
make install-all PREFIX=~/.local
```

### System-wide

```bash
sudo make install
make install-systemd   # systemd services are always per-user
```

### Uninstall

```bash
make uninstall PREFIX=~/.local
make uninstall-systemd
```

## Waybar configuration

Add the modules to `~/.config/waybar/config.jsonc`:

```jsonc
"modules-right": ["custom/logibar-keyboard", "custom/logibar-mouse", "custom/logibar-headset", ...],

"custom/logibar-keyboard": {
    "exec": "logibar-keyboard",
    "return-type": "json",
    "interval": "once",
    "signal": 9,
    "tooltip": true
},
"custom/logibar-mouse": {
    "exec": "logibar-mouse",
    "return-type": "json",
    "interval": "once",
    "signal": 10,
    "tooltip": true
},
"custom/logibar-headset": {
    "exec": "logibar-headset",
    "return-type": "json",
    "interval": "once",
    "signal": 8,
    "tooltip": true
}
```

### Colors

The battery percentage is colored by level out of the box (One Dark palette):

| Class | Range | Default color |
|---|---|---|
| `normal` | >20% | `#98c379` (green) |
| `warning` | 11-20% | `#e5c07b` (yellow) |
| `critical` | 1-10% | `#e06c75` (red) |

To override, pass `--color-*` flags in the `exec` field:

```jsonc
"custom/logibar-keyboard": {
    "exec": "logibar-keyboard --color-normal '#50fa7b' --color-critical '#ff5555'",
    ...
}
```

Available flags: `--color-normal`, `--color-warning`, `--color-critical`.

CSS classes (`normal`, `warning`, `critical`) are also emitted for additional styling via `~/.config/waybar/style.css`.

## How it works

### Architecture

The system uses a **daemon + widget** pattern:

```
┌──────────────────────┐    state files     ┌───────────────────┐
│  logibar-hidpp-      │──→ keyboard,mouse ─→│  logibar-keyboard │
│  monitor (Python)    │   ($XDG_RUNTIME_DIR │  logibar-mouse    │──→ Waybar
│                      │    /logibar/)       │  (Bash widgets)   │
└──────────────────────┘                     └───────────────────┘
┌──────────────────────┐    state file       ┌───────────────────┐
│  logibar-headset-    │──→ headset ────────→│  logibar-headset  │
│  monitor (Python)    │                     │  (Bash widget)    │──→ Waybar
└──────────────────────┘                     └───────────────────┘
```

1. **Daemons** run as systemd user services, continuously monitoring HID devices
2. **State files** in `$XDG_RUNTIME_DIR/logibar/` store battery %, connected status, and charging status (3 lines: `battery\nconnected\ncharging`)
3. **Widget scripts** read the state file and output JSON for Waybar
4. Daemons signal Waybar via `SIGRTMIN+N` for instant updates (no polling interval needed)

### Keyboard & Mouse: HID++ 2.0 Protocol

The `logibar-hidpp-monitor` daemon uses the standard **Logitech HID++ 2.0** protocol to monitor keyboard and mouse battery. This is the same protocol used by [Solaar](https://github.com/pwr-Solaar/Solaar), but implemented directly via hidapi without needing the Solaar daemon.

**How HID++ 2.0 battery reading works:**

1. **Device discovery** -- The daemon enumerates USB HID devices by Vendor ID (`0x046d`) and Product ID. Each device has two PIDs: one for wireless (via Lightspeed receiver) and one for wired (direct USB). Both are monitored in parallel threads.

2. **Feature negotiation** -- HID++ 2.0 uses a feature-based architecture. To read battery, the daemon first queries the **ROOT feature** (index `0x00`) to find the index of the **UNIFIED_BATTERY feature** (`0x1004`):
   ```
   Request:  [0x10, device_idx, 0x00, 0x0d, 0x10, 0x04, 0x00]
                    │             │     │     └─── feature ID 0x1004
                    │             │     └──── function 0 (getFeatureIndex) + SW ID
                    │             └───── ROOT feature is always at index 0
                    └──────────── 0x01 for wireless (via receiver), 0xFF for wired (direct USB)
   Response: [0x10, device_idx, 0x00, 0x0d, feature_index, ...]
   ```

3. **Battery query** -- Once we have the feature index, we call function `0x01` (getStatus) on the UNIFIED_BATTERY feature:
   ```
   Request:  [0x11, device_idx, feature_index, 0x10, 0x00, ..., 0x00]
   Response: [0x11, device_idx, feature_index, 0x10, SoC, level, status, ...]
                                                      │     │      └── 1=charging, 2=slow, 3=full
                                                      │     └── level flags
                                                      └── State of Charge (0-100%)
   ```

4. **Event-driven updates** -- After initial query, the daemon uses **blocking reads with timeout** (1 second). The receiver sends unsolicited HID++ notifications on:
   - **Connection events** (`0x41`) -- device wakes up, goes to sleep, or disconnects. The `link_off` flag (byte 4, bit 6) indicates disconnect.
   - **Battery broadcasts** -- the device periodically reports battery changes (charging started/stopped, level changed).

5. **Wireless + Wired handling** -- Each device runs two threads: one monitoring the wireless receiver PID and one monitoring the wired PID. A shared state dict with a threading lock coordinates which connection is active. When wired is connected, wireless monitoring pauses.

**Supported HID++ device indices:**
- `0x01` -- paired device via Lightspeed wireless receiver
- `0xFF` -- direct USB (wired) connection

### Headset: Custom HID Protocol

The PRO X 2 LIGHTSPEED headset does **not** use the standard HID++ UNIFIED_BATTERY feature. Instead, it uses a device-specific HID protocol that required reverse-engineering (the `tools/` scripts were used for this).

**How the headset battery reading works:**

1. **Device discovery** -- The daemon looks for the headset by VID:PID (`0x046d:0x0af7`) on the HID usage page `0xffa0` (vendor-defined). This is different from keyboard/mouse which use usage page `0xff00`.

2. **Battery request** -- A fixed 64-byte HID report is sent to request battery status:
   ```
   Request: 51 08 00 03 1a 00 03 00 04 0a [00 * 54]
   ```
   This is a vendor-specific command, not part of the standard HID++ protocol.

3. **Battery response** -- The daemon reads responses and looks for a specific pattern:
   ```
   Response: 51 0b XX XX XX XX XX XX 04 XX battery_pct XX charging_status ...
              │  │                    │     │              └── 0x02 = charging
              │  │                    │     └── battery percentage (1-100)
              │  │                    └── response type marker (0x04)
              │  └── response identifier (0x0b)
              └── report ID (0x51)
   ```

4. **On/Off detection** -- The headset sends event packets when it turns on or off:
   ```
   On/Off: 51 05 00 03 00 00 XX 00
                               └── 0x00 = off, 0x01 = on
   ```
   When the headset turns on, a battery request is immediately sent. When it turns off, the state file is cleared and the widget disappears.

5. **Polling** -- Unlike the keyboard/mouse which rely entirely on events, the headset daemon polls battery every 60 seconds when the headset is active (since the headset doesn't broadcast battery changes spontaneously).

### Widget Scripts

All three widget scripts are identical Bash scripts that:

1. Read the 3-line state file from `$XDG_RUNTIME_DIR/logibar/{device}`
2. If disconnected or no battery data, output `{"text": ""}` (Waybar hides the widget)
3. Determine CSS class: `critical` (<=10%), `warning` (<=20%), `normal` (>20%)
4. Output JSON with Nerd Font icon, battery percentage, tooltip, and class

## Adding Devices

To add a new Logitech device to the keyboard/mouse daemon:

1. Find its Product IDs (wireless + wired):
   ```bash
   lsusb | grep 046d
   ```

2. Edit `logibar-hidpp-monitor` and add an entry to the `DEVICES` list:
   ```python
   DEVICES = [
       (0xc547, 0xc357, "keyboard", 9),   # G915 X TKL
       (0xc54d, 0xc09b, "mouse", 10),     # PRO X Superlight 2
       (0xNEW1, 0xNEW2, "newdevice", 11), # Your device
   ]
   ```

3. Create a new widget script (copy `logibar-keyboard` and change the icon, tooltip text, and state file name).

4. Add the new Waybar module with the matching signal number.

The `tools/` directory contains utilities to help identify the correct PIDs and verify HID++ support:
- `logibar-hidpp-battery <hidraw_device>` -- read battery from any HID++ 2.0 device
- `logibar-hidpp-debug <hidraw_device>` -- verbose version that tries multiple device indices
- `logibar-headset-probe` -- probe available HID++ features on a device

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Widget never appears | Daemon not running | `systemctl --user status logibar-hidpp-monitor` |
| Widget shows nothing | Device disconnected | Turn on / wake up the device |
| Permission denied in journal | No hidraw access | Set up the udev rule (see [Requirements](#hid-device-permissions)) |
| Battery stuck at old value | State file stale | Restart daemon: `systemctl --user restart logibar-hidpp-monitor` |
| Headset widget not updating | Wrong hidraw device | Check `ls /dev/hidraw*` and verify the PID matches |

Check daemon logs:

```bash
journalctl --user -u logibar-hidpp-monitor -f
journalctl --user -u logibar-headset-monitor -f
```

## License

[MIT](LICENSE)

## Related

- [Solaar](https://github.com/pwr-Solaar/Solaar) -- Full-featured Logitech device manager (much heavier, GUI-based)
- [Waybar](https://github.com/Alexays/Waybar) -- Status bar for Wayland compositors
- [claudebar](https://github.com/mryll/claudebar) -- Claude AI usage widget for Waybar
- [codexbar](https://github.com/mryll/codexbar) -- OpenAI Codex usage widget for Waybar
