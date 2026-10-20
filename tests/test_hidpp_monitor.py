#!/usr/bin/env python3
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

_original_hid = sys.modules.get("hid")
sys.modules["hid"] = types.SimpleNamespace()

SCRIPT = Path(__file__).parent.parent / "logibar-hidpp-monitor"
loader = SourceFileLoader("logibar_hidpp_monitor", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
monitor = importlib.util.module_from_spec(spec)
loader.exec_module(monitor)

if _original_hid is None:
    sys.modules.pop("hid", None)
else:
    sys.modules["hid"] = _original_hid

sys.path.insert(0, str(Path(__file__).parent))
from fake_hid import (  # noqa: E402
    BATTERY_STATUS_FEATURE,
    BATTERY_VOLTAGE_FEATURE,
    DaemonHarness,
    FakeHidBus,
    FakeNode,
    FakePairedDevice,
    FakeReceiver,
    UNIFIED_BATTERY_FEATURE,
    load_monitor_module,
)


def is_root_lookup(cmd):
    return cmd[0] == 0x10 and cmd[2] == 0x00


def single_device_bus(pid, sheet, path=b"/dev/receiver", **node_kwargs):
    bus = FakeHidBus([FakeNode(path, pid, sheet, **node_kwargs)])
    fd = bus.device()
    fd.open_path(path)
    return bus, fd


def clear_probe_state():
    monitor.receiver_kinds.clear()
    monitor.probes_in_flight.clear()
    monitor.probe_retry_at.clear()
    monitor.probe_generation.clear()


class ReceiverSelectionTests(unittest.TestCase):
    def setUp(self):
        self.get_device_kind = monitor.get_device_kind
        clear_probe_state()

    def tearDown(self):
        monitor.get_device_kind = self.get_device_kind

    def test_shared_pid_selects_and_caches_each_physical_receiver(self):
        kinds = {b"/dev/keyboard": 0x00, b"/dev/mouse": 0x03}
        calls = []
        monitor.get_device_kind = lambda path: calls.append(path) or kinds[path]
        devices = [
            {"path": b"/dev/keyboard", "serial_number": "K1"},
            {"path": b"/dev/keyboard", "serial_number": "K1"},
            {"path": b"/dev/mouse", "serial_number": "M1"},
        ]

        self.assertEqual(
            monitor.receiver_path_for_device(0xC547, "mouse", devices),
            b"/dev/mouse",
        )
        self.assertEqual(
            monitor.receiver_path_for_device(0xC547, "keyboard", devices),
            b"/dev/keyboard",
        )
        self.assertEqual(calls, [b"/dev/keyboard", b"/dev/mouse"])

    def test_shared_pid_waits_when_kind_is_unresolved(self):
        monitor.get_device_kind = lambda _path: None
        devices = [{"path": b"/dev/receiver", "serial_number": "R1"}]

        self.assertIsNone(
            monitor.receiver_path_for_device(0xC547, "mouse", devices)
        )

    def test_absent_shared_device_keeps_present_receiver_cached(self):
        calls = []
        monitor.get_device_kind = lambda path: calls.append(path) or 0x00
        devices = [{"path": b"/dev/keyboard", "serial_number": "K1"}]

        self.assertEqual(
            monitor.receiver_path_for_device(0xC547, "keyboard", devices),
            b"/dev/keyboard",
        )
        self.assertIsNone(
            monitor.receiver_path_for_device(0xC547, "mouse", devices)
        )
        self.assertEqual(
            monitor.receiver_path_for_device(0xC547, "keyboard", devices),
            b"/dev/keyboard",
        )
        self.assertEqual(calls, [b"/dev/keyboard"])

    def test_unique_pid_uses_its_first_hidpp_path(self):
        monitor.get_device_kind = lambda _path: self.fail("unexpected kind probe")
        devices = [{"path": b"/dev/superlight2", "serial_number": "S2"}]

        self.assertEqual(
            monitor.receiver_path_for_device(0xC54D, "mouse", devices),
            b"/dev/superlight2",
        )

    def test_the_open_receiver_is_preferred_over_another_match(self):
        monitor.receiver_kinds[b"/dev/mouse-a"] = (0xC547, "A", 0x03)
        monitor.receiver_kinds[b"/dev/mouse-b"] = (0xC547, "B", 0x03)
        monitor.get_device_kind = lambda _path: self.fail("unexpected kind probe")
        devices = [
            {"path": b"/dev/mouse-a", "serial_number": "A"},
            {"path": b"/dev/mouse-b", "serial_number": "B"},
        ]

        self.assertEqual(
            monitor.receiver_path_for_device(
                0xC547, "mouse", devices, preferred_path=b"/dev/mouse-b"
            ),
            b"/dev/mouse-b",
        )

    def test_a_new_serial_under_a_known_path_forces_a_new_probe(self):
        calls = []
        monitor.receiver_kinds[b"/dev/receiver"] = (0xC547, "OLD", 0x03)
        monitor.get_device_kind = lambda path: calls.append(path) or 0x00
        devices = [{"path": b"/dev/receiver", "serial_number": "NEW"}]

        self.assertIsNone(
            monitor.receiver_path_for_device(0xC547, "mouse", devices)
        )
        self.assertEqual(calls, [b"/dev/receiver"])
        self.assertEqual(
            monitor.receiver_kinds[b"/dev/receiver"], (0xC547, "NEW", 0x00)
        )

    def test_a_failed_probe_backs_off_before_the_next_attempt(self):
        calls = []
        monitor.get_device_kind = lambda path: calls.append(path) or None
        devices = [{"path": b"/dev/receiver", "serial_number": "R1"}]
        original_retry = monitor.PROBE_RETRY_SECONDS
        monitor.PROBE_RETRY_SECONDS = 0.05
        try:
            self.assertIsNone(
                monitor.receiver_path_for_device(0xC547, "mouse", devices)
            )
            self.assertIsNone(
                monitor.receiver_path_for_device(0xC547, "mouse", devices)
            )
            self.assertEqual(len(calls), 1)

            time.sleep(0.06)
            self.assertIsNone(
                monitor.receiver_path_for_device(0xC547, "mouse", devices)
            )
            self.assertEqual(len(calls), 2)
        finally:
            monitor.PROBE_RETRY_SECONDS = original_retry

    def test_a_path_probed_by_another_thread_is_not_probed_again(self):
        monitor.get_device_kind = lambda _path: self.fail("unexpected kind probe")
        monitor.probes_in_flight[b"/dev/receiver"] = 0xC547
        devices = [{"path": b"/dev/receiver", "serial_number": "R1"}]

        self.assertIsNone(
            monitor.receiver_path_for_device(0xC547, "mouse", devices)
        )

    def test_a_stale_probe_result_is_discarded_when_the_receiver_changes(self):
        calls = []

        def probe_while_the_path_disappears(path):
            calls.append(path)
            if len(calls) == 1:
                monitor.forget_receiver_kinds(0xC547, [])
            return 0x03

        monitor.get_device_kind = probe_while_the_path_disappears
        devices = [{"path": b"/dev/receiver", "serial_number": "R1"}]

        self.assertEqual(
            monitor.receiver_path_for_device(0xC547, "mouse", devices),
            b"/dev/receiver",
        )
        self.assertEqual(len(calls), 2)

    def test_an_error_on_the_old_descriptor_keeps_the_new_entry(self):
        monitor.receiver_kinds[b"/dev/receiver"] = (0xC547, "NEW", 0x00)

        monitor.invalidate_receiver_kind(0xC547, b"/dev/receiver", "OLD")

        self.assertEqual(
            monitor.receiver_kinds[b"/dev/receiver"], (0xC547, "NEW", 0x00)
        )

    def test_an_error_on_the_open_descriptor_drops_its_entry(self):
        monitor.receiver_kinds[b"/dev/receiver"] = (0xC547, "R1", 0x03)

        monitor.invalidate_receiver_kind(0xC547, b"/dev/receiver", "R1")

        self.assertNotIn(b"/dev/receiver", monitor.receiver_kinds)

    def test_a_path_change_closes_the_old_descriptor(self):
        class FakeDevice:
            closed = False

            def close(self):
                self.closed = True

        device = FakeDevice()
        monitor.receiver_kinds[b"/dev/old"] = (0xC547, "R1", 0x03)

        result = monitor.close_if_path_changed(
            device, (b"/dev/old", "R1"), b"/dev/new", 0xC547
        )

        self.assertIsNone(result)
        self.assertTrue(device.closed)
        self.assertNotIn(b"/dev/old", monitor.receiver_kinds)

    def test_kind_probe_ignores_other_hidpp_responses(self):
        bus, _fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=50))
        bus.intercept_next(
            b"/dev/receiver", is_root_lookup,
            lambda _req, resp: [[0x11, 0x01, 0x00, 0x1e, 0x00], resp])
        bus.intercept_next(
            b"/dev/receiver", lambda cmd: cmd[0] == 0x11 and cmd[2] == 0x03,
            lambda _req, resp: [[0x11, 0x01, 0x03, 0x1e, 0x00], resp])

        original_hid = monitor.hid
        monitor.hid = bus.namespace()
        try:
            self.assertEqual(monitor.get_device_kind(b"/dev/receiver"), 0x03)
        finally:
            monitor.hid = original_hid

    def test_disconnect_closes_stale_shared_receiver(self):
        class FakeDevice:
            closed = False

            def close(self):
                self.closed = True

        device = FakeDevice()
        state = {
            '_source_id': (0xC547, 0xC094),
            'wireless_connected': True,
            'wired_connected': False,
        }
        writes = []
        original_write_state = monitor.write_state
        monitor.write_state = lambda *args: writes.append(args)
        try:
            self.assertIsNone(
                monitor.disconnect_wireless(
                    device,
                    "mouse",
                    10,
                    state,
                    threading.Lock(),
                )
            )
        finally:
            monitor.write_state = original_write_state

        self.assertTrue(device.closed)
        self.assertFalse(state['wireless_connected'])
        self.assertEqual(writes[0][0:5], ("mouse", 0, False, False, 10))


class SoftwareIdTests(unittest.TestCase):
    def test_a_response_with_a_foreign_software_id_is_ignored(self):
        bus, fd = single_device_bus(
            0xC54D, FakePairedDevice(kind=0x03, battery=50))
        bus.intercept_next(
            b"/dev/receiver", is_root_lookup,
            lambda _req, resp: [[0x11, 0x01, 0x00, 0x0e, 0x05], resp])

        self.assertEqual(
            monitor.get_feature_index(fd, 0x01, 0x1004, retries=1),
            0x06,
        )

    def test_monitor_requests_carry_the_monitor_software_id(self):
        bus, fd = single_device_bus(
            0xC54D, FakePairedDevice(kind=0x03, battery=60, charging=True))

        feat_idx = monitor.get_feature_index(fd, 0x01, 0x1004, retries=1)
        self.assertEqual(feat_idx, 0x06)
        self.assertEqual(
            monitor.query_battery(fd, 0x01, feat_idx, monitor.UNIFIED_BATTERY_FEATURE),
            (60, True),
        )

        writes = [list(entry[4]) for entry in bus.log if entry[0] == "write"]
        root_requests = [c for c in writes if c[0] == 0x10 and c[2] == 0x00]
        battery_requests = [c for c in writes if c[0] == 0x11 and c[2] == 0x06]
        self.assertEqual([c[3] for c in root_requests], [0x0d])
        self.assertEqual(battery_requests[0][0:4], [0x11, 0x01, 0x06, 0x1d])

    def test_probe_requests_carry_the_probe_software_id(self):
        bus, _fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=50))
        original_hid = monitor.hid
        monitor.hid = bus.namespace()
        try:
            self.assertEqual(monitor.get_device_kind(b"/dev/receiver"), 0x03)
        finally:
            monitor.hid = original_hid

        writes = [list(entry[4]) for entry in bus.log if entry[0] == "write"]
        root_requests = [c for c in writes if c[0] == 0x10 and c[2] == 0x00]
        type_requests = [c for c in writes if c[0] == 0x11 and c[2] == 0x03]
        self.assertTrue(root_requests)
        self.assertTrue(all(c[3] == 0x0e for c in root_requests))
        self.assertTrue(type_requests)
        self.assertTrue(all(c[3] == 0x2e for c in type_requests))

    def test_a_broadcast_with_a_request_echo_is_not_a_battery_event(self):
        writes = []
        original_write_state = monitor.write_state
        monitor.write_state = lambda *args: writes.append(args)
        state = {'feat_idx': 0x06, 'battery': 40, 'charging': False,
                 'wired_connected': False}
        lock = threading.Lock()
        try:
            echo = [0x11, 0x01, 0x06, 0x1d, 0x37, 0x00, 0x01]
            monitor.handle_battery_broadcast(
                echo, 0x01, "mouse", 10, monitor.UNIFIED_BATTERY_FEATURE, state, lock
            )
            self.assertEqual(writes, [])

            event = [0x11, 0x01, 0x06, 0x00, 0x37, 0x00, 0x01]
            monitor.handle_battery_broadcast(
                event, 0x01, "mouse", 10, monitor.UNIFIED_BATTERY_FEATURE, state, lock
            )
            self.assertEqual(writes[0][0:5], ("mouse", 0x37, True, True, 10))
        finally:
            monitor.write_state = original_write_state


class ReadCounter:
    """Wraps a fake descriptor and records each read and its timeout."""

    def __init__(self, fd):
        self._fd = fd
        self.count = 0
        self.timeouts = []

    def write(self, cmd):
        return self._fd.write(cmd)

    def read(self, size, timeout_ms=0):
        self.count += 1
        self.timeouts.append(timeout_ms)
        return self._fd.read(size, timeout_ms)


class PairingInfoTests(unittest.TestCase):
    def test_the_pairing_register_returns_the_wireless_pid(self):
        _bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=41, wpid=0x409F))
        self.assertEqual(monitor.read_paired_wpid(fd, 0x01), 0x409F)

    def test_an_empty_slot_returns_none(self):
        _bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=41))
        self.assertIsNone(monitor.read_paired_wpid(fd, 0x01))

    def test_an_unlisted_wpid_keeps_the_table_name(self):
        _bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=41, wpid=0x4099))
        state = {'_model': "PRO X Superlight"}
        monitor.refresh_paired_model(fd, 0x01, "mouse", state, threading.Lock())
        self.assertEqual(state['_paired_model'], "")

    def test_the_request_is_a_long_register_read_of_slot_1(self):
        # 0x83 reads a long register; 0x82 would WRITE it and could change
        # the receiver's pairing state.
        bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=41, wpid=0x409F))
        monitor.read_paired_wpid(fd, 0x01)
        writes = [list(entry[4]) for entry in bus.log if entry[0] == "write"]
        self.assertEqual(writes, [[0x10, 0xFF, 0x83, 0xB5, 0x20, 0x00, 0x00]])

    def test_another_readers_error_does_not_hide_the_reply(self):
        # The error report does not echo the sub-register: an error to
        # another reader's 0xb5 request (Solaar reads 0x30 and 0x40 too)
        # looks the same as an error to this one.
        bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=41, wpid=0x409F))
        foreign_error = bytes([0x10, 0xFF, 0x8F, 0x83, 0xB5, 0x03, 0x00])
        bus.intercept_next(b"/dev/receiver", lambda cmd: cmd[2] == 0x83,
                           lambda _request, reply: [foreign_error, reply])
        self.assertEqual(monitor.read_paired_wpid(fd, 0x01), 0x409F)

    def test_an_error_reply_ends_the_read_after_one_quiet_read(self):
        _bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=41))
        reads = ReadCounter(fd)
        self.assertIsNone(monitor.read_paired_wpid(reads, 0x01))
        # The flush reads until quiet (1), then the error (1), then one
        # quiet read ends it (1).
        self.assertEqual(reads.count, 3)

    def test_a_silent_receiver_gives_up_within_the_read_budget(self):
        bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=41, wpid=0x409F))
        bus.intercept_next(b"/dev/receiver", lambda cmd: cmd[2] == 0x83,
                           lambda _request, _reply: [])
        reads = ReadCounter(fd)
        self.assertIsNone(monitor.read_paired_wpid(reads, 0x01))
        self.assertEqual(reads.count, 1 + 20)
        self.assertTrue(all(timeout <= 50 for timeout in reads.timeouts))

    def test_a_reply_for_another_slot_is_not_taken(self):
        bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=41, wpid=0x409F))
        slot_2 = bytes([0x11, 0xFF, 0x83, 0xB5, 0x21, 0x07, 0x08, 0x40, 0x9F,
                        0x11, 0x00, 0x02] + [0x00] * 8)
        bus.intercept_next(b"/dev/receiver", lambda cmd: cmd[2] == 0x83,
                           lambda _request, _reply: [slot_2])
        self.assertIsNone(monitor.read_paired_wpid(fd, 0x01))

    def test_a_mouse_wpid_never_names_a_keyboard_line(self):
        self.assertEqual(monitor.paired_model("mouse", 0x409F), "G502 X LIGHTSPEED")
        self.assertEqual(monitor.paired_model("keyboard", 0x409F), "")

    def test_a_wake_without_an_answer_keeps_the_paired_name(self):
        bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=41, wpid=0x409F))
        state = {'_paired_model': "G502 X LIGHTSPEED"}
        bus.intercept_next(b"/dev/receiver", lambda cmd: cmd[2] == 0x83,
                           lambda _request, _reply: [])
        monitor.refresh_paired_model(fd, 0x01, "mouse", state, threading.Lock(),
                                     keep_on_no_answer=True)
        self.assertEqual(state['_paired_model'], "G502 X LIGHTSPEED")


class SharedEnumerationTests(unittest.TestCase):
    """Every monitor thread polls safe_enumerate; the scan behind it must run
    once per change of the hidraw node set, not once per call."""

    def setUp(self):
        self.bus = FakeHidBus([
            FakeNode(b"/dev/receiver", 0xC547, FakePairedDevice(kind=0x03, battery=50)),
            FakeNode(b"/dev/cable", 0xC098, FakePairedDevice(kind=0x03, battery=50),
                     device_idx=0xFF),
        ])
        self.module = load_monitor_module(self.bus)
        self.scans = []

        def counting_enumerate(vid, pid):
            self.scans.append((vid, pid))
            return self.bus.enumerate(vid, pid)

        self.module.hid = types.SimpleNamespace(
            device=self.bus.device, enumerate=counting_enumerate)
        self._dir = tempfile.TemporaryDirectory()
        self.hidraw = Path(self._dir.name) / "hidraw"
        self.hidraw.mkdir()
        (self.hidraw / "hidraw0").touch()
        self.module.HIDRAW_CLASS_DIR = str(self.hidraw)

    def tearDown(self):
        self._dir.cleanup()

    def paths(self, pid):
        return [d["path"] for d in self.module.safe_enumerate(0x046D, pid)]

    def test_threads_share_one_scan_until_the_node_set_changes(self):
        self.assertEqual(self.paths(0xC547), [b"/dev/receiver"])
        self.assertEqual(self.paths(0xC098), [b"/dev/cable"])
        self.assertEqual(self.paths(0xC54D), [])
        self.assertEqual(self.scans, [(0x046D, 0)])

        (self.hidraw / "hidraw1").touch()        # a node appears
        self.paths(0xC547)
        (self.hidraw / "hidraw1").unlink()       # and goes away
        self.paths(0xC547)
        self.assertEqual(len(self.scans), 3)

    def test_a_replug_under_the_same_name_scans_again(self):
        self.paths(0xC547)
        before = sorted(os.listdir(self.hidraw))
        # Park the old file OUTSIDE the watched directory: it keeps its inode
        # busy, so the new file cannot reuse it, and the names stay the same.
        # Only the inode tells this replug apart.
        (self.hidraw / "hidraw0").rename(Path(self._dir.name) / "old-node")
        (self.hidraw / "hidraw0").touch()        # new node, same name
        self.assertEqual(sorted(os.listdir(self.hidraw)), before)
        self.paths(0xC547)
        self.assertEqual(len(self.scans), 2)

    def test_an_unreadable_directory_scans_every_call(self):
        self.module.HIDRAW_CLASS_DIR = str(self.hidraw / "missing")
        self.paths(0xC547)
        self.paths(0xC547)
        self.assertEqual(len(self.scans), 2)

    def test_an_old_scan_is_refreshed(self):
        self.module.ENUMERATE_MAX_AGE = 0.0
        self.paths(0xC547)
        self.paths(0xC547)
        self.assertEqual(len(self.scans), 2)

    def test_the_harness_sees_a_fake_hotplug_whatever_the_host_has(self):
        # The host's /sys/class/hidraw does not change when the fake bus
        # does, so a harness that kept it would serve a stale scan.
        bus = FakeHidBus([
            FakeNode(b"/dev/receiver", 0xC547, FakePairedDevice(kind=0x03, battery=50)),
        ])
        with DaemonHarness(bus) as harness:
            class_dir = Path(harness.module.HIDRAW_CLASS_DIR)
            self.assertEqual(class_dir.parent, Path(harness._tmp.name))
            self.assertFalse(class_dir.exists())
            self.assertEqual(len(harness.module.safe_enumerate(0x046D, 0xC547)), 1)
            node = FakeNode(b"/dev/second", 0xC547, FakePairedDevice(kind=0x00, battery=50))
            bus.nodes[node.path] = node
            self.assertEqual(len(harness.module.safe_enumerate(0x046D, 0xC547)), 2)


class StateAggregationTests(unittest.TestCase):
    def test_wired_connect_cannot_be_overwritten_by_wireless_disconnect(self):
        original_state_dir = monitor.STATE_DIR
        original_run = monitor.subprocess.run
        original_write_state = monitor.write_state
        monitor.source_states.clear()
        monitor.published_states.clear()
        monitor.subprocess.run = lambda *_args, **_kwargs: None

        try:
            with tempfile.TemporaryDirectory() as state_dir:
                monitor.STATE_DIR = state_dir
                state = {
                    '_source_id': (0xC547, 0xC094),
                    'wireless_connected': True,
                    'wired_connected': False,
                    'battery': 40,
                    'charging': False,
                }
                state_lock = threading.Lock()
                disconnect_started = threading.Event()
                release_disconnect = threading.Event()
                wired_published = threading.Event()

                def blocking_write(*args):
                    if not args[2]:
                        disconnect_started.set()
                        release_disconnect.wait(timeout=1)
                    original_write_state(*args)

                monitor.write_state = blocking_write
                disconnect_thread = threading.Thread(
                    target=monitor.disconnect_wireless,
                    args=(None, "mouse", 10, state, state_lock),
                )

                def connect_wired():
                    with state_lock:
                        state['wired_connected'] = True
                        state['battery'] = 75
                        monitor.write_state("mouse", 75, True, False, 10, state)
                    wired_published.set()

                wired_thread = threading.Thread(target=connect_wired)
                disconnect_thread.start()
                self.assertTrue(disconnect_started.wait(timeout=1))
                wired_thread.start()
                self.assertFalse(wired_published.wait(timeout=0.05))
                release_disconnect.set()
                disconnect_thread.join(timeout=1)
                wired_thread.join(timeout=1)

                self.assertFalse(disconnect_thread.is_alive())
                self.assertFalse(wired_thread.is_alive())
                self.assertEqual(
                    (Path(state_dir) / "mouse").read_text(),
                    "75\n1\n0\n\nwired",
                )
        finally:
            monitor.STATE_DIR = original_state_dir
            monitor.subprocess.run = original_run
            monitor.write_state = original_write_state
            monitor.source_states.clear()
            monitor.published_states.clear()

    def test_the_lowest_battery_wins_while_both_models_are_connected(self):
        original_state_dir = monitor.STATE_DIR
        original_run = monitor.subprocess.run
        monitor.source_states.clear()
        monitor.published_states.clear()
        monitor.subprocess.run = lambda *_args, **_kwargs: None

        try:
            with tempfile.TemporaryDirectory() as state_dir:
                monitor.STATE_DIR = state_dir
                original = {'_source_id': (0xC547, 0xC094)}
                superlight_2 = {'_source_id': (0xC54D, 0xC09B)}

                monitor.write_state("mouse", 40, True, False, 10, original)
                monitor.write_state("mouse", 60, True, False, 10, superlight_2)
                self.assertEqual(
                    (Path(state_dir) / "mouse").read_text(),
                    "40\n1\n0\n",
                )
        finally:
            monitor.STATE_DIR = original_state_dir
            monitor.subprocess.run = original_run
            monitor.source_states.clear()
            monitor.published_states.clear()

    def test_a_cable_reading_wins_over_a_lower_wireless_one(self):
        # The wireless mouse has its own receiver row; the cable mouse has
        # only this file. Lowest-wins hid the cable mouse entirely.
        original_state_dir = monitor.STATE_DIR
        original_run = monitor.subprocess.run
        monitor.source_states.clear()
        monitor.published_states.clear()
        monitor.subprocess.run = lambda *_args, **_kwargs: None

        try:
            with tempfile.TemporaryDirectory() as state_dir:
                monitor.STATE_DIR = state_dir
                wireless = {'_source_id': (0xC547, 0xC094)}
                cable = {'_source_id': (0xC539, 0xC088), 'wired_connected': True,
                         '_model': 'G Pro Wireless'}

                monitor.write_state("mouse", 30, True, False, 10, wireless)
                monitor.write_state("mouse", 80, True, False, 10, cable)
                self.assertTrue((Path(state_dir) / "mouse").read_text()
                                .startswith("80\n1\n0\nG Pro Wireless\nwired"))
        finally:
            monitor.STATE_DIR = original_state_dir
            monitor.subprocess.run = original_run
            monitor.source_states.clear()
            monitor.published_states.clear()

    def test_a_cable_only_line_names_the_receivers_of_its_family(self):
        # The G502 X line has no receiver PID; its receiver is the c547.
        original_state_dir = monitor.STATE_DIR
        original_run = monitor.subprocess.run
        original_enumerate = monitor.safe_enumerate
        monitor.source_states.clear()
        monitor.published_states.clear()
        monitor.subprocess.run = lambda *_args, **_kwargs: None
        monitor.safe_enumerate = lambda vid, pid: (
            [{'path': b'/dev/c547'}] if pid == 0xC547 else [])

        try:
            with tempfile.TemporaryDirectory() as state_dir:
                monitor.STATE_DIR = state_dir
                cable = {'_source_id': (None, 0xC098), 'wired_connected': True,
                         '_model': 'G502 X LIGHTSPEED'}
                monitor.write_state("mouse", 80, True, False, 10, cable)
                self.assertEqual(
                    (Path(state_dir) / "mouse").read_text(),
                    f"80\n1\n0\nG502 X LIGHTSPEED\nwired rx-{monitor.receiver_hash(b'/dev/c547')}")
        finally:
            monitor.STATE_DIR = original_state_dir
            monitor.subprocess.run = original_run
            monitor.safe_enumerate = original_enumerate
            monitor.source_states.clear()
            monitor.published_states.clear()

    def test_disconnecting_one_model_keeps_the_other_connected(self):
        original_state_dir = monitor.STATE_DIR
        original_run = monitor.subprocess.run
        monitor.source_states.clear()
        monitor.published_states.clear()
        monitor.subprocess.run = lambda *_args, **_kwargs: None

        try:
            with tempfile.TemporaryDirectory() as state_dir:
                monitor.STATE_DIR = state_dir
                original = {'_source_id': (0xC547, 0xC094)}
                superlight_2 = {'_source_id': (0xC54D, 0xC09B)}

                monitor.write_state("mouse", 40, True, False, 10, original)
                monitor.write_state("mouse", 60, True, False, 10, superlight_2)
                monitor.write_state("mouse", 0, False, False, 10, original)
                self.assertEqual(
                    (Path(state_dir) / "mouse").read_text(),
                    "60\n1\n0\n",
                )

                monitor.write_state("mouse", 0, False, False, 10, superlight_2)
                self.assertEqual(
                    (Path(state_dir) / "mouse").read_text(),
                    "0\n0\n0\n",
                )
        finally:
            monitor.STATE_DIR = original_state_dir
            monitor.subprocess.run = original_run
            monitor.source_states.clear()
            monitor.published_states.clear()


class BatteryVoltageTests(unittest.TestCase):
    def test_g_pro_wireless_captured_battery_report(self):
        # Captured from receiver 046d:c539, paired device index 1.
        # Feature 0x1001 resolves to index 6; 0x1000 and 0x1004 are absent.
        report = bytes.fromhex("11 01 06 0d 0e 90 00 00 00 00 00 00 00 00 00 00 00 00 00 00")
        self.assertEqual(
            monitor.decode_battery_report(monitor.BATTERY_VOLTAGE_FEATURE, report),
            (23, False),
        )

    def test_g915_tkl_uses_battery_voltage(self):
        self.assertIn((0xC545, 0xC343, "keyboard", 9, "G915 TKL"), monitor.DEVICES)
        self.assertEqual(
            monitor.BATTERY_FEATURE_BY_RECEIVER[0xC545],
            monitor.BATTERY_VOLTAGE_FEATURE,
        )

    def test_voltage_report_is_converted_to_percentage(self):
        report = [0x11, 0x01, 0x07, 0x00, 0x0E, 0xF9, 0x00]
        self.assertEqual(
            monitor.decode_battery_report(monitor.BATTERY_VOLTAGE_FEATURE, report),
            (55, False),
        )

        report[6] = 0x80
        self.assertEqual(
            monitor.decode_battery_report(monitor.BATTERY_VOLTAGE_FEATURE, report),
            (55, True),
        )

    def test_unified_battery_report_is_unchanged(self):
        report = [0x11, 0x01, 0x06, 0x10, 0x3C, 0x00, 0x02]
        self.assertEqual(
            monitor.decode_battery_report(monitor.UNIFIED_BATTERY_FEATURE, report),
            (60, True),
        )

    def test_voltage_query_uses_function_zero_with_the_monitor_id(self):
        bus, fd = single_device_bus(
            0xC545,
            FakePairedDevice(
                kind=0x00, battery_feature=BATTERY_VOLTAGE_FEATURE, voltage=3833
            ),
        )
        self.assertEqual(
            monitor.query_battery(
                fd,
                0x01,
                0x07,
                monitor.BATTERY_VOLTAGE_FEATURE,
            ),
            (55, False),
        )
        writes = [list(entry[4]) for entry in bus.log if entry[0] == "write"]
        battery_requests = [c for c in writes if c[0] == 0x11 and c[2] == 0x07]
        self.assertEqual(battery_requests[0][0:4], [0x11, 0x01, 0x07, 0x0d])


class BatteryStatusTests(unittest.TestCase):
    def test_mx_master_2s_captured_charging_report_has_approximate_level(self):
        # Captured from the MX Master 2S in Unifying slot 4. The mouse sends
        # level 0 while charging; Solaar's status approximation is 50%.
        report = bytes.fromhex("11 04 07 0b 00 00 01 00 00 00 00 00 00 00 00 00 00 00 00 00")
        self.assertEqual(
            monitor.decode_battery_report(monitor.BATTERY_STATUS_FEATURE, report),
            (50, True),
        )

    def test_battery_status_reports_direct_percentage_when_available(self):
        report = [0x11, 0x04, 0x07, 0x0b, 70, 0, 0]
        self.assertEqual(
            monitor.decode_battery_report(monitor.BATTERY_STATUS_FEATURE, report),
            (70, False),
        )
        # On this device a zero level with FULL status is an approximation.
        report[4], report[6] = 0, 3
        self.assertEqual(
            monitor.decode_battery_report(monitor.BATTERY_STATUS_FEATURE, report),
            (90, True),
        )

    def test_status_query_uses_function_zero_in_the_paired_slot(self):
        bus, fd = single_device_bus(
            0x4069,
            FakePairedDevice(kind=0x03, battery_feature=BATTERY_STATUS_FEATURE,
                             battery=0, charging=True, wpid=0x4069,
                             pairing_slot=4),
        )
        self.assertEqual(monitor.find_paired_index(fd, 0x4069), 4)
        self.assertEqual(monitor.query_battery(fd, 4, 7, BATTERY_STATUS_FEATURE),
                         (50, True))
        writes = [list(entry[4]) for entry in bus.log if entry[0] == "write"]
        self.assertIn([0x11, 4, 7, 0x0d], [c[:4] for c in writes])


class FakeBusTests(unittest.TestCase):
    def test_enumerate_filters_by_ids_and_returns_the_hidapi_fields(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/a", 0xC547, FakePairedDevice(kind=0x00, battery=50), serial="A1"),
            FakeNode(b"/dev/b", 0xC54D, FakePairedDevice(kind=0x03, battery=50), serial="B1"),
        ])
        found = bus.enumerate(0x046D, 0xC547)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["path"], b"/dev/a")
        self.assertEqual(found[0]["usage_page"], 0xFF00)
        self.assertEqual(found[0]["serial_number"], "A1")
        self.assertEqual(bus.enumerate(0x046D, 0x9999), [])

    def test_product_id_zero_matches_every_product(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/a", 0xC547, FakePairedDevice(kind=0x00, battery=50)),
            FakeNode(b"/dev/b", 0xC54D, FakePairedDevice(kind=0x03, battery=50)),
        ])
        self.assertEqual(
            sorted(d["path"] for d in bus.enumerate(0x046D, 0)),
            [b"/dev/a", b"/dev/b"])

    def test_every_open_descriptor_receives_its_own_copy(self):
        bus, first = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=50))
        second = bus.device()
        second.open_path(b"/dev/receiver")

        first.write([0x10, 0x01, 0x00, 0x0D, 0x10, 0x04, 0x00])
        report_first = first.read(64, timeout_ms=100)
        report_second = second.read(64, timeout_ms=100)
        self.assertEqual(report_first, report_second)
        self.assertEqual(report_first[4], 0x06)

        self.assertEqual(second.read(64, timeout_ms=20), [])

    def test_nodes_do_not_share_reports(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/a", 0xC547, FakePairedDevice(kind=0x00, battery=50)),
            FakeNode(b"/dev/b", 0xC54D, FakePairedDevice(kind=0x03, battery=50)),
        ])
        fd_a = bus.device()
        fd_a.open_path(b"/dev/a")
        fd_b = bus.device()
        fd_b.open_path(b"/dev/b")

        fd_a.write([0x10, 0x01, 0x00, 0x0D, 0x10, 0x04, 0x00])
        self.assertTrue(fd_a.read(64, timeout_ms=100))
        self.assertEqual(fd_b.read(64, timeout_ms=20), [])

    def test_a_closed_descriptor_refuses_further_use(self):
        _bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=50))
        fd.close()
        with self.assertRaises(OSError):
            fd.read(64, timeout_ms=10)
        with self.assertRaises(OSError):
            fd.write([0x10])

    def test_an_empty_read_waits_for_its_timeout(self):
        _bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=50))
        started = time.monotonic()
        self.assertEqual(fd.read(64, timeout_ms=120), [])
        self.assertGreaterEqual(time.monotonic() - started, 0.1)

    def test_an_injected_report_wakes_a_blocked_read(self):
        bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=42))
        result = {}

        def blocked_read():
            result["report"] = fd.read(64, timeout_ms=2000)

        reader = threading.Thread(target=blocked_read)
        reader.start()
        time.sleep(0.05)
        started = time.monotonic()
        bus.emit_battery_event(b"/dev/receiver", battery=42)
        reader.join(timeout=1)
        self.assertFalse(reader.is_alive())
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(result["report"][4], 42)

    def test_a_sleeping_device_does_not_answer(self):
        bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=50, awake=False))
        fd.write([0x10, 0x01, 0x00, 0x0D, 0x10, 0x04, 0x00])
        self.assertEqual(fd.read(64, timeout_ms=30), [])
        self.assertEqual(bus.protocol_errors, [])

    def test_an_absent_feature_resolves_to_index_zero(self):
        _bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=50))
        fd.write([0x10, 0x01, 0x00, 0x0D, 0x10, 0x00, 0x00])
        report = fd.read(64, timeout_ms=100)
        self.assertEqual(report[4], 0x00)

    def test_stop_wakes_readers_and_silences_the_bus(self):
        bus, fd = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=50))
        result = {}

        def blocked_read():
            result["report"] = fd.read(64, timeout_ms=5000)

        reader = threading.Thread(target=blocked_read)
        reader.start()
        time.sleep(0.05)
        bus.stop()
        reader.join(timeout=1)
        self.assertFalse(reader.is_alive())
        self.assertEqual(result["report"], [])


class ForeignResponseTests(unittest.TestCase):
    def test_a_foreign_root_response_cannot_replace_the_requested_feature(self):
        for order_name in ("monitor_first", "probe_first"):
            with self.subTest(order=order_name):
                self._run_ordered_lookup(order_name)

    def _run_ordered_lookup(self, order_name):
        bus, fd_monitor = single_device_bus(
            0xC547, FakePairedDevice(kind=0x03, battery=50),
            path=b"/dev/shared")
        fd_probe = bus.device()
        fd_probe.open_path(b"/dev/shared")
        gate = bus.hold_responses(b"/dev/shared", 2, is_root_lookup)
        results = {}

        def monitor_lookup():
            results["monitor"] = monitor.get_feature_index(fd_monitor, 0x01, 0x1004)

        def probe_lookup():
            results["probe"] = monitor.get_feature_index(
                fd_probe, 0x01, 0x0005, swid=0x0E)

        threads = [threading.Thread(target=monitor_lookup),
                   threading.Thread(target=probe_lookup)]
        for thread in threads:
            thread.start()
        gate.wait_until_full(3)

        monitor_index = next(
            i for i, request in enumerate(gate.requests()) if request[3] == 0x0D)
        probe_index = 1 - monitor_index
        if order_name == "monitor_first":
            gate.release([monitor_index, probe_index])
        else:
            gate.release([probe_index, monitor_index])
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(results["monitor"], 0x06)
        self.assertEqual(results["probe"], 0x03)


class BusIntegrationTests(unittest.TestCase):
    def test_mx_master_2s_on_unifying_slot_four_updates_mouse_panel(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/mx2s", 0x4069,
                     FakePairedDevice(kind=0x03,
                                      battery_feature=BATTERY_STATUS_FEATURE,
                                      battery=0, charging=True, wpid=0x4069,
                                      pairing_slot=4), device_idx=4),
        ])
        with DaemonHarness(bus) as harness:
            threads = harness.module.monitor_device(0x4069, None, "mouse", 10)
            self.assertEqual(len(threads), 1)
            harness.threads.extend(threads)
            harness.wait_for_state("mouse", "50\n1\n1\nMX Master 2S")
            bus.emit_battery_event(b"/dev/mx2s", battery=70, charging=False)
            harness.wait_for_state("mouse", "70\n1\n0\nMX Master 2S")
            report = harness.stop()
            self.assertEqual(report["alive"], [])
            self.assertEqual(report["protocol_errors"], [])

    def test_g_pro_wireless_voltage_updates_and_sleep_wake(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/gpro", 0xC539, FakePairedDevice(
                kind=0x03, battery_feature=BATTERY_VOLTAGE_FEATURE,
                voltage=3728, battery_index=0x06)),
        ])
        with DaemonHarness(bus) as harness:
            # Use the production entry point so this also exercises the PID
            # table's battery-feature selection, not a test-supplied feature.
            harness.threads.extend(harness.module.monitor_device(0xC539, 0xC088, "mouse", 10))
            harness.wait_for_state("mouse", "23\n1\n0\nG Pro Wireless")
            bus.emit_battery_event(b"/dev/gpro", voltage=3833, charging=True)
            harness.wait_for_state("mouse", "55\n1\n1\nG Pro Wireless")
            bus.emit_link(b"/dev/gpro", off=True)
            harness.wait_for_state("mouse", "0\n0\n0\n")
            bus.emit_link(b"/dev/gpro", off=False)
            harness.wait_for_state("mouse", "55\n1\n1\nG Pro Wireless")
            report = harness.stop()
            self.assertEqual(report["alive"], [])
            self.assertEqual(report["protocol_errors"], [])

    def test_a_c539_mouse_without_battery_voltage_publishes_nothing(self):
        # Receiver 0xC539 is shared by other LIGHTSPEED mice. One that lacks
        # feature 0x1001 answers the lookup with index 0, so the daemon must
        # stay silent: no state file, no protocol error, no crash.
        bus = FakeHidBus([
            FakeNode(b"/dev/other", 0xC539, FakePairedDevice(
                kind=0x03, battery_feature=UNIFIED_BATTERY_FEATURE, battery=60)),
        ])
        with DaemonHarness(bus) as harness:
            harness.threads.extend(harness.module.monitor_device(0xC539, 0xC088, "mouse", 10))
            # Wait for the daemon to ask ROOT for 0x1001 on this node, so the
            # assertion below runs after the lookup was answered, not before.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if any(kind == "write" and path == b"/dev/other"
                       and req[0] == 0x10 and req[2] == 0x00 and req[4:6] == b"\x10\x01"
                       for kind, _, path, _, req in bus.log):
                    break
                time.sleep(0.02)
            else:
                self.fail("the daemon never looked up feature 0x1001")
            time.sleep(0.3)
            self.assertFalse(harness.state_file("mouse").exists())
            report = harness.stop()
            self.assertEqual(report["alive"], [])
            self.assertEqual(report["protocol_errors"], [])

    def test_g_pro_wired_voltage_updates(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/gpro-wired", 0xC088, FakePairedDevice(
                kind=0x03, battery_feature=BATTERY_VOLTAGE_FEATURE,
                voltage=3833, charging=True, battery_index=0x06), device_idx=0xFF),
        ])
        with DaemonHarness(bus) as harness:
            harness.threads.extend(harness.module.monitor_device(0xC539, 0xC088, "mouse", 10))
            harness.wait_for_state("mouse", "55\n1\n1\nG Pro Wireless\nwired")
            bus.emit_battery_event(b"/dev/gpro-wired", voltage=4186, charging=False)
            harness.wait_for_state("mouse", "100\n1\n0\nG Pro Wireless\nwired")
            report = harness.stop()
            self.assertEqual(report["alive"], [])
            self.assertEqual(report["protocol_errors"], [])

    def test_each_shared_receiver_publishes_its_own_battery(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/kbd", 0xC547,
                     FakePairedDevice(kind=0x00, battery=59), serial="K1"),
            FakeNode(b"/dev/mouse", 0xC547,
                     FakePairedDevice(kind=0x03, battery=41), serial="M1"),
        ])
        with DaemonHarness(bus) as harness:
            barrier = threading.Barrier(2)
            harness.start_wireless(0xC547, "keyboard", 9, barrier=barrier)
            harness.start_wireless(0xC547, "mouse", 10, barrier=barrier)
            harness.wait_for_state("keyboard", "59\n1\n0\n")
            harness.wait_for_state("mouse", "41\n1\n0\n")

            report = harness.stop()
            self.assertEqual(report["alive"], [])
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["protocol_errors"], [])
            self.assertEqual(
                harness.state_file("keyboard").read_text(), "59\n1\n0\n")
            self.assertEqual(
                harness.state_file("mouse").read_text(), "41\n1\n0\n")

    def test_an_injected_battery_event_updates_the_state(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/sl2", 0xC54D,
                     FakePairedDevice(kind=0x03, battery=50)),
        ])
        with DaemonHarness(bus) as harness:
            harness.start_wireless(0xC54D, "mouse", 10)
            harness.wait_for_state("mouse", "50\n1\n0\n")
            harness.wait_for_open(b"/dev/sl2")

            bus.emit_battery_event(b"/dev/sl2", battery=37)
            harness.wait_for_state("mouse", "37\n1\n0\n")

    def test_the_published_model_follows_the_selected_source(self):
        # Two mice, one logical "mouse" file: the lowest battery wins, and the
        # model line must be the one of THAT source — a G Pro number under a
        # Superlight name would be a wrong reading, not a cosmetic slip.
        bus = FakeHidBus([
            FakeNode(b"/dev/gpro", 0xC539, FakePairedDevice(
                kind=0x03, battery_feature=BATTERY_VOLTAGE_FEATURE,
                voltage=3833, battery_index=0x06)),
            FakeNode(b"/dev/sl2", 0xC54D,
                     FakePairedDevice(kind=0x03, battery=30)),
        ])
        with DaemonHarness(bus) as harness:
            harness.threads.extend(harness.module.monitor_device(0xC539, 0xC088, "mouse", 10))
            harness.threads.extend(harness.module.monitor_device(0xC54D, 0xC09B, "mouse", 10))
            harness.wait_for_state("mouse", "30\n1\n0\nPRO X Superlight 2")
            bus.emit_battery_event(b"/dev/sl2", battery=90)
            harness.wait_for_state("mouse", "55\n1\n0\nG Pro Wireless")
            bus.emit_link(b"/dev/gpro", off=True)
            harness.wait_for_state("mouse", "90\n1\n0\nPRO X Superlight 2")
            bus.emit_link(b"/dev/sl2", off=True)
            harness.wait_for_state("mouse", "0\n0\n0\n")
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_a_model_only_change_is_published(self):
        # Same battery, same charging on both sources: when the shown source
        # goes away the number does not move, only the name does — and the
        # dedup must still publish, or the file names a mouse that is gone.
        bus = FakeHidBus([
            FakeNode(b"/dev/gpro", 0xC539, FakePairedDevice(
                kind=0x03, battery_feature=BATTERY_VOLTAGE_FEATURE,
                voltage=3811, battery_index=0x06)),   # 50%
            FakeNode(b"/dev/sl2", 0xC54D,
                     FakePairedDevice(kind=0x03, battery=50)),
        ])
        with DaemonHarness(bus) as harness:
            harness.threads.extend(harness.module.monitor_device(0xC54D, 0xC09B, "mouse", 10))
            harness.wait_for_state("mouse", "50\n1\n0\nPRO X Superlight 2")
            harness.threads.extend(harness.module.monitor_device(0xC539, 0xC088, "mouse", 10))
            # Equal batteries keep the first source on the file, so the second
            # one is invisible there: wait on the daemon's own source table.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with harness.module.publish_lock:
                    sources = dict(harness.module.source_states.get("mouse", {}))
                if sum(1 for src in sources.values() if src[1]) == 2:
                    break
                time.sleep(0.02)
            else:
                self.fail(f"second source never connected: {sources}")
            bus.emit_link(b"/dev/sl2", off=True)
            harness.wait_for_state("mouse", "50\n1\n0\nG Pro Wireless")
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_a_shared_receiver_names_each_device_through_the_table(self):
        # c547 carries a keyboard and a mouse; through the production entry
        # point each logical file gets the model of its own table entry.
        bus = FakeHidBus([
            FakeNode(b"/dev/kbd", 0xC547,
                     FakePairedDevice(kind=0x00, battery=59), serial="K1"),
            FakeNode(b"/dev/mouse", 0xC547,
                     FakePairedDevice(kind=0x03, battery=41), serial="M1"),
        ])
        with DaemonHarness(bus) as harness:
            harness.threads.extend(harness.module.monitor_device(0xC547, 0xC357, "keyboard", 9))
            harness.threads.extend(harness.module.monitor_device(0xC547, 0xC094, "mouse", 10))
            harness.wait_for_state("keyboard", "59\n1\n0\nG915 X TKL")
            harness.wait_for_state("mouse", "41\n1\n0\nPRO X Superlight")
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_a_cable_only_line_monitors_the_cable_alone(self):
        # The G502 X LIGHTSPEED's receiver side is the c547 line; its own
        # line has no receiver, so it must start no wireless monitor.
        bus = FakeHidBus([
            FakeNode(b"/dev/g502-wired", 0xC098, FakePairedDevice(
                kind=0x03, battery=41, charging=True), device_idx=0xFF),
        ])
        with DaemonHarness(bus) as harness:
            threads = harness.module.monitor_device(None, 0xC098, "mouse", 10)
            harness.threads.extend(threads)
            self.assertEqual(len(threads), 1)
            harness.wait_for_state("mouse", "41\n1\n1\nG502 X LIGHTSPEED\nwired")
            bus.emit_battery_event(b"/dev/g502-wired", battery=44, charging=True)
            harness.wait_for_state("mouse", "44\n1\n1\nG502 X LIGHTSPEED\nwired")
            report = harness.stop()
            self.assertEqual(report["alive"], [])
            self.assertEqual(report["protocol_errors"], [])

    def test_a_listed_wireless_pid_names_the_paired_mouse(self):
        # c547 pairs more than one mouse model, and the table names only one.
        # The receiver's pairing register says which mouse is paired. The
        # sheet's reply follows one captured from a G502 X LIGHTSPEED:
        # 11 ff 83 b5 20 07 08 40 9f 11 00 02 ...
        bus = FakeHidBus([
            FakeNode(b"/dev/kbd", 0xC547,
                     FakePairedDevice(kind=0x00, battery=59), serial="K1"),
            FakeNode(b"/dev/mouse", 0xC547,
                     FakePairedDevice(kind=0x03, battery=41, wpid=0x409F),
                     serial="M1"),
        ])
        with DaemonHarness(bus) as harness:
            harness.threads.extend(harness.module.monitor_device(0xC547, 0xC357, "keyboard", 9))
            harness.threads.extend(harness.module.monitor_device(0xC547, 0xC094, "mouse", 10))
            harness.wait_for_state("keyboard", "59\n1\n0\nG915 X TKL")
            harness.wait_for_state("mouse", "41\n1\n0\nG502 X LIGHTSPEED")
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_the_paired_names_do_not_depend_on_the_receiver_order(self):
        # Same two c547 receivers as above, enumerated mouse first.
        bus = FakeHidBus([
            FakeNode(b"/dev/mouse", 0xC547,
                     FakePairedDevice(kind=0x03, battery=41, wpid=0x409F),
                     serial="M1"),
            FakeNode(b"/dev/kbd", 0xC547,
                     FakePairedDevice(kind=0x00, battery=59), serial="K1"),
        ])
        with DaemonHarness(bus) as harness:
            harness.threads.extend(harness.module.monitor_device(0xC547, 0xC357, "keyboard", 9))
            harness.threads.extend(harness.module.monitor_device(0xC547, 0xC094, "mouse", 10))
            harness.wait_for_state("keyboard", "59\n1\n0\nG915 X TKL")
            harness.wait_for_state("mouse", "41\n1\n0\nG502 X LIGHTSPEED")
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_a_keyboard_line_keeps_its_name_when_the_wpid_is_a_mouse(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/kbd", 0xC547,
                     FakePairedDevice(kind=0x00, battery=59, wpid=0x409F),
                     serial="K1"),
        ])
        with DaemonHarness(bus) as harness:
            harness.threads.extend(harness.module.monitor_device(0xC547, 0xC357, "keyboard", 9))
            harness.wait_for_state("keyboard", "59\n1\n0\nG915 X TKL")
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_the_cable_reading_keeps_the_name_of_its_cable(self):
        # The receiver has a G502 X paired, but the cable on this line is
        # the PRO X Superlight one (c094): its reading must not carry the
        # paired name.
        bus = FakeHidBus([
            FakeNode(b"/dev/mouse", 0xC547,
                     FakePairedDevice(kind=0x03, battery=41, wpid=0x409F),
                     serial="M1"),
        ])
        with DaemonHarness(bus) as harness:
            harness.threads.extend(harness.module.monitor_device(0xC547, 0xC094, "mouse", 10))
            harness.wait_for_state("mouse", "41\n1\n0\nG502 X LIGHTSPEED")
            bus.plug(FakeNode(b"/dev/sl-wired", 0xC094, FakePairedDevice(
                kind=0x03, battery=77, charging=True), device_idx=0xFF))
            harness.wait_for_state("mouse", "77\n1\n1\nPRO X Superlight\nwired rx-15d2d89c")
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_a_new_pairing_renames_the_mouse_on_the_next_wake(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/mouse", 0xC547,
                     FakePairedDevice(kind=0x03, battery=41, wpid=0x4093),
                     serial="M1"),
        ])
        with DaemonHarness(bus) as harness:
            # Both monitors, as in production: with no cable node the wired
            # monitor must stay silent, or its requeries swallow the link
            # events below.
            state = harness._fresh_state(0xC547, 0xC094)
            state['_model'] = "PRO X Superlight"
            lock = threading.Lock()
            harness.start_wireless(0xC547, "mouse", 10, state=state, state_lock=lock)
            harness.start_wired(0xC094, "mouse", 10, state=state, state_lock=lock)
            harness.wait_for_state("mouse", "41\n1\n0\nPRO X Superlight")
            harness.wait_for_open(b"/dev/mouse")

            bus.emit_link(b"/dev/mouse", off=True)
            harness.wait_for_state("mouse", "0\n0\n0\n")
            bus.nodes[b"/dev/mouse"].device.wpid = 0x409F
            bus.emit_link(b"/dev/mouse", off=False)
            harness.wait_for_state("mouse", "41\n1\n0\nG502 X LIGHTSPEED")

            bus.emit_link(b"/dev/mouse", off=True)
            harness.wait_for_state("mouse", "0\n0\n0\n")
            bus.nodes[b"/dev/mouse"].device.wpid = 0x4093
            bus.emit_link(b"/dev/mouse", off=False)
            harness.wait_for_state("mouse", "41\n1\n0\nPRO X Superlight")
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_with_no_cable_the_receiver_hears_nothing_after_startup(self):
        # The wired monitor polls every 0.5 s while no cable is plugged. A
        # cable that was never there is not a cable that was unplugged: the
        # receiver must not be re-armed and re-queried on every pass.
        bus = FakeHidBus([
            FakeNode(b"/dev/sl2", 0xC54D,
                     FakePairedDevice(kind=0x03, battery=50)),
        ])
        with DaemonHarness(bus) as harness:
            harness.start_device(0xC54D, 0xC09B, "mouse", 10)
            harness.wait_for_state("mouse", "50\n1\n0\n")
            harness.wait_for_open(b"/dev/sl2")
            time.sleep(0.3)  # let the startup cycle finish its reads

            def receiver_writes():
                return [entry for entry in bus.log
                        if entry[0] == "write" and entry[2] == b"/dev/sl2"]

            before = len(receiver_writes())
            time.sleep(2.5)
            self.assertEqual(receiver_writes()[before:], [])
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_a_lost_startup_reply_is_queried_again(self):
        # With no cable there is no periodic requery any more, so a battery
        # reply lost at startup must be retried, or the widget stays hidden:
        # battery events need the feature index that only a query resolves.
        device = FakePairedDevice(kind=0x03, battery=50)
        bus = FakeHidBus([FakeNode(b"/dev/sl2", 0xC54D, device)])
        bus.intercept_next(
            b"/dev/sl2",
            lambda cmd: cmd[0] == 0x11 and cmd[2] == device.battery_index,
            lambda _request, _reply: [])
        with DaemonHarness(bus) as harness:
            harness.start_device(0xC54D, 0xC09B, "mouse", 10)
            harness.wait_for_state("mouse", "50\n1\n0\n", timeout=8.0)
            bus.emit_battery_event(b"/dev/sl2", battery=49)
            harness.wait_for_state("mouse", "49\n1\n0\n")
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_a_lost_reply_after_a_wake_is_queried_again(self):
        device = FakePairedDevice(kind=0x03, battery=50)
        bus = FakeHidBus([FakeNode(b"/dev/sl2", 0xC54D, device)])
        with DaemonHarness(bus) as harness:
            harness.start_device(0xC54D, 0xC09B, "mouse", 10)
            harness.wait_for_state("mouse", "50\n1\n0\n")
            harness.wait_for_open(b"/dev/sl2")
            bus.emit_link(b"/dev/sl2", off=True)
            harness.wait_for_state("mouse", "0\n0\n0\n")
            bus.intercept_next(
                b"/dev/sl2",
                lambda cmd: cmd[0] == 0x11 and cmd[2] == device.battery_index,
                lambda _request, _reply: [])
            bus.emit_link(b"/dev/sl2", off=False)
            harness.wait_for_state("mouse", "50\n1\n0\n", timeout=8.0)
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_the_retry_delay_doubles_up_to_its_cap(self):
        monitor = load_monitor_module(FakeHidBus([]))
        delays = [monitor.REQUERY_FIRST_DELAY]
        for _ in range(8):
            delays.append(monitor.next_requery_delay(delays[-1]))
        self.assertEqual(delays[:3], [2.0, 4.0, 8.0])
        self.assertEqual(delays[-1], monitor.REQUERY_MAX_DELAY)

    def test_no_cable_never_flags_a_cable_disconnect(self):
        bus = FakeHidBus([])
        with DaemonHarness(bus) as harness:
            state = harness.start_wired(0xC09B, "mouse", 10)
            time.sleep(1.2)
            self.assertFalse(state['wired_just_disconnected'])
            report = harness.stop()
            self.assertEqual(report["errors"], [])

    def test_a_cable_unplug_brings_the_wireless_reading_back(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/sl2", 0xC54D,
                     FakePairedDevice(kind=0x03, battery=50)),
            FakeNode(b"/dev/sl2-wired", 0xC09B, FakePairedDevice(
                kind=0x03, battery=77, charging=True), device_idx=0xFF),
        ])
        with DaemonHarness(bus) as harness:
            harness.start_device(0xC54D, 0xC09B, "mouse", 10)
            harness.wait_for_state("mouse", "77\n1\n1\n\nwired rx-5cde0fe3")
            bus.unplug(b"/dev/sl2-wired")
            harness.wait_for_state("mouse", "50\n1\n0\n")
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_a_cable_that_connects_during_the_requery_keeps_its_reading(self):
        # After a cable unplug the wireless monitor re-queries the receiver.
        # If the cable comes back while that query is in flight, the late
        # wireless reading must not replace the cable one. The wired monitor
        # is played by hand so the interleaving is fixed.
        bus = FakeHidBus([
            FakeNode(b"/dev/sl2", 0xC54D,
                     FakePairedDevice(kind=0x03, battery=50)),
        ])
        with DaemonHarness(bus) as harness:
            lock = threading.Lock()
            state = harness._fresh_state(0xC54D, 0xC09B)
            harness.start_wireless(0xC54D, "mouse", 10, state=state, state_lock=lock)
            harness.wait_for_state("mouse", "50\n1\n0\n")
            harness.wait_for_open(b"/dev/sl2")
            time.sleep(0.3)  # let the startup cycle finish its reads

            gate = bus.hold_responses(b"/dev/sl2", 1, is_root_lookup)
            with lock:
                state['wired_just_disconnected'] = True
            gate.wait_until_full(timeout=5)

            with lock:
                state['wired_connected'] = True
                harness.module.write_state("mouse", 77, True, True, 10, state)
            gate.release()

            # The wireless monitor closes the receiver once it sees the
            # cable, which is after the requery has published or not.
            deadline = time.monotonic() + 5
            while b"/dev/sl2" in bus.open_paths():
                self.assertLess(time.monotonic(), deadline, "the receiver stayed open")
                time.sleep(0.02)
            self.assertEqual(harness.state_file("mouse").read_text(), "77\n1\n1\n\nwired rx-5cde0fe3")
            report = harness.stop()
            self.assertEqual(report["protocol_errors"], [])

    def test_link_events_toggle_the_published_state(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/sl2", 0xC54D,
                     FakePairedDevice(kind=0x03, battery=50)),
        ])
        with DaemonHarness(bus) as harness:
            harness.start_wireless(0xC54D, "mouse", 10)
            harness.wait_for_state("mouse", "50\n1\n0\n")
            harness.wait_for_open(b"/dev/sl2")

            bus.emit_link(b"/dev/sl2", off=True)
            harness.wait_for_state("mouse", "0\n0\n0\n")

            bus.emit_link(b"/dev/sl2", off=False)
            harness.wait_for_state("mouse", "50\n1\n0\n")

    def test_a_wired_device_uses_the_direct_device_index(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/wired", 0xC09B,
                     FakePairedDevice(kind=0x03, battery=66), device_idx=0xFF),
        ])
        with DaemonHarness(bus) as harness:
            harness.start_wired(0xC09B, "mouse", 10)
            harness.wait_for_state("mouse", "66\n1\n0\n\nwired")

        writes = [list(entry[4]) for entry in bus.log if entry[0] == "write"]
        hidpp_requests = [c for c in writes if c[0] in (0x10, 0x11) and c[2] != 0x80]
        self.assertTrue(hidpp_requests)
        self.assertTrue(all(c[1] == 0xFF for c in hidpp_requests))


class SleepingReceiverTests(unittest.TestCase):
    def test_a_sleeping_receiver_publishes_no_state(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/shared", 0xC547,
                     FakePairedDevice(kind=0x03, battery=50, awake=False),
                     serial="S1"),
        ])
        with DaemonHarness(bus) as harness:
            harness.start_wireless(0xC547, "mouse", 10)

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                sessions = harness.probe_sessions(b"/dev/shared")
                if sessions and sessions[0][1] is not None:
                    break
                time.sleep(0.05)
            else:
                self.fail("the probe session never closed")

            time.sleep(0.2)
            self.assertEqual(len(harness.probe_sessions(b"/dev/shared")), 1)
            self.assertFalse(harness.state_file("mouse").exists())

            report = harness.stop()
            self.assertEqual(report["alive"], [])
            self.assertEqual(report["errors"], [])


class DeviceCoverageTests(unittest.TestCase):
    EMULATED_SHEETS = {
        (0x4069, None): ("mouse", 10, FakePairedDevice(
            kind=0x03, battery_feature=BATTERY_STATUS_FEATURE,
            battery=0, charging=True, wpid=0x4069, pairing_slot=4)),
        (0xC539, 0xC088): ("mouse", 10, FakePairedDevice(
            kind=0x03, battery_feature=BATTERY_VOLTAGE_FEATURE,
            voltage=3728, battery_index=0x06)),
        (0xC545, 0xC343): ("keyboard", 9, FakePairedDevice(
            kind=0x00, battery_feature=BATTERY_VOLTAGE_FEATURE, voltage=3833)),
        (0xC547, 0xC357): ("keyboard", 9, FakePairedDevice(
            kind=0x00, battery=59)),
        (0xC547, 0xC094): ("mouse", 10, FakePairedDevice(
            kind=0x03, battery=41)),
        (0xC54D, 0xC09B): ("mouse", 10, FakePairedDevice(
            kind=0x03, battery=50)),
        (None, 0xC098): ("mouse", 10, FakePairedDevice(
            kind=0x03, battery=41, charging=True)),
    }

    def test_every_supported_device_has_an_emulated_sheet(self):
        declared = {(wireless, wired): (name, signal)
                    for wireless, wired, name, signal, _ in monitor.DEVICES}
        self.assertEqual(set(declared), set(self.EMULATED_SHEETS))
        for pids, (name, signal) in declared.items():
            self.assertEqual((name, signal), self.EMULATED_SHEETS[pids][:2])

    def test_every_model_is_a_plain_name_the_widgets_accept(self):
        # The widgets take the model line only when it is printable ASCII of
        # at most 48 characters (MODEL_RE in logibar-status); a model outside
        # that set would silently show as the generic name.
        import re
        plain = re.compile(r"[A-Za-z0-9 ._()+/-]{1,48}")
        models = [model for _, _, _, _, model in monitor.DEVICES]
        for model in models:
            self.assertIsNotNone(plain.fullmatch(model), model)
        self.assertEqual(len(models), len(set(models)))
        self.assertEqual(monitor.MODEL_BY_PIDS[(0xC539, 0xC088)], "G Pro Wireless")
        for kind, model in monitor.MODEL_BY_WPID.values():
            self.assertIn(kind, monitor.DEVICE_KIND)
            self.assertIsNotNone(plain.fullmatch(model), model)

    def test_a_cable_only_line_names_its_mouse_like_the_receiver_does(self):
        # The receiver side of a cable-only line is named by MODEL_BY_WPID;
        # the cable side by its own line. Both must give the same name.
        paired = set(monitor.MODEL_BY_WPID.values())
        cable_only = [(name, model) for wireless, _, name, _, model
                      in monitor.DEVICES if wireless is None]
        self.assertTrue(cable_only)
        for name, model in cable_only:
            self.assertIn((name, model), paired)

    def test_g915_tkl_and_g_pro_wireless_report_voltage(self):
        self.assertEqual(set(monitor.BATTERY_FEATURE_BY_RECEIVER), {0x4069, 0xC545, 0xC539})
        self.assertEqual(
            self.EMULATED_SHEETS[(0x4069, None)][2].battery_feature,
            BATTERY_STATUS_FEATURE,
        )
        self.assertEqual(
            self.EMULATED_SHEETS[(0xC545, 0xC343)][2].battery_feature,
            0x1001,
        )
        self.assertEqual(
            self.EMULATED_SHEETS[(0xC539, 0xC088)][2].battery_feature,
            0x1001,
        )


class HeadsetStateFileTests(unittest.TestCase):
    """The headset daemon writes the same four-line contract."""

    def _load(self, state_dir):
        original = sys.modules.get("hid")
        sys.modules["hid"] = types.SimpleNamespace()
        env = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = state_dir
        try:
            script = Path(__file__).parent.parent / "logibar-headset-monitor"
            ldr = SourceFileLoader("logibar_headset_monitor_test", str(script))
            sp = importlib.util.spec_from_loader(ldr.name, ldr)
            mod = importlib.util.module_from_spec(sp)
            ldr.exec_module(mod)
        finally:
            if original is None:
                sys.modules.pop("hid", None)
            else:
                sys.modules["hid"] = original
            if env is None:
                os.environ.pop("XDG_RUNTIME_DIR", None)
            else:
                os.environ["XDG_RUNTIME_DIR"] = env
        return mod

    def test_the_headset_publishes_its_model_only_while_connected(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = self._load(tmp)
            mod.subprocess = types.SimpleNamespace(run=lambda *a, **k: None)  # no pkill
            state = Path(tmp) / "logibar" / "headset"
            mod.write_state(64, True, False)
            self.assertEqual(state.read_text(), "64\n1\n0\nPRO X 2 LIGHTSPEED")
            first = state.stat().st_ino
            mod.write_state(64, True, False)
            self.assertEqual(state.stat().st_ino, first, "an identical state is not rewritten")
            mod.write_state(64, True, True)
            self.assertEqual(state.read_text(), "64\n1\n1\nPRO X 2 LIGHTSPEED")
            mod.write_state(0, False, False)
            self.assertEqual(state.read_text(), "0\n0\n0\n")
            self.assertRegex(mod.MODEL, r"^[A-Za-z0-9 ._()+/-]{1,48}$")


FAKE_DAEMON = """import os, signal, sys, time
state_dir = sys.argv[1]
def rescan(*_):
    tmp = os.path.join(state_dir, 'manifest.tmp')
    with open(tmp, 'w') as f:
        f.write('logibar-receivers-v1\\n')
    os.replace(tmp, os.path.join(state_dir, 'receiver-devices'))
signal.signal(signal.SIGUSR1, rescan)
open(os.path.join(state_dir, 'ready'), 'w').close()
while True:
    time.sleep(0.05)
"""


def pid_line(mod, pid):
    return f"{pid} {mod.process_start_time(pid)}\n"


class ClickRefreshTests(unittest.TestCase):
    """`--refresh` is what the panel runs on a click. It asks the daemon
    for a rescan and never opens a hidraw node itself."""

    def _module(self, tmp):
        bus = FakeHidBus([])
        mod = load_monitor_module(bus)
        mod.STATE_DIR = tmp
        return mod, bus

    def _spawn_daemon(self, tmp, name="logibar-hidpp-monitor", extra=()):
        script = Path(tmp) / name
        script.write_text(FAKE_DAEMON)
        proc = subprocess.Popen([sys.executable, str(script), tmp, *extra])
        self.addCleanup(proc.wait, 5)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 5
        while not (Path(tmp) / 'ready').exists():
            self.assertLess(time.monotonic(), deadline, "the fake daemon never started")
            time.sleep(0.02)
        return proc

    def test_a_refresh_signals_the_daemon_and_returns_once_it_rescanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, bus = self._module(tmp)
            proc = self._spawn_daemon(tmp)
            (Path(tmp) / mod.PID_FILE).write_text(pid_line(mod, proc.pid))
            self.assertEqual(mod.refresh_via_daemon(wait=5), 0)
            self.assertEqual((Path(tmp) / 'receiver-devices').read_text(),
                             'logibar-receivers-v1\n')
            self.assertIsNone(proc.poll())
        self.assertEqual(bus.log, [])

    def test_a_refresh_with_no_daemon_fails_fast_without_touching_hardware(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, bus = self._module(tmp)
            started = time.monotonic()
            self.assertEqual(mod.refresh_via_daemon(wait=5), 1)
            self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(bus.log, [])

    def test_a_stale_pid_file_never_signals_an_unrelated_process(self):
        # SIGUSR1's default action kills: a pid the kernel reused for another
        # program must not receive it.
        with tempfile.TemporaryDirectory() as tmp:
            mod, _ = self._module(tmp)
            other = self._spawn_daemon(tmp, name="some-other-program")
            (Path(tmp) / mod.PID_FILE).write_text(pid_line(mod, other.pid))
            self.assertEqual(mod.refresh_via_daemon(wait=1), 1)
            time.sleep(0.2)
            self.assertIsNone(other.poll())
            self.assertFalse((Path(tmp) / 'receiver-devices').exists())

    def test_a_pid_file_that_is_a_symlink_or_garbage_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, _ = self._module(tmp)
            proc = self._spawn_daemon(tmp)
            target = Path(tmp) / 'elsewhere'
            target.write_text(pid_line(mod, proc.pid))
            (Path(tmp) / mod.PID_FILE).symlink_to(target)
            self.assertIsNone(mod.running_daemon_pid())
            (Path(tmp) / mod.PID_FILE).unlink()
            (Path(tmp) / mod.PID_FILE).write_text(f"{proc.pid} --refresh\n")
            self.assertIsNone(mod.running_daemon_pid())
            (Path(tmp) / mod.PID_FILE).write_text(pid_line(mod, 1))
            self.assertIsNone(mod.running_daemon_pid())

    def test_a_reused_pid_with_another_start_time_is_never_signalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, _ = self._module(tmp)
            proc = self._spawn_daemon(tmp)
            start = int(mod.process_start_time(proc.pid))
            (Path(tmp) / mod.PID_FILE).write_text(f"{proc.pid} {start + 1}\n")
            self.assertIsNone(mod.running_daemon_pid())
            (Path(tmp) / mod.PID_FILE).write_text(f"{proc.pid}\n")
            self.assertIsNone(mod.running_daemon_pid(), "a pid with no start time")
            (Path(tmp) / mod.PID_FILE).write_text(pid_line(mod, proc.pid))
            self.assertEqual(mod.running_daemon_pid(), proc.pid)

    def test_a_program_that_only_names_the_monitor_file_is_never_signalled(self):
        # An editor open on the script has its path as argv[1].
        with tempfile.TemporaryDirectory() as tmp:
            mod, _ = self._module(tmp)
            self.assertTrue(mod.is_monitor_argv([b"/usr/bin/python3", b"/usr/bin/logibar-hidpp-monitor", b""]))
            self.assertTrue(mod.is_monitor_argv([b"/usr/bin/logibar-hidpp-monitor", b""]))
            self.assertTrue(mod.is_monitor_argv([b"python3", b"-I", b"-u", b"/usr/bin/logibar-hidpp-monitor", b""]))
            self.assertFalse(mod.is_monitor_argv([b"python3", b"-c", b"print(1)", b"logibar-hidpp-monitor"]))
            self.assertFalse(mod.is_monitor_argv([b"nvim", b"/usr/bin/logibar-hidpp-monitor", b""]))
            self.assertFalse(mod.is_monitor_argv([b"less", b"logibar-hidpp-monitor"]))

    def test_a_pid_that_is_another_refresh_is_never_signalled(self):
        # Another click's `--refresh` child has the daemon's name in argv but
        # no SIGUSR1 handler: the signal would kill it.
        with tempfile.TemporaryDirectory() as tmp:
            mod, _ = self._module(tmp)
            proc = self._spawn_daemon(tmp, extra=("--refresh",))
            (Path(tmp) / mod.PID_FILE).write_text(pid_line(mod, proc.pid))
            self.assertIsNone(mod.running_daemon_pid())
            self.assertEqual(mod.refresh_via_daemon(wait=1), 1)
            time.sleep(0.2)
            self.assertIsNone(proc.poll())

    def test_a_scan_request_runs_the_next_scan_without_waiting_the_interval(self):
        mod, _ = self._module(tempfile.gettempdir())
        mod.RESCAN_SETTLE_SECONDS = 0.05
        scans = []
        mod.scan_receivers = lambda: scans.append(time.monotonic())
        loop = threading.Thread(target=mod.receiver_scan_loop, daemon=True)
        loop.start()
        try:
            deadline = time.monotonic() + 3
            while not scans:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            mod.request_scan()
            while len(scans) < 2:
                self.assertLess(time.monotonic(), deadline, "the requested scan never ran")
                time.sleep(0.02)
        finally:
            mod.running = False
            loop.join(3)
        self.assertFalse(loop.is_alive())

    def test_a_monitor_publishing_a_change_asks_for_a_rescan(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod, _ = self._module(tmp)
            mod.subprocess = types.SimpleNamespace(run=lambda *a, **k: None,
                                                   DEVNULL=subprocess.DEVNULL)
            state = {'_source_id': (0xC54D, None), '_model': 'PRO X Superlight 2'}
            mod.write_state("mouse", 70, True, False, 10, state)
            self.assertTrue(mod.scan_requested.is_set())
            mod.scan_requested.clear()
            mod.write_state("mouse", 70, True, False, 10, state)
            self.assertFalse(mod.scan_requested.is_set(), "an unchanged reading rescanned")


class ReceiverDiscoveryTests(unittest.TestCase):
    def _status_with(self, files, flags=("--last-known",), ages=None):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "logibar"
            state.mkdir()
            for name, content in files.items():
                (state / name).write_text(content)
            for name, seconds in (ages or {}).items():
                then = time.time() - seconds
                os.utime(state / name, (then, then))
            env = os.environ.copy()
            env['XDG_RUNTIME_DIR'] = tmp
            done = subprocess.run([str(SCRIPT.parent / 'logibar-status'), '--json', *flags],
                                  env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(done.returncode, 0)
            return json.loads(done.stdout)

    def test_a_live_cable_replaces_the_receiver_row_it_no_longer_answers_on(self):
        # A mouse charging on its cable is still paired to the receiver, which
        # cannot reach it: the cable reading must show, not a dimmed
        # last-known value from the receiver.
        now = int(time.time())
        status = self._status_with({
            "receiver-devices": "logibar-receivers-v1\nrx-0a0b0c0d-1\tmouse\n",
            "rx-0a0b0c0d-1": f"88\n0\n0\nG Pro\n{now - 600}\n4079",
            "mouse": "41\n1\n1\nG Pro Wireless\nwired rx-0a0b0c0d",
        })
        self.assertEqual([(d['id'], d['battery'], d['charging']) for d in status['devices']],
                         [("mouse", 41, True)])

    def test_a_live_cable_keeps_a_sleeping_device_on_another_receiver(self):
        # A G Pro on its cable says nothing about an MX Master asleep on a
        # Unifying receiver: its recent reading stays.
        now = int(time.time())
        status = self._status_with({
            "receiver-devices": "logibar-receivers-v1\nrx-0a0b0c0d-1\tmouse\nrx-11223344-1\tmouse\n",
            "rx-0a0b0c0d-1": f"88\n0\n0\nG Pro\n{now - 600}\n4079",
            "rx-11223344-1": f"55\n0\n0\nMX Master 2S\n{now - 600}\n4069",
            "mouse": "41\n1\n1\nG Pro Wireless\nwired rx-0a0b0c0d",
        })
        self.assertEqual(sorted((d['id'], d['state']) for d in status['devices']),
                         [("mouse", "ok"), ("rx-11223344-1", "last_known")])

    def test_a_device_whose_link_came_up_is_connecting_for_a_while(self):
        now = int(time.time())
        files = {
            "receiver-devices": "logibar-receivers-v1\nrx-11223344-1\tmouse\n",
            "rx-11223344-1": f"55\n0\n0\nM720 Triathlon\n{now - 600}\n405e\nconnecting",
        }
        status = self._status_with(files, flags=())
        [device] = status['devices']
        self.assertEqual((device['state'], device['battery'], device['connected']),
                         ("connecting", None, False))
        self.assertEqual(status['aggregate']['connected_count'], 0)
        # The same mark 31 s old: a daemon that stopped mid-scan.
        [device] = self._status_with(files, flags=(), ages={"rx-11223344-1": 31})['devices']
        self.assertEqual(device['state'], "disconnected")
        # A reading wins over the mark.
        files["rx-11223344-1"] = f"55\n1\n0\nM720 Triathlon\n{now}\n405e\nconnecting"
        [device] = self._status_with(files, flags=())['devices']
        self.assertEqual((device['state'], device['battery']), ("ok", 55))

    def test_without_last_known_a_silent_device_is_disconnected(self):
        # The default: the receiver cannot tell asleep from off, and a device
        # that does not answer goes, like one that is off.
        now = int(time.time())
        files = {
            "receiver-devices": "logibar-receivers-v1\nrx-11223344-1\tmouse\n",
            "rx-11223344-1": f"55\n0\n0\nMX Master 2S\n{now - 600}\n4069",
        }
        [device] = self._status_with(files, flags=())['devices']
        self.assertEqual((device['state'], device['last_known_battery']), ("disconnected", None))
        [device] = self._status_with(files)['devices']
        self.assertEqual((device['state'], device['last_known_battery']), ("last_known", 55))

    def test_a_cable_with_no_receiver_of_its_family_hides_no_row(self):
        now = int(time.time())
        status = self._status_with({
            "receiver-devices": "logibar-receivers-v1\nrx-11223344-1\tmouse\n",
            "rx-11223344-1": f"55\n0\n0\nMX Master 2S\n{now - 600}\n4069",
            "mouse": "41\n1\n1\nG Pro Wireless\nwired",
        })
        self.assertEqual(sorted(d['id'] for d in status['devices']),
                         ["mouse", "rx-11223344-1"])

    def test_a_live_cable_keeps_another_device_of_its_kind_that_answers(self):
        status = self._status_with({
            "receiver-devices": "logibar-receivers-v1\nrx-0a0b0c0d-2\tmouse\n",
            "rx-0a0b0c0d-2": f"60\n1\n0\nMX Master\n{int(time.time())}\n4041",
            "mouse": "41\n1\n1\nG Pro Wireless\nwired",
        })
        self.assertEqual(sorted(d['id'] for d in status['devices']),
                         ["mouse", "rx-0a0b0c0d-2"])

    def test_a_wireless_category_reading_never_doubles_a_receiver_row(self):
        status = self._status_with({
            "receiver-devices": "logibar-receivers-v1\nrx-0a0b0c0d-1\tmouse\n",
            "rx-0a0b0c0d-1": f"70\n1\n0\nPRO X 2\n{int(time.time())}\n40a9",
            "mouse": "70\n1\n0\nPRO X Superlight 2",
        })
        self.assertEqual([d['id'] for d in status['devices']], ["rx-0a0b0c0d-1"])

    def _late_reply_bus(self):
        mouse = FakePairedDevice(kind=0x03, battery=70, wpid=0x40A9,
                                 name="PRO X 2")
        return FakeHidBus([FakeNode(b"/dev/sl2", 0xC54D, mouse)])

    def test_a_late_battery_lookup_reply_is_never_taken_for_the_next_request(self):
        # Live on a PRO X 2 at 70 %, discovery published 7: the reply to the
        # 0x1004 lookup came after its 200 ms window, the 0x1000 lookup with
        # the same software id adopted it, and function 0 on 0x1004 is
        # getCapabilities, whose first byte (0x07) passed for a percentage.
        bus = self._late_reply_bus()
        mod = load_monitor_module(bus)
        mod.RECEIVER_SLOTS = {0xC54D: 1}

        def is_battery_lookup(cmd):
            # ROOT getFeature for anything but the name feature.
            return cmd[0] == 0x10 and cmd[2] == 0x00 and (cmd[4], cmd[5]) != (0x00, 0x05)

        # Pen the first battery lookup's reply AND the next lookup's, then
        # deliver them in send order: the late reply lands first, while the
        # second request is the one waiting.
        gate = bus.hold_responses(b"/dev/sl2", 2, is_battery_lookup)
        with tempfile.TemporaryDirectory() as tmp:
            mod.STATE_DIR = os.path.join(tmp, "logibar")
            mod.HIDRAW_CLASS_DIR = os.path.join(tmp, "no-hidraw")
            result = {}
            scanner = threading.Thread(
                target=lambda: result.setdefault('devices', mod.scan_receivers()))
            scanner.start()
            gate.wait_until_full(timeout=10)
            gate.release()
            scanner.join(20)
            self.assertFalse(scanner.is_alive())
            [device] = result['devices']
            self.assertEqual(device['battery'], 70)
            saved = (Path(mod.STATE_DIR) / device['id']).read_text().splitlines()
            self.assertEqual(saved[:2], ["70", "1"])
        self.assertEqual(bus.protocol_errors, [])

    def test_a_reply_late_by_several_requests_finds_no_one_waiting(self):
        # The rotation must outlast one device's whole exchange: with five
        # ids, the sixth request (the 0x1004 lookup) reused the first one's
        # id, took the late name-feature index for its own, and a 70 %
        # battery came out as another number.
        bus = self._late_reply_bus()
        mod = load_monitor_module(bus)
        mod.RECEIVER_SLOTS = {0xC54D: 1}
        first = mod.DISCOVERY_SWIDS[0]

        def is_lookup_with_first_id(cmd):
            return cmd[0] == 0x10 and cmd[2] == 0x00 and cmd[3] == first

        gate = bus.hold_responses(b"/dev/sl2", 2, is_lookup_with_first_id)
        with tempfile.TemporaryDirectory() as tmp:
            mod.STATE_DIR = os.path.join(tmp, "logibar")
            mod.HIDRAW_CLASS_DIR = os.path.join(tmp, "no-hidraw")
            result = {}
            scanner = threading.Thread(
                target=lambda: result.setdefault('devices', mod.scan_receivers()))
            scanner.start()
            deadline = time.monotonic() + 20
            while scanner.is_alive() and not gate._full.is_set():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            # Full: a later request reused the first id, and the late reply
            # lands while it waits. Not full: the late reply lands after the
            # scan, when no one waits.
            gate.release()
            scanner.join(20)
            self.assertFalse(scanner.is_alive())
            [device] = result['devices']
            self.assertEqual((device['name'], device['battery']), ("PRO X 2", 70))

    def test_discovery_and_the_monitor_share_a_receiver_without_crossing_readings(self):
        # The monitor thread keeps the receiver open and answers its own
        # requests and events while discovery scans the same node: every
        # reply reaches both descriptors.
        bus = self._late_reply_bus()
        with DaemonHarness(bus) as harness:
            mod = harness.module
            mod.RECEIVER_SLOTS = {0xC54D: 1}
            mod.STATE_DIR = str(harness.state_file("logibar"))
            os.makedirs(mod.STATE_DIR)
            harness.start_wireless(0xC54D, "mouse", 10)
            harness.wait_for_state("logibar/mouse", "70\n1\n0\n")
            for battery in (70, 64, 58):
                bus.emit_battery_event(b"/dev/sl2", battery=battery)
                [device] = mod.scan_receivers()
                self.assertEqual(device['battery'], battery)
                self.assertEqual(device['name'], "PRO X 2")
                harness.wait_for_state(
                    "logibar/mouse", f"{battery}\n1\n0\n")
            report = harness.stop()
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["protocol_errors"], [])

    def test_a_busy_pairing_read_does_not_drop_a_paired_device(self):
        bus = self._late_reply_bus()
        mod = load_monitor_module(bus)
        mod.RECEIVER_SLOTS = {0xC54D: 1}
        busy = bytes([0x10, 0xff, 0x8f, 0x83, 0xb5, 0x07, 0x00])
        bus.intercept_next(b"/dev/sl2",
                           lambda cmd: cmd[2] == 0x83 and cmd[4] == 0x20,
                           lambda request, report: [busy])
        with tempfile.TemporaryDirectory() as tmp:
            mod.STATE_DIR = os.path.join(tmp, "logibar")
            mod.HIDRAW_CLASS_DIR = os.path.join(tmp, "no-hidraw")
            [device] = mod.scan_receivers()
        self.assertEqual((device['name'], device['battery']), ("PRO X 2", 70))

    def _scan_with_one_busy_reply(self, predicate):
        bus = self._late_reply_bus()
        mod = load_monitor_module(bus)
        mod.RECEIVER_SLOTS = {0xC54D: 1}
        mod.BUSY_RETRY_SECONDS = 0.01

        def busy(request, report):
            # HID++ 2.0 error from the device: 0xff, the request's feature
            # index and function/software id, then ERR_BUSY (0x08).
            return [bytes([0x11, request[1], 0xff, request[2], request[3], 0x08]
                          + [0] * 14)]

        bus.intercept_next(b"/dev/sl2", predicate, busy)
        with tempfile.TemporaryDirectory() as tmp:
            mod.STATE_DIR = os.path.join(tmp, "logibar")
            mod.HIDRAW_CLASS_DIR = os.path.join(tmp, "no-hidraw")
            [device] = mod.scan_receivers()
        return device

    def test_a_feature_index_in_the_register_range_is_never_used(self):
        # A hostile device answers the 0x1004 lookup with index 0x82: the
        # battery read would go out as 11 xx 82 ..., a long-register write.
        bus = self._late_reply_bus()
        mod = load_monitor_module(bus)
        mod.RECEIVER_SLOTS = {0xC54D: 1}

        def hostile(request, report):
            reply = bytearray(report)
            reply[4] = 0x82
            return [bytes(reply)]

        bus.intercept_next(
            b"/dev/sl2",
            lambda cmd: cmd[0] == 0x10 and cmd[2] == 0x00 and (cmd[4], cmd[5]) == (0x10, 0x04),
            hostile)
        with tempfile.TemporaryDirectory() as tmp:
            mod.STATE_DIR = os.path.join(tmp, "logibar")
            mod.HIDRAW_CLASS_DIR = os.path.join(tmp, "no-hidraw")
            mod.scan_receivers()
        sent = [entry[4] for entry in bus.log if entry[0] == "write"]
        self.assertFalse([cmd for cmd in sent if len(cmd) > 2 and cmd[2] >= 0x80
                          and cmd[2] not in (0x81, 0x83)],
                         "a request went out with a register-write sub-id")

    def test_a_busy_identity_lookup_is_tried_again(self):
        device = self._scan_with_one_busy_reply(
            lambda cmd: cmd[0] == 0x10 and cmd[2] == 0x00 and (cmd[4], cmd[5]) == (0x00, 0x05))
        self.assertEqual((device['name'], device['battery']), ("PRO X 2", 70))

    def test_a_busy_battery_read_is_tried_again(self):
        device = self._scan_with_one_busy_reply(
            # 0x1004 sits at index 0x06 on the fake device; getStatus is function 1.
            lambda cmd: cmd[0] == 0x11 and cmd[2] == 0x06 and (cmd[3] & 0xf0) == 0x10)
        self.assertEqual((device['name'], device['battery']), ("PRO X 2", 70))

    def test_a_busy_hidpp10_percentage_read_is_tried_again(self):
        # A busy receiver answers 0x8f/0x07; falling through to the 0x07
        # level register gave a rougher number, or none when it was silent.
        receiver = FakeReceiver({
            1: FakePairedDevice(kind=0x00, battery=50, legacy_level=73,
                                wpid=0x400F, pairing_slot=1, name="K750"),
        })
        bus = FakeHidBus([FakeNode(b"/dev/unifying", 0xC52B, receiver)])
        mod = load_monitor_module(bus)
        mod.RECEIVER_SLOTS = {0xC52B: 1}
        mod.BUSY_RETRY_SECONDS = 0.01
        bus.intercept_next(
            b"/dev/unifying", lambda cmd: cmd[2] == 0x81 and cmd[3] == 0x0d,
            lambda request, report: [bytes([0x10, request[1], 0x8f, 0x81, 0x0d, 0x07, 0x00])])
        with tempfile.TemporaryDirectory() as tmp:
            mod.STATE_DIR = os.path.join(tmp, "logibar")
            mod.HIDRAW_CLASS_DIR = os.path.join(tmp, "no-hidraw")
            [device] = mod.scan_receivers()
        self.assertEqual((device['name'], device['battery']), ("K750", 73))

    def test_notifications_keep_the_flags_another_program_set(self):
        # Register 0x00 is the one hardware write: read, OR, write back.
        receiver = FakeReceiver({1: FakePairedDevice(kind=0x03, battery=50, wpid=0x405E)})
        receiver.notification_flags = 0x000010   # a flag logibar does not own
        bus = FakeHidBus([FakeNode(b"/dev/unifying", 0xC52B, receiver)])
        mod = load_monitor_module(bus)
        dev = mod.hid.device()
        dev.open_path(b"/dev/unifying")
        mod.enable_notifications(dev)
        self.assertEqual(receiver.notification_writes, [0x000910])
        mod.enable_notifications(dev)
        self.assertEqual(receiver.notification_writes, [0x000910], "set bits are not written again")

    def _listening(self, receiver, slots=1):
        bus = FakeHidBus([FakeNode(b"/dev/unifying", 0xC52B, receiver)])
        mod = load_monitor_module(bus)
        mod.RECEIVER_SLOTS = {0xC52B: slots}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mod.STATE_DIR = os.path.join(tmp.name, "logibar")
        mod.HIDRAW_CLASS_DIR = os.path.join(tmp.name, "no-hidraw")
        [device] = mod.scan_receivers()
        listener = threading.Thread(target=mod.listen_receiver,
                                    args=(b"/dev/unifying", slots), daemon=True)
        listener.start()
        self.addCleanup(listener.join, 3)
        self.addCleanup(setattr, mod, 'running', False)
        time.sleep(0.2)   # the listener enables notifications first
        return bus, mod, Path(mod.STATE_DIR) / device['id']

    def _wait_for(self, row, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            lines = row.read_text().splitlines()
            if predicate(lines):
                return lines
            time.sleep(0.02)
        self.fail(f"row never matched: {row.read_text()!r}")

    def test_a_link_change_updates_the_row_at_once(self):
        # Like G HUB: off -> the row stops answering at once; on -> it says
        # connecting until the scan brings the reading.
        mouse = FakePairedDevice(kind=0x03, battery=50, wpid=0x405E, name="M720 Triathlon")
        bus, mod, row = self._listening(FakeReceiver({1: mouse}))
        self.assertEqual(row.read_text().splitlines()[:2], ["50", "1"])

        mod.scan_requested.clear()
        bus.emit_link(b"/dev/unifying", off=True, slot=1)
        lines = self._wait_for(row, lambda l: l[1] == "0")
        self.assertEqual(lines[0], "50", "the reading stays for --last-known")
        self.assertTrue(mod.scan_requested.is_set())

        bus.emit_link(b"/dev/unifying", off=False, slot=1)
        self._wait_for(row, lambda l: l[6:7] == ["connecting"])
        mod.scan_receivers()
        self.assertEqual(row.read_text().splitlines(),
                         ["50", "1", "0", "M720 Triathlon", row.read_text().splitlines()[4], "405e"])

    def test_a_scan_already_running_keeps_a_newer_connecting_mark(self):
        mouse = FakePairedDevice(kind=0x03, battery=50, wpid=0x405E, name="M720 Triathlon")
        bus, mod, row = self._listening(FakeReceiver({1: mouse}))
        bus.emit_link(b"/dev/unifying", off=True, slot=1)
        self._wait_for(row, lambda l: l[1] == "0")
        started = time.time()
        devices = mod.discover_receiver_devices()   # the scan finds no answer...
        bus.emit_link(b"/dev/unifying", off=False, slot=1)   # ...the link comes up meanwhile
        mouse.awake = False
        self._wait_for(row, lambda l: l[6:7] == ["connecting"])
        mod.publish_discovered(devices, started)
        self.assertEqual(row.read_text().splitlines()[6:7], ["connecting"])
        mod.publish_discovered(devices, time.time() + 1)   # a scan newer than the mark
        self.assertEqual(row.read_text().splitlines()[6:7], [])

    def test_a_link_down_during_a_scan_wins_over_the_scan(self):
        # Live: the M720 switched off, a scan already running had heard it,
        # and its publish brought the row back for 1.4 s.
        mouse = FakePairedDevice(kind=0x03, battery=50, wpid=0x405E, name="M720 Triathlon")
        bus, mod, row = self._listening(FakeReceiver({1: mouse}))
        started = time.time()
        devices = mod.discover_receiver_devices()   # the scan hears it...
        bus.emit_link(b"/dev/unifying", off=True, slot=1)   # ...then it goes
        self._wait_for(row, lambda l: l[1] == "0")
        mod.publish_discovered(devices, started)
        self.assertEqual(row.read_text().splitlines()[:2], ["50", "0"])

    def test_the_listener_never_writes_flags_it_could_not_read(self):
        receiver = FakeReceiver({1: FakePairedDevice(kind=0x03, battery=50, wpid=0x405E)})
        receiver.notification_flags = 0x000d00
        bus = FakeHidBus([FakeNode(b"/dev/unifying", 0xC52B, receiver)])
        mod = load_monitor_module(bus)
        mod.read_notification_flags = lambda dev: None   # the read gets no answer
        dev = mod.hid.device()
        dev.open_path(b"/dev/unifying")
        self.assertFalse(mod.enable_notifications(dev, blind=False))
        self.assertEqual(receiver.notification_flags, 0x000d00)
        self.assertEqual(getattr(receiver, 'notification_writes', []), [])

    def test_a_listener_that_fails_comes_back_with_a_scan(self):
        receiver = FakeReceiver({1: FakePairedDevice(kind=0x03, battery=50, wpid=0x405E)})
        bus = FakeHidBus([FakeNode(b"/dev/unifying", 0xC52B, receiver)])
        mod = load_monitor_module(bus)
        mod.RECEIVER_SLOTS = {0xC52B: 1}
        mod.LISTENER_RETRY_SECONDS = 0.2
        mod.enable_notifications = lambda dev, blind=True: (_ for _ in ()).throw(OSError("transient"))
        mod.scan_requested.clear()
        mod.listeners[b"/dev/unifying"] = None
        mod.listen_receiver(b"/dev/unifying", 1)
        self.assertNotIn(b"/dev/unifying", mod.listeners)
        mod.ensure_listeners()
        self.assertNotIn(b"/dev/unifying", mod.listeners, "no restart before the retry time")
        self.assertTrue(mod.scan_requested.wait(2), "a scan comes after the retry time")
        mod.running = False

    def test_a_listener_without_notifications_waits_for_the_periodic_scan(self):
        receiver = FakeReceiver({1: FakePairedDevice(kind=0x03, battery=50, wpid=0x405E)})
        bus = FakeHidBus([FakeNode(b"/dev/unifying", 0xC52B, receiver)])
        mod = load_monitor_module(bus)
        mod.RECEIVER_SLOTS = {0xC52B: 1}
        mod.LISTENER_RETRY_SECONDS = 0.2
        mod.enable_notifications = lambda dev, blind=True: False   # the read gets no answer
        mod.listeners[b"/dev/unifying"] = None
        mod.scan_requested.clear()
        before = time.monotonic()
        mod.listen_receiver(b"/dev/unifying", 1)
        self.assertNotIn(b"/dev/unifying", mod.listeners, "it does not stay without link events")
        self.assertGreaterEqual(mod.listener_retry_at[b"/dev/unifying"],
                                before + 300, "the next try waits for the periodic scan")
        self.assertFalse(mod.scan_requested.wait(0.5), "no fast scan loop")
        mod.ensure_listeners()
        self.assertNotIn(b"/dev/unifying", mod.listeners, "no restart before the periodic scan")
        mod.running = False

    def test_multiple_models_and_legacy_device_share_receiver(self):
        receiver = FakeReceiver({
            1: FakePairedDevice(kind=0x00, battery=50, legacy_level=73,
                                wpid=0x400F, pairing_slot=1, name="K750"),
            2: FakePairedDevice(kind=0x03, battery=60, wpid=0x4041,
                                pairing_slot=2, name="MX Master"),
            3: FakePairedDevice(kind=0x03, battery=40, awake=False,
                                wpid=0x1020, pairing_slot=3, name="M215"),
            4: FakePairedDevice(kind=0x03, battery_feature=BATTERY_STATUS_FEATURE,
                                battery=55, charging=True, wpid=0x4069,
                                pairing_slot=4, name="MX Master 2S"),
            5: FakePairedDevice(kind=0x06, battery=45, wpid=0x9999,
                                pairing_slot=5, name="Presenter"),
        })
        lightspeed = FakeReceiver({
            1: FakePairedDevice(kind=0x00, battery_feature=BATTERY_VOLTAGE_FEATURE,
                                voltage=3989, wpid=0x408E, name="G915 TKL"),
        })
        bus = FakeHidBus([
            FakeNode(b"/dev/unifying", 0xC52B, receiver),
            FakeNode(b"/dev/lightspeed", 0xC545, lightspeed),
        ])
        mod = load_monitor_module(bus)
        mod.RECEIVER_SLOTS = {0xC52B: 5, 0xC545: 1}
        with tempfile.TemporaryDirectory() as tmp:
            mod.STATE_DIR = os.path.join(tmp, "logibar")
            devices = mod.scan_receivers()
            self.assertEqual(len(devices), 6)
            self.assertEqual([(d['name'], d['battery']) for d in devices], [
                ("K750", 73), ("MX Master", 60), ("M215", None),
                ("MX Master 2S", 55), ("Presenter", 45), ("G915 TKL", 80),
            ])
            self.assertEqual([d['kind'] for d in devices],
                             ["keyboard", "mouse", "mouse", "mouse", "other", "keyboard"])
            self.assertTrue(devices[3]['charging'])
            saved = (Path(mod.STATE_DIR) / devices[3]['id']).read_text().splitlines()
            self.assertEqual(saved[:4], ["55", "1", "1", "MX Master 2S"])
            self.assertEqual(saved[5], "4069")
            self.assertLess(abs(int(saved[4]) - int(time.time())), 5)
            env = os.environ.copy()
            env['XDG_RUNTIME_DIR'] = tmp
            with subprocess.Popen([str(SCRIPT.parent / 'logibar-status'), '--json'],
                                  env=env, text=True, stdout=subprocess.PIPE) as proc:
                raw, _ = proc.communicate()
                self.assertEqual(proc.returncode, 0)
            status = json.loads(raw)
            self.assertEqual(status['aggregate']['connected_count'], 5)
            self.assertEqual(len(status['devices']), 6)
            self.assertEqual(status['devices'][2]['battery'], None)
            self.assertTrue(status['devices'][2]['paired'])
            with subprocess.Popen([str(SCRIPT.parent / 'logibar-status'), '--json',
                                   '--devices', 'mouse'], env=env, text=True,
                                  stdout=subprocess.PIPE) as proc:
                raw, _ = proc.communicate()
                self.assertEqual(proc.returncode, 0)
            self.assertEqual(len(json.loads(raw)['devices']), 3)
            with subprocess.Popen([str(SCRIPT.parent / 'logibar-status'), '--json',
                                   '--devices', 'other'], env=env, text=True,
                                  stdout=subprocess.PIPE) as proc:
                raw, _ = proc.communicate()
                self.assertEqual(proc.returncode, 0)
            self.assertEqual(json.loads(raw)['devices'][0]['name'], 'Presenter')
            # Old category state must not resurrect a sleeping paired mouse.
            for device in devices:
                if device['kind'] == 'mouse':
                    (Path(mod.STATE_DIR) / device['id']).write_text(
                        '55\n0\n0\n' + device['name'])
            (Path(mod.STATE_DIR) / 'mouse').write_text('90\n1\n0\nMX Master 2S')
            with subprocess.Popen([str(SCRIPT.parent / 'logibar-status'), '--json'],
                                  env=env, text=True, stdout=subprocess.PIPE) as proc:
                raw, _ = proc.communicate()
                self.assertEqual(proc.returncode, 0)
            sleeping = json.loads(raw)
            self.assertEqual(len(sleeping['devices']), 6)
            self.assertFalse(any(d['kind'] == 'mouse' and d['connected']
                                 for d in sleeping['devices']))
            self.assertEqual(sleeping['aggregate']['connected_count'], 3)
        self.assertEqual(bus.protocol_errors, [])

    def test_sleep_retains_recent_reading_without_charge_or_old_pairings(self):
        mouse = FakePairedDevice(kind=0x03, battery=55, charging=True,
                                 wpid=0x4069, name="MX Master 2S")
        old = FakePairedDevice(kind=0x03, battery=70, awake=False,
                               wpid=0x4041, pairing_slot=2, name="MX Master")
        receiver = FakeReceiver({1: mouse, 2: old})
        bus = FakeHidBus([FakeNode(b"/dev/unifying", 0xC52B, receiver)])
        mod = load_monitor_module(bus)
        mod.RECEIVER_SLOTS = {0xC52B: 2}
        with tempfile.TemporaryDirectory() as tmp:
            mod.STATE_DIR = os.path.join(tmp, "logibar")
            env = os.environ.copy()
            env['XDG_RUNTIME_DIR'] = tmp

            def status(*flags):
                with subprocess.Popen([str(SCRIPT.parent / 'logibar-status'), '--json',
                                       '--last-known', *flags],
                                      env=env, text=True, stdout=subprocess.PIPE) as proc:
                    raw, _ = proc.communicate()
                    self.assertEqual(proc.returncode, 0)
                return json.loads(raw)

            scanned = mod.scan_receivers()
            self.assertEqual(status()['devices'][0]['battery'], 55)
            mouse.awake = False
            mod.scan_receivers()
            sleeping = status()
            recent, never_seen = sleeping['devices']
            self.assertFalse(recent['connected'])
            self.assertIsNone(recent['battery'])
            self.assertEqual(recent['last_known_battery'], 55)
            self.assertEqual(recent['state'], 'last_known')
            self.assertIsNotNone(recent['last_seen_at'])
            self.assertFalse(recent['charging'])
            self.assertEqual(never_seen['state'], 'disconnected')
            self.assertIsNone(never_seen['last_known_battery'])
            self.assertEqual(sleeping['aggregate']['connected_count'], 0)
            with subprocess.Popen([str(SCRIPT.parent / 'logibar-status'), '--last-known'],
                                  env=env, text=True, stdout=subprocess.PIPE) as proc:
                raw, _ = proc.communicate()
                self.assertEqual(proc.returncode, 0)
            waybar = json.loads(raw)
            self.assertEqual(waybar['class'], 'last-known')
            self.assertEqual(waybar['percentage'], 55)
            self.assertIn('Last known', waybar['tooltip'])
            self.assertNotIn('Charging', waybar['tooltip'])
            self.assertNotIn('MX Master<', waybar['tooltip'])

            state_file = Path(mod.STATE_DIR) / scanned[0]['id']
            lines = state_file.read_text().splitlines()
            lines[4] = str(int(time.time()) - mod.LAST_KNOWN_SECONDS - 1)
            state_file.write_text('\n'.join(lines))
            self.assertEqual(status()['devices'][0]['state'], 'disconnected')
            self.assertIsNone(status()['devices'][0]['last_known_battery'])

            mouse.awake = True
            mod.scan_receivers()
            self.assertTrue(status()['devices'][0]['connected'])
            receiver.devices.clear()
            mod.scan_receivers()
            self.assertEqual(status()['devices'], [])
            category = Path(mod.STATE_DIR) / 'mouse'
            category.write_text('90\n1\n0\nMX Master 2S')
            self.assertEqual(status()['devices'], [])
            category.write_text('80\n1\n0\nG Pro Wired\nwired')
            self.assertEqual(status()['devices'][0]['battery'], 80)
            receiver.devices[1] = mouse
            del bus.nodes[b"/dev/unifying"]
            mod.scan_receivers()
            self.assertEqual(status()['devices'][0]['battery'], 80)
            category.unlink()
            self.assertEqual(status()['devices'], [])
        self.assertEqual(bus.protocol_errors, [])


if __name__ == "__main__":
    unittest.main()
