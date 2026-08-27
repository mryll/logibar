#!/usr/bin/env python3
import importlib.util
import sys
import tempfile
import threading
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

sys.modules["hid"] = types.SimpleNamespace()

SCRIPT = Path(__file__).parent.parent / "logibar-hidpp-monitor"
loader = SourceFileLoader("logibar_hidpp_monitor", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
monitor = importlib.util.module_from_spec(spec)
loader.exec_module(monitor)


class ReceiverSelectionTests(unittest.TestCase):
    def setUp(self):
        self.get_device_kind = monitor.get_device_kind
        monitor.receiver_kinds.clear()

    def tearDown(self):
        monitor.get_device_kind = self.get_device_kind

    def test_shared_pid_selects_and_caches_each_physical_receiver(self):
        kinds = {b"/dev/keyboard": 0x00, b"/dev/mouse": 0x03}
        calls = []
        monitor.get_device_kind = lambda path: calls.append(path) or kinds[path]
        devices = [
            {"path": b"/dev/keyboard"},
            {"path": b"/dev/keyboard"},
            {"path": b"/dev/mouse"},
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
        devices = [{"path": b"/dev/receiver"}]

        self.assertIsNone(
            monitor.receiver_path_for_device(0xC547, "mouse", devices)
        )

    def test_absent_shared_device_keeps_present_receiver_cached(self):
        calls = []
        monitor.get_device_kind = lambda path: calls.append(path) or 0x00
        devices = [{"path": b"/dev/keyboard"}]

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
        devices = [{"path": b"/dev/superlight2"}]

        self.assertEqual(
            monitor.receiver_path_for_device(0xC54D, "mouse", devices),
            b"/dev/superlight2",
        )

    def test_kind_probe_ignores_other_hidpp_responses(self):
        class FakeDevice:
            def __init__(self):
                self.responses = iter([
                    [],
                    [],
                    [0x11, 0x01, 0x00, 0x1d, 0x00],
                    [0x11, 0x01, 0x00, 0x0d, 0x03],
                    [],
                    [0x11, 0x01, 0x03, 0x10, 0x00],
                    [0x11, 0x01, 0x03, 0x20, 0x03],
                    [],
                    [0x11, 0x01, 0x03, 0x20, 0x03],
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
                    0xC547,
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
                    args=(None, 0xC547, "mouse", 10, state, state_lock),
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


if __name__ == "__main__":
    unittest.main()
