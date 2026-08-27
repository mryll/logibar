#!/usr/bin/env bash
# Static contract of the not-installed discrimination and the copy-install
# button in the panel. Every expectation is written out by hand.
set -euo pipefail
cd "$(dirname "$0")/.."
panel=omarchy/Panel.qml

pass=0
check() {
    local desc=$1; shift
    if "$@" >/dev/null 2>&1; then
        echo "  ok   $desc"; pass=$((pass + 1))
    else
        echo "  FAIL $desc"; exit 1
    fi
}

check "startRun resets sawExit"            grep -qF 'sawExit = false' "$panel"
check "onExited sets sawExit"              grep -qF 'root.sawExit = true' "$panel"
check "startRun resets tripwireFired"     grep -qF 'tripwireFired = false' "$panel"
check "the empty branch gates on the per-run tripwire flag, not stale text" \
                                           grep -qF 'if (tripwireFired) {' "$panel"
check "not-installed gated on !sawExit"    grep -qF '} else if (!sawExit) {' "$panel"
check "a run that exited empty never claims not-installed" \
                                           grep -qF 'produced no output (exit ' "$panel"
check "installCmd literal appears exactly once" \
    test "$(grep -cF 'yay -S logibar' "$panel")" = 1
check "wl-copy goes through execArgv argv-style" \
    grep -qF 'Util.execArgv(["wl-copy", root.installCmd])' "$panel"
check "no shell line is built around wl-copy" \
    bash -c '! grep -qF "bash -c" "$0"' "$panel"
check "button gates on notInstalled, not on error text" \
    grep -qF 'visible: root.notInstalled' "$panel"

echo "test_copy_button: $pass passed"
