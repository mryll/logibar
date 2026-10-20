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
    load_monitor_module,
)


def is_root_lookup(cmd):
    return cmd[0] == 0x10 and cmd[2] == 0x00


def single_device_bus(pid, sheet, path=b"/dev/receiver", **node_kwargs):
    bus = FakeHidBus([FakeNode(path, pid, sheet, **node_kwargs)])
    fd = bus.device()
    fd.open_path(path)
    return bus, fd


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

    def test_the_lowest_reading_and_the_cable_reading_have_their_own_files(self):
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
                # The single-device widget shows the lowest, like the bar;
                # the CLI reads the cable reading from its own file.
                self.assertEqual((Path(state_dir) / "mouse").read_text(), "30\n1\n0\n")
                self.assertTrue((Path(state_dir) / "wired-mouse").read_text()
                                .startswith("80\n1\n0\nG Pro Wireless\nwired"))
        finally:
            monitor.STATE_DIR = original_state_dir
            monitor.subprocess.run = original_run
            monitor.source_states.clear()
            monitor.published_states.clear()

    def test_a_cable_names_only_the_receivers_that_have_its_row(self):
        # Two receivers of the same family: only the one where the G502 X
        # is paired loses its silent row to the cable.
        original_state_dir = monitor.STATE_DIR
        original_run = monitor.subprocess.run
        original_rows = monitor.receiver_row_wpids
        monitor.source_states.clear()
        monitor.published_states.clear()
        monitor.subprocess.run = lambda *_args, **_kwargs: None
        monitor.receiver_row_wpids = {'rx-0000c547-1': 0x407B, 'rx-0000c547-2': 0x409F,
                                      'rx-1111c547-1': 0x4093}

        try:
            with tempfile.TemporaryDirectory() as state_dir:
                monitor.STATE_DIR = state_dir
                cable = {'_source_id': ('cable', b'/dev/hidraw9'), 'wired_connected': True,
                         '_model': 'G502 X LIGHTSPEED', '_cable_wpids': frozenset({0x409F, 0xC098})}
                monitor.write_state("mouse", 80, True, False, 10, cable)
                self.assertEqual(
                    (Path(state_dir) / "mouse").read_text(),
                    "80\n1\n0\nG502 X LIGHTSPEED\nwired rx-0000c547")
        finally:
            monitor.STATE_DIR = original_state_dir
            monitor.subprocess.run = original_run
            monitor.receiver_row_wpids = original_rows
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


    def test_g_pro_wired_voltage_updates(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/gpro-wired", 0xC088, FakePairedDevice(
                kind=0x03, battery_feature=BATTERY_VOLTAGE_FEATURE,
                voltage=3833, charging=True, battery_index=0x06,
                name="G Pro Wireless"), device_idx=0xFF),
        ])
        with DaemonHarness(bus) as harness:
            harness.start_cable(b"/dev/gpro-wired", 0xC088)
            harness.wait_for_state("mouse", "55\n1\n1\nG Pro Wireless\nwired")
            bus.emit_battery_event(b"/dev/gpro-wired", voltage=4186, charging=False)
            harness.wait_for_state("mouse", "100\n1\n0\nG Pro Wireless\nwired")
            report = harness.stop()
            self.assertEqual(report["alive"], [])
            self.assertEqual(report["protocol_errors"], [])


    def test_a_cable_device_names_itself_and_its_receiver(self):
        # No list names the G502 X on its cable: the name is the one the
        # mouse gives, and its model id carries its wireless PID, which
        # finds its row on the c547. A row of another device on another
        # receiver is not named.
        bus = FakeHidBus([
            FakeNode(b"/dev/g502-wired", 0xC098, FakePairedDevice(
                kind=0x03, battery=41, charging=True, name="G502 X LIGHTSPEED",
                model_ids=[0x409F, 0xC098]), device_idx=0xFF),
        ])
        with DaemonHarness(bus) as harness:
            harness.set_receiver_rows({"rx-1a2b3c4d-2": 0x409F, "rx-99999999-1": 0x40A9})
            harness.start_cable(b"/dev/g502-wired", 0xC098)
            harness.wait_for_state("mouse", "41\n1\n1\nG502 X LIGHTSPEED\nwired rx-1a2b3c4d")
            bus.emit_battery_event(b"/dev/g502-wired", battery=44, charging=True)
            harness.wait_for_state("mouse", "44\n1\n1\nG502 X LIGHTSPEED\nwired rx-1a2b3c4d")
            report = harness.stop()
            self.assertEqual(report["alive"], [])
            self.assertEqual(report["protocol_errors"], [])


    def test_the_retry_delay_doubles_up_to_its_cap(self):
        monitor = load_monitor_module(FakeHidBus([]))
        delays = [monitor.REQUERY_FIRST_DELAY]
        for _ in range(8):
            delays.append(monitor.next_requery_delay(delays[-1]))
        self.assertEqual(delays[:3], [2.0, 4.0, 8.0])
        self.assertEqual(delays[-1], monitor.REQUERY_MAX_DELAY)


    def test_a_wired_device_uses_the_direct_device_index(self):
        bus = FakeHidBus([
            FakeNode(b"/dev/wired", 0xC09B,
                     FakePairedDevice(kind=0x03, battery=66), device_idx=0xFF),
        ])
        with DaemonHarness(bus) as harness:
            harness.start_cable(b"/dev/wired", 0xC09B)
            harness.wait_for_state("mouse", "66\n1\n0\nFake Logitech\nwired")

        writes = [list(entry[4]) for entry in bus.log if entry[0] == "write"]
        hidpp_requests = [c for c in writes if c[0] in (0x10, 0x11) and c[2] != 0x80]
        self.assertTrue(hidpp_requests)
        self.assertTrue(all(c[1] == 0xFF for c in hidpp_requests))


class CableDiscoveryTests(unittest.TestCase):
    """Which hidraw nodes get a cable monitor. The sysfs path of each node
    is given by hand: hid-logitech-dj puts a paired device's node under its
    receiver, and a Bluetooth node is 0005:."""

    SYSFS = {
        b"/dev/receiver": "/sys/devices/usb8/8-3/8-3:1.2/0003:046D:C54D.003E",
        b"/dev/paired": "/sys/devices/usb8/8-3/8-3:1.2/0003:046D:C54D.003E/0003:046D:40A9.003F",
        b"/dev/cable": "/sys/devices/usb8/8-2/8-2:1.2/0003:046D:C09B.0042",
        b"/dev/bluetooth": "/sys/devices/virtual/misc/uhid/0005:046D:B023.0050",
    }

    def setUp(self):
        mouse = dict(kind=0x03, battery=64, name="PRO X 2", model_ids=[0x40A9, 0xC09B])
        self.bus = FakeHidBus([
            FakeNode(b"/dev/receiver", 0xC54D, FakeReceiver({1: FakePairedDevice(**mouse)})),
            FakeNode(b"/dev/paired", 0x40A9, FakePairedDevice(**mouse)),
            FakeNode(b"/dev/cable", 0xC09B, FakePairedDevice(**mouse), device_idx=0xFF),
            FakeNode(b"/dev/bluetooth", 0xB023, FakePairedDevice(**mouse), device_idx=0xFF),
        ])
        self.harness = DaemonHarness(self.bus)
        self.harness.module.sysfs_hid_device = lambda path: self.SYSFS.get(path, "")

    def tearDown(self):
        self.stop()
        self.harness.__exit__(None, None, None)

    def stop(self):
        """Stop and join the cable monitors too: they are not harness threads."""
        self.harness.threads.extend(self.harness.module.cable_monitors.values())
        return self.harness.stop()

    def writes_to(self, path):
        return [entry for entry in self.bus.log if entry[0] == "write" and entry[2] == path]

    def test_only_a_usb_node_outside_a_receiver_is_a_cable(self):
        module = self.harness.module
        module.ensure_cable_monitors()
        self.harness.wait_for_state("mouse", "64\n1\n0\nPRO X 2\nwired")
        self.assertEqual(set(module.cable_monitors), {b"/dev/cable"})
        for path in (b"/dev/receiver", b"/dev/paired", b"/dev/bluetooth"):
            self.assertEqual(self.writes_to(path), [], path)
        self.assertEqual(self.stop()["protocol_errors"], [])

    def test_a_node_that_is_no_cable_device_is_probed_once(self):
        # A Logitech USB node that does not answer HID++ 2.0 (or is not a
        # keyboard or a mouse) is left alone until the node set changes:
        # each probe of a silent node costs a few seconds of retries.
        module = self.harness.module
        self.bus.nodes[b"/dev/cable"].device.kind = 0x08   # not a keyboard or a mouse
        module.hidraw_fingerprint = lambda: (("hidraw2", 1),)
        module.ensure_cable_monitors()
        deadline = time.monotonic() + 5
        while b"/dev/cable" not in module.cable_skipped:
            self.assertLess(time.monotonic(), deadline, "the probe never finished")
            time.sleep(0.02)
        probes = len(self.writes_to(b"/dev/cable"))
        module.ensure_cable_monitors()
        time.sleep(0.3)
        self.assertEqual(len(self.writes_to(b"/dev/cable")), probes)
        self.assertFalse(self.harness.state_file("mouse").exists())
        module.hidraw_fingerprint = lambda: (("hidraw2", 2),)   # replugged
        module.ensure_cable_monitors()
        deadline = time.monotonic() + 5
        while len(self.writes_to(b"/dev/cable")) == probes:
            self.assertLess(time.monotonic(), deadline, "no new probe after the node set changed")
            time.sleep(0.02)


    def test_a_silent_probe_is_tried_again(self):
        # A probe that gets no answer once (a busy device) is not a verdict:
        # the node is probed again after a short wait.
        module = self.harness.module
        module.CABLE_RETRY_SECONDS = 0.3
        module.scan_requested.clear()
        device = self.bus.nodes[b"/dev/cable"].device
        self.bus.intercept_next(
            b"/dev/cable",
            lambda cmd: cmd[0] == 0x11 and cmd[2] == device.name_index and cmd[3] >> 4 == 2,
            lambda _request, _reply: [])
        module.ensure_cable_monitors()
        deadline = time.monotonic() + 5
        while module.cable_failures.get(b"/dev/cable") != 1:
            self.assertLess(time.monotonic(), deadline, "the probe never finished")
            time.sleep(0.02)
        self.assertNotIn(b"/dev/cable", module.cable_skipped)
        self.assertTrue(module.scan_requested.wait(2), "no scan after the retry time")
        module.ensure_cable_monitors()
        self.harness.wait_for_state("mouse", "64\n1\n0\nPRO X 2\nwired")

    def test_a_probe_that_ends_after_a_replug_leaves_the_new_device_alone(self):
        # The old probe gets no answer; meanwhile the node set changes and a
        # device that answers sits on the same path. The old probe must not
        # mark that path, and must ask for the scan that starts its monitor.
        module = self.harness.module
        module.scan_requested.clear()
        real_probe, release = module.probe_cable, threading.Event()
        calls = []

        def probe(dev, pid):
            calls.append(pid)
            if len(calls) == 1:          # the old device: no answer
                release.wait(5)
                return 'silent', None
            return real_probe(dev, pid)

        module.probe_cable = probe
        module.hidraw_fingerprint = lambda: (("hidraw2", 1),)
        module.ensure_cable_monitors()
        module.hidraw_fingerprint = lambda: (("hidraw2", 2),)   # replugged
        module.ensure_cable_monitors()   # the old probe still holds the path
        self.assertEqual(len(calls), 1)
        release.set()
        deadline = time.monotonic() + 5
        while b"/dev/cable" in module.cable_monitors:
            self.assertLess(time.monotonic(), deadline, "the old probe never ended")
            time.sleep(0.02)
        self.assertNotIn(b"/dev/cable", module.cable_skipped)
        self.assertNotIn(b"/dev/cable", module.cable_retry_at)
        self.assertTrue(module.scan_requested.is_set())
        module.ensure_cable_monitors()
        self.harness.wait_for_state("mouse", "64\n1\n0\nPRO X 2\nwired")

    def test_a_monitor_that_fails_on_a_present_node_waits_before_the_next(self):
        # The device answered its probe, then its read failed while the node
        # stays listed. Its last write asks for a scan, as every change does;
        # that scan must not start a monitor at once, or each one fails again.
        module = self.harness.module
        module.CABLE_RETRY_SECONDS = 0.5
        module.hidraw_fingerprint = lambda: (("hidraw2", 1),)
        module.ensure_cable_monitors()
        self.harness.wait_for_state("mouse", "64\n1\n0\nPRO X 2\nwired")
        self.bus.close_all()   # the next read fails; the node is still there
        deadline = time.monotonic() + 5
        while b"/dev/cable" in module.cable_monitors:
            self.assertLess(time.monotonic(), deadline, "the monitor never ended")
            time.sleep(0.02)
        self.assertEqual(module.cable_failures.get(b"/dev/cable"), 1)
        module.ensure_cable_monitors()
        self.assertNotIn(b"/dev/cable", module.cable_monitors, "a new monitor before the wait")
        module.scan_requested.clear()
        self.assertTrue(module.scan_requested.wait(2), "no scan after the wait")

    def test_a_late_reply_does_not_give_its_index_to_the_next_lookup(self):
        # The name lookup's reply comes late, during the battery lookup. With
        # one software id for both, the battery index became the name
        # feature's, and the name length (7) was published as 7 %.
        sheet = FakePairedDevice(kind=0x03, battery=66, name="PRO X 2",
                                 battery_feature=BATTERY_STATUS_FEATURE)
        bus = FakeHidBus([FakeNode(b"/dev/cable", 0xC09B, sheet, device_idx=0xFF)])

        def with_late_name_reply(request, reply):
            name_lookup = next(entry[4] for entry in bus.log if entry[0] == "write"
                               and entry[4][2] == 0x00 and entry[4][4:6] == b"\x00\x05")
            late = bytes([0x10, 0xFF, 0x00, name_lookup[3], sheet.name_index, 0, 0])
            return [late, reply]

        bus.intercept_next(b"/dev/cable",
                           lambda cmd: cmd[0] == 0x10 and cmd[2] == 0x00 and cmd[4:6] == [0x10, 0x00],
                           with_late_name_reply)
        with DaemonHarness(bus) as harness:
            harness.start_cable(b"/dev/cable", 0xC09B)
            harness.wait_for_state("mouse", "66\n1\n0\nPRO X 2\nwired")


    def test_a_flood_of_foreign_reports_does_not_publish_each_one(self):
        bus = FakeHidBus([FakeNode(b"/dev/cable", 0xC09B, FakePairedDevice(
            kind=0x03, battery=64, name="PRO X 2"), device_idx=0xFF)])
        with DaemonHarness(bus) as harness:
            harness.start_cable(b"/dev/cable", 0xC09B)
            harness.wait_for_state("mouse", "64\n1\n0\nPRO X 2\nwired")
            calls = []
            original = harness.module.write_state
            harness.module.write_state = lambda *args: (calls.append(args), original(*args))
            with bus._lock:
                node = bus.nodes[b"/dev/cable"]
                for _ in range(500):
                    bus._broadcast_locked(node, bytes([0x11, 0xFF, 0x55, 0x00, 1, 2, 3]))
            time.sleep(1.5)
            self.assertLessEqual(len(calls), 3)


class CategoryProjectionTests(unittest.TestCase):
    """The single-device widgets read one file per kind, written from the
    receiver rows: the same devices, with the same names, as the combined
    widget and the Omarchy panel."""

    def scan(self, nodes, slots=None):
        bus = FakeHidBus(nodes)
        mod = load_monitor_module(bus)
        if slots:
            mod.RECEIVER_SLOTS = slots
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mod.STATE_DIR = os.path.join(tmp.name, "logibar")
        mod.HIDRAW_CLASS_DIR = os.path.join(tmp.name, "no-hidraw")
        mod.scan_requested.clear()
        devices = mod.scan_receivers()
        return bus, mod, devices, Path(mod.STATE_DIR)

    def test_each_kind_shows_its_lowest_device_with_its_own_name(self):
        _, mod, _, state = self.scan([
            FakeNode(b"/dev/unifying", 0xC52B, FakeReceiver({
                1: FakePairedDevice(kind=0x03, battery=50, wpid=0x405E, name="M720 Triathlon"),
                2: FakePairedDevice(kind=0x00, battery=80, wpid=0x4075, name="K850",
                                    pairing_slot=2)})),
            FakeNode(b"/dev/lightspeed", 0xC54D, FakeReceiver({
                1: FakePairedDevice(kind=0x03, battery=30, wpid=0x40A9, name="PRO X 2")})),
        ], slots={0xC52B: 2, 0xC54D: 1})
        self.assertEqual((state / "mouse").read_text(), "30\n1\n0\nPRO X 2")
        self.assertEqual((state / "keyboard").read_text(), "80\n1\n0\nK850")
        self.assertFalse(mod.scan_requested.is_set(),
                         "the projection of a scan asked for another scan")

    def test_no_connected_device_of_a_kind_clears_its_file(self):
        mouse = FakePairedDevice(kind=0x03, battery=50, wpid=0x405E, name="M720 Triathlon")
        _, mod, _, state = self.scan([FakeNode(b"/dev/unifying", 0xC52B, FakeReceiver({1: mouse}))],
                                     slots={0xC52B: 1})
        self.assertEqual((state / "mouse").read_text(), "50\n1\n0\nM720 Triathlon")
        self.assertEqual((state / "keyboard").read_text(), "0\n0\n0\n")
        mouse.awake = False
        mod.scan_receivers()
        self.assertEqual((state / "mouse").read_text(), "0\n0\n0\n")

    def test_a_cable_mouse_and_a_lower_receiver_mouse(self):
        # Seen on the probook: the PRO X 2 at 98 % on its cable, the M720 at
        # 50 % on its receiver. The bar's lowest face shows the M720, and so
        # must the single-device widget; the cable reading goes to the CLI.
        _, mod, _, state = self.scan([FakeNode(b"/dev/unifying", 0xC52B, FakeReceiver({
            1: FakePairedDevice(kind=0x03, battery=50, wpid=0x405E, name="M720 Triathlon")}))],
            slots={0xC52B: 1})
        rx = mod.receiver_hash(b"/dev/unifying")
        mod.receiver_row_wpids[f"rx-{rx}-2"] = 0x40A9
        cable = {'_source_id': ('cable', b'/dev/hidraw9'), 'wired_connected': True,
                 '_model': 'PRO X 2', '_cable_wpids': frozenset({0x40A9})}
        mod.write_state("mouse", 98, True, True, 10, cable)
        self.assertEqual((state / "mouse").read_text(), "50\n1\n0\nM720 Triathlon")
        self.assertEqual((state / "wired-mouse").read_text(), f"98\n1\n1\nPRO X 2\nwired rx-{rx}")
        self.assertTrue(mod.scan_requested.is_set(), "a cable change asks for a scan")
        mod.write_state("mouse", 0, False, False, 10, dict(cable, wired_connected=False))
        self.assertEqual((state / "mouse").read_text(), "50\n1\n0\nM720 Triathlon")
        self.assertEqual((state / "wired-mouse").read_text(), "0\n0\n0\n")

    def test_the_devices_of_the_old_list_are_read_by_the_receiver_scan(self):
        # The devices that had a line in the old table, as contributors
        # captured them: G Pro Wireless (c539) and G915 TKL (c545) report
        # BATTERY_VOLTAGE; the MX Master 2S reports 0 % while it charges.
        _, _, devices, state = self.scan([
            FakeNode(b"/dev/c539", 0xC539, FakeReceiver({1: FakePairedDevice(
                kind=0x03, battery_feature=BATTERY_VOLTAGE_FEATURE, voltage=3728,
                battery_index=0x06, wpid=0x4079, name="G Pro")})),
            FakeNode(b"/dev/c545", 0xC545, FakeReceiver({1: FakePairedDevice(
                kind=0x00, battery_feature=BATTERY_VOLTAGE_FEATURE, voltage=3833,
                wpid=0x408E, name="G915 TKL")})),
            FakeNode(b"/dev/c52b", 0xC52B, FakeReceiver({4: FakePairedDevice(
                kind=0x03, battery_feature=BATTERY_STATUS_FEATURE, battery=0, charging=True,
                wpid=0x4069, pairing_slot=4, name="MX Master 2S")})),
        ], slots={0xC539: 1, 0xC545: 1, 0xC52B: 6})
        self.assertEqual(sorted((d['name'], d['battery'], d['charging']) for d in devices),
                         [("G Pro", 23, False), ("G915 TKL", 55, False),
                          ("MX Master 2S", 50, True)])
        self.assertEqual((state / "mouse").read_text(), "23\n1\n0\nG Pro")
        self.assertEqual((state / "keyboard").read_text(), "55\n1\n0\nG915 TKL")


    def pending_case(self):
        """A scanned M720 and an event waiting for its write: the waiting
        reading is 30 %, the scan heard 50 %."""
        _, mod, devices, state = self.scan([FakeNode(b"/dev/unifying", 0xC52B, FakeReceiver({
            1: FakePairedDevice(kind=0x03, battery=50, wpid=0x405E, name="M720 Triathlon")}))],
            slots={0xC52B: 1})
        row_id = devices[0]['id']
        mod.battery_written_at[row_id] = time.time()   # the last write was just now
        _, index = mod.receiver_row_battery[row_id]
        mod.on_battery_event(row_id, [0x11, 0x01, index, 0x00, 30, 0x00, 0x00])
        self.assertIn(row_id, mod.pending_battery)
        mod.battery_written_at[row_id] = 0               # the second has passed
        return mod, devices, state / row_id

    def test_a_waiting_reading_is_written_when_nothing_newer_came(self):
        mod, _, row = self.pending_case()
        mod.flush_battery_events()
        self.assertEqual(row.read_text().splitlines()[0], "30")

    def test_a_waiting_reading_never_undoes_a_later_link_down(self):
        mod, devices, row = self.pending_case()
        mod.on_link_change(devices[0]['id'], True)
        mod.flush_battery_events()
        self.assertEqual(row.read_text().splitlines()[:2], ["50", "0"])

    def test_a_waiting_reading_never_undoes_a_later_scan(self):
        mod, devices, row = self.pending_case()
        time.sleep(0.01)
        devices[0]['battery'] = 90
        mod.publish_discovered(devices, time.time())
        mod.flush_battery_events()
        self.assertEqual(row.read_text().splitlines()[0], "90")

    def test_a_waiting_reading_never_goes_to_a_new_pairing(self):
        mod, devices, row = self.pending_case()
        mod.receiver_row_wpids[devices[0]['id']] = 0x4093
        lines = row.read_text().splitlines()
        lines[5] = "4093"
        row.write_text("\n".join(lines))
        mod.flush_battery_events()
        self.assertEqual(row.read_text().splitlines()[0], "50")

    def test_many_link_ups_arm_one_retry(self):
        _, mod, devices, _ = self.scan([FakeNode(b"/dev/unifying", 0xC52B, FakeReceiver({
            1: FakePairedDevice(kind=0x03, battery=50, wpid=0x405E, name="M720 Triathlon")}))],
            slots={0xC52B: 1})
        timers = []
        real_timer = threading.Timer

        def counting(*args, **kwargs):
            timer = real_timer(*args, **kwargs)
            timers.append(timer)
            return timer

        mod.LINK_UP_RETRY_SECONDS = 0.3
        mod.threading = types.SimpleNamespace(Timer=counting, Lock=threading.Lock,
                                              Thread=threading.Thread, Event=threading.Event,
                                              current_thread=threading.current_thread)
        for _ in range(50):
            mod.on_link_change(devices[0]['id'], False)
        self.assertEqual(len(timers), 1)
        mod.scan_requested.clear()
        self.assertTrue(mod.scan_requested.wait(2))
        mod.on_link_change(devices[0]['id'], False)   # fired: the next one arms again
        self.assertEqual(len(timers), 2)

class ReceiverBatteryEventTests(unittest.TestCase):
    """A battery event from a paired device updates its row and its kind's
    file at once, with no scan, like the old per-device monitors did."""

    def setUp(self):
        self.mouse = FakePairedDevice(kind=0x03, battery=50, wpid=0x405E, name="M720 Triathlon")
        self.bus = FakeHidBus([FakeNode(b"/dev/unifying", 0xC52B, FakeReceiver({1: self.mouse}))])
        self.mod = load_monitor_module(self.bus)
        self.mod.RECEIVER_SLOTS = {0xC52B: 1}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mod.STATE_DIR = os.path.join(tmp.name, "logibar")
        self.mod.HIDRAW_CLASS_DIR = os.path.join(tmp.name, "no-hidraw")
        self.mod.scan_receivers()
        self.state = Path(self.mod.STATE_DIR)
        self.row = self.state / f"rx-{self.mod.receiver_hash(b'/dev/unifying')}-1"
        listener = threading.Thread(target=self.mod.listen_receiver,
                                    args=(b"/dev/unifying", 1), daemon=True)
        listener.start()
        self.addCleanup(listener.join, 3)
        self.addCleanup(setattr, self.mod, 'running', False)
        time.sleep(0.3)   # the listener enables notifications first
        self.mod.scan_requested.clear()

    def wait_for(self, path, text):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if path.read_text() == text:
                return
            time.sleep(0.02)
        self.fail(f"{path.name} never became {text!r}: {path.read_text()!r}")

    def send(self, report):
        with self.bus._lock:
            self.bus._broadcast_locked(self.bus.nodes[b"/dev/unifying"], bytes(report))

    def test_a_battery_event_updates_the_row_and_the_file(self):
        self.send([0x11, 0x01, self.mouse.battery_index, 0x00, 37, 0x00, 0x01])
        self.wait_for(self.state / "mouse", "37\n1\n1\nM720 Triathlon")
        self.assertEqual(self.row.read_text().splitlines()[:4], ["37", "1", "1", "M720 Triathlon"])
        self.assertFalse(self.mod.scan_requested.is_set(), "a battery event asked for a scan")

    def test_only_an_event_of_the_battery_feature_counts(self):
        # A request echo (software id not 0), another feature's event, and
        # an event from a slot the scan does not know are no battery reading.
        self.send([0x11, 0x01, self.mouse.battery_index, 0x1d, 37, 0x00, 0x01])
        self.send([0x11, 0x01, self.mouse.name_index, 0x00, 37, 0x00, 0x01])
        self.send([0x11, 0x02, self.mouse.battery_index, 0x00, 37, 0x00, 0x01])
        time.sleep(0.3)
        self.assertEqual((self.state / "mouse").read_text(), "50\n1\n0\nM720 Triathlon")
        self.send([0x11, 0x01, self.mouse.battery_index, 0x00, 44, 0x00, 0x00])
        self.wait_for(self.state / "mouse", "44\n1\n0\nM720 Triathlon")


    def row_writes(self):
        """Count the writes of this test's row from here on."""
        writes, original = [], self.mod.atomic_state_write

        def counting(path, content):
            if path == str(self.row):
                writes.append(content)
            return original(path, content)

        self.mod.atomic_state_write = counting
        return writes

    def test_a_flood_of_events_writes_the_row_at_most_once_a_second(self):
        # A hostile device on the receiver repeats or alternates its battery
        # without pause: the row is written at most once a second, and the
        # last reading still arrives.
        writes = self.row_writes()
        for i in range(200):
            self.send([0x11, 0x01, self.mouse.battery_index, 0x00, 30 + i % 2, 0x00, 0x00])
        time.sleep(0.5)
        self.assertLessEqual(len(writes), 1)
        self.wait_for(self.state / "mouse", "31\n1\n0\nM720 Triathlon")
        self.assertLessEqual(len(writes), 3)

    def test_a_scan_that_started_before_an_event_keeps_the_event(self):
        # The scan heard 50 %; a 20 % event came after the scan began and
        # before it published: the row keeps 20 %.
        started = time.time()
        devices = self.mod.discover_receiver_devices()
        self.send([0x11, 0x01, self.mouse.battery_index, 0x00, 20, 0x00, 0x00])
        self.wait_for(self.state / "mouse", "20\n1\n0\nM720 Triathlon")
        self.mod.publish_discovered(devices, started)
        self.assertEqual(self.row.read_text().splitlines()[0], "20")
        self.assertEqual((self.state / "mouse").read_text(), "20\n1\n0\nM720 Triathlon")

    def test_an_event_does_not_write_a_row_that_names_another_pairing(self):
        # The row on disk already names the device a newer scan found in
        # the slot; an event decoded for the old pairing must not touch it.
        lines = self.row.read_text().splitlines()
        lines[5] = "4093"
        self.row.write_text("\n".join(lines))
        self.send([0x11, 0x01, self.mouse.battery_index, 0x00, 37, 0x00, 0x00])
        time.sleep(0.3)
        self.assertEqual(self.row.read_text().splitlines()[0], "50")

    def test_a_scan_that_misses_the_reading_keeps_the_battery_index(self):
        # A scan whose battery request got no answer: events still decode.
        self.bus.intercept_next(
            b"/dev/unifying",
            lambda cmd: cmd[0] == 0x11 and cmd[2] == self.mouse.battery_index,
            lambda _request, _reply: [])
        self.mod.scan_receivers()
        self.assertIn(self.row.name, self.mod.receiver_row_battery)
        self.send([0x11, 0x01, self.mouse.battery_index, 0x00, 42, 0x00, 0x00])
        self.wait_for(self.state / "mouse", "42\n1\n0\nM720 Triathlon")

    def test_a_link_up_asks_for_one_more_scan_a_little_later(self):
        self.mod.LINK_UP_RETRY_SECONDS = 0.4
        self.bus.emit_link(b"/dev/unifying", off=False, slot=1)
        deadline = time.monotonic() + 2
        while not self.mod.scan_requested.is_set():
            self.assertLess(time.monotonic(), deadline, "the link-up asked for no scan")
            time.sleep(0.02)
        self.mod.scan_requested.clear()
        self.assertTrue(self.mod.scan_requested.wait(2), "no second scan after a link-up")

    def test_an_older_projection_never_writes_after_a_newer_one(self):
        # One projection reads the rows while the mouse is on, then stalls;
        # the mouse goes off and a second projection runs. The first must
        # not write its older view after the second.
        mod, row_name = self.mod, self.row.name
        reading, stall = threading.Event(), threading.Event()
        original = mod.read_small_file

        def stalling(path, limit):
            content = original(path, limit)
            if (os.path.basename(path) == row_name
                    and threading.current_thread().name == "older"):
                reading.set()
                stall.wait(2)
            return content

        mod.read_small_file = stalling
        older = threading.Thread(target=mod.publish_categories, name="older")
        older.start()
        self.assertTrue(reading.wait(2))
        lines = self.row.read_text().splitlines()
        lines[1] = "0"
        self.row.write_text("\n".join(lines))
        newer = threading.Thread(target=mod.publish_categories, name="newer")
        newer.start()
        time.sleep(0.2)
        stall.set()
        older.join(3)
        newer.join(3)
        self.assertEqual((self.state / "mouse").read_text(), "0\n0\n0\n")

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

    def test_the_cli_takes_the_cable_reading_from_its_own_file(self):
        # The probook case: "mouse" is the lowest (the M720, for the
        # single-device widget); the cable PRO X 2 is in "wired-mouse". The
        # CLI shows both, hides the silent row of the cabled mouse, and its
        # lowest is the M720.
        now = int(time.time())
        status = self._status_with({
            "receiver-devices": "logibar-receivers-v1\nrx-0a0b0c0d-1\tmouse\nrx-11223344-1\tmouse\n",
            "rx-0a0b0c0d-1": f"50\n1\n0\nM720 Triathlon\n{now}\n405e",
            "rx-11223344-1": f"98\n0\n0\nPRO X 2\n{now - 60}\n40a9",
            "mouse": "50\n1\n0\nM720 Triathlon",
            "wired-mouse": "98\n1\n1\nPRO X 2\nwired rx-11223344",
        }, flags=())
        self.assertEqual(sorted((d['id'], d['name'], d['battery']) for d in status['devices']),
                         [("mouse", "PRO X 2", 98), ("rx-0a0b0c0d-1", "M720 Triathlon", 50)])
        self.assertEqual(status['aggregate']['worst_battery'], 50)

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
        mod.subprocess = types.SimpleNamespace(run=lambda *a, **k: None,
                                               DEVNULL=subprocess.DEVNULL)
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
            cable = Path(mod.STATE_DIR) / 'wired-mouse'
            category.write_text('90\n1\n0\nMX Master 2S')
            self.assertEqual(status()['devices'], [])
            cable.write_text('80\n1\n0\nG Pro Wired\nwired')
            self.assertEqual(status()['devices'][0]['battery'], 80)
            receiver.devices[1] = mouse
            del bus.nodes[b"/dev/unifying"]
            mod.scan_receivers()
            self.assertEqual(status()['devices'][0]['battery'], 80)
            cable.unlink()
            category.unlink()
            self.assertEqual(status()['devices'], [])
        self.assertEqual(bus.protocol_errors, [])


if __name__ == "__main__":
    unittest.main()
