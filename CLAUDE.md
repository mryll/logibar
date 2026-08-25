# CLAUDE.md

## Tooling

- Install: `make install PREFIX=~/.local` (no build step)
- Tests: `make test` (or `bash tests/test_status.sh`) — hermetic suite for `logibar-status`: JSON schema, aggregation/thresholds, waybar output, argument errors, and the color chain. It fakes `HOME`/`XDG_CACHE_HOME`/`XDG_RUNTIME_DIR`, so it needs neither the daemons nor a real theme
- No linter or CI configured; shellcheck is run ad hoc

## Non-Obvious Rules

- **Quickshell emits NEITHER `started` NOR `exited` when the command does not exist** — `running` just drops back to false. That is the only signal a failed start gives. Anything that waits on `onExited` to leave a loading state hangs for ever when the CLI is not installed, which is the first run of everyone who installs the plugin from the marketplace: the plugin is a git clone, the CLI is a package, and nothing installs the second for you. The `onRunningChanged` guard in the panel's `Process` is what makes the not-installed message reachable — verified against a running shell, not assumed.

- **A tooltip meter is PARKED, not rendered in place.** The bar has to reach the tooltip's right edge, and that edge is the widest TEXT line — which does not exist yet while the lines are being collected. So a meter pushes `METER:<i>` into `lines` plus one entry in the parallel `meter_*` arrays, and the width pass resolves it. The width pass MUST skip `METER:` lines, or the measurement is circular. Every meter in one tooltip gets the SAME bar length: they stack, so a reader compares them against each other.

- Scripts have no file extensions: widgets (`logibar-status`, `logibar-keyboard`, `logibar-mouse`, `logibar-headset`) are Bash, daemons (`logibar-hidpp-monitor`, `logibar-headset-monitor`) and `tools/` are Python
- `logibar-status` is the single source of truth for severity thresholds and aggregation — the combined waybar module and the Omarchy shell plugin (`omarchy/`) both consume it; never re-derive thresholds in a frontend
- The three widget scripts are near-identical — only `ICON`, `ICON_CHARGING`, `TOOLTIP`, and `STATE_FILE` differ. Keep them in sync when changing shared logic
- Widget output uses Pango markup inside JSON (`<span>` tags) — Waybar renders it
- Daemons notify Waybar via `pkill -RTMIN+N waybar` — signal numbers are hardcoded per device and must match waybar config
- State files are 3 lines: `battery\nconnected\ncharging` in `$XDG_RUNTIME_DIR/logibar/`
- Python dependency is the Cython hidapi binding (`import hid` with `hid.device`), packaged as `python-hidapi` on Arch / `hidapi` on pip — NOT `python-hid`/`hid`, an incompatible binding that claims the same module name and makes the daemons fail silently
