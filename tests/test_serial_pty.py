# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Real serial port tests over a pseudo-terminal
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The real serial transport against a pseudo-terminal (POSIX with pyserial only; skipped elsewhere).

`open_serial_port()` and `ReconnectingSerialConnection` are the two parts that the in-memory fakes of `test_serial_transport.py`
cannot reach. Here a thread plays the controller on the master side of an `os.openpty()` pair, running the GRBL emulator, and the
bridge talks to the slave side through a real pyserial port: the status query, a real-time byte and the reopening of a port that
went away.
"""

from __future__ import annotations

import os
import threading
import time
import unittest

try:
    import serial  # noqa: F401  (only to know whether pyserial is installed)
except ImportError:  # pragma: no cover - depends on the machine
    serial = None  # type: ignore[assignment]

if os.name == "posix":
    import select

from hydra_umc_bridge_cnc import GrblRealtimeControl, GrblSerialProbe, open_serial_port
from hydra_umc_bridge_cnc.serial_transport import ReconnectingSerialConnection
from hydra_umc_sdk.bridge_contract import MachineState

from grbl_emulator import GrblEmulator


class _Controller(threading.Thread):
    """The machine side: bytes that arrive on the master are fed to the emulator, what it answers goes back."""

    def __init__(self, master_fd: int) -> None:
        super().__init__(daemon=True)
        self.master_fd = master_fd
        self.emulator = GrblEmulator()
        self.stop_flag = threading.Event()

    def run(self) -> None:
        while not self.stop_flag.is_set():
            try:
                ready, _, _ = select.select([self.master_fd], [], [], 0.05)
                if ready:
                    data = os.read(self.master_fd, 256)
                    if data:
                        self.emulator.write(data)
                while True:
                    line = self.emulator.readline()
                    if not line:
                        break
                    os.write(self.master_fd, line)
            except OSError:
                return


@unittest.skipUnless(os.name == "posix" and serial is not None, "needs POSIX pseudo-terminals and pyserial")
class SerialPortOverPtyTest(unittest.TestCase):
    def _pair(self):
        master, slave = os.openpty()
        controller = _Controller(master)
        controller.start()
        self.addCleanup(self._finish, controller, master, slave)
        return controller, os.ttyname(slave)

    @staticmethod
    def _finish(controller: _Controller, master: int, slave: int) -> None:
        controller.stop_flag.set()
        controller.join(1)
        for fd in (master, slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def test_a_real_status_query_is_answered_through_the_port(self) -> None:
        controller, path = self._pair()
        port = open_serial_port(path, 115200, 1.0)
        self.addCleanup(port.close)
        snapshot = GrblSerialProbe().query_status(port, estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.IDLE)

    def test_a_real_time_byte_reaches_the_controller(self) -> None:
        controller, path = self._pair()
        port = open_serial_port(path, 115200, 1.0)
        self.addCleanup(port.close)
        probe = GrblSerialProbe()
        control = GrblRealtimeControl()
        controller.emulator.start_program()  # a feed hold only changes a machine that is running
        result = control.feed_hold(port)
        self.assertTrue(result.executed, result)
        deadline = time.monotonic() + 2
        state = MachineState.IDLE
        while time.monotonic() < deadline and state is not MachineState.HOLDING:
            state = probe.query_status(port, estop=lambda: False, door_closed=lambda: True).machine_state()
        self.assertEqual(state, MachineState.HOLDING)

    def test_a_port_that_went_away_is_reopened_once(self) -> None:
        controller, path = self._pair()
        opened: list[object] = []

        def connect():
            port = open_serial_port(path, 115200, 1.0)
            opened.append(port)
            return port

        link = ReconnectingSerialConnection(connect)
        self.addCleanup(link.close)
        snapshot = GrblSerialProbe().query_status(link, estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.IDLE)
        opened[0].close()  # the cable is pulled: the next write raises and the link opens the port again
        link.write(b"?")
        self.assertEqual(len(opened), 2)
        snapshot = GrblSerialProbe().query_status(link, estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.IDLE)


if __name__ == "__main__":
    unittest.main()
