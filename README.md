# logibar

[![AUR version](https://img.shields.io/aur/version/logibar)](https://aur.archlinux.org/packages/logibar)
[![License: MIT](https://img.shields.io/github/license/mryll/logibar)](LICENSE)

logibar shows the battery level of supported Logitech Lightspeed and Unifying devices — keyboard, mouse and headset — in your status bar. It runs on Waybar and on the Omarchy shell (Quickshell). It reads each battery over HID++, and it does not need a daemon from the vendor. It refreshes when a device wakes, goes to sleep, or when a charge cycle begins.

The same core drives both frontends, so a number reads the same on either one:

| The Omarchy shell plugin | The Waybar module |
| :---: | :---: |
| <img src="screenshots/omarchy-desktop.png" alt="logibar in the Omarchy shell: the bar face and the battery panel"> | <img src="screenshots/waybar-desktop.png" alt="logibar in Waybar: the bar face and the battery tooltip"> |

## Contents

- [Features](#features)
- [Supported devices](#supported-devices)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Omarchy shell plugin](#omarchy-shell-plugin)
- [Configuration](#configuration)
- [Structured JSON output](#structured-json-output)
- [How it works](#how-it-works)
- [Add a device](#add-a-device)
- [Troubleshooting](#troubleshooting)
- [Related](#related)

## Features

- **One widget for all devices.** The bar shows one battery glyph and the device that needs attention first, on Waybar and on Omarchy. The Omarchy face can also be the glyph alone or one value for each device (`barFace`). One click or one hover shows the details of each device.
- **Receiver discovery.** The HID++ monitor reads the pairing slots of Unifying and Lightspeed receivers. Each paired device gets its own row, so two mice show as two rows.
- **Event-driven, like G HUB.** Two small daemons listen for HID++ events, and the bar refreshes when the hardware sends one. Each receiver says when a device connects or disconnects: a device that you switch on shows "Connecting…" at once and its battery a second or two later; a device that you switch off disappears at once. The receiver scan also runs at startup, when a receiver is plugged in or out, every five minutes, and when you click the Omarchy widget.
- **A real gauge.** The meters go from red at empty, through amber, to green at full. The colors come from your theme.
- **Two frontends, one brain.** `logibar-status` makes every decision. Waybar and the Omarchy plugin only draw the result.
- **Theme-aware.** The colors come from the active Omarchy theme, or from a pywal cache, or from a built-in palette.
- **Monochrome mode.** A flag or the `NO_COLOR` variable removes the colors. Then your own CSS gives the bar its style.
- **Structured JSON.** A documented output without colors for your own scripts.

## Supported devices

| Devices | Type | Daemon |
|---|---|---|
| Any device paired to a Unifying (`c52b`, `c532`) or Lightspeed (`c539`, `c545`, `c547`, `c54d`) receiver | Keyboard, mouse, other | `logibar-hidpp-monitor` |
| Any HID++ 2.0 keyboard or mouse on its USB cable | Keyboard, mouse | `logibar-hidpp-monitor` |
| PRO X 2 LIGHTSPEED | Headset | `logibar-headset-monitor` |

Seen on hardware: MX Master 2S, M720 Triathlon, G915 TKL, G915 X TKL, G Pro Wireless, PRO X Superlight, PRO X 2 (PRO X Superlight 2), G502 X LIGHTSPEED.

No keyboard or mouse needs a table entry: the receiver scan below finds the paired devices, and the daemon finds a device on its cable (see [Devices on a cable](#devices-on-a-cable)). Each device gives its own name. Another receiver family needs its product ID in a table; the section [Add a device](#add-a-device) gives the procedure.

Receiver discovery no longer needs a model table for devices paired to Unifying (`c52b`, `c532`) or supported Lightspeed (`c539`, `c545`, `c547`, `c54d`) receivers. It reads each pairing slot, asks the device for its name and battery through HID++ 2.0 (`0x1004`, `0x1000`, `0x1001`), or through the HID++ 1.0 battery registers (`0x0d`, `0x07`) when the device speaks HID++ 1.0. A device that stops replying disappears, like a device that is off: the receiver cannot tell a sleeping device from one that is off, so a mouse that stays still for some minutes also disappears until it answers again. With `--last-known` (Omarchy setting `showLastKnown`), a device that responded within the past 24 hours remains visible instead, with a dimmed, explicitly **last-known** percentage; the charging icon disappears until a fresh reply confirms charging, and the panel shows when that device was last seen. Never-seen pairings and readings older than 24 hours stay hidden, as do pairings absent from the latest receiver scan. A device paired in the slot of another one of the same model shows that device's last-known reading until it answers for the first time. A missing reply can mean sleep, power off, out of range, or an unsupported query, so the UI says **Not responding** rather than guessing. Legacy wired and headset state remains a fallback when the scan has no pairing of that kind. Bolt receivers use different pairing registers and are not covered by this discovery path.

### Devices on a cable

A device on its USB cable has its own HID node. The daemon looks at each Logitech USB node that is not a receiver and is not the node of a device paired to a receiver (the kernel puts that node under its receiver). It asks the device at index `0xff` for its kind and name (DEVICE_NAME, `0x0005`) and for a battery feature (`0x1004`, `0x1000` or `0x1001`). A keyboard or a mouse that answers gets a monitor, and its reading has the name that the device gives, the same name as through the receiver. The device also gives its model ID (DEVICE_INFO, `0x0003`), which contains the wireless PID of the device. That PID finds the device's row on its receiver: while the cable is connected, the CLI hides that row when it does not answer, and only that row. A node that does not answer is probed again only when the set of HID nodes changes. A device on Bluetooth is not a cable device.

In the Omarchy plugin, clicking the bar widget or pressing `r` in its panel asks the running daemon to scan the receivers now (`logibar-hidpp-monitor --refresh`). The plugin itself never talks to the hardware. The scan shows nothing while it runs. The click refresh needs a daemon from the same release as the plugin: `omarchy plugin update` does not update the package, so run `yay -S logibar` too. With an older daemon, or with the daemon stopped, the bar tooltip shows "Refresh needs a running, up-to-date logibar daemon" and the click does nothing else. The default panel settings also need the package of the same release: an older `logibar-status` does not know the `other` kind. The MX Master 2S can report only charge ranges while charging; a displayed 50% or 90% may be an estimate rather than an exact percentage.

## Requirements

- Python 3
- **[`python-hidapi`](https://pypi.org/project/hidapi/)** — `pacman -S python-hidapi`, or `pip install hidapi`
- [`jq`](https://jqlang.github.io/jq/) — all the widget scripts use it
- [Waybar](https://github.com/Alexays/Waybar), or the Omarchy shell
- [`inotify-tools`](https://github.com/inotify-tools/inotify-tools) — only for `logibar-status --watch`
- A [Nerd Font](https://www.nerdfonts.com/) for the icons

> [!IMPORTANT]
> Install `python-hidapi`. Do not install `python-hid`. Both packages give you a module with the same name `hid`, but the API is different. If you install the wrong package, the daemons stop at the first device and write nothing. No error message appears, because the daemons continue to run. The widget stays empty.

### Device permissions

The daemons read and write `/dev/hidraw*`. The AUR package adds the rule for you. For a manual installation, add the rule yourself:

```bash
sudo tee /etc/udev/rules.d/70-logitech-hidraw.rules << 'EOF'
# Logitech HID++ devices — allow user access for battery monitoring
KERNEL=="hidraw*", ATTRS{idVendor}=="046d", MODE="0660", TAG+="uaccess"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger --action=change --subsystem-match=hidraw
```

> [!NOTE]
> The `70-` prefix is necessary. The rule must come before the `73-seat-late.rules` file of systemd, which applies the `uaccess` ACL. A higher number, such as `99-`, applies the tag too late, and you get no access. If you installed the rule as `99-logitech-hidraw.rules` before, delete it: `sudo rm /etc/udev/rules.d/99-logitech-hidraw.rules`.

## Installation

### Omarchy

On [Omarchy](https://omarchy.org), the complete installation is two commands:

```bash
yay -S logibar
omarchy plugin add https://github.com/mryll/logibar.git --enable
```

The first command installs the scripts, the udev rule and the two monitor services. The second command installs the bar widget and enables it. Refer to [Omarchy shell plugin](#omarchy-shell-plugin) for the panel and its settings.

### Arch Linux (AUR)

```bash
yay -S logibar
```

The package adds the udev rule and enables both services.

### From source

```bash
git clone https://github.com/mryll/logibar.git
cd logibar
make install PREFIX=~/.local
make install-systemd
sudo make install-udev
```

These commands install 4 widget scripts and 2 daemons in `~/.local/bin/`, 2 systemd user services, and the udev rule.

| Target | Result |
|---|---|
| `make install` | Widget scripts and daemons |
| `make install-systemd` | User services, enabled for the next login |
| `make install-udev` | The udev rule (needs root) |
| `make install-tools` | Debug tools for a new device |
| `make install-all` | Everything above, except the udev rule |
| `make install-omarchy` | The Omarchy shell plugin |
| `make test` | The test suite |

To remove any of them, use the same name with `uninstall`, such as `make uninstall PREFIX=~/.local`.

## Quick start

### Waybar: one module for every device

The bar shows the battery of the responding device with the lowest charge. With `--last-known`, it shows a dimmed last-known reading when only recently seen devices remain. The tooltip gives one block for each visible device: its glyph, level meter, and a line with charging or last-known status and the time of its last successful reading.

The time is per device, not per tooltip. Each daemon writes its own device's reading at its own time, so a single "Updated" line would put the newest device's time next to every other device's older number.

![The Waybar bar module](screenshots/waybar-bar.png)
![The Waybar tooltip](screenshots/waybar-tooltip.png)

Add these lines to `~/.config/waybar/config.jsonc`:

```jsonc
"modules-right": ["custom/logibar", ...],

"custom/logibar": {
    "exec": "logibar-status --watch",
    "return-type": "json",
    "restart-interval": 5,
    "tooltip": true
}
```

Run `logibar-status --help` for the full reference: the usage line, every flag, and the format placeholders.

`--watch` prints a new line on each change, so the bar does not poll. This flag needs `inotify-tools`. Without that package, use `"exec": "logibar-status"` and select your own `"interval"`. To hide a device, give the names of the devices that you want: `--devices keyboard,mouse`. To keep a device that stops answering, with its last reading, add `--last-known`.

### Waybar: one module per device

The three original modules still work, and they now share the colors of the combined module.

![The three per-device modules](screenshot.png)

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

> [!NOTE]
> These modules use `"interval": "once"`. The daemons send `SIGRTMIN+N` to Waybar after each change, so a polling interval is not necessary.

## Omarchy shell plugin

The plugin in [`omarchy/`](omarchy/) is a native widget for the bar of the [Omarchy](https://github.com/basecamp/omarchy) shell. The face of the bar shows the same short summary as Waybar. A click opens a panel with one row for each device. The row shows the name, an animated battery meter, the charge, the charging state, and the time of the last update.

![The logibar face in the Omarchy bar](screenshots/omarchy-bar.png)

![The Omarchy panel](screenshots/omarchy-panel.png)

Install the plugin, then add it to your bar:

### Install the plugin

From the marketplace, or from this repository directly:

```bash
omarchy plugin add https://github.com/mryll/logibar.git --enable
```

That clones the repository into `~/.config/omarchy/plugins/mryll.logibar` and
validates the manifest before it is enabled. To remove it later:
`omarchy plugin remove mryll.logibar`.

The plugin runs `logibar-status` from your PATH and needs the battery daemons, so install those too — from the AUR (`yay -S logibar`) or with `make install-all PREFIX=~/.local`.

For development, link the working copy instead of cloning a second one:

```bash
make install-omarchy
```

```json
"right": [
    { "id": "mryll.logibar" },
    ...
]
```

The command links this repository into `~/.config/omarchy/plugins/mryll.logibar`. The plugin needs `logibar-status` and both daemons. Install them first.

| Setting | Type | Default | Result |
|---|---|---|---|
| `colorMode` | enum | `full` | Where the colors appear: `full`, `none`, `bar-only`, `panel-only` |
| `barFace` | enum | `lowest` | The bar face: `lowest` (one glyph and the lowest battery), `icon` (the glyph alone), `devices` (one value for each device) |
| `showKeyboard` | boolean | `true` | Includes the keyboard |
| `showMouse` | boolean | `true` | Includes the mouse |
| `showHeadset` | boolean | `true` | Includes the headset |
| `showOther` | boolean | `true` | Includes paired devices of other kinds (a presenter, a numpad) |
| `showLastKnown` | boolean | `false` | Keeps a device that stops answering, dimmed, with its last reading from the past 24 hours. A sleeping device also stops answering |

Mouse buttons: **left** opens the panel and asks the daemon for a receiver scan, **middle** asks for the scan only. The widget disappears when no device is responding or has a reading from the past 24 hours.

> [!TIP]
> After you edit a file in `omarchy/`, run `omarchy restart shell`. A rescan of the plugins does not compile the QML again.

The plugin also answers the shell's IPC, so a keybind or a script can drive it without the mouse:

```bash
qs ipc call mryll.logibar toggle    # open or close the panel
qs ipc call mryll.logibar refresh   # fetch now, without opening anything
```

## Configuration

### Colors

The battery charge has three levels:

| Class | Range | Color |
|---|---|---|
| `normal` | above 20% | green |
| `warning` | 11–20% | yellow |
| `critical` | 1–10% | red |

To use your own colors, add the flags to the `exec` line:

```jsonc
"custom/logibar": {
    "exec": "logibar-status --watch --color-normal '#50fa7b' --color-critical '#ff5555'",
    ...
}
```

The flags are `--color-normal`, `--color-warning` and `--color-critical`. Each flag takes a hex color, such as `#50fa7b`, `#5f7` or `#50fa7bff`. Waybar also receives the class name, so you can give the module a style in `~/.config/waybar/style.css` instead.

### Theming

The colors come from the first source in this list that has them:

1. The `--color-*` flags.
2. The active Omarchy theme, at `$XDG_STATE_HOME/omarchy/current/theme/colors.toml`. Older versions of Omarchy keep the theme at `~/.config/omarchy/current/theme/colors.toml`, and that path also works.
3. A pywal cache, at `$XDG_CACHE_HOME/wal/colors.json`. This source applies only when there is no Omarchy theme.
4. The built-in One Dark palette.

The pywal file is a standard in practice. The original [pywal](https://github.com/dylanaraps/pywal) has no maintainer now, but [pywal16](https://github.com/eylles/pywal16) writes the same file, and [wallust](https://codeberg.org/explosion-mental/wallust) has a target that is compatible with pywal. logibar reads `special.foreground`, `special.background`, and the colors `color1` (red), `color2` (green), `color3` (yellow) and `color4` (accent).

The same colors reach both frontends, in every theme:

| Flexoki Light | Rosé Pine | Hackerman |
|:---:|:---:|:---:|
| ![Flexoki Light tooltip](screenshots/waybar-theme-flexoki-light.png) | ![Rosé Pine tooltip](screenshots/waybar-theme-rose-pine.png) | ![Hackerman tooltip](screenshots/waybar-theme-hackerman.png) |
| ![Flexoki Light panel](screenshots/omarchy-theme-flexoki-light.png) | ![Rosé Pine panel](screenshots/omarchy-theme-rose-pine.png) | ![Hackerman panel](screenshots/omarchy-theme-hackerman.png) |

| Ristretto | Nord | Kanagawa |
|:---:|:---:|:---:|
| ![Ristretto tooltip](screenshots/waybar-theme-ristretto.png) | ![Nord tooltip](screenshots/waybar-theme-nord.png) | ![Kanagawa tooltip](screenshots/waybar-theme-kanagawa.png) |
| ![Ristretto panel](screenshots/omarchy-theme-ristretto.png) | ![Nord panel](screenshots/omarchy-theme-nord.png) | ![Kanagawa panel](screenshots/omarchy-theme-kanagawa.png) |

> [!NOTE]
> **Earlier versions did not detect the theme.** The widgets looked for the theme in the old path. They also looked for a `color1` key that current themes do not write. The failure was silent. The tooltips used the built-in palette. The colors now follow your theme.

### Monochrome mode

For a bar without colors, use `--no-color`:

```bash
logibar-status --no-color            # the same as --no-color=all
logibar-status --no-color=bar        # a plain bar, a colored tooltip
logibar-status --no-color=tooltip    # a colored bar, a plain tooltip
```

| Command | Bar text | Tooltip |
|---|---|---|
| none | colored | colored |
| `--no-color` or `--no-color=all` | plain | plain |
| `--no-color=bar` | plain | colored |
| `--no-color=tooltip` | colored | plain |

The [`NO_COLOR`](https://no-color.org) environment variable does the same as `--no-color=all`, if the value is not empty. A flag on the command line is more exact, so the flag has priority: `NO_COLOR=1 logibar-status --no-color=bar` keeps the colors in the tooltip.

Icons, meters, borders and bold text stay. Only the colors disappear.

| Waybar tooltip | Omarchy panel |
|---|---|
| ![A monochrome tooltip](screenshots/waybar-tooltip-mono.png) | ![A monochrome panel](screenshots/omarchy-panel-mono.png) |

The Omarchy plugin has the same four states in its `colorMode` setting.

> [!TIP]
> Waybar still receives the class name for each level. Remove the colors and write your own rules:
> ```css
> #custom-logibar.warning  { color: #d79921; }
> #custom-logibar.critical { color: #cc241d; font-weight: bold; }
> ```

### Tooltip font

The tooltip is pinned to a monospace font. That is not decoration: its rules are box-drawing characters, and in a proportional font one of those is nearly twice as wide as a letter. The tooltip then sizes itself to the rules, and a dead margin opens to the right of the text. Waybar draws the tooltip in a GTK window that ignores `font-family` from your CSS, so the markup is the only place this can be said.

The default is a **list** of families, tried in order:

```
JetBrainsMono Nerd Font, JetBrainsMono Nerd Font Mono, monospace
```

Pango falls through to the next name when one is not installed. Both families are fully monospaced — every glyph, icons included, advances 0.6 em — so the rules align the same with either. The difference is the drawn size of the icons: the `…Mono` family shrinks them to fit the cell, about 40% smaller. The non-Mono family draws them at full size, and for that reason it comes first. The Arch package `ttf-jetbrains-mono-nerd` ships both families; `ttf-jetbrains-mono-nerd-basic` — the one Omarchy installs — ships only the non-Mono one.

To use a different font, name any monospace family (or your own list):

```bash
logibar-status --tooltip-font "FiraCode Nerd Font Mono"
```

> [!NOTE]
> **`--frame` and `--frame-font` are deprecated.** `--frame` drew the tooltip as a bordered card. It is still accepted, so an existing Waybar config keeps working, but it now does nothing; `--frame-font` is an alias for `--tooltip-font`.
>
> The box was a second way of drawing the same content — more code, more documentation, more screenshots — and it only lined up when the pinned font was a complete Mono Nerd Font. Pinning the font on the one remaining tooltip gives the alignment without the box.

### Spacing

Change `padding` and `margin` in `~/.config/waybar/style.css`:

```css
#custom-logibar {
    padding: 0 8px;
    margin: 0 4px;
}
```

## Structured JSON output

`logibar-status --json` prints the data behind the widget. Use this output in your own scripts, or read it to see the same data as the widget. The command always exits with 0, and the output contains no colors and no markup.

```json
{
  "schema_version": 1,
  "devices": [
    { "id": "keyboard", "name": "G915 TKL", "battery": 92, "connected": true,
      "charging": true, "state": "ok", "updated_at": "2026-08-20T02:21:04-03:00" }
  ],
  "aggregate": { "worst_battery": 8, "any_charging": true, "connected_count": 3, "state": "critical" },
  "palette": {
    "ok": "#9ece6a", "warning": "#e0af68", "critical": "#f7768e",
    "accent": "#7aa2f7", "foreground": "#a9b1d6", "background": "#1a1b26",
    "thresholds": { "warning": 20, "critical": 10 },
    "stops": [
      { "pct": 0, "color": "#f7768e" }, { "pct": 10, "color": "#f7768e" },
      { "pct": 20, "color": "#e0af68" }, { "pct": 100, "color": "#9ece6a" }
    ]
  }
}
```

| Field | Meaning |
|---|---|
| `devices[].state` | `ok`, `warning`, `critical`, `last_known`, or `disconnected` |
| `devices[].last_known_battery`, `last_seen_at` | On receiver-discovered devices, the last-known battery (when not responding) and time of the last successful query; null when never seen or expired |
| `devices[].updated_at` | The time when the daemon last wrote this device, in ISO-8601 |
| `aggregate` | The device with the lowest charge, and the state of the group |
| `palette` | The colors of the gauge, after the theme and the flags |
| `palette.stops` | The gauge, as colors and the percentages where they apply |

An error is also a JSON document, with an `error` field and the same exit code 0. Programs that read this output do not parse text.

> [!NOTE]
> `palette.stops` is the reason why one change of a threshold moves both frontends. `logibar-status` keeps the numbers, and the Omarchy panel interpolates between the stops that it receives. `--no-color` has no effect on this output.

## How it works

### Architecture

```
┌──────────────────────┐   state files    ┌──────────────────┐
│  logibar-hidpp-      │──→ keyboard,mouse│                  │
│  monitor (Python)    │  ($XDG_RUNTIME_  │ logibar-status   │──→ Waybar
├──────────────────────┤   DIR/logibar/)  │ logibar-keyboard │
│  logibar-headset-    │──→ headset ─────→│ logibar-mouse    │──→ Omarchy shell
│  monitor (Python)    │                  │ logibar-headset  │
└──────────────────────┘                  └──────────────────┘
```

1. The **daemons** run as systemd user services and monitor the HID devices.
2. The daemon writes each change to a **state file** in `$XDG_RUNTIME_DIR/logibar/`, in 4 lines: `battery`, `connected`, `charging`, `model`. A live cable reading adds `wired` and the receivers that have a row for that device as a fifth line; the CLI reads it from `wired-<kind>`. Receiver-discovered entries instead add `last_success_epoch` and the paired device's wireless PID, so a sleeping device can retain a bounded last-known reading without inheriting another device's battery when a slot is reused. A seventh line `connecting` means that the link came up and the reading is on its way; the CLI honors it for 30 seconds only. Each write is atomic. A 3-line file from an older daemon is still valid; the widgets then show a generic name.
3. **`logibar-status`** reads these files. It selects the worst device, applies the thresholds, finds the colors, and prints the result. The `model` line becomes the device name in the tooltip, in the JSON (`devices[].name`) and in the Omarchy panel. The widgets accept a model only as ASCII letters, digits, space and `._()+/-`, 1 to 48 characters; any other line (accents, markup, control bytes, a longer name) keeps the generic name, so a file that the widget does not write cannot put markup into the tooltip. A NUL byte is dropped by the shell before the check. The name is the one that the device gives: its name in the receiver's pairing register, or HID++ DEVICE_NAME (`0x0005`), on the receiver and on the cable.
4. For Waybar, the daemons also send `SIGRTMIN+N`. For the Omarchy shell, the plugin monitors the files and runs `logibar-status` again after each change.

### Keyboard and mouse: HID++ 2.0

`logibar-hidpp-monitor` uses the standard **Logitech HID++ 2.0** protocol. [Solaar](https://github.com/pwr-Solaar/Solaar) uses the same protocol, but logibar sends the commands directly to hidapi, with no extra daemon.

Each device paired to a receiver has its own row (`rx-<receiver>-<slot>`). The single-device widgets (`logibar-mouse`, `logibar-keyboard`) read one file for each kind (`mouse`, `keyboard`), which the daemon writes from those rows: the connected device of that kind with the lowest battery, with its own name. That is the same device that the combined widget and the Omarchy bar show; a device on its USB cable counts too. The cable reading also goes to its own file (`wired-mouse`, `wired-keyboard`), from which the combined widget and the Omarchy bar show the cable device next to the receiver rows: a cable reading hides only the silent row of that same device.

To read most batteries, the daemon asks the ROOT feature at index `0x00` for the index of the UNIFIED_BATTERY feature (`0x1004`). Then it asks that feature for the charge:

```
Request:  [0x10, device_idx, 0x00, 0x0d, 0x10, 0x04, 0x00]
                  │             │     │     └─── feature ID 0x1004
                  │             │     └──── function 0 (getFeatureIndex) + SW ID
                  │             └───── ROOT feature is always at index 0
                  └──────────── 0x01 for wireless (via receiver), 0xFF for wired (direct USB)
Response: [0x10, device_idx, 0x00, 0x0d, feature_index, ...]

Request:  [0x11, device_idx, feature_index, 0x1d, 0x00, ..., 0x00]
Response: [0x11, device_idx, feature_index, 0x1d, SoC, level, status, ...]
                                                   │     │      └── 1=charging, 2=slow, 3=full
                                                   │     └── level flags
                                                   └── State of Charge (0-100%)
```

Byte 3 of a request is `(function << 4) | software_id`. hidraw copies every response to every open descriptor on a node, so each request in the daemon signs with its own software ID (`0xD` for a battery query, and a rotation of 13 IDs for the receiver scan and the cable probe) and accepts only a response with its full echo. Broadcast events carry software ID `0`.

To find the paired devices, the receiver scan sends one HID++ 1.0 request to the receiver for each slot. The request reads the pairing information of the slot: the wireless PID and the kind of the paired device. It is a long-register READ (sub-ID `0x83`). The daemon never sends the long-register write (`0x82`). For slot 1:

```
Request:  [0x10, 0xff, 0x83, 0xb5, 0x20, 0x00, 0x00]
                  │     │     │     └── sub-register 0x20 = pairing information, slot 1
                  │     │     └──── register 0xb5
                  │     └───── 0x83 = read long register
                  └────── 0xff = the receiver
Response: [0x11, 0xff, 0x83, 0xb5, 0x20, ..., wpid_hi, wpid_lo, ...]
                                              └── bytes 7 and 8
Error:    [0x10, 0xff, 0x8f, 0x83, 0xb5, error, 0x00]
```

The error report does not include the sub-register. Thus, an error can also be the answer to a request from a different program. After an error, the daemon continues to read. It stops when it gets the correct response, or when it gets no data for 50 ms. It also stops after 20 reads, also when the reports from other programs continue. Thus, the request can fail on a busy receiver; a busy answer is tried again, and a slot that gets no answer is read again at the next scan.

The name from the pairing information applies only to a reading from the receiver. A reading from the cable uses the name that the device gives on its cable (`0x0005`).

The G915 TKL and G Pro Wireless instead expose BATTERY_VOLTAGE (`0x1001`). Its query is function 0 (request byte `0x0d`), and the response contains a two-byte millivolt reading and charging flags. Logibar converts that voltage to a percentage using the same discharge curve as Solaar. A drained cell converts to `0%`, which the widgets show as a critical reading — only `connected` decides visibility.

The MX Master 2S exposes BATTERY_STATUS (`0x1000`), also queried with function 0. The scan reads it in the slot where it is paired. The device may report a level of `0` while charging or full. In that case logibar uses the same status-based approximations as Solaar: charging or almost full → 50%, full → 90%, slow charging → 20%. These are coarse estimates, not measured percentages. The charging indicator follows the reported status.

The scan asks each device for UNIFIED_BATTERY, then BATTERY_STATUS, then BATTERY_VOLTAGE, and uses the first one that the device has. No battery feature is selected by product ID.

Between scans, one listener for each receiver blocks on the receiver with a timeout of one second. It sends no request to the devices. The receiver sends a message after each change of the state:

- **Connection events** (`0x41`) — the device wakes, goes to sleep, or disconnects. Bit 6 of byte 4 is the `link_off` flag.
- **Battery events** — the device reports a new charge, or the start or the end of a charge cycle. The listener takes a HID++ 2.0 event only from the battery feature index that the scan found for that slot, with software ID `0`, and updates the row and the file of its kind at once. A HID++ 1.0 device reports a battery register instead; that report starts a scan.

### Headset: a vendor protocol

The PRO X 2 LIGHTSPEED does not answer the UNIFIED_BATTERY feature. It needs a vendor command. The scripts in [`tools/`](tools/) helped to find that command. The headset also uses the HID usage page `0xffa0`, not `0xff00`.

```
Battery request:  51 08 00 03 1a 00 03 00 04 0a [00 * 54]
Battery response: 51 0b XX XX XX XX XX XX 04 XX battery_pct XX charging_status ...
                   │  │                    │     │              └── 0x02 = charging
                   │  │                    │     └── battery percentage (1-100)
                   │  │                    └── response type marker (0x04)
                   │  └── response identifier (0x0b)
                   └── report ID (0x51)

Power event:      51 05 00 03 00 00 XX 00
                                     └── 0x00 = off, 0x01 = on
```

The headset reports each power event, but it does not report a new charge without a request. For this reason, the daemon asks for the charge every 60 seconds while the headset has power. It also asks immediately after a power-on event.

## Add a device

A keyboard or a mouse needs no entry: the daemon finds it on a supported receiver or on its cable, and the device gives its own name and battery feature.

To add a receiver family that uses the same HID++ 1.0 pairing registers (Unifying and Lightspeed do; Bolt does not):

1. Find the product ID of the receiver:

   ```bash
   lsusb | grep 046d
   ```

2. Add the product ID and the number of pairing slots to `RECEIVER_SLOTS` in `logibar-hidpp-monitor`:

   ```python
   RECEIVER_SLOTS = {
       0xc52b: 6, 0xc532: 6,  # Unifying
       0xc539: 2, 0xc545: 2, 0xc547: 2, 0xc54d: 2,  # Lightspeed
       0xNEW1: 2,
   }
   ```

3. Add a test with an emulated receiver (`FakeReceiver` with a `FakePairedDevice` for each slot) to `CategoryProjectionTests` in `tests/test_hidpp_monitor.py`. Test with your physical hardware too: the sheet protects the receiver on FUTURE refactors of the daemon, but it can only prove that the daemon honors what the sheet declares — it cannot prove that the sheet matches the real device.

   `tests/fake_hid.py` emulates the hidraw layer: declared devices answer HID++ from their sheet, and every open descriptor receives a copy of every report, so the daemon runs against it unmodified.

A new device TYPE (not a keyboard or a mouse) also needs `DEVICE_IDS`, `DEVICE_ICON` and `DEVICE_NAME` in `logibar-status`, and, for a single-device widget, a copy of `logibar-keyboard` with new values for `ICON`, `TOOLTIP` and `STATE_FILE`.

> [!TIP]
> The [`tools/`](tools/) directory helps you to find the correct IDs:
> - `logibar-hidpp-battery <hidraw_device>` — reads the battery of any HID++ 2.0 device
> - `logibar-hidpp-debug <hidraw_device>` — the same, but it prints each step and tries several device indexes
> - `logibar-headset-probe` — lists the features that a device answers

## Troubleshooting

| Symptom | Cause | Solution |
|---|---|---|
| The widget never appears | The daemon does not run | `systemctl --user status logibar-hidpp-monitor` |
| The widget appears, then stays empty | The wrong `hid` package | Install `python-hidapi`, remove `python-hid`, then restart the services |
| The widget is empty | The device has no power, or it is asleep | Set the power switch to on, or press a key |
| `Permission denied` in the journal | No access to `hidraw` | Add the [udev rule](#device-permissions) |
| The charge stays the same | The state file is old | `systemctl --user restart logibar-hidpp-monitor` |
| The Omarchy widget shows a warning sign | `logibar-status` is not on PATH | `make install PREFIX=~/.local` |
| A change in `omarchy/` does nothing | The QML is still the old version | `omarchy restart shell` |

To read the logs of the daemons:

```bash
journalctl --user -u logibar-hidpp-monitor -f
journalctl --user -u logibar-headset-monitor -f
```

To see the state of every device at once:

```bash
logibar-status --json | jq
```

## Related

- [claudebar](https://github.com/mryll/claudebar) — Claude AI plan usage
- [codexbar](https://github.com/mryll/codexbar) — OpenAI Codex subscription usage
- [meteobar](https://github.com/mryll/meteobar) — the weather, from Open-Meteo
- [printbar](https://github.com/mryll/printbar) — any printer: supplies, trays and queue
- [tickerbar](https://github.com/mryll/tickerbar) — prices of crypto, stocks, indices, commodities and forex
- [Omarchy](https://github.com/basecamp/omarchy) — the Linux setup for these widgets
- [Waybar](https://github.com/Alexays/Waybar) — the status bar for Wayland
