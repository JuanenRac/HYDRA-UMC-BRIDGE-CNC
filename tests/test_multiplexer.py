# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Serial port multiplexer tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The multiplexer between the bridge, a G-code sender and a GRBL controller, against the GRBL emulator and real TCP on loopback."""

from __future__ import annotations

import socket
import time
import unittest

from hydra_umc_bridge_cnc import GrblController
from hydra_umc_bridge_cnc.multiplexer import (
    ALLOW_REMOTE_ENVIRONMENT,
    LISTEN_ENVIRONMENT,
    MODE_ENVIRONMENT,
    PortSharingConfig,
    SerialMultiplexer,
    port_sharing_from_environment,
    share_serial_port,
)
from hydra_umc_sdk.bridge_contract import CellState, MachineState

from grbl_emulator import GrblEmulator


def _wait(condition, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def _read_until(sock: socket.socket, needle: bytes, seconds: float = 3.0) -> bytes:
    sock.settimeout(0.1)
    data = b""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and needle not in data:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        data += chunk
    return data


class _Rig(unittest.TestCase):
    """A GRBL emulator behind a multiplexer on an ephemeral loopback port."""

    def setUp(self) -> None:
        self.grbl = GrblEmulator()
        self.mux = SerialMultiplexer(self.grbl, PortSharingConfig(enabled=True, port=0, read_timeout_seconds=1.0))
        self.address = self.mux.start()
        self.addCleanup(self.mux.close)
        self.link = self.mux.bridge_link()

    def connect_sender(self) -> socket.socket:
        sender = socket.create_connection(self.address, timeout=2.0)
        self.addCleanup(sender.close)
        self.assertTrue(_wait(lambda: self.mux.sender_connected))
        return sender


class ConfigTests(unittest.TestCase):
    def test_sharing_is_off_unless_asked_for(self) -> None:
        self.assertFalse(port_sharing_from_environment({}).enabled)
        self.assertFalse(port_sharing_from_environment({MODE_ENVIRONMENT: "ser2net"}).enabled)
        self.assertFalse(port_sharing_from_environment({MODE_ENVIRONMENT: "off"}).enabled)

    def test_the_multiplexer_mode_and_address_come_from_the_environment(self) -> None:
        config = port_sharing_from_environment({MODE_ENVIRONMENT: "Multiplexer", LISTEN_ENVIRONMENT: "127.0.0.1:6001"})
        self.assertEqual((config.enabled, config.host, config.port), (True, "127.0.0.1", 6001))

    def test_a_bad_address_turns_sharing_off(self) -> None:
        self.assertFalse(port_sharing_from_environment({MODE_ENVIRONMENT: "multiplexer", LISTEN_ENVIRONMENT: "host:abc"}).enabled)

    def test_a_front_on_the_network_needs_an_explicit_switch(self) -> None:
        with self.assertRaises(ValueError):
            SerialMultiplexer(GrblEmulator(), PortSharingConfig(enabled=True, host="0.0.0.0", port=0))
        allowed = port_sharing_from_environment({MODE_ENVIRONMENT: "multiplexer", LISTEN_ENVIRONMENT: "0.0.0.0:6001", ALLOW_REMOTE_ENVIRONMENT: "1"})
        self.assertTrue(allowed.allow_remote)
        allowed.validate()

    def test_sharing_off_hands_back_the_port_itself(self) -> None:
        grbl = GrblEmulator()
        connection, multiplexer = share_serial_port(grbl, PortSharingConfig(enabled=False))
        self.assertIs(connection, grbl)
        self.assertIsNone(multiplexer)


class BridgeAndSenderShareThePortTests(_Rig):
    def test_the_bridge_reads_the_status_while_a_sender_is_connected(self) -> None:
        self.connect_sender()
        controller = GrblController(self.link)
        snapshot = controller.read_snapshot(estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.IDLE)

    def test_the_sender_runs_a_program_line_and_gets_its_answer(self) -> None:
        sender = self.connect_sender()
        sender.sendall(b"G1 X10 F100\n")
        self.assertIn(b"ok", _read_until(sender, b"ok"))

    def test_the_bridge_can_feed_hold_and_resume_a_running_job(self) -> None:
        sender = self.connect_sender()
        sender.sendall(b"G1 X10 F100\n")
        _read_until(sender, b"ok")
        self.grbl.start_program()
        controller = GrblController(self.link)
        self.assertTrue(controller.feed_hold().executed)
        self.assertTrue(_wait(lambda: self.grbl.state == "Hold"))
        snapshot = controller.read_snapshot(estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.HOLDING)
        self.assertTrue(controller.cycle_start_resume(CellState.READY, snapshot).executed)
        self.assertTrue(_wait(lambda: self.grbl.state == "Run"))

    def test_a_status_report_asked_by_the_bridge_also_reaches_the_sender_harmlessly(self) -> None:
        sender = self.connect_sender()
        GrblController(self.link).read_snapshot(estop=lambda: False, door_closed=lambda: True)
        self.assertIn(b"<Idle", _read_until(sender, b"<Idle"))

    def test_the_sender_realtime_bytes_pass_and_the_bridge_sees_the_result(self) -> None:
        sender = self.connect_sender()
        sender.sendall(b"G1 X10 F100\n")
        _read_until(sender, b"ok")
        self.grbl.start_program()
        sender.sendall(b"!")
        self.assertTrue(_wait(lambda: self.grbl.state == "Hold"))
        snapshot = GrblController(self.link).read_snapshot(estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.HOLDING)

    def test_a_second_sender_is_refused_while_one_is_connected(self) -> None:
        self.connect_sender()
        second = socket.create_connection(self.address, timeout=2.0)
        self.addCleanup(second.close)
        self.assertIn(b"already connected", _read_until(second, b"connected"))
        self.assertTrue(self.mux.sender_connected)

    def test_a_new_sender_can_connect_after_the_first_left(self) -> None:
        first = self.connect_sender()
        first.close()
        self.assertTrue(_wait(lambda: not self.mux.sender_connected))
        sender = self.connect_sender()
        sender.sendall(b"G1 X1 F100\n")
        self.assertIn(b"ok", _read_until(sender, b"ok"))

    def test_the_bridge_works_with_no_sender_at_all(self) -> None:
        snapshot = GrblController(self.link).read_snapshot(estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.IDLE)


class TheBridgeNeverStreamsAProgramTests(_Rig):
    def test_only_realtime_bytes_can_be_written_by_the_bridge(self) -> None:
        for forbidden in (b"G1 X10\n", b"$X\n", b"M3 S1000\n", b"?G", b"", b"\n"):
            with self.assertRaises(ValueError):
                self.link.write(forbidden)
        self.assertEqual(self.grbl.state, "Idle")

    def test_every_realtime_byte_is_accepted(self) -> None:
        for byte in (b"?", b"!", b"~", b"\x18"):
            self.assertEqual(self.link.write(byte), 1)

    def test_a_refused_write_does_not_reach_the_controller(self) -> None:
        self.grbl.start_program()
        with self.assertRaises(ValueError):
            self.link.write(b"!G1 X1\n")
        self.assertEqual(self.grbl.state, "Run")


class SenderGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.grbl = GrblEmulator()
        self.allowed = True
        self.mux = SerialMultiplexer(self.grbl, PortSharingConfig(enabled=True, port=0), sender_allowed=lambda: self.allowed)
        self.address = self.mux.start()
        self.addCleanup(self.mux.close)

    def test_a_line_is_held_while_the_gate_says_no_and_realtime_bytes_still_pass(self) -> None:
        sender = socket.create_connection(self.address, timeout=2.0)
        self.addCleanup(sender.close)
        self.allowed = False
        sender.sendall(b"G1 X10 F100\n")
        self.assertIn(b"error:9", _read_until(sender, b"error:9"))
        self.grbl.start_program()
        sender.sendall(b"!")
        self.assertTrue(_wait(lambda: self.grbl.state == "Hold"))
        self.allowed = True
        sender.sendall(b"G1 X10 F100\n")
        self.assertIn(b"ok", _read_until(sender, b"ok"))

    def test_a_gate_that_fails_holds_the_line(self) -> None:
        def broken() -> bool:
            raise RuntimeError("cannot tell")

        mux = SerialMultiplexer(GrblEmulator(), PortSharingConfig(enabled=True, port=0), sender_allowed=broken)
        address = mux.start()
        self.addCleanup(mux.close)
        sender = socket.create_connection(address, timeout=2.0)
        self.addCleanup(sender.close)
        sender.sendall(b"G1 X1 F100\n")
        self.assertIn(b"error:9", _read_until(sender, b"error:9"))


class FailureTests(unittest.TestCase):
    def test_a_dead_port_leaves_the_bridge_reading_nothing_and_the_error_visible(self) -> None:
        grbl = GrblEmulator()
        mux = SerialMultiplexer(grbl, PortSharingConfig(enabled=True, port=0))
        mux.start()
        self.addCleanup(mux.close)
        grbl.close()
        self.assertTrue(_wait(lambda: mux.port_error is not None))
        snapshot = GrblController(mux.bridge_link()).read_snapshot(estop=lambda: False, door_closed=lambda: True)
        self.assertNotEqual(snapshot.machine_state(), MachineState.IDLE)

    def test_sharing_on_gives_the_bridge_a_link_and_a_multiplexer(self) -> None:
        grbl = GrblEmulator()
        connection, multiplexer = share_serial_port(grbl, PortSharingConfig(enabled=True, port=0))
        self.assertIsNotNone(multiplexer)
        self.addCleanup(multiplexer.close)
        self.assertIsNot(connection, grbl)
        snapshot = GrblController(connection).read_snapshot(estop=lambda: False, door_closed=lambda: True)
        self.assertEqual(snapshot.machine_state(), MachineState.IDLE)


if __name__ == "__main__":
    unittest.main()
