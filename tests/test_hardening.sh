#!/usr/bin/env bash
# Tests for the bounded-read hardening: the widgets must survive a file they
# do not own being swapped for a FIFO, a directory, a device node, or a
# multi-megabyte blob. This matters more than it looks — the Omarchy shell
# plugin runs `logibar-status` from inside the shell's own long-lived process,
# so a read that never returns hangs the whole desktop shell, not one widget.
# Standalone: bash tests/test_hardening.sh   (also wired as `make test`)
#
# Every expected size in this file is WRITTEN OUT BY HAND — 65536, not
# "$MAX_STATE_BYTES". A test that reads the same constant it is checking moves
# with that constant and can never fail: raise the cap to a gigabyte and such a
# test still passes, while the protection is gone. The numbers below are the
# contract; if a cap changes on purpose, this file has to change with it.
#
# The helper under test is EXTRACTED FROM THE SHIPPED SCRIPTS, not copied here,
# for the same reason: a copy drifts, and then the suite tests the copy.

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
command -v timeout >/dev/null 2>&1 || { echo "timeout is required to run the tests"; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/home" "$TMP/cache" "$TMP/run/logibar" "$TMP/fixtures"

WIDGETS=(logibar-status logibar-keyboard logibar-mouse logibar-headset)
LEGACY=(logibar-keyboard logibar-mouse logibar-headset)
DEVICES=(keyboard mouse headset)

# ── sandbox helpers ──────────────────────────────────────────────────────────

# Same hermetic sandbox the other two suites use: no real theme, no real pywal
# cache, no real daemon state. Every run is wrapped in `timeout`, because the
# whole point of these cases is that an unguarded read never comes back — a
# regression must show up as a failed test, not as a suite that hangs for ever.
run_widget() {  # run_widget WIDGET [args...]
    timeout 10 env HOME="$TMP/home" XDG_CACHE_HOME="$TMP/cache" \
        XDG_STATE_HOME="$TMP/home/.local/state" XDG_RUNTIME_DIR="$TMP/run" \
        bash "$REPO/$1" "${@:2}"
}

# rc + stdout of a run, without letting `set -o pipefail` swallow the code.
RC=0
OUT=""
run_capture() {  # run_capture WIDGET [args...]
    OUT=$(run_widget "$@" 2>/dev/null)
    RC=$?
}

clear_states() { rm -rf "$TMP/run/logibar"; mkdir -p "$TMP/run/logibar"; }
clear_theme()  { rm -rf "$TMP/home/.local/state/omarchy" "$TMP/home/.config/omarchy"; }
clear_wal()    { rm -rf "$TMP/cache/wal"; }
theme_dir()    { echo "$TMP/home/.local/state/omarchy/current/theme"; }

reset_all() { clear_states; clear_theme; clear_wal; }

# The helper is pulled out of the real script every time, so this suite can
# never pass against a script whose helper was gutted.
run_helper() {  # run_helper SCRIPT PATH MAXBYTES
    local src
    src=$(sed -n '/^read_bounded() {/,/^}/p' "$REPO/$1")
    [[ -n "$src" ]] || return 111   # no helper in the script at all
    timeout 5 bash -c "$src"$'\n''read_bounded "$1" "$2"' _ "$2" "$3"
}

# The same helper with its path check cut out. `[[ -f ]]` runs on the PATH and
# the open that follows runs on whatever that path names BY THEN, so the two
# are a race: swap the regular file for a FIFO in between and the open is the
# one that has to survive. A real race cannot be timed reliably from a test, so
# this reproduces the state the race leaves behind — the open, on a FIFO, with
# no check in front of it — and asks whether it comes back.
run_helper_unguarded() {  # run_helper_unguarded SCRIPT PATH MAXBYTES
    local src
    src=$(sed -n '/^read_bounded() {/,/^}/p' "$REPO/$1" | grep -v '\[\[ -f')
    [[ -n "$src" ]] || return 111
    timeout 5 bash -c "$src"$'\n''read_bounded "$1" "$2"' _ "$2" "$3"
}

# Evaluate a constant the way bash would, so `$((64 * 1024))` is compared as a
# number and not as source text.
const_of() {  # const_of SCRIPT NAME
    local line
    line=$(grep -m1 "^$2=" "$REPO/$1") || return 1
    bash -c "$line"$'\n'"printf '%s' \"\${$2}\""
}

# ── fixtures ─────────────────────────────────────────────────────────────────

BIG="$TMP/fixtures/big"                 # 10 MiB of a single repeated byte
head -c 10485760 /dev/zero | tr '\0' 'x' > "$BIG"

SMALL="$TMP/fixtures/small"
printf 'hello' > "$SMALL"

FIFO="$TMP/fixtures/fifo"
mkfifo "$FIFO"

FIFOLINK="$TMP/fixtures/fifolink"
ln -s "$FIFO" "$FIFOLINK"

ZERO="$TMP/fixtures/zero"               # symlink to an endless character device
ln -s /dev/zero "$ZERO"

DIR="$TMP/fixtures/dir"
mkdir -p "$DIR"

# ── the helper: what it refuses ──────────────────────────────────────────────
#
# The guard has two halves and they are tested apart. `-f` CLASSIFIES: a FIFO,
# a symlink to one, a directory and a device node are not regular files, so the
# helper returns 1 and every caller reads that as "no data". That is this
# block. `iflag=nonblock` is what makes the open itself SAFE, and the block
# after this one is the only place that can see it.

for w in "${WIDGETS[@]}"; do
    check "$w: read_bounded exists" bash -c "sed -n '/^read_bounded() {/,/^}/p' '$REPO/$w' | grep -q 'iflag=nonblock'"

    out=$(run_helper "$w" "$FIFO" 65536); rc=$?
    checkeq "$w: refuses a FIFO (rc)" 1 "$rc"
    checkeq "$w: refuses a FIFO (no output)" "" "$out"

    out=$(run_helper "$w" "$FIFOLINK" 65536); rc=$?
    checkeq "$w: refuses a symlink to a FIFO" 1 "$rc"

    out=$(run_helper "$w" "$ZERO" 65536); rc=$?
    checkeq "$w: refuses a character device" 1 "$rc"

    out=$(run_helper "$w" "$DIR" 65536); rc=$?
    checkeq "$w: refuses a directory" 1 "$rc"

    out=$(run_helper "$w" "$TMP/fixtures/nope" 65536); rc=$?
    checkeq "$w: refuses a missing file" 1 "$rc"

    out=$(run_helper "$w" "$SMALL" 65536); rc=$?
    checkeq "$w: passes a small regular file through" "hello" "$out"
done

# ── the helper: the open cannot block ───────────────────────────────────────
#
# The control for the TOCTOU fix, and the only case in this file that `-f`
# cannot pass on the helper's behalf. `dd iflag=nonblock` opens a FIFO and
# comes straight back with nothing. `head -c` opens without O_NONBLOCK and
# waits for a writer that never comes: `timeout` kills it at rc 124, which
# reads as a FAIL here rather than as a suite that never finishes.

for w in "${WIDGETS[@]}"; do
    out=$(run_helper_unguarded "$w" "$FIFO" 65536); rc=$?
    checkeq "$w: opening a FIFO with no path check in front returns (rc)" 0 "$rc"
    check   "$w: opening a FIFO is not killed by the timeout" test "$rc" -ne 124
    checkeq "$w: a FIFO nobody writes to yields nothing" "" "$out"
done

# ── the helper: where it cuts ────────────────────────────────────────────────
#
# Byte counts written out in full on purpose (see the header). 10485760 is the
# fixture size, so "did not truncate" is a distinguishable answer, not a
# coincidence.

for w in "${WIDGETS[@]}"; do
    n=$(run_helper "$w" "$BIG" 65536 | wc -c)
    checkeq "$w: cuts a 10 MiB file at 65536 bytes" 65536 "$n"

    n=$(run_helper "$w" "$BIG" 262144 | wc -c)
    checkeq "$w: cuts a 10 MiB file at 262144 bytes" 262144 "$n"
done

# ── the caps themselves ──────────────────────────────────────────────────────
#
# 64 KiB for a four-line state file, 256 KiB for a hand-written theme or
# palette cache. Hard-coded here so that widening a cap turns this suite red.

checkeq "logibar-status: state cap is 64 KiB" 65536 "$(const_of logibar-status MAX_STATE_BYTES)"
checkeq "logibar-status: theme cap is 256 KiB" 262144 "$(const_of logibar-status MAX_THEME_BYTES)"
for w in "${LEGACY[@]}"; do
    checkeq "$w: state cap is 64 KiB" 65536 "$(const_of "$w" MAX_STATE_BYTES)"
done

# ── no unguarded read may come back ──────────────────────────────────────────
#
# Behaviour tests cannot see a read that was merely ADDED next to the guarded
# one, so these pin the shape of the call sites: the three paths this script
# does not own are reached through read_bounded and nothing else.

check "logibar-status: state files go through read_bounded" \
    grep -q 'read_bounded "$file" "$MAX_STATE_BYTES"' "$REPO/logibar-status"
check "logibar-status: no per-line sed on a state file" \
    bash -c "! grep -q \"sed -n '[0-9]p' \\\"\\\$file\\\"\" '$REPO/logibar-status'"
check "logibar-status: theme goes through read_bounded" \
    grep -q 'read_bounded "$_theme" "$MAX_THEME_BYTES"' "$REPO/logibar-status"
check "logibar-status: theme loop does not redirect from the path" \
    bash -c "! grep -q 'done < \"\$_theme\"' '$REPO/logibar-status'"
check "logibar-status: pywal cache goes through read_bounded" \
    grep -q 'read_bounded "$_cache" "$MAX_THEME_BYTES"' "$REPO/logibar-status"
check "logibar-status: jq is not handed the cache path" \
    bash -c "! grep -q '\"\$_cache\" 2>/dev/null' '$REPO/logibar-status'"

for w in "${LEGACY[@]}"; do
    check "$w: state file goes through read_bounded" \
        grep -q 'read_bounded "$STATE_FILE" "$MAX_STATE_BYTES"' "$REPO/$w"
    check "$w: no per-line sed on the state file" \
        bash -c "! grep -q \"sed -n '[0-9]p' \\\"\\\$STATE_FILE\\\"\" '$REPO/$w'"
done

# ── end to end: a FIFO where a file is expected ──────────────────────────────
#
# The contract every widget keeps on every path, including this one: exit 0
# with a valid Waybar document. A hang shows up here as rc 124 from `timeout`.

reset_all
mkfifo "$TMP/run/logibar/mouse"
run_capture logibar-status
checkeq "waybar mode survives a FIFO state file (rc)" 0 "$RC"
check   "waybar mode survives a FIFO state file (JSON)" jq -e . <<< "$OUT"

run_capture logibar-status --json
checkeq "--json survives a FIFO state file (rc)" 0 "$RC"
check   "--json survives a FIFO state file (JSON)" jq -e '.schema_version == 1' <<< "$OUT"
check   "--json reports the FIFO device as disconnected" \
    jq -e '.devices[] | select(.id == "mouse") | .connected == false' <<< "$OUT"

reset_all
for i in 0 1 2; do
    mkfifo "$TMP/run/logibar/${DEVICES[$i]}"
    run_capture "${LEGACY[$i]}"
    checkeq "${LEGACY[$i]} survives a FIFO state file (rc)" 0 "$RC"
    check   "${LEGACY[$i]} survives a FIFO state file (JSON)" jq -e . <<< "$OUT"
    rm -f "$TMP/run/logibar/${DEVICES[$i]}"
done

reset_all
mkdir -p "$(theme_dir)"
mkfifo "$(theme_dir)/colors.toml"
run_capture logibar-status
checkeq "a FIFO theme does not hang the widget (rc)" 0 "$RC"
check   "a FIFO theme does not hang the widget (JSON)" jq -e . <<< "$OUT"

# The pywal cache was the one path whose guard was `-r`, which is TRUE for a
# FIFO — so jq opened it and blocked for ever. It is the regression this case
# exists for.
reset_all
mkdir -p "$TMP/cache/wal"
mkfifo "$TMP/cache/wal/colors.json"
run_capture logibar-status --json
checkeq "a FIFO pywal cache does not hang the widget (rc)" 0 "$RC"
check   "a FIFO pywal cache falls back to the defaults" \
    jq -e '.palette.critical == "#e06c75"' <<< "$OUT"

reset_all
mkdir -p "$(theme_dir)"
ln -s /dev/zero "$(theme_dir)/colors.toml"
run_capture logibar-status
checkeq "an endless device as the theme does not hang the widget" 0 "$RC"
check   "an endless device as the theme still gives JSON" jq -e . <<< "$OUT"

reset_all
mkdir -p "$TMP/run/logibar/headset"
run_capture logibar-status
checkeq "a directory where a state file belongs (rc)" 0 "$RC"
check   "a directory where a state file belongs (JSON)" jq -e . <<< "$OUT"
run_capture logibar-headset
checkeq "legacy widget: a directory where a state file belongs (rc)" 0 "$RC"
check   "legacy widget: a directory where a state file belongs (JSON)" jq -e . <<< "$OUT"

# ── end to end: the cut is real, not just fast ───────────────────────────────
#
# The strongest case in this file, because it reads a DIFFERENT ANSWER rather
# than a shorter one. The theme parser takes the last assignment of a key, so a
# second `red` planted past the cap changes the palette if — and only if — the
# whole file was read. 300017 bytes in, against a 262144-byte cap, both written
# out by hand. Widen the cap and the widget starts answering #222222.
reset_all
mkdir -p "$(theme_dir)"
{
    printf 'red = "#111111"\n'
    head -c 300000 /dev/zero | tr '\0' 'x'
    printf '\nred = "#222222"\n'
} > "$(theme_dir)/colors.toml"
run_capture logibar-status --json
checkeq "an oversized theme still answers (rc)" 0 "$RC"
checkeq "a key past the cap is never read" "#111111" "$(jq -r '.palette.critical' <<< "$OUT")"

# Same trick, one byte class down: a key that sits just INSIDE the cap must
# still be read, or the test above would also pass with a cap of zero.
reset_all
mkdir -p "$(theme_dir)"
{
    printf 'red = "#111111"\n'
    head -c 200000 /dev/zero | tr '\0' 'x'
    printf '\nred = "#333333"\n'
} > "$(theme_dir)/colors.toml"
run_capture logibar-status --json
checkeq "a key inside the cap is still read" "#333333" "$(jq -r '.palette.critical' <<< "$OUT")"

# ── end to end: an oversized file still gives the right answer ───────────────

reset_all
{ printf '77\n1\n0\n'; head -c 10485760 /dev/zero | tr '\0' 'x'; } > "$TMP/run/logibar/keyboard"
run_capture logibar-status
checkeq "a 10 MiB state file still answers (rc)" 0 "$RC"
check   "a 10 MiB state file still reads 77%" jq -e '.text | test("77")' <<< "$OUT"
run_capture logibar-keyboard
checkeq "legacy widget: a 10 MiB state file still answers (rc)" 0 "$RC"
check   "legacy widget: a 10 MiB state file still reads 77%" jq -e '.text | test("77%")' <<< "$OUT"
# The fourth line of that file is the model, and here it is a 64 KiB run of
# `x` (the cap, written out by hand: 65536). A model is accepted only up to 48
# printable characters, so the tooltip must carry the generic name and never
# the blob — and the run must stay fast: the regex sees at most 64 characters.
check   "a 10 MiB model line stays out of the tooltip" \
    jq -e '.tooltip | test("G915 TKL") and (test("xxxxxxxx") | not) and (length < 4096)' <<< "$OUT"
run_capture logibar-status
check   "a 10 MiB model line stays out of the combined tooltip" \
    jq -e '.tooltip | test("G915 TKL") and (test("xxxxxxxx") | not) and (length < 4096)' <<< "$OUT"
# Control bytes and invalid UTF-8 in the model line, written out by hand so
# the case is the same on every run: a 0x01 inside a name, a tab, and two
# bytes that are not UTF-8 at all. Each one keeps the generic name.
for _bad in 'G\001Pro' 'G\tPro' '\377\376X'; do
    reset_all
    { printf '77\n1\n0\n'; printf "$_bad"; } > "$TMP/run/logibar/keyboard"
    run_capture logibar-keyboard
    checkeq "control bytes in the model line ($_bad) (rc)" 0 "$RC"
    check   "control bytes in the model line ($_bad) keep the generic name" \
        jq -e '.tooltip | test("G915 TKL") and (test("Pro|X") | not)' <<< "$OUT"
    run_capture logibar-status --json
    checkeq "control bytes in the model line ($_bad), combined (rc)" 0 "$RC"
    check   "control bytes in the model line ($_bad), combined" \
        jq -e '(.devices[] | select(.id == "keyboard") | .name) == "G915 TKL"' <<< "$OUT"
done

reset_all
mkdir -p "$TMP/cache/wal"
{ head -c 10485760 /dev/zero | tr '\0' 'x'; } > "$TMP/cache/wal/colors.json"
run_capture logibar-status --json
checkeq "a 10 MiB pywal cache still answers (rc)" 0 "$RC"
check   "a 10 MiB pywal cache falls back to the defaults" \
    jq -e '.palette.critical == "#e06c75"' <<< "$OUT"

# ── the battery line is never evaluated ──────────────────────────────────────
#
# Bash arithmetic expands the text it is given, and an array subscript runs a
# command substitution: `[[ $x -le 10 ]]` on `a[$(touch f)]` creates f. The
# battery line is text the widgets do not write, so it must be checked before
# any arithmetic sees it. The marker sits inside the sandbox, never in /tmp.

PWNED="$TMP/pwned"
for _hostile in 'a[$(touch '"$PWNED"')]' '$(touch '"$PWNED"')' 'x[`touch '"$PWNED"'`]+1'; do
    for w in "${WIDGETS[@]}"; do
        reset_all
        rm -f "$PWNED"
        for d in "${DEVICES[@]}"; do
            printf '%s\n1\n0\n' "$_hostile" > "$TMP/run/logibar/$d"
        done
        run_capture "$w"
        checkeq "$w: a hostile battery line ($_hostile) (rc)" 0 "$RC"
        check   "$w: a hostile battery line ($_hostile) is valid JSON" jq -e . <<< "$OUT"
        check   "$w: a hostile battery line ($_hostile) runs nothing" test ! -e "$PWNED"
    done
done

# ── the daemons ──────────────────────────────────────────────────────────────
#
# The write side of the same problem: a FIFO planted at the state file's path
# would block `open()` for ever. Both daemons already dodge it by writing to a
# fresh mkstemp name and renaming over the target, which never opens the
# predictable path. These pin that, because a later "simplification" to
# `open(state_file, 'w')` would reintroduce the hang silently.

for d in logibar-hidpp-monitor logibar-headset-monitor; do
    check "$d: writes through mkstemp" grep -q 'tempfile.mkstemp' "$REPO/$d"
    check "$d: publishes by rename" grep -q 'os.rename' "$REPO/$d"
    check "$d: never opens the state file directly" \
        bash -c "! grep -qE \"open\\((STATE_FILE|state_file)\" '$REPO/$d'"
    check "$d: no unbounded read loop" \
        bash -c "! grep -q 'while True:' '$REPO/$d'"
done

check "logibar-hidpp-monitor: flush_buffer is capped" \
    grep -q 'for _ in range(FLUSH_MAX_PACKETS)' "$REPO/logibar-hidpp-monitor"

# ── the panel never reads a state file itself ────────────────────────────────
#
# The QML lives in the shell's process, so anything it opens is opened without
# any of the guards above. One directory watch is allowed as a doorbell; the
# per-file FileViews that used to call reload() are not.

PANEL="$REPO/omarchy/Panel.qml"

# Comments are stripped first, or these match the very paragraph that explains
# why the calls are gone — a test that passes on its own documentation.
panel_code() { sed 's;//.*;;' "$PANEL"; }

checkeq "the panel keeps exactly one FileView" 1 "$(panel_code | grep -c '^\s*FileView {')"
not_in_panel() { ! panel_code | grep -qE "$1"; }

check "the panel never reloads a watched file" not_in_panel 'reload\(\)'
check "the panel never reads a watched file's text" not_in_panel '\.text\(\)|\.data\(\)'
check "the panel does not preload what it watches" grep -q 'preload: false' "$PANEL"
check "the panel caps what it retains from the CLI" grep -q 'maxChars: 1024 \* 1024' "$PANEL"
# UTF-16 units, not bytes: a name or a message that says "bytes" or "KiB" is
# claiming a bound three times tighter than the one the code enforces.
check "the panel does not call its char tripwire a byte cap" \
    bash -c "! grep -qE 'maxBytes|KiB' '$PANEL'"

# ── a click asks the daemon; the shell never scans the hardware ──────────────
#
# The click refresh runs a child from the shell process. It must be bounded in
# time (a hung child left the refresh flag up and every later click dead) and
# must only signal the daemon: a second scanner on the receivers crossed its
# replies with the daemon's and published wrong batteries. A daemon older than
# the plugin ignores `--refresh` and would become that second scanner, so the
# click runs only when the new daemon's pid file exists.

check "the click refresh is bounded by timeout" \
    grep -qF '"timeout", stateDir, "-k", "2", String(refreshTimeoutSeconds)' "$PANEL"
check "the click refresh only asks the daemon" \
    grep -qF '"logibar-hidpp-monitor", "--refresh"]' "$PANEL"
check "the click refresh runs only when the daemon wrote its pid file" \
    grep -qF '[ -s "$1/hidpp-monitor.pid" ] || exit 3; shift; exec "$0" "$@"' "$PANEL"
check "the pid file gate checks the directory the panel watches" \
    grep -qF '"timeout", stateDir,' "$PANEL"
check "the pid file the panel checks is the one the daemon writes" \
    bash -c "grep -qF \"PID_FILE = 'hidpp-monitor.pid'\" '$REPO/logibar-hidpp-monitor' && grep -qF \"'logibar')\" '$REPO/logibar-hidpp-monitor'"
check "the click refresh resets its flag on a failed start too" \
    bash -c "sed 's;//.*;;' '$PANEL' | grep -A8 'id: forceProc' | grep -q 'onRunningChanged'"
check "the default configuration passes no --devices (an older CLI rejects \"other\")" \
    grep -qF 'devs.length === 4 ? [binName, "--json"]' "$PANEL"
check "the panel does not poll the hardware on a timer" not_in_panel 'interval: 300000'
check "--refresh signals the daemon instead of scanning" \
    grep -q 'return refresh_via_daemon()' "$REPO/logibar-hidpp-monitor"

# ── device data renders as plain text ─────────────────────────────────────────
#
# A device name comes from the device. QML Text defaults to AutoText, which
# renders markup as soon as it sees a tag; the name filter is not the only
# guard. Every Text whose text is not a string literal sets PlainText.

text_without_plain() {  # prints FILE:LINE of each data-bearing Text without PlainText
    python3 -I - "$@" <<'PY'
import re, sys
for path in sys.argv[1:]:
    lines = open(path).read().split('\n')
    for i, line in enumerate(lines):
        if not re.search(r'\bText\s*\{', line):
            continue
        depth, block = 0, []
        for tail in lines[i:]:
            depth += tail.count('{') - tail.count('}')
            block.append(tail)
            if depth <= 0:
                break
        body = '\n'.join(block)
        literal = re.search(r'^\s*text:\s*"[^"]*"\s*$', body, re.M)
        if 'Text.PlainText' not in body and not literal:
            print(f"{path}:{i + 1}")
PY
}
_offenders=$(text_without_plain "$PANEL" "$REPO/omarchy/BarWidget.qml")
check "every data-bearing Text in the plugin is PlainText" test -z "$_offenders"

# ── bar face and silent devices are settings ────────────────────────────────
#
# 0.7.0 drew one value per device and kept silent devices by default; both
# changed the bar that users already had. The defaults are the 0.6.1 face and
# the G HUB rule (a device that does not answer goes). Neither default adds a
# CLI flag, so the default plugin also runs on an older CLI.

MANIFEST="$REPO/manifest.json"
check "the manifest offers barFace lowest|icon|devices, default lowest" \
    bash -c "jq -e '.barWidget.schema[] | select(.key == \"barFace\") | .options == [\"lowest\",\"icon\",\"devices\"] and .defaultValue == \"lowest\"' '$MANIFEST' && jq -e '.barWidget.defaults.barFace == \"lowest\"' '$MANIFEST'"
check "the manifest offers showLastKnown, default off" \
    bash -c "jq -e '.barWidget.schema[] | select(.key == \"showLastKnown\") | .defaultValue == false' '$MANIFEST' && jq -e '.barWidget.defaults.showLastKnown == false' '$MANIFEST'"
check "the panel reads barFace with lowest as the fallback" grep -qF 'setting("barFace", "lowest")' "$PANEL"
check "the panel adds --last-known only when the setting is on" \
    grep -qF 'if (showLastKnown) cmd.push("--last-known")' "$PANEL"
check "the bar draws no scan glyph (it shifted the other widgets)" \
    bash -c "! grep -q 'fullRefreshing' '$REPO/omarchy/BarWidget.qml'"
check "the CLI keeps a silent device only with --last-known" \
    grep -qF 'if [[ "$connected" != "1" ]] && $SHOW_LAST_KNOWN; then' "$REPO/logibar-status"

# ── the README installs from upstream only ───────────────────────────────────
#
# An external contribution once pointed every install command at its fork.
# Any repository named logibar, any clone and any plugin add must be this one.

README="$REPO/README.md"
no_foreign_logibar() { ! grep -oE 'github\.com/[A-Za-z0-9_.-]+/logibar' "$README" | grep -qv '^github\.com/mryll/logibar$'; }
no_foreign_install() { ! grep -E '(git clone|omarchy plugin add) ' "$README" | grep -qv 'https://github\.com/mryll/logibar\.git'; }
check "the README names no logibar repository but mryll/logibar" no_foreign_logibar
check "every clone and plugin add in the README is mryll/logibar" no_foreign_install

# ── summary ──────────────────────────────────────────────────────────────────

echo
echo "passed: $PASS, failed: $FAIL"
[[ $FAIL -eq 0 ]]
