#!/usr/bin/env python3
import importlib.util
import sys
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


if __name__ == "__main__":
    unittest.main()
