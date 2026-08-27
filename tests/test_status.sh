#!/usr/bin/env bash
# Tests for logibar-status: the structured JSON mode, the combined Waybar
# module, and the color-resolution chain (flags > Omarchy theme > pywal cache
# > One Dark defaults).
# Standalone: bash tests/test_status.sh   (also wired as `make test`)
#
# Every case runs hermetically: HOME, XDG_CACHE_HOME, XDG_STATE_HOME and
# XDG_RUNTIME_DIR all point into a temp dir, so the suite never reads the
# developer's real theme, pywal cache, or daemon state files — and never needs
# the daemons running. XDG_STATE_HOME in particular must be pinned: leaving it
# to the ambient environment makes every theme case read the live machine.

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LOGIBAR="$SCRIPT_DIR/../logibar-status"

PASS=0
FAIL=0

check() {
    local name="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "ok   $name"
        PASS=$((PASS + 1))
    else
        echo "FAIL $name"
        FAIL=$((FAIL + 1))
    fi
}

checkeq() {  # checkeq NAME EXPECTED ACTUAL
    if [[ "$2" == "$3" ]]; then
        echo "ok   $1"
        PASS=$((PASS + 1))
    else
        echo "FAIL $1 — expected '$2', got '$3'"
        FAIL=$((FAIL + 1))
    fi
}

command -v jq >/dev/null 2>&1 || { echo "jq is required to run the tests"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/home" "$TMP/cache" "$TMP/run"

# ── sandbox helpers ──────────────────────────────────────────────────────────

run_status() { env HOME="$TMP/home" XDG_CACHE_HOME="$TMP/cache" \
                   XDG_STATE_HOME="$TMP/home/.local/state" \
                   XDG_RUNTIME_DIR="$TMP/run" bash "$LOGIBAR" "$@"; }

palette_of() { run_status --json "$@" | jq -r '[.palette.critical, .palette.warning, .palette.ok] | join(" ")'; }

set_state() {  # set_state DEVICE BATTERY CONNECTED CHARGING
    mkdir -p "$TMP/run/logibar"
    printf '%s\n%s\n%s' "$2" "$3" "$4" > "$TMP/run/logibar/$1"
}
clear_states() { rm -rf "$TMP/run/logibar"; }

write_wal() { mkdir -p "$TMP/cache/wal"; cat > "$TMP/cache/wal/colors.json"; }
clear_wal() { rm -rf "$TMP/cache/wal"; }

write_theme() {
    mkdir -p "$TMP/home/.local/state/omarchy/current/theme"
    cat > "$TMP/home/.local/state/omarchy/current/theme/colors.toml"
}
clear_theme() { rm -rf "$TMP/home/.local/state/omarchy" "$TMP/home/.config/omarchy"; }

ONE_DARK="#e06c75 #e5c07b #98c379"   # critical warning ok

# ── structured JSON: shape ───────────────────────────────────────────────────

clear_theme; clear_wal; clear_states
set_state keyboard 90 1 0
set_state mouse 55 1 0
set_state headset 8 1 1

out=$(run_status --json)
check "json mode exits 0" test "$?" -eq 0
check "json mode is valid JSON" jq -e . <<< "$out"
check "schema_version is 1" jq -e '.schema_version == 1' <<< "$out"
check "top-level keys" \
    jq -e 'keys == (["schema_version","devices","aggregate","palette"] | sort)' <<< "$out"
check "devices is an array of three" jq -e '.devices | type == "array" and length == 3' <<< "$out"
check "device keys" \
    jq -e '.devices[0] | keys == (["id","name","battery","connected","charging","state","updated_at"] | sort)' <<< "$out"
check "aggregate keys" \
    jq -e '.aggregate | keys == (["worst_battery","any_charging","connected_count","state"] | sort)' <<< "$out"
check "palette keys" \
    jq -e '.palette | keys == (["ok","warning","critical","accent","foreground",
                                "background","thresholds","stops"] | sort)' <<< "$out"
check "battery values are numbers 0-100" \
    jq -e '[.devices[] | select(.connected) | .battery | type == "number" and . >= 0 and . <= 100] | all' <<< "$out"
check "updated_at is ISO-8601 or null" \
    jq -e '[.devices[] | .updated_at | . == null or test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T")] | all' <<< "$out"

# ── aggregation and severity thresholds ──────────────────────────────────────

check "worst battery wins the aggregate" jq -e '.aggregate.worst_battery == 8' <<< "$out"
check "any_charging is true when one device charges" jq -e '.aggregate.any_charging == true' <<< "$out"
check "connected_count counts connected devices" jq -e '.aggregate.connected_count == 3' <<< "$out"
check "aggregate state follows the worst device" jq -e '.aggregate.state == "critical"' <<< "$out"
check "per-device state: ok above 20" \
    jq -e '.devices[] | select(.id == "keyboard") | .state == "ok"' <<< "$out"
check "per-device state: critical at 8" \
    jq -e '.devices[] | select(.id == "headset") | .state == "critical"' <<< "$out"

clear_states
set_state keyboard 20 1 0
check "20% is warning (inclusive)" \
    jq -e '.aggregate.state == "warning"' <<< "$(run_status --json)"
clear_states
set_state keyboard 21 1 0
check "21% is ok" jq -e '.aggregate.state == "ok"' <<< "$(run_status --json)"
clear_states
set_state keyboard 10 1 0
check "10% is critical (inclusive)" \
    jq -e '.aggregate.state == "critical"' <<< "$(run_status --json)"
clear_states
set_state keyboard 0 1 0
out=$(run_status --json)
check "a connected 0% battery stays visible" \
    jq -e '.devices[] | select(.id == "keyboard") | .connected == true and .battery == 0' <<< "$out"
check "a connected 0% battery is critical" \
    jq -e '.devices[] | select(.id == "keyboard") | .state == "critical"' <<< "$out"
check "a connected 0% wins the aggregate" \
    jq -e '.aggregate.worst_battery == 0 and .aggregate.state == "critical"' <<< "$out"

# ── disconnected / absent devices ────────────────────────────────────────────

clear_states
set_state keyboard 0 0 0
out=$(run_status --json)
check "disconnected device reports connected false" \
    jq -e '.devices[] | select(.id == "keyboard") | .connected == false' <<< "$out"
check "disconnected device has null battery" \
    jq -e '.devices[] | select(.id == "keyboard") | .battery == null' <<< "$out"
check "disconnected device state" \
    jq -e '.devices[] | select(.id == "keyboard") | .state == "disconnected"' <<< "$out"
check "empty aggregate has null worst_battery" jq -e '.aggregate.worst_battery == null' <<< "$out"
check "empty aggregate counts zero" jq -e '.aggregate.connected_count == 0' <<< "$out"

clear_states
check "missing state dir still emits valid JSON" jq -e '.schema_version == 1' <<< "$(run_status --json)"
checkeq "missing state dir hides the waybar module" '' "$(run_status | jq -r .text)"

# ── device filter ────────────────────────────────────────────────────────────

clear_states
set_state keyboard 90 1 0
set_state mouse 30 1 0
set_state headset 12 1 0
check "unfiltered aggregate uses every device" \
    jq -e '.aggregate.worst_battery == 12' <<< "$(run_status --json)"
check "--devices narrows the device list" \
    jq -e '.devices | length == 2' <<< "$(run_status --json --devices keyboard,mouse)"
check "--devices narrows the aggregate too" \
    jq -e '.aggregate.worst_battery == 30 and .aggregate.state == "ok"' \
    <<< "$(run_status --json --devices keyboard,mouse)"

# ── waybar mode ──────────────────────────────────────────────────────────────

out=$(run_status)
check "waybar mode is valid JSON" jq -e . <<< "$out"
check "waybar keys present" \
    jq -e 'has("text") and has("tooltip") and has("class") and has("percentage")' <<< "$out"
check "waybar percentage is the worst battery" jq -e '.percentage == 12' <<< "$out"
check "waybar class reflects severity" jq -e '.class == "warning"' <<< "$out"
check "waybar tooltip lists every connected device" \
    jq -e '.tooltip | test("G915") and test("PRO X Superlight") and test("PRO X 2")' <<< "$out"

# ── argument errors: exit 0 with a structured document ───────────────────────

out=$(run_status --json --devices nope); rc=$?
check "unknown device exits 0" test "$rc" -eq 0
check "unknown device emits a JSON error" \
    jq -e '.schema_version == 1 and (.error.message | test("nope"))' <<< "$out"
out=$(run_status --json --bogus); rc=$?
check "unknown option exits 0" test "$rc" -eq 0
check "unknown option emits a JSON error" jq -e '.error.message | test("bogus")' <<< "$out"
check "waybar arg error stays waybar-shaped" \
    jq -e 'has("text") and .class == "critical"' <<< "$(run_status --bogus)"
check "--devices without a value errors" \
    jq -e '.error.message | test("requires a value")' <<< "$(run_status --json --devices)"

# ── color chain: flags > Omarchy theme > pywal > One Dark ────────────────────

clear_theme; clear_wal
checkeq "no theme, no pywal: One Dark defaults" "$ONE_DARK" "$(palette_of)"

write_wal <<'EOF'
{"special":{"background":"#101010","foreground":"#e8e8e8","cursor":"#00ffcc"},
 "colors":{"color1":"#cc3344","color2":"#33cc55","color3":"#ddbb22","color4":"#4488ff"}}
EOF
checkeq "pywal cache is used when no Omarchy theme" "#cc3344 #ddbb22 #33cc55" "$(palette_of)"

write_theme <<'EOF'
red = "#f7768e"
yellow = "#e0af68"
green = "#9ece6a"
EOF
checkeq "Omarchy theme beats pywal" "#f7768e #e0af68 #9ece6a" "$(palette_of)"
checkeq "flags beat the Omarchy theme" "#f7768e #e0af68 #123456" \
    "$(palette_of --color-normal '#123456')"
checkeq "flags beat pywal and the theme together" "#010203 #e0af68 #123456" \
    "$(palette_of --color-normal '#123456' --color-critical '#010203')"

# Legacy themes only carry the terminal slots.
write_theme <<'EOF'
color1 = "#aa0000"
color2 = "#00aa00"
color3 = "#aaaa00"
EOF
checkeq "legacy color1/2/3 theme keys still work" "#aa0000 #aaaa00 #00aa00" "$(palette_of)"

write_theme <<'EOF'
color1 = "#aa0000"
color2 = "#00aa00"
color3 = "#aaaa00"
red = "#ff5555"
yellow = "#ffff55"
green = "#55ff55"
EOF
checkeq "semantic theme keys beat the legacy slots" "#ff5555 #ffff55 #55ff55" "$(palette_of)"

# ── pywal robustness ─────────────────────────────────────────────────────────

clear_theme
write_wal <<'EOF'
{ not json ]
EOF
checkeq "malformed pywal JSON degrades to defaults" "$ONE_DARK" "$(palette_of)"
check "malformed pywal JSON still exits 0 with valid JSON" \
    jq -e '.schema_version == 1' <<< "$(run_status --json)"

write_wal <<'EOF'
[1, 2, 3]
EOF
checkeq "non-object pywal JSON degrades to defaults" "$ONE_DARK" "$(palette_of)"

write_wal <<'EOF'
{"colors":{"color2":"#00ff00"}}
EOF
checkeq "missing pywal keys fall back per color" "#e06c75 #e5c07b #00ff00" "$(palette_of)"

write_wal <<'EOF'
{"colors":{"color1":"rgb(1,2,3)","color2":"#0f0","color3":"#aabbccdd"}}
EOF
checkeq "non-hex ignored, #rgb expanded, alpha dropped" "#e06c75 #aabbcc #00ff00" "$(palette_of)"

write_wal <<'EOF'
{"colors":{"color1":"#111111","color2":"#222222","color3":"#333333"}}
EOF
chmod 000 "$TMP/cache/wal/colors.json"
if [[ -r "$TMP/cache/wal/colors.json" ]]; then
    echo "skip unreadable-cache case (running as root)"
else
    checkeq "unreadable pywal cache degrades to defaults" "$ONE_DARK" "$(palette_of)"
fi
chmod 644 "$TMP/cache/wal/colors.json"

# REGRESSION: a pywal cache whose `special` block is absent leaves holes in the
# middle of the value list. Reading those values with a TAB-separated `read`
# collapses the empty fields (tab is IFS whitespace) and shifts every later
# color one slot left — red lands in foreground, green in background, and the
# gauge silently paints with the wrong colors. Keep the reader line-based (or
# use a non-whitespace separator) so absent keys hold their position.
checkeq "absent pywal keys do not shift later colors" "#111111 #333333 #222222" "$(palette_of)"

write_wal <<'EOF'
{"special":{"foreground":"#eeeeee"},"colors":{"color3":"#345678"}}
EOF
checkeq "leading and trailing holes keep their positions" "#e06c75 #345678 #98c379" "$(palette_of)"

# ── published ramp: colors AND stop positions ────────────────────────────────

clear_theme; clear_wal; clear_states
set_state keyboard 90 1 0
out=$(run_status --json)
check "palette carries the surface colors too" \
    jq -e '.palette | has("accent") and has("foreground") and has("background")' <<< "$out"
check "palette publishes the thresholds" \
    jq -e '.palette.thresholds | .warning == 20 and .critical == 10' <<< "$out"
check "palette publishes ramp stops" \
    jq -e '.palette.stops | type == "array" and length >= 2' <<< "$out"
check "stops are ordered by pct" \
    jq -e '[.palette.stops[].pct] | . == sort' <<< "$out"
check "stops span the whole scale" \
    jq -e '(.palette.stops[0].pct == 0) and (.palette.stops[-1].pct == 100)' <<< "$out"
check "every stop is a hex color" \
    jq -e '[.palette.stops[].color | test("^#[0-9a-f]{6}$")] | all' <<< "$out"
# The stop positions must be DERIVED from the thresholds, not written twice:
# this is what lets a threshold change move the QML ramp as well.
check "stop positions come from the thresholds" \
    jq -e '.palette.stops as $s | .palette.thresholds as $t
           | ($s[1].pct == $t.critical) and ($s[2].pct == $t.warning)' <<< "$out"
check "the ramp ends carry the gauge colors" \
    jq -e '.palette as $p | ($p.stops[0].color == $p.critical) and ($p.stops[-1].color == $p.ok)' <<< "$out"
check "an override reaches the stops" \
    jq -e '.palette.stops[-1].color == "#00ff00"' <<< "$(run_status --json --color-normal '#0f0')"

# ── colour overrides are validated at the door ───────────────────────────────

check "a quote in an override is refused" \
    jq -e '.error.message | test("expects a hex color")' <<< "$(run_status --json --color-normal "#fff'>evil")"
check "a named colour is refused" \
    jq -e '.error.message | test("expects a hex color")' <<< "$(run_status --json --color-critical red)"
check "a refused override still exits 0" \
    test "$(run_status --json --color-normal bogus >/dev/null 2>&1; echo $?)" -eq 0
check "the waybar path refuses it in waybar shape" \
    jq -e 'has("text") and .class == "critical"' <<< "$(run_status --color-normal bogus)"
checkeq "#rgb overrides are normalized" "#00ff00" "$(run_status --json --color-normal '#0f0' | jq -r .palette.ok)"
checkeq "#rrggbbaa overrides drop the alpha" "#aabbcc" \
    "$(run_status --json --color-warning '#aabbccdd' | jq -r .palette.warning)"
check "a refused override never becomes markup" \
    jq -e '.tooltip | test("<span") | not' <<< "$(run_status --color-normal "#fff'>evil" 2>/dev/null)"

# ── XDG_STATE_HOME ───────────────────────────────────────────────────────────

state_run() {  # state_run STATE_HOME [args...]
    env HOME="$TMP/home" XDG_CACHE_HOME="$TMP/cache" XDG_STATE_HOME="$1" \
        XDG_RUNTIME_DIR="$TMP/run" bash "$LOGIBAR" "${@:2}"
}

mkdir -p "$TMP/xdgstate/omarchy/current/theme"
cat > "$TMP/xdgstate/omarchy/current/theme/colors.toml" <<'EOF'
red = "#aa1111"
yellow = "#aaaa11"
green = "#11aa11"
EOF
checkeq "XDG_STATE_HOME is where the theme is looked up" "#aa1111 #aaaa11 #11aa11" \
    "$(state_run "$TMP/xdgstate" --json | jq -r '[.palette.critical, .palette.warning, .palette.ok] | join(" ")')"
# An explicitly empty XDG_STATE_HOME must mean "unset", not the current dir.
checkeq "empty XDG_STATE_HOME falls back to ~/.local/state" "$ONE_DARK" \
    "$(state_run "" --json | jq -r '[.palette.critical, .palette.warning, .palette.ok] | join(" ")')"

# ── invalid theme values fall through instead of blocking ────────────────────

write_theme <<'EOF'
red = "not-a-color"
color1 = "#123123"
green = "#00ff00"
EOF
checkeq "an invalid semantic key falls back to the legacy slot" "#123123" \
    "$(palette_of | cut -d' ' -f1)"
checkeq "valid semantic keys are unaffected by an invalid sibling" "#00ff00" \
    "$(palette_of | cut -d' ' -f3)"
clear_theme

# ── monochrome mode: --no-color[=all|bar|tooltip] and NO_COLOR ───────────────

clear_theme; clear_wal; clear_states
# A critical battery so the bar text is colored in the default state — at "ok"
# the bar carries no color anyway and the flag would be untestable there.
set_state keyboard 5 1 0

# Colored means "carries Pango color markup on that surface"; plain is the
# negation. Both senses get their own helper so `check` stays a plain command
# runner (a bare `!` would be expanded as a command name, not as negation).
bar_colored() { run_status "$@" | jq -r .text | grep -q "foreground="; }
tip_colored() { run_status "$@" | jq -r .tooltip | grep -q "foreground="; }
bar_plain() { ! bar_colored "$@"; }
tip_plain() { ! tip_colored "$@"; }

check "default: bar text is colored" bar_colored
check "default: tooltip is colored" tip_colored
check "--no-color: bar text is plain" bar_plain --no-color
check "--no-color: tooltip is plain" tip_plain --no-color
check "--no-color=all: bar text is plain" bar_plain --no-color=all
check "--no-color=all: tooltip is plain" tip_plain --no-color=all
check "--no-color=bar: bar text is plain" bar_plain --no-color=bar
check "--no-color=bar: tooltip stays colored" tip_colored --no-color=bar
check "--no-color=tooltip: bar text stays colored" bar_colored --no-color=tooltip
check "--no-color=tooltip: tooltip is plain" tip_plain --no-color=tooltip

check "plain output keeps the device glyphs" \
    jq -e '.tooltip | test("󰌌")' <<< "$(run_status --no-color)"
check "plain output keeps bold weight and box drawing" \
    jq -e '.tooltip | test("font_weight=.bold.") and test("─")' <<< "$(run_status --no-color)"
# --frame is deprecated and a no-op: still ACCEPTED (an existing Waybar config
# keeps working) and it draws nothing. The font pin survives monochrome, because
# it is what keeps the rule the same width as the text it underlines.
check "deprecated --frame draws no box" \
    jq -e '.tooltip | test("╭") | not' <<< "$(run_status --no-color --frame)"
check "deprecated --frame keeps the font pin" \
    jq -e '.tooltip | test("font_family=")' <<< "$(run_status --no-color --frame)"
check "plain tooltip has no color markup" \
    jq -e '.tooltip | test("foreground=") | not' <<< "$(run_status --no-color --frame)"

# The class field is a machine contract: monochrome users style it from CSS.
checkeq "class survives --no-color" "critical" "$(run_status --no-color | jq -r .class)"
checkeq "percentage survives --no-color" "5" "$(run_status --no-color | jq -r .percentage)"

no_color_env() {  # no_color_env NO_COLOR_VALUE [args...]
    env HOME="$TMP/home" XDG_CACHE_HOME="$TMP/cache" XDG_RUNTIME_DIR="$TMP/run" \
        XDG_STATE_HOME="$TMP/home/.local/state" NO_COLOR="$1" bash "$LOGIBAR" "${@:2}"
}
env_bar_colored() { no_color_env "$@" | jq -r .text | grep -q "foreground="; }
env_tip_colored() { no_color_env "$@" | jq -r .tooltip | grep -q "foreground="; }
env_bar_plain() { ! env_bar_colored "$@"; }
env_tip_plain() { ! env_tip_colored "$@"; }

check "NO_COLOR=1 makes the bar plain" env_bar_plain 1
check "NO_COLOR=1 makes the tooltip plain" env_tip_plain 1
check "NO_COLOR=anything non-empty counts" env_tip_plain 0
check "empty NO_COLOR is ignored (bar stays colored)" env_bar_colored ""
check "empty NO_COLOR is ignored (tooltip stays colored)" env_tip_colored ""
# The explicit flag is the more specific instruction and beats the env var.
check "explicit --no-color=bar beats NO_COLOR for the tooltip" env_tip_colored 1 --no-color=bar
check "explicit --no-color=bar still plains the bar under NO_COLOR" env_bar_plain 1 --no-color=bar
check "explicit --no-color=tooltip beats NO_COLOR for the bar" env_bar_colored 1 --no-color=tooltip

check "unknown --no-color value is an arg error" \
    jq -e '.error.message | test("unknown --no-color value")' <<< "$(run_status --json --no-color=purple)"
check "unknown --no-color value exits 0" test "$(run_status --json --no-color=purple >/dev/null 2>&1; echo $?)" -eq 0
check "unknown --no-color value keeps the waybar shape" \
    jq -e 'has("text") and .class == "critical"' <<< "$(run_status --no-color=purple)"

# The structured document is data, never presentation: the flag must not touch it.
checkeq "--json is byte-identical with --no-color" \
    "$(run_status --json)" "$(run_status --json --no-color)"
checkeq "--json is byte-identical under NO_COLOR" \
    "$(run_status --json)" "$(no_color_env 1 --json)"
check "--json still carries the palette under --no-color" \
    jq -e '.palette | has("ok") and has("warning") and has("critical")' <<< "$(run_status --json --no-color)"

clear_states
set_state keyboard 90 1 0
set_state mouse 30 1 0
set_state headset 12 1 0

# ── XDG_CACHE_HOME is honored ────────────────────────────────────────────────

mkdir -p "$TMP/altcache/wal"
cat > "$TMP/altcache/wal/colors.json" <<'EOF'
{"colors":{"color1":"#abcdef","color2":"#fedcba","color3":"#123abc"}}
EOF
alt=$(env HOME="$TMP/home" XDG_CACHE_HOME="$TMP/altcache" XDG_RUNTIME_DIR="$TMP/run" \
      XDG_STATE_HOME="$TMP/home/.local/state" \
      bash "$LOGIBAR" --json | jq -r '[.palette.critical, .palette.warning, .palette.ok] | join(" ")')
checkeq "XDG_CACHE_HOME points at the pywal cache" "#abcdef #123abc #fedcba" "$alt"

# The waybar module paints from the same resolved palette. The gauge is a
# CONTINUOUS ramp (the same one the panel paints and `palette.stops` publishes),
# so a mid-range battery lands on a BLEND of two anchors rather than on an
# anchor itself — asserting an exact anchor hex would only pass by accident.
# What must hold is that swapping the palette swaps the tooltip's colors.
tip_alt=$(env HOME="$TMP/home" XDG_CACHE_HOME="$TMP/altcache" XDG_RUNTIME_DIR="$TMP/run" \
              XDG_STATE_HOME="$TMP/home/.local/state" bash "$LOGIBAR" | jq -r '.tooltip')
tip_def=$(env HOME="$TMP/home" XDG_CACHE_HOME="$TMP/nocache" XDG_RUNTIME_DIR="$TMP/run" \
              XDG_STATE_HOME="$TMP/home/.local/state" bash "$LOGIBAR" | jq -r '.tooltip')
check "waybar tooltip carries color markup" \
    grep -q "foreground='#" <<< "$tip_alt"
checkeq "waybar tooltip follows the resolved palette" "different" \
    "$([[ "$tip_alt" != "$tip_def" ]] && echo different || echo same)"

# ── summary ──────────────────────────────────────────────────────────────────

echo
echo "passed: $PASS, failed: $FAIL"
[[ $FAIL -eq 0 ]]
