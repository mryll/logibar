#!/usr/bin/env python3
"""In-memory emulation of the hidraw layer for logibar tests.

What it reproduces from the real stack:
- hidapi's surface: ``enumerate(vid, pid)``, ``device()``, ``open_path``,
  ``write``, ``read(size, timeout_ms)``, ``close``.
- hidraw semantics: every open descriptor on a node has its OWN queue, and
  every input report is copied to ALL open descriptors of that node. This is
  the property that makes cross-talk between readers testable.
- ``read`` honors ``timeout_ms`` with a monotonic deadline, so the daemon's
  blocking loops behave as in production instead of spinning.

Deliberate simplification: queues are unbounded. Real hidraw keeps a finite
per-descriptor ring buffer and drops the NEWEST report when it is full. None
of the current tests depend on saturation behavior.

The emulator answers from a declared sheet (``FakePairedDevice``). It can only
validate that the daemon honors the sheet — it cannot confirm that the sheet
matches a physical device.
"""
import importlib.util
import itertools
import subprocess
import sys
import tempfile
import threading
import time
import types
from collections import deque
from importlib.machinery import SourceFileLoader
from pathlib import Path

DEVICE_NAME_FEATURE = 0x0005
BATTERY_VOLTAGE_FEATURE = 0x1001
UNIFIED_BATTERY_FEATURE = 0x1004

_DEFAULT_BATTERY_INDEX = {
    UNIFIED_BATTERY_FEATURE: 0x06,
    BATTERY_VOLTAGE_FEATURE: 0x07,
}


class FakePairedDevice:
    """Declarative sheet for one paired device. Answers by inspecting the
    request, never by position."""

    def __init__(self, kind, battery_feature=UNIFIED_BATTERY_FEATURE,
                 battery=None, voltage=None, charging=False, awake=True,
                 name_index=0x03, battery_index=None):
        if battery_feature not in _DEFAULT_BATTERY_INDEX:
            raise ValueError(f"unsupported battery feature 0x{battery_feature:04x}")
        if battery_index is None:
            battery_index = _DEFAULT_BATTERY_INDEX[battery_feature]
        if not name_index or not battery_index:
            raise ValueError("feature indices must be non-zero")
        if battery_feature == UNIFIED_BATTERY_FEATURE and battery is None:
            raise ValueError("a unified-battery sheet needs a battery percentage")
        if battery_feature == BATTERY_VOLTAGE_FEATURE and voltage is None:
            raise ValueError("a battery-voltage sheet needs a voltage")
        self.kind = kind
        self.battery_feature = battery_feature
        self.battery = battery
        self.voltage = voltage
        self.charging = charging
        self.awake = awake
        self.name_index = name_index
        self.battery_index = battery_index

    def feature_index(self, feature_id):
        known = {DEVICE_NAME_FEATURE: self.name_index,
                 self.battery_feature: self.battery_index}
        return known.get(feature_id, 0)

    def respond(self, cmd):
        """Returns (report | None, protocol_error | None) for one request."""
        if len(cmd) >= 4 and cmd[0] == 0x10 and cmd[1] == 0xff and cmd[2] == 0x80:
            return None, None  # HID++ 1.0 set-register (enable_notifications)
        if not self.awake:
            return None, None
        if len(cmd) >= 7 and cmd[0] == 0x10 and cmd[2] == 0x00:
            feature_id = cmd[4] << 8 | cmd[5]
            index = self.feature_index(feature_id)
            return bytes([cmd[0], cmd[1], 0x00, cmd[3], index, 0x00, 0x00]), None
        if len(cmd) >= 4 and cmd[0] == 0x11 and cmd[2] == self.name_index:
            return self._name_feature_report(cmd)
        if len(cmd) >= 4 and cmd[0] == 0x11 and cmd[2] == self.battery_index:
            return self._battery_report(cmd)
        return None, f"unknown command {bytes(cmd[:8]).hex()}"

    def _name_feature_report(self, cmd):
        if cmd[3] >> 4 != 0x2:  # only getDeviceType (function 2)
            return None, f"unsupported DEVICE_NAME function byte 0x{cmd[3]:02x}"
        return bytes([0x11, cmd[1], cmd[2], cmd[3], self.kind, 0x00, 0x00]), None

    def _battery_report(self, cmd):
        function = cmd[3] >> 4
        if self.battery_feature == UNIFIED_BATTERY_FEATURE:
            if function != 0x1:  # getStatus
                return None, f"unsupported UNIFIED_BATTERY function byte 0x{cmd[3]:02x}"
            status = 0x01 if self.charging else 0x00
            return bytes([0x11, cmd[1], cmd[2], cmd[3], self.battery, 0x00, status]), None
        if function != 0x0:  # getBatteryInfo
            return None, f"unsupported BATTERY_VOLTAGE function byte 0x{cmd[3]:02x}"
        flags = 0x80 if self.charging else 0x00
        return bytes([0x11, cmd[1], cmd[2], cmd[3],
                      self.voltage >> 8, self.voltage & 0xff, flags]), None

    def battery_event(self, device_idx):
        if self.battery_feature == UNIFIED_BATTERY_FEATURE:
            status = 0x01 if self.charging else 0x00
            return bytes([0x11, device_idx, self.battery_index, 0x00,
                          self.battery, 0x00, status])
        flags = 0x80 if self.charging else 0x00
        return bytes([0x11, device_idx, self.battery_index, 0x00,
                      self.voltage >> 8, self.voltage & 0xff, flags])


class FakeNode:
    """One /dev/hidrawN. device_idx is 0x01 for a wireless receiver and 0xff
    for a direct USB (wired) connection — events carry it."""

    def __init__(self, path, pid, device=None, serial="", vid=0x046d,
                 usage_page=0xff00, device_idx=0x01):
        self.path = path
        self.pid = pid
        self.device = device
        self.serial = serial
        self.vid = vid
        self.usage_page = usage_page
        self.device_idx = device_idx


class ResponseGate:
    """Pens matching responses until the test releases them in a chosen,
    GLOBAL order — every descriptor of the node sees the same order, exactly
    like the kernel."""

    def __init__(self, bus, node, count, predicate):
        self._bus = bus
        self._node = node
        self._count = count
        self._predicate = predicate
        self._pen = []  # (request_bytes, report_bytes)
        self._full = threading.Event()

    def _offer(self, request, report):
        """Called by the bus under its lock. True if the response was penned."""
        if len(self._pen) >= self._count or not self._predicate(list(request)):
            return False
        self._pen.append((request, report))
        if len(self._pen) == self._count:
            self._full.set()
        return True

    def wait_until_full(self, timeout):
        if not self._full.wait(timeout):
            raise AssertionError(
                f"gate holds {len(self._pen)}/{self._count} responses after {timeout}s")

    def release(self, order=None):
        """Broadcast the penned reports. `order` is a list of pen indexes;
        None keeps arrival order."""
        with self._bus._lock:
            sequence = order if order is not None else range(len(self._pen))
            for i in sequence:
                self._bus._broadcast_locked(self._node, self._pen[i][1])
            self._bus._gates.pop(self._node.path, None)

    def requests(self):
        return [list(request) for request, _ in self._pen]


class _FakeFd:
    def __init__(self, bus):
        self._bus = bus
        self._id = None

    def open_path(self, path):
        self._id = self._bus._open(path)

    def write(self, cmd):
        return self._bus._write(self._require_open(), cmd)

    def read(self, size, timeout_ms=0):
        return self._bus._read(self._require_open(), size, timeout_ms)

    def close(self):
        if self._id is not None:
            self._bus._close(self._id)
            self._id = None

    def _require_open(self):
        if self._id is None:
            raise OSError("descriptor is not open")
        return self._id


class FakeHidBus:
    def __init__(self, nodes):
        self._lock = threading.Lock()
        self.nodes = {node.path: node for node in nodes}
        self._fds = {}  # fd id -> dict(node, queue, cond, owner)
        self._ids = itertools.count(1)
        self._stopped = False
        self._interceptors = {}  # path -> [(predicate, transform)]
        self._gates = {}         # path -> ResponseGate
        self.log = []            # ("open"/"close"/"write", monotonic, path, fd, bytes|None)
        self.protocol_errors = []

    # -- hidapi surface -------------------------------------------------

    def enumerate(self, vid, pid):
        with self._lock:
            return [
                {"path": node.path, "usage_page": node.usage_page,
                 "serial_number": node.serial, "vendor_id": node.vid,
                 "product_id": node.pid}
                for node in self.nodes.values()
                if node.vid == vid and node.pid == pid
            ]

    def device(self):
        return _FakeFd(self)

    def namespace(self):
        """What the daemon sees as its `hid` module."""
        return types.SimpleNamespace(device=self.device, enumerate=self.enumerate)

    # -- test controls --------------------------------------------------

    def stop(self):
        """Wake every blocked read; from here on every read returns []."""
        with self._lock:
            self._stopped = True
            for fd in self._fds.values():
                fd["cond"].notify_all()

    def close_all(self):
        with self._lock:
            for fd_id in list(self._fds):
                self._close_locked(fd_id)

    def open_paths(self):
        with self._lock:
            return [fd["node"].path for fd in self._fds.values()]

    def intercept_next(self, path, predicate, transform):
        """One-shot: for the first request on `path` matching `predicate`,
        broadcast `transform(request, default_report)` (a list of reports)
        instead of the default. Runs under the bus lock — it must not call
        back into the bus."""
        self._interceptors.setdefault(path, []).append((predicate, transform))

    def hold_responses(self, path, count, predicate):
        gate = ResponseGate(self, self.nodes[path], count, predicate)
        self._gates[path] = gate
        return gate

    def emit_battery_event(self, path, battery=None, voltage=None, charging=None):
        """Update the sheet, then broadcast the event (software id 0)."""
        with self._lock:
            node = self.nodes[path]
            if battery is not None:
                node.device.battery = battery
            if voltage is not None:
                node.device.voltage = voltage
            if charging is not None:
                node.device.charging = charging
            self._broadcast_locked(node, node.device.battery_event(node.device_idx))

    def emit_link(self, path, off):
        """0x41 connection event; the device sleeps on link_off and wakes
        BEFORE the daemon's follow-up query on link_on."""
        with self._lock:
            node = self.nodes[path]
            node.device.awake = not off
            flags = 0x40 if off else 0x00
            self._broadcast_locked(
                node, bytes([0x10, node.device_idx, 0x41, 0x00, flags]))

    # -- internals ------------------------------------------------------

    def _open(self, path):
        with self._lock:
            if path not in self.nodes:
                raise OSError(f"no such node: {path!r}")
            fd_id = next(self._ids)
            self._fds[fd_id] = {
                "node": self.nodes[path],
                "queue": deque(),
                "cond": threading.Condition(self._lock),
            }
            self.log.append(("open", time.monotonic(), path, fd_id, None))
            return fd_id

    def _close(self, fd_id):
        with self._lock:
            self._close_locked(fd_id)

    def _close_locked(self, fd_id):
        fd = self._fds.pop(fd_id, None)
        if fd is not None:
            self.log.append(("close", time.monotonic(), fd["node"].path, fd_id, None))
            fd["cond"].notify_all()

    def _write(self, fd_id, cmd):
        request = bytes(cmd)
        with self._lock:
            fd = self._fds.get(fd_id)
            if fd is None:
                raise OSError("descriptor is closed")
            node = fd["node"]
            self.log.append(("write", time.monotonic(), node.path, fd_id, request))
            report, error = self._default_response_locked(node, cmd)
            if error:
                self.protocol_errors.append((node.path, error))
            reports = self._apply_interceptor_locked(node, cmd, request, report)
            for item in reports:
                gate = self._gates.get(node.path)
                if gate is not None and gate._offer(request, item):
                    continue
                self._broadcast_locked(node, item)
            return len(request)

    def _default_response_locked(self, node, cmd):
        if node.device is None:
            return None, "write to a node with no paired device"
        return node.device.respond(list(cmd))

    def _apply_interceptor_locked(self, node, cmd, request, report):
        pending = self._interceptors.get(node.path, [])
        for i, (predicate, transform) in enumerate(pending):
            if predicate(list(cmd)):
                del pending[i]
                return [bytes(r) for r in transform(list(request), report)]
        return [report] if report is not None else []

    def _broadcast_locked(self, node, report):
        for fd in self._fds.values():
            if fd["node"] is node:
                fd["queue"].append(report)
                fd["cond"].notify_all()

    def _read(self, fd_id, size, timeout_ms):
        deadline = time.monotonic() + timeout_ms / 1000
        with self._lock:
            fd = self._fds.get(fd_id)
            if fd is None:
                raise OSError("descriptor is closed")
            while not fd["queue"] and not self._stopped and fd_id in self._fds:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                fd["cond"].wait(remaining)
            if self._stopped or fd_id not in self._fds or not fd["queue"]:
                return []
            return list(fd["queue"].popleft()[:size])


_MODULE_IDS = itertools.count(1)
_IMPORT_LOCK = threading.Lock()
_DAEMON_PATH = Path(__file__).parent.parent / "logibar-hidpp-monitor"


def load_monitor_module(bus):
    """Load a FRESH instance of the daemon with the bus installed as `hid`.
    Every module-level global (running, caches, publish state) is private to
    the instance, so harnesses cannot leak state into each other."""
    name = f"logibar_hidpp_monitor_bus_{next(_MODULE_IDS)}"
    with _IMPORT_LOCK:
        original = sys.modules.get("hid")
        sys.modules["hid"] = bus.namespace()
        try:
            loader = SourceFileLoader(name, str(_DAEMON_PATH))
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
        finally:
            if original is None:
                sys.modules.pop("hid", None)
            else:
                sys.modules["hid"] = original
    return module


class DaemonHarness:
    """Runs monitor threads from a fresh daemon module against a FakeHidBus,
    with a bounded, verifiable teardown."""

    JOIN_BUDGET = 3.0

    def __init__(self, bus):
        self.bus = bus
        self._tmp = tempfile.TemporaryDirectory()
        self.module = load_monitor_module(bus)
        self.module.STATE_DIR = self._tmp.name
        self.module.subprocess = types.SimpleNamespace(
            run=lambda *args, **kwargs: None, DEVNULL=subprocess.DEVNULL)
        self.threads = []
        self.thread_errors = []
        self.report = None

    # -- selective start ------------------------------------------------

    def start_wireless(self, wireless_pid, name, signal, battery_feature=None,
                       barrier=None, state=None, state_lock=None):
        state = state or self._fresh_state(wireless_pid, None)
        feature = battery_feature or UNIFIED_BATTERY_FEATURE
        self._spawn(self.module.monitor_wireless,
                    (wireless_pid, name, signal, feature, state,
                     state_lock or threading.Lock()), barrier)
        return state

    def start_wired(self, wired_pid, name, signal, battery_feature=None,
                    barrier=None, state=None, state_lock=None):
        state = state or self._fresh_state(None, wired_pid)
        feature = battery_feature or UNIFIED_BATTERY_FEATURE
        self._spawn(self.module.monitor_wired,
                    (wired_pid, name, signal, feature, state,
                     state_lock or threading.Lock()), barrier)
        return state

    def start_device(self, wireless_pid, wired_pid, name, signal,
                     battery_feature=None):
        state = self._fresh_state(wireless_pid, wired_pid)
        lock = threading.Lock()
        feature = battery_feature or UNIFIED_BATTERY_FEATURE
        self.start_wireless(wireless_pid, name, signal, feature,
                            state=state, state_lock=lock)
        self.start_wired(wired_pid, name, signal, feature,
                         state=state, state_lock=lock)
        return state

    def _fresh_state(self, wireless_pid, wired_pid):
        return {
            '_source_id': (wireless_pid, wired_pid),
            'wireless_connected': False,
            'wired_connected': False,
            'wired_just_disconnected': False,
            'battery': None,
            'charging': None,
            'feat_idx': None,
            'wired_feat_idx': None,
        }

    def _spawn(self, target, args, barrier):
        def wrapped():
            try:
                if barrier is not None:
                    barrier.wait(timeout=5)
                target(*args)
            except Exception as error:
                self.thread_errors.append(error)
        thread = threading.Thread(target=wrapped, daemon=True)
        thread.start()
        self.threads.append(thread)

    # -- observation ----------------------------------------------------

    def state_file(self, name):
        return Path(self._tmp.name) / name

    def wait_for_state(self, name, expected, timeout=5.0):
        deadline = time.monotonic() + timeout
        path = self.state_file(name)
        while time.monotonic() < deadline:
            if path.exists() and path.read_text() == expected:
                return
            time.sleep(0.02)
        current = path.read_text() if path.exists() else "<missing>"
        raise AssertionError(
            f"state {name!r} never became {expected!r} (last: {current!r})\n"
            f"protocol errors: {self.bus.protocol_errors}\n"
            f"log tail: {self.bus.log[-15:]}")

    def wait_for_open(self, path, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path in self.bus.open_paths():
                return
            time.sleep(0.02)
        raise AssertionError(f"no descriptor was opened on {path!r}")

    def probe_sessions(self, path):
        """Open/close pairs on `path`, oldest first — one logical probe is one
        session even though it writes several ROOT requests (retries)."""
        opens = {fd: t for kind, t, p, fd, _ in self.bus.log
                 if kind == "open" and p == path}
        closes = {fd: t for kind, t, p, fd, _ in self.bus.log
                  if kind == "close" and p == path}
        return [(opens[fd], closes.get(fd)) for fd in sorted(opens)]

    # -- teardown -------------------------------------------------------

    def stop(self):
        """Idempotent. Returns a report dict; cleanup always runs."""
        if self.report is not None:
            return self.report
        self.module.running = False
        self.bus.stop()
        deadline = time.monotonic() + self.JOIN_BUDGET
        for thread in self.threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        self.bus.close_all()
        for thread in self.threads:
            if thread.is_alive():
                thread.join(0.2)
        self.report = {
            "alive": [t for t in self.threads if t.is_alive()],
            "errors": list(self.thread_errors),
            "protocol_errors": list(self.bus.protocol_errors),
        }
        return self.report

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.stop()
        finally:
            self._tmp.cleanup()
        return False
