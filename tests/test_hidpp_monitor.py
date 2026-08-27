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
        class FakeDevice:
            def __init__(self):
                self.responses = iter([
                    [],
                    [],
                    [0x11, 0x01, 0x00, 0x1e, 0x00],
                    [0x11, 0x01, 0x00, 0x0e, 0x03],
                    [],
                    [0x11, 0x01, 0x03, 0x1e, 0x00],
                    [0x11, 0x01, 0x03, 0x2e, 0x03],
                    [],
                    [0x11, 0x01, 0x03, 0x2e, 0x03],
                ])

            def open_path(self, _path):
                pass

            def write(self, _command):
                pass

            def read(self, _size, timeout_ms):
                del timeout_ms
                return next(self.responses, [])

            def close(self):
                pass

        original_device = getattr(monitor.hid, "device", None)
        monitor.hid.device = FakeDevice
        try:
            self.assertEqual(monitor.get_device_kind(b"/dev/receiver"), 0x03)
        finally:
            if original_device is None:
                del monitor.hid.device
            else:
                monitor.hid.device = original_device

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
        class FakeDevice:
            def __init__(self):
                self.responses = iter([
                    [],
                    [0x11, 0x01, 0x00, 0x0e, 0x05],
                    [0x11, 0x01, 0x00, 0x0d, 0x06],
                ])

            def write(self, _command):
                pass

            def read(self, _size, timeout_ms):
                del timeout_ms
                return next(self.responses, [])

        self.assertEqual(
            monitor.get_feature_index(FakeDevice(), 0x01, 0x1004, retries=1),
            0x06,
        )

    def test_monitor_requests_carry_the_monitor_software_id(self):
        class FakeDevice:
            def __init__(self):
                self.commands = []
                self.responses = iter([
                    [],
                    [0x11, 0x01, 0x00, 0x0d, 0x06],
                    [],
                    [0x11, 0x01, 0x06, 0x1d, 0x3C, 0x00, 0x01],
                ])

            def write(self, command):
                self.commands.append(command)

            def read(self, _size, timeout_ms):
                del timeout_ms
                return next(self.responses, [])

        device = FakeDevice()
        feat_idx = monitor.get_feature_index(device, 0x01, 0x1004, retries=1)
        self.assertEqual(feat_idx, 0x06)
        self.assertEqual(device.commands[0][3], 0x0d)

        self.assertEqual(
            monitor.query_battery(device, 0x01, feat_idx, monitor.UNIFIED_BATTERY_FEATURE),
            (60, True),
        )
        self.assertEqual(device.commands[1][0:4], [0x11, 0x01, 0x06, 0x1d])

    def test_probe_requests_carry_the_probe_software_id(self):
        class FakeDevice:
            def __init__(self):
                self.commands = []
                self.responses = iter([
                    [],
                    [],
                    [0x11, 0x01, 0x00, 0x0e, 0x03],
                    [],
                    [0x11, 0x01, 0x03, 0x2e, 0x03],
                    [],
                    [0x11, 0x01, 0x03, 0x2e, 0x03],
                ])

            def open_path(self, _path):
                pass

            def write(self, command):
                self.commands.append(command)

            def read(self, _size, timeout_ms):
                del timeout_ms
                return next(self.responses, [])

            def close(self):
                pass

        device = FakeDevice()
        original_device = getattr(monitor.hid, "device", None)
        monitor.hid.device = lambda: device
        try:
            self.assertEqual(monitor.get_device_kind(b"/dev/receiver"), 0x03)
        finally:
            if original_device is None:
                del monitor.hid.device
            else:
                monitor.hid.device = original_device

        root_requests = [c for c in device.commands if c[0] == 0x10 and c[2] == 0x00]
        type_requests = [c for c in device.commands if c[0] == 0x11 and c[2] == 0x03]
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
        class FakeDevice:
            def __init__(self):
                self.commands = []
                self.responses = iter([
                    [],
                    [0x11, 0x01, 0x07, 0x1d, 0x64, 0x00, 0x00],
                    [0x11, 0x01, 0x07, 0x0d, 0x0E, 0xF9, 0x00],
                ])

            def write(self, command):
                self.commands.append(command)

            def read(self, _size, timeout_ms):
                del timeout_ms
                return next(self.responses, [])

        device = FakeDevice()
        self.assertEqual(
            monitor.query_battery(
                device,
                0x01,
                0x07,
                monitor.BATTERY_VOLTAGE_FEATURE,
            ),
            (55, False),
        )
        self.assertEqual(device.commands[0][0:4], [0x11, 0x01, 0x07, 0x0d])


if __name__ == "__main__":
    unittest.main()
