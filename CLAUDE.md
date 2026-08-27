# CLAUDE.md

## Tooling

- Install: `make install PREFIX=~/.local` (no build step)
- Tests: `make test` (or `bash tests/test_status.sh`) — hermetic suite for `logibar-status`: JSON schema, aggregation/thresholds, waybar output, argument errors, and the color chain. It fakes `HOME`/`XDG_CACHE_HOME`/`XDG_RUNTIME_DIR`, so it needs neither the daemons nor a real theme
- Hardening suite: `bash tests/test_hardening.sh` — the widgets read files they do not own (daemon state, Omarchy theme, pywal cache), so it plants a FIFO, a directory, a character device and a 10 MiB blob at each of those paths and demands exit 0 with valid JSON. Every expected size is **written out by hand** (`65536`, not `$MAX_STATE_BYTES`): a test that reads the constant it checks moves with that constant and can never fail
- No linter or CI configured; shellcheck is run ad hoc

## Non-Obvious Rules

- **Quickshell emits NEITHER `started` NOR `exited` when the command does not exist** — `running` just drops back to false. `sawExit` is the discriminator: no `exited` = the run could not start; an `exited` run with empty output is an operational failure, never "not installed".
- **`installCmd` is the one constant** — the message shows it and the button copies it (`Util.execArgv(["wl-copy", ...])`, no shell line, no trailing newline). The button gates on `notInstalled`, never on error text. Pinned in `test_copy_button.sh`.

- **A tooltip meter is PARKED, not rendered in place.** The bar has to reach the tooltip's right edge, and that edge is the widest TEXT line — which does not exist yet while the lines are being collected. So a meter pushes `METER:<i>` into `lines` plus one entry in the parallel `meter_*` arrays, and the width pass resolves it. The width pass MUST skip `METER:` lines, or the measurement is circular. Every meter in one tooltip gets the SAME bar length: they stack, so a reader compares them against each other.

- Scripts have no file extensions: widgets (`logibar-status`, `logibar-keyboard`, `logibar-mouse`, `logibar-headset`) are Bash, daemons (`logibar-hidpp-monitor`, `logibar-headset-monitor`) and `tools/` are Python
- `logibar-status` is the single source of truth for severity thresholds and aggregation — the combined waybar module and the Omarchy shell plugin (`omarchy/`) both consume it; never re-derive thresholds in a frontend
- The three widget scripts are near-identical — only `ICON`, `ICON_CHARGING`, `TOOLTIP`, and `STATE_FILE` differ. Keep them in sync when changing shared logic
- Widget output uses Pango markup inside JSON (`<span>` tags) — Waybar renders it
- Daemons notify Waybar via `pkill -RTMIN+N waybar` — signal numbers are hardcoded per device and must match waybar config
- State files are 3 lines: `battery\nconnected\ncharging` in `$XDG_RUNTIME_DIR/logibar/`
- Python dependency is the Cython hidapi binding (`import hid` with `hid.device`), packaged as `python-hidapi` on Arch / `hidapi` on pip — NOT `python-hid`/`hid`, an incompatible binding that claims the same module name and makes the daemons fail silently

## Release

1. Commit `chore: release X.Y.Z` on `develop` bumping the `manifest.json` version (the script carries no version string; the tag and the manifest ARE the version), push.
2. Move master to the release — master only advances here: `git push origin develop:master`. Then `git tag vX.Y.Z && git push origin --tags`.
3. `gh release create vX.Y.Z` (bash widget: source-only release, nothing to build).
4. Only then bump the AUR package (`logibar`) per the workspace `AGENTS.md` (`~/Work/personal/AGENTS.md`).
