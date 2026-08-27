pragma ComponentBehavior: Bound
import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Logitech battery panel. Owns the `logibar-status --json` data (one source
// of truth for thresholds and aggregation, shared with the waybar module) and
// renders one section per connected device: identity glyph, name, an animated
// battery meter, bold percent with a charging glyph, and a muted "updated"
// time from the daemon's last state-file write.
//
// Each meter's track carries a SPATIAL gradient fixed to the full 0–100%
// scale — urgent at the 0% origin, blending through the warning anchor at
// 20% to the plain foreground at 100% — and the fill clips it at the current
// level: a healthy battery shows mostly-normal colors with the red segment
// only at its origin, a critical one reveals only the red start. Percent
// labels, hero glyph, and the bar face are tinted by the JSON's severity
// `state` (the semantic source of truth), never by a value-following ramp.
//
// Updates stay event-driven: one watch on the state DIRECTORY only rings a
// doorbell that re-runs logibar-status (plus a 30s safety net). Nothing in
// this file ever reads a state file — the CLI is the only reader, and it is
// the only side with a bounded, regular-file-only read. Nothing
// animates while idle, and every color initializes at its final value — a
// mid-flight Behavior interrupted by the bar's startup animation gate freezes
// otherwise (learned the hard way).
Panel {
  id: root
  moduleName: "mryll.logibar"
  ipcTarget: "mryll.logibar"
  manageIpc: false

  property var anchorItem: null

  // The bar tracks the widget mounted in its slot — BarWidget.qml — not this
  // nested panel, so everything the bar identifies a panel by must be that
  // widget (popout coordinator, switchPanelFrom).
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  // The panel draws on the POPUP CARD, so it takes the popup surface's text
  // token — not the bar's. bar.foreground is chosen against the bar, which on a
  // transparent bar means "against the wallpaper"; that is the wrong contrast
  // reference for a card, and a theme that defines popups.text separately would
  // be ignored outright. (printbar already did this; the rest of the family now
  // agrees.)
  readonly property color foreground: Color.popups.text
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color track: Style.selectedFillFor(foreground, Color.accent, urgent)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property bool vertical: bar ? bar.vertical : false

  readonly property string binName: "logibar-status"

  readonly property string stateDir: {
    var dir = Quickshell.env("XDG_RUNTIME_DIR")
    return (dir && dir.length > 0) ? dir + "/logibar" : ""
  }

  // Settings map to the CLI's --devices filter; the aggregate is computed by
  // the CLI over the filtered set, never re-derived here.
  readonly property var enabledDevices: {
    var list = []
    if (setting("showKeyboard", true) === true) list.push("keyboard")
    if (setting("showMouse", true) === true) list.push("mouse")
    if (setting("showHeadset", true) === true) list.push("headset")
    return list
  }

  // ---------------------------------------------------------------- data

  // Last good parse, kept on any failure so the bar never flashes empty.
  property var status: null
  readonly property bool hasData: status !== null
  property string loadError: ""

  readonly property var aggregate: status && status.aggregate ? status.aggregate : null
  readonly property var deviceRows: {
    if (!status || !status.devices) return []
    var out = []
    for (var i = 0; i < status.devices.length; i++) {
      var d = status.devices[i]
      if (d && d.connected === true && typeof d.battery === "number") out.push(d)
    }
    return out
  }
  readonly property int connectedCount: aggregate ? (aggregate.connected_count || 0) : 0

  // Identity glyphs (same Nerd Font icons as the waybar renderers) and the
  // stepped battery glyphs, decile-mapped like the first-party power widget.
  readonly property var identityGlyphs: ({ keyboard: "󰌌", mouse: "󰍽", headset: "󰋎" })
  readonly property var levelGlyphs: ["󰁺", "󰁻", "󰁼", "󰁽", "󰁾", "󰁿", "󰂀", "󰂁", "󰂂", "󰁹"]
  readonly property var chargingGlyphs: ["󰢜", "󰂆", "󰂇", "󰂈", "󰢝", "󰂉", "󰢞", "󰂊", "󰂋", "󰂅"]

  function batteryGlyph(pct, charging) {
    var i = Math.max(0, Math.min(9, Math.floor(pct / 10)))
    return charging ? chargingGlyphs[i] : levelGlyphs[i]
  }

  // ---------------------------------------------------------------- colors

  function mix(a, b, t) {
    return Qt.rgba(a.r + (b.r - a.r) * t, a.g + (b.g - a.g) * t,
                   a.b + (b.b - a.b) * t, a.a + (b.a - a.a) * t)
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)) }
  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

  // Monochrome mode, mirroring the CLI's --no-color for a frontend that has no
  // flags: "full" (default), "none", "bar-only", "panel-only". A plain surface
  // draws in foreground tones only — no ramp, no accent, no urgent — while
  // keeping glyphs, meters and layout. Severity stays readable as text and
  // glyphs, and the JSON `state` field still carries it for anything scripted.
  // An unrecognized value normalizes to "full": a hand-edited shell.json must
  // not be able to silently take the color off both surfaces.
  readonly property string colorMode: {
    var v = String(setting("colorMode", "full"))
    return ["full", "none", "bar-only", "panel-only"].indexOf(v) >= 0 ? v : "full"
  }
  readonly property bool barColored:   colorMode === "full" || colorMode === "bar-only"
  readonly property bool panelColored: colorMode === "full" || colorMode === "panel-only"

  // Surface-aware wrappers around the ramp. Kept separate so "bar-only" and
  // "panel-only" can disagree about the very same percentage.
  function panelRamp(pct) { return panelColored ? scaleColor(pct) : foreground }
  function barRamp(pct) {
    var base = bar ? bar.barForeground : Color.foreground
    return barColored ? legibleOnBar(scaleColor(pct)) : base
  }

  // Severity → color from the JSON's `state` fields (the CLI owns the
  // thresholds): urgent at critical, dimmed foreground at warning, base
  // otherwise — same mapping the widget used before the meters existed.
  function stateColor(state, base, hot) {
    if (state === "critical") return hot
    if (state === "warning") return Qt.darker(base, 1.4)
    return base
  }

  // Hero brand mark: the widget's IDENTITY, unconditionally. It never doubles as
  // a severity indicator — a fault is already carried by the meters, the status
  // pills, the status label and the error card, and letting the identity mark
  // move too means the panel has no fixed point a reader can recognise. Pure
  // theme accent (foreground when the panel is monochrome).
  readonly property color brandColor: panelColored ? Color.accent : foreground

  // ---------------------------------------------------------------- meter ramp

  // Fuel-gauge anchors come from the CLI's `palette`: logibar-status resolves
  // them from the active theme's own semantic colors (red/yellow/green, with
  // --color-* overrides applied), so both frontends paint the same gauge and
  // the tones stay inside the theme's palette instead of a derived ramp that
  // shouts over it. The shell's Color singleton only maps red → urgent, which
  // is why the green and amber have to travel with the data.
  function paletteColor(key, fallback) {
    var hex = status && status.palette ? status.palette[key] : ""
    return /^#[0-9a-fA-F]{6}$/.test(String(hex)) ? Qt.color(String(hex)) : fallback
  }

  // Why this panel consumes the palette at all: the shell's Color singleton
  // exposes foreground, background, accent, urgent and muted — there is no
  // green and no yellow in it, so a red→amber→green battery gauge cannot know
  // what "green at 100%" is unless logibar-status tells it.
  //
  // The converse also holds: where the shell DOES own a color, the shell wins.
  // `urgent` is transparency-aware and animates on theme change, so the bad end
  // of the ramp is the shell token, never the CLI's hex for the same role.
  readonly property color gaugeCritical: urgent
  readonly property color gaugeWarning: paletteColor("warning", mix(foreground, urgent, 0.5))
  readonly property color gaugeOk: paletteColor("ok", foreground)

  // The WHOLE ramp — the colors and the percentages they sit at — is published
  // by logibar-status, which owns the thresholds. Nothing here re-derives one:
  // moving CRITICAL_LEVEL or WARNING_LEVEL in the CLI moves this panel too.
  // Entries are validated (finite pct, #rrggbb color) and assumed ordered by
  // pct, which is how the CLI emits them.
  readonly property var rampStops: {
    var out = []
    var pal = status && status.palette ? status.palette : null
    var raw = pal ? pal.stops : null
    // Wherever a stop carries the CLI's critical color, paint the shell's
    // urgent token instead — same role, but transparency-aware and animated.
    var criticalHex = pal && pal.critical ? String(pal.critical).toLowerCase() : ""
    if (raw && raw.length > 0) {
      for (var i = 0; i < raw.length; i++) {
        var p = Number(raw[i] ? raw[i].pct : NaN)
        var c = String(raw[i] ? raw[i].color : "")
        if (isFinite(p) && /^#[0-9a-fA-F]{6}$/.test(c))
          out.push({ pct: clamp(p, 0, 100),
                     color: c.toLowerCase() === criticalHex ? gaugeCritical : Qt.color(c) })
      }
    }
    if (out.length === 0) {
      // Pre-`stops` logibar-status: the ramp this widget shipped with.
      out = [{ pct: 0, color: gaugeCritical }, { pct: 10, color: gaugeCritical },
             { pct: 20, color: gaugeWarning }, { pct: 100, color: gaugeOk }]
    }
    return out
  }

  // One stop by index, clamped to the last — see the meter's fixed slots.
  function stopAt(i) {
    var s = rampStops
    return s[Math.max(0, Math.min(i, s.length - 1))]
  }

  // Color of the battery SCALE at a given percentage — red at empty, green at
  // full, the universal gauge idiom (inverted versus the usage meters in the
  // sibling widgets, where 100% is the bad end). Piecewise-linear over the
  // published stops, so the shape of the ramp is the CLI's decision.
  function scaleColor(pct) {
    var p = clamp(Number(pct) || 0, 0, 100)
    var s = rampStops
    if (p <= s[0].pct) return s[0].color
    for (var i = 0; i < s.length - 1; i++) {
      var a = s[i]
      var b = s[i + 1]
      if (p <= b.pct) {
        var span = b.pct - a.pct
        return span > 0 ? mix(a.color, b.color, (p - a.pct) / span) : b.color
      }
    }
    return s[s.length - 1].color
  }

  // The gradient spans the FILL, not the track, so each scale anchor's stop
  // is rescaled by the level currently painted; anchors past the tip collapse
  // onto 1.0 carrying the tip's color. That keeps the ramp pinned to the
  // scale — a given battery percentage always reads the same color, whatever
  // the level — while the fill keeps its rounded tip (no clipping container).
  // WCAG relative luminance / contrast ratio — same helpers as the printbar
  // sibling, used as a legibility floor for the bar face.
  function relLuminance(c) {
    function chan(v) { return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
    return 0.2126 * chan(c.r) + 0.7152 * chan(c.g) + 0.0722 * chan(c.b)
  }

  function contrastRatio(a, b) {
    var la = relLuminance(a)
    var lb = relLuminance(b)
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
  }

  // The bar face carries the ramp color at the percentage it displays, so the
  // same number reads the same way on the bar and in the panel. barForeground
  // is the only value the shell runs through its wallpaper-contrast machinery
  // (omarchy-bar-text-color on transparent bars), so a pale ramp tone is
  // blended toward it — hue preserved — only as far as it takes to clear
  // 4.5:1 against the bar background. Normally a no-op: the palette tones
  // clear the ratio on their own.
  function legibleOnBar(c) {
    var bg = bar ? bar.background : Color.bar.background
    var safe = bar ? bar.barForeground : Color.foreground
    var out = c
    for (var i = 0; i < 12 && contrastRatio(out, bg) < 4.5; i++)
      out = mix(out, safe, 0.2)
    return out
  }

  function rampStop(anchorPct, levelPct) {
    return levelPct > 0 ? Math.min(1, anchorPct / levelPct) : 0
  }

  // Through panelRamp, so a monochrome panel resolves every stop to the
  // foreground and the meter fills solid instead of ramped.
  function rampColor(anchorPct, levelPct) {
    return panelRamp(Math.min(anchorPct, levelPct))
  }

  // ---------------------------------------------------------------- bar face

  // Face keeps last-known-good data (dimmed behind an error); "⚠" only when
  // there has never been data — an error must not look like "disconnected".
  readonly property string faceGlyph: {
    if (!hasData) return loadError !== "" ? "⚠" : ""
    if (!aggregate || connectedCount === 0 || aggregate.worst_battery === null) return ""
    return batteryGlyph(aggregate.worst_battery, aggregate.any_charging === true)
  }

  readonly property string faceLabel: {
    if (vertical || !hasData || !aggregate || connectedCount === 0) return ""
    if (aggregate.worst_battery === null) return ""
    return aggregate.worst_battery + "%"
  }

  // Glyph and number share this one color, so the bar element reads as a
  // single unit — and it is the ramp color at the very percentage shown.
  readonly property color barColor: {
    var base = bar ? bar.barForeground : Color.foreground
    if (!hasData || !aggregate || aggregate.worst_battery === null) return base
    return barRamp(aggregate.worst_battery)
  }

  readonly property bool degraded: !hasData || loadError !== ""

  // Hover tooltip: quick summary only — the panel is the detail view.
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  }

  readonly property string tooltipText: {
    if (!hasData) return loadError !== "" ? "<span>" + esc(loadError) + "</span>" : ""
    var lines = []
    for (var i = 0; i < deviceRows.length; i++) {
      var d = deviceRows[i]
      lines.push((identityGlyphs[d.id] || "") + " " + d.battery + "%"
        + (d.charging === true ? " · Charging" : ""))
    }
    return lines.length > 0 ? "<span>" + lines.join("<br/>") + "</span>" : ""
  }

  // ---------------------------------------------------------------- helpers

  function updatedText(iso) {
    if (!iso) return ""
    var t = new Date(String(iso))
    if (isNaN(t.getTime())) return ""
    return Qt.formatTime(t, "HH:mm")
  }

  // ---------------------------------------------------------------- process

  // Run state machine: collector and exit signal race, so a run finalizes
  // only once both report (timer fallback for failed starts). A refresh
  // requested mid-run is queued — last command wins — instead of dropped.
  property bool collectorDone: true
  property bool processDone: true
  property string capturedText: ""
  property int exitCode: 0
  property var pendingCmd: null

  // True when this run's collector refused oversize output. Its message
  // must survive finalizeRun; a stale error from a previous run must not.
  property bool tripwireFired: false

  // True when onExited fired for the current run. A missing command emits
  // no exited. This separates "could not start" from "ran, no output".
  property bool sawExit: false

  // True only when the run could not START. Gates the copy button.
  // Operational errors never set it.
  property bool notInstalled: false

  // One constant, two users: the error message shows it and the copy
  // button copies it.
  readonly property string installCmd: "yay -S logibar"

  // The copy button shows a check for a moment.
  property bool installCopied: false
  Timer {
    id: copiedReset
    interval: 1500
    onTriggered: root.installCopied = false
  }

  // The shell's base handler covers open/close/show/hide/toggle; this one adds
  // `refresh` so a keybind or a script can force a fetch without opening the
  // panel. Overriding means restating the five, so `manageIpc: false` above
  // turns the base one off and this is the only handler on the target.
  IpcHandler {
    target: root.ipcTarget

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void { root.refresh() }
  }

  function refresh() {
    // The host injects `settings` during construction, before bindings are
    // initialized — enabledDevices can be transiently undefined here.
    var devs = enabledDevices
    if (devs === undefined || devs === null) return
    if (devs.length === 0) {
      status = null
      loadError = ""
      return
    }
    startRun([binName, "--json", "--devices", devs.join(",")])
  }

  function startRun(cmd) {
    if (statusProc.running) { pendingCmd = cmd; return }
    collectorDone = false
    processDone = false
    capturedText = ""
    sawExit = false
    tripwireFired = false
    exitCode = 0
    statusProc.command = cmd
    statusProc.running = true
  }

  function maybeFinalize() {
    if (!collectorDone || !processDone) return
    exitFallback.stop()
    finalizeRun()
  }

  function setError(message) { loadError = String(message) }

  function finalizeRun() {
    notInstalled = false
    var text = capturedText.trim()
    if (text === "") {
      // Empty output has three causes. (1) The tripwire already set an
      // error: keep it. (2) No exited = failed start: report not-installed.
      // (3) The process ran and printed nothing: an operational error,
      // never "not installed".
      if (tripwireFired) {
        // Already explained by this run's tripwire.
      } else if (!sawExit) {
        notInstalled = true
        setError(binName + " could not start — not installed or not on PATH?\n\n"
                 + "Install it with:  " + installCmd + "\n"
                 + "Then open this panel again.")
      } else {
        setError(binName + " produced no output (exit " + exitCode + ")")
      }
    } else {
      handle(text)
    }
    if (pendingCmd) {
      var c = pendingCmd
      pendingCmd = null
      Qt.callLater(function() { root.startRun(c) })
    }
  }

  // Keeps last-known-good on ANY failure; a structured error document wins
  // over a generic exit-code message. Never a silent swallow.
  function handle(out) {
    var d = null
    try { d = JSON.parse(out) } catch (e) { d = null }
    if (d === null || typeof d !== "object") {
      setError(exitCode !== 0
        ? binName + " failed (exit " + exitCode + ")"
        : binName + " returned malformed output")
      return
    }
    if (d.error && d.error.message) { setError(String(d.error.message)); return }
    if (!d.aggregate || !d.devices) { setError(binName + " returned an unexpected document"); return }
    status = d
    loadError = ""
  }

  // ---------------------------------------------------------------- open sweep

  // On every panel open the meters sweep 0 → level in one 200ms pass. The
  // sweep is its own NumberAnimation on a shared fraction — never a Behavior
  // retargeted from a construction-time zero (the frozen-Behavior lesson):
  // openProgress initializes at its final value (1), openSweeping disables
  // the meters' width Behaviors BEFORE the jump to 0, and onFinished (not
  // onStopped, which restart() would fire spuriously) re-enables them once
  // the fraction is back at 1. Only the fill width moves: the meters' stops
  // are recomputed from the fraction painted each frame, so the growing fill
  // uncovers a fixed scale rather than stretching a compressed copy of it.
  property real openProgress: 1
  property bool openSweeping: false

  function startOpenSweep() {
    openSweeping = true
    openProgress = 0
    openSweep.restart()
  }

  NumberAnimation {
    id: openSweep
    target: root
    property: "openProgress"
    from: 0
    to: 1
    duration: 200
    easing.type: Easing.OutCubic
    onFinished: root.openSweeping = false
  }

  onSettingsChanged: refresh()
  onOpenedChanged: if (opened) {
    refresh()
    startOpenSweep()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Process {
    id: statusProc
    // A command that does not exist gives NEITHER `started` NOR `exited` —
    // Quickshell just drops `running` back to false. That is the only signal a
    // failed start emits, and without this handler the panel sits on its
    // loading text for ever: maybeFinalize() waits on processDone, which
    // nothing would ever set. This IS the first run of anyone who installed
    // the plugin from the marketplace and does not have the CLI yet.
    onRunningChanged: {
      if (running) return
      root.processDone = true
      exitFallback.restart()
      root.maybeFinalize()
    }
    onExited: function(code) {
      root.sawExit = true
      root.exitCode = code
      root.processDone = true
      exitFallback.restart()   // failed-start case: collector may never fire
      root.maybeFinalize()
    }
    stdout: StdioCollector {
      waitForEnd: true
      // A tripwire, not a limit, and it counts UTF-16 units rather than bytes —
      // QML's String.length has no byte view. A megabyte of units is up to
      // three megabytes of UTF-8, which is still far outside anything the CLI
      // can produce now that every file and every response it reads is capped.
      // The real bound is there; this only refuses to RETAIN an answer that
      // could not have come from a healthy run. StdioCollector has already
      // buffered the whole stream by the time this runs, so it cannot cap the
      // peak memory either — what it saves is parsing megabytes of unknown
      // text into the long-lived shell process.
      readonly property int maxChars: 1024 * 1024
      onStreamFinished: {
        if (text.length > maxChars) {
          root.tripwireFired = true
          root.capturedText = ""
          root.setError(root.binName + " returned more than " + maxChars + " characters — refusing it")
        } else {
          root.capturedText = text
        }
        root.collectorDone = true
        root.maybeFinalize()
      }
    }
  }

  Timer {
    id: exitFallback
    interval: 300
    repeat: false
    onTriggered: { root.collectorDone = true; root.maybeFinalize() }
  }

  // ---------------------------------------------------------------- triggers

  // ONE watch, on the DIRECTORY, and it is a doorbell — never a reader.
  //
  // This used to be a directory watch plus one FileView per state file, and
  // those per-file views called reload() on every change. That pulled the
  // daemons' files INTO the shell process: a FileView opens and loads before
  // any QML of ours can look at what it opened, so a same-user process that
  // swaps `$XDG_RUNTIME_DIR/logibar/mouse` for a FIFO stalls the shell, and
  // one that swaps it for a huge file makes the shell hold it in memory. The
  // bounded-read guard in logibar-status cannot help here: it protects the
  // CLI's own reads, and the FileView never went through the CLI.
  //
  // Worse, the load was pure waste — nothing here ever read `.text()`. The
  // handlers only called refresh(), which runs the CLI, which is the one
  // thing on this path that vets a path before reading it.
  //
  // Per-file watches are also redundant: both daemons publish state the same
  // way, mkstemp + rename into this directory, so every single update is a
  // directory-entry event this watch already sees. A directory has no content
  // to load — `preload: false` skips even the attempt, and neither `.text()`
  // nor `.data()` nor `reload()` is called anywhere in this file.
  //
  // The 30s timer below stays the safety net (a daemon started after the
  // shell, when even the directory is missing at load), and the panel also
  // refreshes on open, so a missed doorbell is bounded and invisible.
  FileView {
    path: root.stateDir
    watchChanges: true
    preload: false
    printErrors: false
    onFileChanged: root.refresh()
  }

  Timer {
    interval: 30000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // ---------------------------------------------------------------- panel

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(300))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(480))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onMoveRequested: function(dx, dy) {
        if (dy !== 0)
          panelFlick.contentY = root.clamp(panelFlick.contentY + dy * Style.space(56), 0,
                                           Math.max(0, panelFlick.contentHeight - panelFlick.height))
      }
      onActivateRequested: root.refresh()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) { if (t === "r" || t === "R") root.refresh() }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          // ---------- Hero: aggregate glyph · connected count ----------
          PanelHero {
            width: parent.width
            title: "Logitech"
            meta: root.connectedCount > 0
              ? root.connectedCount + (root.connectedCount === 1 ? " device" : " devices")
              : ""
            foreground: root.foreground
            fontFamily: root.fontFamily

            iconComponent: Component {
              Text {
                text: root.faceGlyph !== "" && root.faceGlyph !== "⚠"
                  ? root.faceGlyph : "󰌌"
                textFormat: Text.PlainText
                color: root.brandColor
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
          }

          // ---------- Empty / error state ----------
          Text {
            visible: root.deviceRows.length === 0
            width: parent.width
            topPadding: Style.space(16)
            text: root.loadError !== "" ? root.loadError : "No devices connected."
            textFormat: Text.PlainText
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          // Copies installCmd as one argv element: no shell line, no
          // trailing newline. Gated on notInstalled, never on error text.
          PanelActionButton {
            visible: root.notInstalled
            anchors.horizontalCenter: parent.horizontalCenter
            iconText: root.installCopied ? "󰄬" : "󰆏"
            tooltipText: root.installCopied ? "Copied" : "Copy install command"
            foreground: root.dim
            hoverColor: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.caption
            size: Style.space(20)
            onClicked: {
              Util.execArgv(["wl-copy", root.installCmd])
              root.installCopied = true
              copiedReset.restart()
            }
          }

          // ---------- Devices ----------
          PanelSeparator {
            visible: root.deviceRows.length > 0
            foreground: root.foreground
          }

          Column {
            visible: root.deviceRows.length > 0
            width: parent.width
            spacing: Style.space(14)

            PanelSectionHeader {
              width: parent.width
              text: "DEVICES"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Repeater {
              model: root.deviceRows

              DeviceSection {
                required property var modelData
                width: parent.width
                device: modelData
              }
            }
          }

          // ---------- Error behind stale data ----------
          PanelSeparator {
            visible: root.hasData && root.loadError !== ""
            foreground: root.foreground
          }

          Text {
            visible: root.hasData && root.loadError !== ""
            width: parent.width
            text: root.hasData ? root.loadError : ""
            textFormat: Text.PlainText
            color: root.panelColored ? root.mix(root.dim, root.urgent, 0.5) : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

        }
      }
    }
  }

  // One device: identity glyph + name, bold percent with a charging glyph,
  // animated battery meter in the continuous level gradient, and a caption
  // row with the charging state and the daemon's last write time.
  component DeviceSection: Column {
    id: section
    property var device: null

    readonly property int pct: device ? Math.round(Number(device.battery || 0)) : 0
    readonly property bool charging: device ? device.charging === true : false

    // What the meter is painting this frame, rounded exactly like the target
    // so the last frame of a sweep lands on the real figure with no jump.
    // While nothing is animating this simply equals `pct`.
    readonly property int displayPct: Math.round(meter.shownPct)

    // The FIGURE counts, but its color states the truth about the device.
    //
    // Tinting by the counting value was tried and rejected: this gauge is
    // inverted (urgent at 0%, healthy at 100%), so a figure sweeping up from
    // zero starts inside the critical band. Slowed to 10x it plainly reads as
    // an alarm — a 96% keyboard showing "7%" in urgent red. At the shipped
    // 200ms that lasts about 8ms, under one frame at 60Hz, but it is a false
    // reading in the frames where it lands, so the color stays on `pct`.
    //
    // The meter underneath is unaffected: its red origin is a position on a
    // fixed scale, not a claim about this device, so it stays pinned as built.
    readonly property color rampTint: root.panelRamp(pct)

    spacing: Style.space(5)

    Item {
      width: parent.width
      implicitHeight: Math.max(nameRow.implicitHeight, valueLabel.implicitHeight)

      Row {
        id: nameRow
        spacing: Style.spacing.sm
        anchors.left: parent.left
        anchors.right: valueLabel.left
        anchors.rightMargin: Style.spacing.sm
        anchors.verticalCenter: parent.verticalCenter

        Text {
          text: section.device ? (root.identityGlyphs[section.device.id] || "") : ""
          textFormat: Text.PlainText
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.icon
          verticalAlignment: Text.AlignVCenter
        }

        Text {
          text: section.device ? String(section.device.name || section.device.id) : ""
          textFormat: Text.PlainText
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
          verticalAlignment: Text.AlignVCenter
        }
      }

      Text {
        id: valueLabel
        text: (section.charging ? root.batteryGlyph(section.pct, true) + " " : "")
          + section.displayPct + "%"
        textFormat: Text.PlainText
        color: section.rampTint
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    Meter {
      id: meter
      width: parent.width
      value: section.pct / 100
    }

    Item {
      width: parent.width
      implicitHeight: Math.max(statusLabel.implicitHeight, updatedLabel.implicitHeight)

      Text {
        id: statusLabel
        text: section.charging ? "Charging" : "On battery"
        textFormat: Text.PlainText
        color: section.charging ? root.mix(root.dim, root.foreground, 0.6) : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        id: updatedLabel
        // logibar has no single footer — each device reports its own reading —
        // but the line is written in the same house shape the other widgets
        // use in theirs: clock glyph, "Updated HH:MM".
        text: {
          var at = root.updatedText(section.device ? section.device.updated_at : "")
          return at !== "" ? "󰅐  Updated " + at : ""
        }
        textFormat: Text.PlainText
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
      }
    }
  }

  // Rounded track with a rounded fill that carries the battery ramp. The
  // gradient spans the FILL (no clipping container, so the tip keeps the
  // track's radius) and every stop is rescaled by the fraction actually
  // painted this frame — read off the ANIMATED width, not the static level.
  // That is what keeps the ramp pinned to the scale: 83% shows mostly-normal
  // color with red only at its origin, 8% shows just the red start, and the
  // open sweep uncovers a fixed scale instead of stretching a compressed copy
  // of it across the 200ms.
  //
  // Only the width animates: the 160ms Behavior covers data-driven changes and
  // is disabled while the panel's open sweep drives the shared fraction, so
  // the two never fight. No color animation exists to freeze mid-flight.
  component Meter: Item {
    id: meter
    property real value: 0
    // The percentage this meter is painting right now, straight off the
    // animated fill. The gradient stops already ride this; the figure beside
    // the meter rides it too, so the two cannot drift apart.
    readonly property real shownPct: meterFill.shownPct
    property real thickness: Math.max(Style.space(4), Math.round(Style.spacing.controlHeight * 0.14))

    implicitHeight: thickness

    Rectangle {
      id: meterTrack
      anchors.fill: parent
      radius: height / 2
      color: root.track
    }

    Rectangle {
      id: meterFill
      anchors.left: meterTrack.left
      anchors.verticalCenter: meterTrack.verticalCenter
      height: meterTrack.height
      radius: meterTrack.radius
      width: meterTrack.width * root.clamp(meter.value, 0, 1) * root.openProgress

      // Battery percentage currently painted. Derived from the animated width
      // so the stops recompute every frame of the sweep (and of any data
      // transition); rampStop() guards the zero-width frame at sweep start.
      readonly property real shownPct: meterTrack.width > 0
        ? width / meterTrack.width * 100 : 0

      Behavior on width {
        enabled: !root.openSweeping
        NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
      }

      // Fixed slots fed from the published stops. Building GradientStop
      // objects per frame would churn QML objects throughout the sweep, so
      // there are six: any surplus slot collapses onto the last stop —
      // same position, same color — which paints identically. Six covers the
      // CLI's four with room to grow.
      gradient: Gradient {
        orientation: Gradient.Horizontal
        GradientStop {
          position: root.rampStop(root.stopAt(0).pct, meterFill.shownPct)
          color: root.rampColor(root.stopAt(0).pct, meterFill.shownPct)
        }
        GradientStop {
          position: root.rampStop(root.stopAt(1).pct, meterFill.shownPct)
          color: root.rampColor(root.stopAt(1).pct, meterFill.shownPct)
        }
        GradientStop {
          position: root.rampStop(root.stopAt(2).pct, meterFill.shownPct)
          color: root.rampColor(root.stopAt(2).pct, meterFill.shownPct)
        }
        GradientStop {
          position: root.rampStop(root.stopAt(3).pct, meterFill.shownPct)
          color: root.rampColor(root.stopAt(3).pct, meterFill.shownPct)
        }
        GradientStop {
          position: root.rampStop(root.stopAt(4).pct, meterFill.shownPct)
          color: root.rampColor(root.stopAt(4).pct, meterFill.shownPct)
        }
        GradientStop {
          position: root.rampStop(root.stopAt(5).pct, meterFill.shownPct)
          color: root.rampColor(root.stopAt(5).pct, meterFill.shownPct)
        }
      }
    }
  }
}
