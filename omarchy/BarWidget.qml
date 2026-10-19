import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// Omarchy shell bar widget for logibar. Thin host following the first-party
// weather plugin's pattern: the button lives here, all data (logibar-status
// --json), the state-file watchers, and the detail panel live in Panel.qml
// (loaded once, shared identity for the bar's popout coordinator).
//
// Bar face: current readings and individually dimmed recent last-known values.
// A charging glyph appears only while a device responds.
BarWidget {
  id: root
  moduleName: "mryll.logibar"

  readonly property var panelItem: panelLoader.item
  readonly property string faceGlyph: panelItem ? panelItem.faceGlyph : ""
  readonly property string faceLabel: panelItem ? panelItem.faceLabel : ""
  readonly property string faceText: faceLabel !== "" ? faceLabel : faceGlyph

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  function refresh() {
    if (panelItem && panelItem.requestFullRefresh) panelItem.requestFullRefresh()
  }

  function togglePanel() {
    if (panelItem && panelItem.toggle) panelItem.toggle()
  }

  // Shape contract for shell summon/hide/toggle routing: Bar.findPanelWidget
  // requires open/close/opened on the bar-widget root.
  readonly property bool opened: panelItem ? panelItem.opened === true : false

  function open() {
    if (panelItem && panelItem.open) panelItem.open()
  }

  function close() {
    if (panelItem && panelItem.close) panelItem.close()
  }

  // Forwarded so this widget can stand in for the panel as the bar's popout
  // identity (Bar.requestPopout prefers closeForPopoutSwitch over close).
  readonly property bool popoutSwitchClosing: panelItem ? panelItem.popoutSwitchClosing === true : false

  function closeForPopoutSwitch() {
    if (panelItem) panelItem.closeForPopoutSwitch()
  }

  visible: faceText !== ""
  // How wide the bar's open-panel underline should be. Without this hint the bar
  // falls back to 55% of the SLOT, which reads as a dot under a narrow widget
  // but as a bar that visibly stops short under a wide one. The painted content
  // is the honest extent, so the mark tracks what the widget draws instead of a
  // fraction of the box it happens to sit in. (Same hint the first-party clock
  // gives; it passes its label width.)
  readonly property real openPanelIndicatorWidth: button.vertical ? button.labelWidth : faceRow.implicitWidth

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: vertical ? root.faceGlyph : ""
    labelVisible: vertical
    hasVisualContent: root.faceText !== ""
    fixedWidth: vertical ? -1 : Math.max(12, faceRow.implicitWidth + scaledHorizontalMargin * 2)
    foreground: root.panelItem ? root.panelItem.barColor
      : (root.bar ? root.bar.barForeground : Commons.Color.foreground)
    // Dim the icon-only face when every value is last known. In a horizontal
    // bar each device has its own opacity. A failed poll behind good data
    // used to drop the whole face to 45% opacity, which restated the error in
    // the same channel the battery ramp already uses — the percentage read as a
    // different color depending on whether the last read succeeded, and it
    // disagreed with the panel, which kept painting the true ramp color. The
    // error is reported in the panel.
    dimmed: root.panelItem ? root.panelItem.hasData !== true
      || (vertical && root.panelItem.connectedCount === 0) : true
    // Quick summary on hover; the panel is the detail view.
    tooltipText: root.panelItem ? root.panelItem.tooltipText : ""

    onPressed: function(b) {
      if (b === Qt.MiddleButton) root.refresh()
      else if (b !== Qt.RightButton) {
        root.togglePanel()
        root.refresh()
      }
    }

    Row {
      id: faceRow
      visible: !button.vertical
      anchors.centerIn: parent
      spacing: Style.space(10)

      Repeater {
        model: root.panelItem ? root.panelItem.faceEntries : []
        Text {
          required property var modelData
          text: modelData.text
          textFormat: Text.PlainText
          color: modelData.lastKnown
            ? (root.bar ? root.bar.barForeground : Commons.Color.foreground)
            : root.panelItem.barRamp(modelData.battery)
          opacity: modelData.lastKnown ? 0.45 : 1
          font.family: button.fontFamily
          font.pixelSize: button.fontSize
          renderType: Text.NativeRendering
        }
      }

      Text {
        visible: root.panelItem && root.panelItem.fullRefreshing
        text: "󰑐"
        color: root.bar ? root.bar.barForeground : Commons.Color.foreground
        font.family: button.fontFamily
        font.pixelSize: button.fontSize
        renderType: Text.NativeRendering
      }
    }
  }
}
