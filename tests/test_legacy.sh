#!/usr/bin/env bash
# Tests for the three legacy per-device widgets (logibar-keyboard, -mouse,
# -headset). They are compatibility shims for existing Waybar configs, and
# their colors are resolved by logibar-status so they cannot drift from the
# combined module — that delegation is what most of this file checks.
# Standalone: bash tests/test_legacy.sh   (also wired as `make test`)

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO="$SCRIPT_DIR/.."

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
mkdir -p "$TMP/home" "$TMP/cache" "$TMP/run/logibar"

# Sandboxed run of a legacy widget: no real theme, no real daemon state.
run_widget() {  # run_widget WIDGET [args...]
    env HOME="$TMP/home" XDG_CACHE_HOME="$TMP/cache" \
        XDG_STATE_HOME="$TMP/home/.local/state" XDG_RUNTIME_DIR="$TMP/run" \
        bash "$REPO/$1" "${@:2}"
}

set_state() {  # set_state DEVICE BATTERY CONNECTED CHARGING
    printf '%s\n%s\n%s' "$2" "$3" "$4" > "$TMP/run/logibar/$1"
}

write_theme() {
    mkdir -p "$TMP/home/.local/state/omarchy/current/theme"
    cat > "$TMP/home/.local/state/omarchy/current/theme/colors.toml"
}
clear_theme() { rm -rf "$TMP/home/.local/state/omarchy"; }

WIDGETS=(logibar-keyboard logibar-mouse logibar-headset)
DEVICES=(keyboard mouse headset)

# ── rendering ────────────────────────────────────────────────────────────────

clear_theme
for i in 0 1 2; do
    set_state "${DEVICES[$i]}" 77 1 0
    out=$(run_widget "${WIDGETS[$i]}")
    check "${WIDGETS[$i]}: valid JSON" jq -e . <<< "$out"
    check "${WIDGETS[$i]}: shows the battery" jq -e '.text | test("77%")' <<< "$out"
    check "${WIDGETS[$i]}: class is normal at 77%" jq -e '.class == "normal"' <<< "$out"
    check "${WIDGETS[$i]}: tooltip has a gauge" jq -e '.tooltip | test("█")' <<< "$out"
done

set_state keyboard 8 1 0
check "critical class below 10%" jq -e '.class == "critical"' <<< "$(run_widget logibar-keyboard)"
set_state keyboard 15 1 0
check "warning class in 11-20%" jq -e '.class == "warning"' <<< "$(run_widget logibar-keyboard)"
set_state keyboard 55 1 1
check "charging shows the charging icon" jq -e '.text | test("⚡")' <<< "$(run_widget logibar-keyboard)"
for i in 0 1 2; do
    set_state "${DEVICES[$i]}" 0 1 0
    check "${WIDGETS[$i]}: a connected 0% battery shows as critical" \
        jq -e '(.text | test("0%")) and .class == "critical"' <<< "$(run_widget "${WIDGETS[$i]}")"
    set_state "${DEVICES[$i]}" 77 1 0
done
set_state keyboard 0 0 0
checkeq "disconnected hides the module" '' "$(run_widget logibar-keyboard | jq -r .text)"
rm -f "$TMP/run/logibar/keyboard"
checkeq "missing state file hides the module" '' "$(run_widget logibar-keyboard | jq -r .text)"
set_state keyboard 77 1 0

# ── colors are resolved by logibar-status, not re-implemented ────────────────

write_theme <<'EOF'
red = "#dd2222"
yellow = "#dddd22"
green = "#22dd22"
accent = "#2222dd"
EOF
tip=$(run_widget logibar-keyboard | jq -r .tooltip)
check "the theme's green reaches the widget" grep -q "#22dd22" <<< "$tip"
check "the theme's accent reaches the widget" grep -q "#2222dd" <<< "$tip"
check "One Dark green is gone" bash -c '! grep -q "#98c379" <<< "$1"' _ "$tip"

# Semantic keys are a modern-theme feature the legacy scripts never had; the
# whole point of delegating is that they get it for free.
write_theme <<'EOF'
color1 = "#111144"
color2 = "#114411"
color3 = "#444411"
EOF
check "legacy color1/2/3 themes still resolve" \
    grep -q "#114411" <<< "$(run_widget logibar-keyboard | jq -r .tooltip)"

# pywal, likewise, only exists in logibar-status.
clear_theme
mkdir -p "$TMP/cache/wal"
cat > "$TMP/cache/wal/colors.json" <<'EOF'
{"colors":{"color1":"#ab1212","color2":"#12ab12","color3":"#abab12"}}
EOF
check "a pywal cache reaches the legacy widget" \
    grep -q "#12ab12" <<< "$(run_widget logibar-keyboard | jq -r .tooltip)"
rm -rf "$TMP/cache/wal"

# ── graceful degradation when logibar-status is missing ──────────────────────

mkdir -p "$TMP/lonely"
cp "$REPO/logibar-keyboard" "$TMP/lonely/"
lonely=$(env HOME="$TMP/home" XDG_CACHE_HOME="$TMP/cache" \
             XDG_STATE_HOME="$TMP/home/.local/state" XDG_RUNTIME_DIR="$TMP/run" \
             PATH=/usr/bin:/bin bash "$TMP/lonely/logibar-keyboard")
check "still renders without logibar-status" jq -e '.text | test("77%")' <<< "$lonely"
check "falls back to the One Dark palette" grep -q "#98c379" <<< "$(jq -r .tooltip <<< "$lonely")"
lonely_override=$(env HOME="$TMP/home" XDG_CACHE_HOME="$TMP/cache" \
                      XDG_STATE_HOME="$TMP/home/.local/state" XDG_RUNTIME_DIR="$TMP/run" \
                      PATH=/usr/bin:/bin bash "$TMP/lonely/logibar-keyboard" --color-normal '#123456')
check "an override still applies without logibar-status" \
    grep -q "#123456" <<< "$(jq -r .tooltip <<< "$lonely_override")"

# ── argument handling ────────────────────────────────────────────────────────

for w in "${WIDGETS[@]}"; do
    check "$w: refuses a quote in an override" \
        jq -e '.class == "critical" and (.tooltip | test("expects a hex color"))' \
        <<< "$(run_widget "$w" --color-normal "#fff'>evil")"
done
check "a refused override never becomes markup" \
    jq -e '.tooltip | test("<span") | not' <<< "$(run_widget logibar-keyboard --color-normal "#fff'>evil")"
check "a named colour is refused" \
    jq -e '.class == "critical"' <<< "$(run_widget logibar-keyboard --color-warning red)"
checkeq "a valid override is normalized by logibar-status" "yes" \
    "$(run_widget logibar-keyboard --color-normal '#0f0' | jq -r '.tooltip | if test("#00ff00") then "yes" else "no" end')"

# A multi-line option name must not produce invalid JSON (the old hand-rolled
# escaper only handled backslashes and quotes).
weird=$(run_widget logibar-keyboard "--bogus
evil")
check "control characters in an unknown option stay valid JSON" jq -e . <<< "$weird"
check "unknown option is reported" jq -e '.tooltip | test("Unknown option")' <<< "$weird"
check "missing value for --color-normal errors" \
    jq -e '.class == "critical"' <<< "$(run_widget logibar-keyboard --color-normal)"
check "unknown --no-color value errors" \
    jq -e '.tooltip | test("unknown --no-color value")' <<< "$(run_widget logibar-keyboard --no-color=purple)"

# ── monochrome ───────────────────────────────────────────────────────────────

write_theme <<'EOF'
red = "#dd2222"
yellow = "#dddd22"
green = "#22dd22"
EOF
set_state keyboard 8 1 0   # critical, so the bar text carries color by default

bar_colored() { run_widget logibar-keyboard "$@" | jq -r .text | grep -q "foreground="; }
tip_colored() { run_widget logibar-keyboard "$@" | jq -r .tooltip | grep -q "foreground="; }
bar_plain() { ! bar_colored "$@"; }
tip_plain() { ! tip_colored "$@"; }

check "default: bar text is colored" bar_colored
check "default: tooltip is colored" tip_colored
check "--no-color: bar text is plain" bar_plain --no-color
check "--no-color: tooltip is plain" tip_plain --no-color
check "--no-color=bar: tooltip stays colored" tip_colored --no-color=bar
check "--no-color=tooltip: bar text stays colored" bar_colored --no-color=tooltip
check "plain output keeps the gauge characters" \
    jq -e '.tooltip | test("█")' <<< "$(run_widget logibar-keyboard --no-color)"
checkeq "class survives --no-color" "critical" \
    "$(run_widget logibar-keyboard --no-color | jq -r .class)"

env_tip_colored() {
    env HOME="$TMP/home" XDG_CACHE_HOME="$TMP/cache" XDG_STATE_HOME="$TMP/home/.local/state" \
        XDG_RUNTIME_DIR="$TMP/run" NO_COLOR="$1" bash "$REPO/logibar-keyboard" "${@:2}" \
        | jq -r .tooltip | grep -q "foreground="
}
env_tip_plain() { ! env_tip_colored "$@"; }

check "NO_COLOR makes the tooltip plain" env_tip_plain 1
check "empty NO_COLOR is ignored" env_tip_colored ""
check "an explicit --no-color=bar beats NO_COLOR" env_tip_colored 1 --no-color=bar

# ── summary ──────────────────────────────────────────────────────────────────

echo
echo "passed: $PASS, failed: $FAIL"
[[ $FAIL -eq 0 ]]
