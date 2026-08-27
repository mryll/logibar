#!/usr/bin/env python3
import importlib.util
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
    BATTERY_VOLTAGE_FEATURE,
    DaemonHarness,
    FakeHidBus,
    FakeNode,
    FakePairedDevice,
    UNIFIED_BATTERY_FEATURE,
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
                    "75\n1\n0",
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
                    "40\n1\n0",
                )
        finally:
            monitor.STATE_DIR = original_state_dir
            monitor.subprocess.run = original_run
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
                    "60\n1\n0",
                )

                monitor.write_state("mouse", 0, False, False, 10, superlight_2)
                self.assertEqual(
                    (Path(state_dir) / "mouse").read_text(),
                    "0\n0\n0",
                )
        finally:
            monitor.STATE_DIR = original_state_dir
            monitor.subprocess.run = original_run
            monitor.source_states.clear()
            monitor.published_states.clear()


class BatteryVoltageTests(unittest.TestCase):
    def test_g915_tkl_uses_battery_voltage(self):
        self.assertIn((0xC545, 0xC343, "keyboard", 9), monitor.DEVICES)
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
            harness.wait_for_state("keyboard", "59\n1\n0")
            harness.wait_for_state("mouse", "41\n1\n0")

            report = harness.stop()
            self.assertEqual(report["alive"], [])
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["protocol_errors"], [])
            self.assertEqual(
                harness.state_file("keyboard").read_text(), "59\n1\n0")
            self.assertEqual(
                harness.state_file("mouse").read_text(), "41\n1\n0")

    def test_an_injected_battery_event_updates_the_state(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/sl2", 0xC54D,
                     FakePairedDevice(kind=0x03, battery=50)),
        ])
        with DaemonHarness(bus) as harness:
            harness.start_wireless(0xC54D, "mouse", 10)
            harness.wait_for_state("mouse", "50\n1\n0")
            harness.wait_for_open(b"/dev/sl2")

            bus.emit_battery_event(b"/dev/sl2", battery=37)
            harness.wait_for_state("mouse", "37\n1\n0")

    def test_link_events_toggle_the_published_state(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/sl2", 0xC54D,
                     FakePairedDevice(kind=0x03, battery=50)),
        ])
        with DaemonHarness(bus) as harness:
            harness.start_wireless(0xC54D, "mouse", 10)
            harness.wait_for_state("mouse", "50\n1\n0")
            harness.wait_for_open(b"/dev/sl2")

            bus.emit_link(b"/dev/sl2", off=True)
            harness.wait_for_state("mouse", "0\n0\n0")

            bus.emit_link(b"/dev/sl2", off=False)
            harness.wait_for_state("mouse", "50\n1\n0")

    def test_a_wired_device_uses_the_direct_device_index(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/wired", 0xC09B,
                     FakePairedDevice(kind=0x03, battery=66), device_idx=0xFF),
        ])
        with DaemonHarness(bus) as harness:
            harness.start_wired(0xC09B, "mouse", 10)
            harness.wait_for_state("mouse", "66\n1\n0")

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
        (0xC545, 0xC343): ("keyboard", 9, FakePairedDevice(
            kind=0x00, battery_feature=BATTERY_VOLTAGE_FEATURE, voltage=3833)),
        (0xC547, 0xC357): ("keyboard", 9, FakePairedDevice(
            kind=0x00, battery=59)),
        (0xC547, 0xC094): ("mouse", 10, FakePairedDevice(
            kind=0x03, battery=41)),
        (0xC54D, 0xC09B): ("mouse", 10, FakePairedDevice(
            kind=0x03, battery=50)),
    }

    def test_every_supported_device_has_an_emulated_sheet(self):
        declared = {(wireless, wired): (name, signal)
                    for wireless, wired, name, signal in monitor.DEVICES}
        self.assertEqual(set(declared), set(self.EMULATED_SHEETS))
        for pids, (name, signal) in declared.items():
            self.assertEqual((name, signal), self.EMULATED_SHEETS[pids][:2])

    def test_only_the_g915_tkl_reports_voltage(self):
        self.assertEqual(set(monitor.BATTERY_FEATURE_BY_RECEIVER), {0xC545})
        self.assertEqual(
            self.EMULATED_SHEETS[(0xC545, 0xC343)][2].battery_feature,
            0x1001,
        )


if __name__ == "__main__":
    unittest.main()
