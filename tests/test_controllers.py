# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Spindle gate, controller links and MTConnect tests
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The spindle gate, the LinuxCNC and CNCjs links (against their simulations), the MTConnect view of the CNC and the bridge
running over each of them."""

import json
import socket
import unittest

from hydra_umc_sdk.bridge_contract import BridgeJob, CellState, JobPhase, MachineState, job_to_dict
from hydra_umc_bridge_cnc import (
    CncCellBridge,
    CncjsController,
    CncMqttBridge,
    CncSnapshot,
    GrblController,
    ShdrAdapter,
    SimulatedCncjs,
    SimulatedLinuxcnc,
    observations_from_snapshot,
    observations_payload,
    shdr_lines,
    snapshot_from_cncjs_state,
    snapshot_from_linuxcnc_stat,
    spindle_gate_from_environment,
)
from hydra_umc_bridge_cnc.linuxcnc_link import DEFAULTS
from hydra_umc_bridge_cnc.mqtt_transport import MTCONNECT_TOPIC, TOPIC_PREFIX
from hydra_umc_bridge_cnc.observation import snapshot_from_grbl_status, snapshot_from_mapping

from grbl_emulator import GrblEmulator

NO_ESTOP = lambda: False  # noqa: E731
DOOR_CLOSED = lambda: True  # noqa: E731
READY = lambda: CellState.READY  # noqa: E731


def job(phase=JobPhase.LOAD):
    return BridgeJob("j-1", "k-1", "orchestrator", phase, MachineState.IDLE, {})


class StatusFieldsTests(unittest.TestCase):
    def test_a_full_grbl_report_carries_its_spindle_feed_and_positions(self):
        snapshot = snapshot_from_grbl_status(
            "<Run|MPos:1.000,2.000,3.000|FS:500,12000|WCO:0.000,0.000,0.000|Ov:100,100,100>", estop=False, door_closed=True
        )
        self.assertEqual(snapshot.controller_state, "Run")
        self.assertEqual(snapshot.spindle_speed, 12000.0)
        self.assertEqual(snapshot.feed_rate, 500.0)
        self.assertEqual(snapshot.machine_position, (1.0, 2.0, 3.0))
        self.assertEqual(snapshot.work_offset, (0.0, 0.0, 0.0))
        self.assertEqual(snapshot.overrides, (100.0, 100.0, 100.0))

    def test_a_report_without_the_fields_leaves_them_unknown_not_zero(self):
        snapshot = snapshot_from_grbl_status("<Idle|MPos:0,0,0>", estop=False, door_closed=True)
        self.assertIsNone(snapshot.spindle_speed)
        self.assertIsNone(snapshot.spindle_stopped())

    def test_a_mapping_takes_only_real_numbers(self):
        good = snapshot_from_mapping({"state": "Idle", "estop": False, "door_closed": True, "spindle_speed": 0, "feed_rate": 0.0})
        self.assertTrue(good.spindle_stopped())
        for bad in ("0", True, float("nan"), float("inf"), None):
            with self.subTest(bad=bad):
                self.assertIsNone(snapshot_from_mapping({"spindle_speed": bad}).spindle_speed)

    def test_the_state_payload_keeps_working_with_the_new_fields(self):
        emulator = GrblEmulator()
        bridge = CncMqttBridge(emulator, NO_ESTOP, DOOR_CLOSED, READY)
        payload = json.loads(bridge.handle_message(f"{TOPIC_PREFIX}cmd/status", b"")[0].payload)
        self.assertEqual(payload["machine_state"], "IDLE")
        self.assertEqual(payload["spindle_speed"], 0.0)
        self.assertEqual(len(payload["machine_position"]), 3)


class SpindleGateTests(unittest.TestCase):
    def idle(self, **fields):
        return CncSnapshot("Idle", False, True, **fields)

    def test_a_stopped_spindle_lets_the_arm_in(self):
        for phase in (JobPhase.LOAD, JobPhase.UNLOAD):
            with self.subTest(phase=phase):
                self.assertTrue(CncCellBridge().plan(job(phase), CellState.READY, self.idle(spindle_speed=0.0, feed_rate=0.0)).allowed)

    def test_a_turning_spindle_keeps_the_arm_out_even_when_the_controller_is_idle(self):
        decision = CncCellBridge().plan(job(JobPhase.LOAD), CellState.READY, self.idle(spindle_speed=12000.0, feed_rate=0.0))
        self.assertFalse(decision.allowed)
        self.assertIn("spindle", decision.reason)

    def test_a_moving_feed_keeps_the_arm_out(self):
        self.assertFalse(CncCellBridge().plan(job(JobPhase.UNLOAD), CellState.READY, self.idle(spindle_speed=0.0, feed_rate=250.0)).allowed)

    def test_the_default_does_not_refuse_what_the_controller_did_not_say(self):
        # a GRBL board with the status mask that omits FS keeps working as before
        self.assertTrue(CncCellBridge().plan(job(JobPhase.LOAD), CellState.READY, self.idle()).allowed)

    def test_required_refuses_what_the_controller_did_not_say(self):
        decision = CncCellBridge("required").plan(job(JobPhase.LOAD), CellState.READY, self.idle())
        self.assertFalse(decision.allowed)
        self.assertIn("did not report", decision.reason)
        self.assertTrue(CncCellBridge("required").plan(job(JobPhase.LOAD), CellState.READY, self.idle(spindle_speed=0.0)).allowed)

    def test_off_is_the_old_behaviour(self):
        self.assertTrue(CncCellBridge("off").plan(job(JobPhase.LOAD), CellState.READY, self.idle(spindle_speed=12000.0)).allowed)

    def test_only_the_phases_in_which_an_arm_reaches_in_are_gated(self):
        # PROCESS is the machine's own work; the gate is about the arm
        self.assertTrue(CncCellBridge("required").plan(job(JobPhase.PROCESS), CellState.READY, self.idle(spindle_speed=12000.0)).allowed)

    def test_abort_is_always_forwarded(self):
        self.assertTrue(CncCellBridge("required").plan(job(JobPhase.ABORT), CellState.SAFE_STOP, self.idle(spindle_speed=12000.0)).allowed)

    def test_an_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            CncCellBridge("sometimes")

    def test_the_mode_comes_from_the_environment_with_a_conservative_default(self):
        self.assertEqual(spindle_gate_from_environment({}), "when_known")
        self.assertEqual(spindle_gate_from_environment({"HYDRA_UMC_CNC_SPINDLE_GATE": "Required"}), "required")
        self.assertEqual(spindle_gate_from_environment({"HYDRA_UMC_CNC_SPINDLE_GATE": "banana"}), "when_known")

    def test_over_mqtt_a_running_spindle_is_a_refused_job_end_to_end(self):
        emulator = GrblEmulator()
        emulator.spindle_speed = 8000  # idle, but the spindle is still turning
        bridge = CncMqttBridge(emulator, NO_ESTOP, DOOR_CLOSED, READY)
        answer = json.loads(bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", json.dumps(job_to_dict(job())).encode())[0].payload)
        self.assertFalse(answer["allowed"])
        emulator.spindle_speed = 0
        answer = json.loads(
            bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", json.dumps(job_to_dict(BridgeJob("j-2", "k-2", "o", JobPhase.LOAD, MachineState.IDLE, {}))).encode())[0].payload
        )
        self.assertTrue(answer["allowed"])

    def test_the_bridge_reads_the_mode_it_is_given(self):
        emulator = GrblEmulator()
        bridge = CncMqttBridge(emulator, NO_ESTOP, DOOR_CLOSED, READY, spindle_gate="off")
        emulator.spindle_speed = 8000
        answer = json.loads(bridge.handle_message(f"{TOPIC_PREFIX}cmd/job", json.dumps(job_to_dict(job())).encode())[0].payload)
        self.assertTrue(answer["allowed"])


class LinuxcncTests(unittest.TestCase):
    def link(self, **fields):
        simulation = SimulatedLinuxcnc(**fields)
        return simulation, simulation.controller()

    def read(self, link, estop=False, door=True):
        return link.read_snapshot(estop=lambda: estop, door_closed=lambda: door)

    def test_an_idle_machine_that_is_on(self):
        _, link = self.link()
        snapshot = self.read(link)
        self.assertEqual(snapshot.machine_state(), MachineState.IDLE)
        self.assertEqual(snapshot.spindle_speed, 0.0)
        self.assertTrue(snapshot.spindle_stopped())

    def test_a_running_program_and_a_turning_spindle(self):
        _, link = self.link(interp_state=DEFAULTS["INTERP_READING"], current_vel=2.5, spindle=[{"enabled": True, "speed": -10000.0}])
        snapshot = self.read(link)
        self.assertEqual(snapshot.machine_state(), MachineState.RUNNING)
        self.assertEqual(snapshot.spindle_speed, 10000.0)  # the direction is not a speed
        self.assertEqual(snapshot.feed_rate, 150.0)
        self.assertFalse(snapshot.spindle_stopped())

    def test_a_spindle_that_is_not_enabled_is_stopped_whatever_its_set_speed(self):
        _, link = self.link(spindle=[{"enabled": False, "speed": 5000.0}])
        self.assertEqual(self.read(link).spindle_speed, 0.0)

    def test_jogging_is_motion_even_with_the_interpreter_idle(self):
        _, link = self.link(current_vel=1.0)
        self.assertEqual(self.read(link).machine_state(), MachineState.RUNNING)

    def test_a_paused_program_is_holding(self):
        _, link = self.link(interp_state=DEFAULTS["INTERP_PAUSED"])
        self.assertEqual(self.read(link).machine_state(), MachineState.HOLDING)

    def test_an_emergency_stop_is_a_safe_stop_from_either_source(self):
        _, link = self.link(task_state=DEFAULTS["STATE_ESTOP"], estop=True)
        self.assertEqual(self.read(link).machine_state(), MachineState.SAFE_STOP)
        _, link = self.link()
        self.assertEqual(self.read(link, estop=True).machine_state(), MachineState.SAFE_STOP)

    def test_a_machine_that_is_off_is_a_fault_not_idle(self):
        _, link = self.link(task_state=DEFAULTS["STATE_OFF"])
        self.assertEqual(self.read(link).machine_state(), MachineState.FAULT)

    def test_the_door_comes_from_the_cell_and_a_missing_one_fails_closed(self):
        _, link = self.link()
        self.assertEqual(self.read(link, door=False).machine_state(), MachineState.SAFE_STOP)
        broken = link.read_snapshot(estop=lambda: False, door_closed=lambda: 1)  # not a bool
        self.assertEqual(broken.machine_state(), MachineState.SAFE_STOP)

    def test_a_linuxcnc_that_does_not_answer_is_not_idle(self):
        simulation, link = self.link(poll_error=RuntimeError("no emc"))
        snapshot = self.read(link)
        self.assertEqual(snapshot.controller_state, "")
        self.assertNotEqual(snapshot.machine_state(), MachineState.IDLE)

    def test_a_stat_with_nothing_on_it_is_unknown(self):
        snapshot = snapshot_from_linuxcnc_stat(object(), estop=False, door_closed=True)
        self.assertEqual(snapshot.machine_state(), MachineState.OFFLINE)

    def test_feed_hold_pauses_and_abort_stops_and_both_are_always_allowed(self):
        simulation, link = self.link(interp_state=DEFAULTS["INTERP_READING"], current_vel=2.0)
        result = link.feed_hold()
        self.assertTrue(result.allowed and result.executed)
        self.assertEqual(simulation.commands[-1], ("auto", DEFAULTS["AUTO_PAUSE"]))
        self.assertEqual(self.read(link).machine_state(), MachineState.HOLDING)
        result = link.soft_reset()
        self.assertTrue(result.executed)
        self.assertEqual(simulation.commands[-1], ("abort", None))
        self.assertEqual(self.read(link).machine_state(), MachineState.IDLE)

    def test_resume_needs_a_ready_cell_and_a_paused_machine(self):
        simulation, link = self.link(interp_state=DEFAULTS["INTERP_PAUSED"])
        held = self.read(link)
        self.assertFalse(link.cycle_start_resume(CellState.INHIBITED, held).executed)
        self.assertEqual(simulation.commands, [])
        self.assertFalse(link.cycle_start_resume(CellState.READY, CncSnapshot("Idle", False, True)).executed)
        result = link.cycle_start_resume(CellState.READY, held)
        self.assertTrue(result.executed)
        self.assertEqual(simulation.commands, [("auto", DEFAULTS["AUTO_RESUME"])])

    def test_a_command_linuxcnc_does_not_confirm_is_not_reported_as_executed(self):
        simulation, link = self.link(wait_result=-1)
        self.assertFalse(link.feed_hold().executed)

    def test_a_command_that_raises_is_not_executed(self):
        simulation, link = self.link()
        simulation.auto = lambda mode: (_ for _ in ()).throw(RuntimeError("emc down"))  # type: ignore[method-assign]
        result = link.feed_hold()
        self.assertTrue(result.allowed)
        self.assertFalse(result.executed)

    def test_the_link_never_runs_a_program(self):
        simulation, link = self.link(interp_state=DEFAULTS["INTERP_PAUSED"])
        link.feed_hold()
        link.soft_reset()
        link.cycle_start_resume(CellState.READY, self.read(link))
        self.assertTrue(all(name in ("auto", "abort") for name, _ in simulation.commands))
        self.assertNotIn(("auto", DEFAULTS["AUTO_RUN"]), simulation.commands)

    def test_open_linuxcnc_without_the_module_is_a_clear_error(self):
        from hydra_umc_bridge_cnc import open_linuxcnc

        with self.assertRaises(RuntimeError) as context:
            open_linuxcnc()
        self.assertIn("linuxcnc", str(context.exception))

    def test_the_bridge_runs_over_a_linuxcnc_link(self):
        simulation, link = self.link(interp_state=DEFAULTS["INTERP_READING"], current_vel=2.0)
        bridge = CncMqttBridge(None, NO_ESTOP, DOOR_CLOSED, READY, controller=link)
        state = json.loads(bridge.handle_message(f"{TOPIC_PREFIX}cmd/status", b"")[0].payload)
        self.assertEqual(state["machine_state"], "RUNNING")
        answer = json.loads(bridge.handle_message(f"{TOPIC_PREFIX}cmd/feed_hold", b"")[0].payload)
        self.assertTrue(answer["executed"])
        self.assertTrue(answer["confirmed"])
        self.assertEqual(answer["controller_state_after"], "HOLD")


class CncjsTests(unittest.TestCase):
    def setUp(self):
        self.server = SimulatedCncjs()
        self.now = [100.0]
        self.link = CncjsController(self.server, "/dev/ttyUSB0", max_age_seconds=3.0, clock=lambda: self.now[0])

    def read(self, estop=False, door=True):
        return self.link.read_snapshot(estop=lambda: estop, door_closed=lambda: door)

    def test_nothing_is_known_before_cncjs_says_anything(self):
        # no reading is the same as a failed query of the serial bridge: never idle, and the door counts as open
        self.assertEqual(self.read().controller_state, "")
        self.assertEqual(self.read().machine_state(), MachineState.SAFE_STOP)

    def test_an_open_port_and_a_state_are_an_idle_machine(self):
        self.server.open_port()
        self.server.publish_state("Idle")
        snapshot = self.read()
        self.assertEqual(snapshot.machine_state(), MachineState.IDLE)
        self.assertEqual(snapshot.spindle_speed, 0.0)
        self.assertEqual(snapshot.machine_position, (0.0, 0.0, 0.0))
        self.assertEqual(snapshot.overrides, (100.0, 100.0, 100.0))

    def test_the_substate_of_hold_and_door_is_kept(self):
        self.server.open_port()
        self.server.publish_state("Hold", sub_state=0)
        self.assertEqual(self.read().machine_state(), MachineState.HOLDING)
        self.server.publish_state("Door", sub_state=1)
        self.assertEqual(self.read().machine_state(), MachineState.SAFE_STOP)

    def test_an_old_reading_is_not_an_idle_machine(self):
        self.server.open_port()
        self.server.publish_state("Idle")
        self.now[0] += 3.5
        self.assertEqual(self.read().controller_state, "")
        self.assertNotEqual(self.read().machine_state(), MachineState.IDLE)

    def test_a_closed_port_forgets_the_state(self):
        self.server.open_port()
        self.server.publish_state("Idle")
        self.server.close_port()
        self.assertEqual(self.read().controller_state, "")
        self.assertNotEqual(self.read().machine_state(), MachineState.IDLE)

    def test_a_controller_that_is_not_grbl_is_not_read_as_one(self):
        self.server.open_port()
        self.server.broadcast("controller:state", "Marlin", {"status": {"activeState": "Idle"}})
        self.assertNotEqual(self.read().machine_state(), MachineState.IDLE)

    def test_a_payload_that_is_not_a_state_is_unknown(self):
        for payload in (None, "Idle", {}, {"status": "Idle"}, {"status": {"activeState": 5}}, {"status": {"activeState": ""}}):
            with self.subTest(payload=payload):
                self.assertEqual(snapshot_from_cncjs_state(payload, estop=False, door_closed=True).machine_state(), MachineState.OFFLINE)

    def test_numbers_that_are_not_numbers_are_not_readings(self):
        snapshot = snapshot_from_cncjs_state(
            {"status": {"activeState": "Idle", "spindle": "fast", "feedrate": True, "mpos": {"x": "nan", "y": "0", "z": "0"}, "ov": [1, 2]}},
            estop=False,
            door_closed=True,
        )
        self.assertIsNone(snapshot.spindle_speed)
        self.assertIsNone(snapshot.feed_rate)
        self.assertIsNone(snapshot.machine_position)
        self.assertIsNone(snapshot.overrides)

    def test_the_safeguards_of_the_cell_still_decide(self):
        self.server.open_port()
        self.server.publish_state("Idle")
        self.assertEqual(self.read(door=False).machine_state(), MachineState.SAFE_STOP)
        self.assertEqual(self.read(estop=True).machine_state(), MachineState.SAFE_STOP)

    def test_feed_hold_and_reset_use_the_cncjs_words_on_the_open_port(self):
        self.server.open_port()
        self.assertTrue(self.link.feed_hold().executed)
        self.assertTrue(self.link.soft_reset().executed)
        self.assertEqual(self.server.commands(), ["feedhold", "reset"])
        self.assertEqual(self.server.emitted[0], ("command", ("/dev/ttyUSB0", "feedhold")))

    def test_nothing_is_sent_when_cncjs_has_no_port_open(self):
        result = self.link.feed_hold()
        self.assertTrue(result.allowed)
        self.assertFalse(result.executed)
        self.assertEqual(self.server.emitted, [])

    def test_resume_needs_a_ready_cell_and_a_holding_machine(self):
        self.server.open_port()
        self.server.publish_state("Hold", sub_state=0)
        held = self.read()
        self.assertFalse(self.link.cycle_start_resume(CellState.FAULT, held).executed)
        self.server.publish_state("Idle")
        self.assertFalse(self.link.cycle_start_resume(CellState.READY, self.read()).executed)
        self.assertEqual(self.server.commands(), [])
        self.assertTrue(self.link.cycle_start_resume(CellState.READY, held).executed)
        self.assertEqual(self.server.commands(), ["cyclestart"])

    def test_a_failing_socket_is_a_command_that_did_not_happen(self):
        self.server.open_port()
        self.server.fail_emit = OSError("socket closed")
        result = self.link.feed_hold()
        self.assertTrue(result.allowed)
        self.assertFalse(result.executed)

    def test_the_link_never_sends_a_program(self):
        self.server.open_port()
        self.server.publish_state("Hold", sub_state=0)
        self.link.feed_hold()
        self.link.soft_reset()
        self.link.cycle_start_resume(CellState.READY, self.read())
        self.assertTrue(set(self.server.commands()) <= {"feedhold", "reset", "cyclestart"})
        self.assertNotIn("gcode", [event for event, _ in self.server.emitted])

    def test_the_constructor_validates_its_arguments(self):
        with self.assertRaises(ValueError):
            CncjsController(self.server, "")
        with self.assertRaises(ValueError):
            CncjsController(self.server, "/dev/ttyUSB0", max_age_seconds=0)
        with self.assertRaises(ValueError):
            CncjsController(self.server, "/dev/ttyUSB0", max_age_seconds=float("nan"))

    def test_open_cncjs_without_a_client_is_a_clear_error(self):
        from hydra_umc_bridge_cnc import open_cncjs

        try:
            import socketio  # noqa: F401

            self.skipTest("python-socketio is installed in this environment")
        except ImportError:
            pass
        with self.assertRaises(RuntimeError) as context:
            open_cncjs("http://localhost:8000", "/dev/ttyUSB0")
        self.assertIn("python-socketio", str(context.exception))

    def test_the_bridge_runs_over_a_cncjs_link_without_owning_the_port(self):
        self.server.open_port()
        self.server.publish_state("Run", feedrate=400, spindle=9000)
        bridge = CncMqttBridge(None, NO_ESTOP, DOOR_CLOSED, READY, controller=self.link)
        state = json.loads(bridge.handle_message(f"{TOPIC_PREFIX}cmd/status", b"")[0].payload)
        self.assertEqual(state["machine_state"], "RUNNING")
        self.assertEqual(state["spindle_speed"], 9000.0)
        self.server.publish_state("Hold", sub_state=0)  # what CNCjs reports after the feed hold
        answer = json.loads(bridge.handle_message(f"{TOPIC_PREFIX}cmd/feed_hold", b"")[0].payload)
        self.assertTrue(answer["executed"] and answer["confirmed"])
        self.assertEqual(self.server.commands(), ["feedhold"])


class BridgeConstructionTests(unittest.TestCase):
    def test_a_connection_or_a_controller_is_required(self):
        with self.assertRaises(ValueError):
            CncMqttBridge(None, NO_ESTOP, DOOR_CLOSED, READY)

    def test_grbl_stays_the_default_link(self):
        bridge = CncMqttBridge(GrblEmulator(), NO_ESTOP, DOOR_CLOSED, READY)
        self.assertIsInstance(bridge._controller, GrblController)  # noqa: SLF001

    def test_a_read_only_link_refuses_with_a_reason(self):
        from hydra_umc_bridge_cnc import ReadOnlyController

        link = ReadOnlyController()
        for result in (link.feed_hold(), link.soft_reset(), link.cycle_start_resume(CellState.READY, CncSnapshot("Hold", False, True))):
            self.assertFalse(result.allowed)
            self.assertFalse(result.executed)
            self.assertIn("read-only", result.reason)


class MtconnectTests(unittest.TestCase):
    def values(self, snapshot):
        return {item.id: item.value for item in observations_from_snapshot(snapshot)}

    def test_an_idle_machine(self):
        values = self.values(CncSnapshot("Idle", False, True, spindle_speed=0.0, feed_rate=0.0))
        self.assertEqual(values["avail"], "AVAILABLE")
        self.assertEqual(values["execution"], "READY")
        self.assertEqual(values["estop"], "ARMED")
        self.assertEqual(values["door"], "CLOSED")
        self.assertEqual(values["spindle_speed"], "0")
        self.assertEqual(values["system"], "NORMAL")

    def test_the_executions(self):
        cases = {"Run": "ACTIVE", "Hold:0": "FEED_HOLD", "Alarm": "STOPPED", "Stopped": "STOPPED"}
        for word, execution in cases.items():
            with self.subTest(word=word):
                self.assertEqual(self.values(CncSnapshot(word, False, True))["execution"], execution)

    def test_a_machine_that_is_not_reporting_is_unavailable_not_ready(self):
        values = self.values(CncSnapshot("", False, True))
        self.assertEqual(values["avail"], "UNAVAILABLE")
        self.assertEqual(values["execution"], "UNAVAILABLE")
        self.assertEqual(values["spindle_speed"], "UNAVAILABLE")
        self.assertEqual(values["system"], "UNAVAILABLE")

    def test_a_spindle_the_controller_did_not_report_is_unavailable(self):
        self.assertEqual(self.values(CncSnapshot("Idle", False, True))["spindle_speed"], "UNAVAILABLE")

    def test_estop_and_door_are_conditions_with_a_native_code(self):
        by_id = {item.id: item for item in observations_from_snapshot(CncSnapshot("Idle", True, True))}
        self.assertEqual(by_id["estop"].value, "TRIGGERED")
        self.assertEqual((by_id["system"].value, by_id["system"].native_code), ("FAULT", "ESTOP"))
        by_id = {item.id: item for item in observations_from_snapshot(CncSnapshot("Idle", False, False))}
        self.assertEqual(by_id["door"].value, "OPEN")
        self.assertEqual(by_id["system"].native_code, "DOOR_OPEN")
        self.assertEqual(self.values(CncSnapshot("Idle", True, True))["execution"], "INTERRUPTED")

    def test_shdr_lines_follow_the_protocol(self):
        lines = shdr_lines(CncSnapshot("Run", False, True, spindle_speed=8000.0, feed_rate=250.5), 1_700_000_000_123)
        self.assertIn("2023-11-14T22:13:20.123Z|execution|ACTIVE", lines)
        self.assertIn("2023-11-14T22:13:20.123Z|spindle_speed|8000", lines)
        self.assertIn("2023-11-14T22:13:20.123Z|path_feedrate|250.5", lines)
        self.assertIn("2023-11-14T22:13:20.123Z|system|NORMAL||||", lines)
        fault = shdr_lines(CncSnapshot("Run", True, True), 0)
        self.assertIn("1970-01-01T00:00:00.000Z|system|FAULT|ESTOP|||emergency stop", fault)
        unavailable = shdr_lines(CncSnapshot("", False, True), 0)
        self.assertIn("1970-01-01T00:00:00.000Z|system|UNAVAILABLE||||", unavailable)

    def test_the_payload_is_json_with_the_device_and_the_time(self):
        payload = observations_payload(CncSnapshot("Idle", False, True), 5, "Lathe")
        self.assertEqual(payload["device"], "Lathe")
        self.assertEqual(payload["observed_at"], 5)
        self.assertEqual({item["id"] for item in payload["observations"]}, {"avail", "execution", "estop", "door", "spindle_speed", "path_feedrate", "system"})
        json.dumps(payload)

    def test_the_topic_is_off_by_default_and_retained_when_switched_on(self):
        off = CncMqttBridge(GrblEmulator(), NO_ESTOP, DOOR_CLOSED, READY, mtconnect=False)
        self.assertEqual([p.topic for p in off.handle_message(f"{TOPIC_PREFIX}cmd/status", b"")], [f"{TOPIC_PREFIX}state"])
        on = CncMqttBridge(GrblEmulator(), NO_ESTOP, DOOR_CLOSED, READY, mtconnect=True, clock=lambda: 7.0)
        publishes = on.handle_message(f"{TOPIC_PREFIX}cmd/status", b"")
        self.assertEqual([p.topic for p in publishes], [f"{TOPIC_PREFIX}state", MTCONNECT_TOPIC])
        self.assertTrue(publishes[1].retain)
        self.assertEqual(json.loads(publishes[1].payload)["observed_at"], 7000)


class ShdrAdapterTests(unittest.TestCase):
    def connect(self, port):
        client = socket.create_connection(("127.0.0.1", port), timeout=3)
        return client, client.makefile("rb")

    def test_a_client_gets_the_latest_values_and_then_the_changes_and_can_only_ping(self):
        adapter = ShdrAdapter("Mill", clock=lambda: 1_700_000_000.0)
        adapter.update(CncSnapshot("Idle", False, True, spindle_speed=0.0))
        port = adapter.listen()
        try:
            client, reader = self.connect(port)
            self.assertEqual(reader.readline().strip(), b"* shdrVersion: 2")
            self.assertEqual(reader.readline().strip(), b"* device: Mill")
            greeting = []
            while len(greeting) < 7:
                greeting.append(reader.readline().decode().strip())
            self.assertIn("2023-11-14T22:13:20.000Z|execution|READY", greeting)
            client.sendall(b"* PING\n")
            self.assertEqual(reader.readline().strip(), b"* PONG 10000")
            client.sendall(b"2023|execution|ACTIVE\n")  # a client cannot set a value
            changed = adapter.update(CncSnapshot("Run", False, True, spindle_speed=9000.0))
            self.assertEqual(len(changed), 2)  # execution and spindle_speed; nothing else changed
            self.assertEqual(sorted(reader.readline().decode().strip().split("|")[1] for _ in range(2)), ["execution", "spindle_speed"])
            self.assertEqual(adapter.update(CncSnapshot("Run", False, True, spindle_speed=9000.0)), [])
            client.close()
        finally:
            adapter.close()

    def test_a_pipe_in_a_value_is_escaped(self):
        from hydra_umc_bridge_cnc.mtconnect_publish import escape_shdr

        self.assertEqual(escape_shdr("a|b\nc"), "a\\|b c")


if __name__ == "__main__":
    unittest.main()
