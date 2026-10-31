# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - The service: one entry point, one configuration file
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Run the bridge as a service: ``python -m hydra_umc_bridge_cnc --config /etc/hydra-umc/bridge-cnc.toml``.

Everything that depends on the machine lives in the configuration file and nothing in the code: which controller (a GRBL
board on a serial port, CNCjs, LinuxCNC), where the E-STOP and the door come from, how the serial port is shared with a
G-code sender, the spindle gate, MTConnect, the MQTT broker and where the state of the cell comes from. The file is TOML; a
key the service does not know is refused, so a typo cannot silently leave a safeguard unconfigured.

Credentials do not go in the file: the MQTT user, password and TLS come from the service environment (``HYDRA_MQTT_*``), the
CNCjs token from ``HYDRA_UMC_CNC_CNCJS_TOKEN``; systemd reads them from a file only root can read.

Fail-closed rules of the service:

* an E-STOP or a door with no source is "E-STOP pressed" and "door open";
* a signal file that is missing, unreadable, unknown or older than ``max_age_seconds`` gives the same answer, and so does a
  GPIO line that cannot be read;
* the state of the cell comes from ``[cell_state]``: a file that is missing, unknown or stale is ``SAFE_STOP``, and the
  default source is a fixed ``INHIBITED``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from hydra_umc_sdk.bridge_contract import CellState

from .cell import SPINDLE_GATE_MODES, SPINDLE_GATE_WHEN_KNOWN
from .global_stop import DEFAULT_TOPIC, GlobalStopSettings
from .multiplexer import DEFAULT_HOST, DEFAULT_PORT, PortSharingConfig

CONFIG_ENVIRONMENT = "HYDRA_UMC_BRIDGE_CNC_CONFIG"
CNCJS_TOKEN_ENVIRONMENT = "HYDRA_UMC_CNC_CNCJS_TOKEN"
DEFAULT_CONFIG_PATH = "/etc/hydra-umc/bridge-cnc.toml"
CONTROLLER_KINDS = ("grbl", "cncjs", "linuxcnc")

_LOG = logging.getLogger(__name__)


class ConfigError(ValueError):
    """The configuration file is wrong; the message names the key."""


@dataclass(frozen=True)
class MqttSettings:
    host: str = "127.0.0.1"
    port: int = 1883
    client_id: str = "hydra-umc-bridge-cnc"
    max_connect_attempts: int = 5
    connect_retry_delay_seconds: float = 2.0


@dataclass(frozen=True)
class ControllerConfig:
    kind: str = "grbl"
    device: str | None = None  # grbl: port name or stable USB id; cncjs: the port CNCjs has open
    baud: int = 115200
    timeout_seconds: float = 1.0
    url: str | None = None  # cncjs only


@dataclass(frozen=True)
class SignalConfig:
    """Where one safety signal comes from.

    ``none``: no source (the answer is the unsafe one). ``gpio``: ``chip`` and ``line``. ``file``: the first word of ``path``.
    ``unsafe_level`` is what the signal reads when the cell is NOT safe ("high" or "low"; for a file, "1" or "0"): the wiring
    is declared, not guessed, and a cut wire reads as unsafe (the bias is chosen for it).
    """

    source: str = "none"
    chip: str | None = None
    line: int | None = None
    path: str | None = None
    unsafe_level: str | None = None
    debounce_ms: float = 10.0
    max_age_seconds: float = 30.0


@dataclass(frozen=True)
class CellStateConfig:
    source: str = "fixed"
    value: CellState = CellState.INHIBITED
    path: str | None = None
    max_age_seconds: float = 30.0


@dataclass(frozen=True)
class ServiceConfig:
    estop: SignalConfig = field(default_factory=SignalConfig)
    door: SignalConfig = field(default_factory=SignalConfig)
    mqtt: MqttSettings = field(default_factory=MqttSettings)
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    port_sharing: PortSharingConfig = field(default_factory=PortSharingConfig)
    cell_state: CellStateConfig = field(default_factory=CellStateConfig)
    spindle_gate: str = SPINDLE_GATE_WHEN_KNOWN
    mtconnect: bool = False
    idempotency_path: str | None = None
    global_stop: GlobalStopSettings = field(default_factory=GlobalStopSettings)


def _table(parent: dict, key: str) -> dict:
    value = parent.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _only(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown key in {where}: {', '.join(unknown)}")


def _kind(table: dict, key: str, kind: type, where: str, default: object = None):
    if key not in table:
        return default
    value = table[key]
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) if kind is float else isinstance(value, kind) and (kind is not int or not isinstance(value, bool))
    if not ok:
        raise ConfigError(f"{where}.{key} must be a {kind.__name__}")
    return float(value) if kind is float else value


def _signal(parent: dict, name: str, levels: tuple[str, str]) -> SignalConfig:
    table = _table(parent, name)
    where = name
    _only(table, {"source", "chip", "line", "path", "unsafe_level", "debounce_ms", "max_age_seconds"}, where)
    source = _kind(table, "source", str, where, "none")
    if source not in {"none", "gpio", "file"}:
        raise ConfigError(f"{where}.source must be none, gpio or file")
    unsafe = _kind(table, "unsafe_level", str, where)
    config = SignalConfig(
        source,
        _kind(table, "chip", str, where),
        _kind(table, "line", int, where),
        _kind(table, "path", str, where),
        unsafe,
        _kind(table, "debounce_ms", float, where, 10.0),
        _kind(table, "max_age_seconds", float, where, 30.0),
    )
    if source == "none":
        return config
    if unsafe not in ("high", "low"):
        # the safe level depends on the wiring and not on the code
        raise ConfigError(f"{where}.unsafe_level must be high or low (what the signal reads when {levels[0]})")
    if config.max_age_seconds <= 0 or config.debounce_ms < 0:
        raise ConfigError(f"{where}: max_age_seconds must be positive and debounce_ms must not be negative")
    if source == "gpio" and (not config.chip or config.line is None or config.line < 0):
        raise ConfigError(f"{where}: a gpio source needs chip and line")
    if source == "file" and not config.path:
        raise ConfigError(f"{where}: a file source needs path")
    return config


def parse_config(data: dict) -> ServiceConfig:
    """Turn the parsed TOML into the configuration, refusing what is unknown or wrong."""

    _only(data, {"mqtt", "controller", "estop", "door", "port_sharing", "cell_state", "gate", "mtconnect", "idempotency", "global_stop"}, "the file")
    mqtt_table = _table(data, "mqtt")
    _only(mqtt_table, {"host", "port", "client_id", "max_connect_attempts", "connect_retry_delay_seconds"}, "mqtt")
    d = MqttSettings()
    mqtt = MqttSettings(
        _kind(mqtt_table, "host", str, "mqtt", d.host),
        _kind(mqtt_table, "port", int, "mqtt", d.port),
        _kind(mqtt_table, "client_id", str, "mqtt", d.client_id),
        _kind(mqtt_table, "max_connect_attempts", int, "mqtt", d.max_connect_attempts),
        _kind(mqtt_table, "connect_retry_delay_seconds", float, "mqtt", d.connect_retry_delay_seconds),
    )
    if not 0 < mqtt.port <= 65535:
        raise ConfigError("mqtt.port must be 1 to 65535")

    table = _table(data, "controller")
    _only(table, {"kind", "device", "baud", "timeout_seconds", "url"}, "controller")
    controller = ControllerConfig(
        _kind(table, "kind", str, "controller", "grbl"),
        _kind(table, "device", str, "controller"),
        _kind(table, "baud", int, "controller", 115200),
        _kind(table, "timeout_seconds", float, "controller", 1.0),
        _kind(table, "url", str, "controller"),
    )
    if controller.kind not in CONTROLLER_KINDS:
        raise ConfigError(f"controller.kind must be one of {', '.join(CONTROLLER_KINDS)}")
    if controller.kind in ("grbl", "cncjs") and not controller.device:
        raise ConfigError("controller.device is required for a grbl or cncjs controller")
    if controller.kind == "cncjs" and not controller.url:
        raise ConfigError("controller.url is required for a cncjs controller")

    sharing_table = _table(data, "port_sharing")
    _only(sharing_table, {"enabled", "host", "port", "allow_remote", "read_timeout_seconds"}, "port_sharing")
    sharing = PortSharingConfig(
        _kind(sharing_table, "enabled", bool, "port_sharing", False),
        _kind(sharing_table, "host", str, "port_sharing", DEFAULT_HOST),
        _kind(sharing_table, "port", int, "port_sharing", DEFAULT_PORT),
        _kind(sharing_table, "allow_remote", bool, "port_sharing", False),
        _kind(sharing_table, "read_timeout_seconds", float, "port_sharing", 1.0),
    )
    try:
        sharing.validate()
    except ValueError as error:
        raise ConfigError(f"port_sharing: {error}") from error
    if sharing.enabled and controller.kind != "grbl":
        raise ConfigError("port_sharing needs a grbl controller on a serial port")

    cell_table = _table(data, "cell_state")
    _only(cell_table, {"source", "value", "path", "max_age_seconds"}, "cell_state")
    source = _kind(cell_table, "source", str, "cell_state", "fixed")
    if source not in {"fixed", "file"}:
        raise ConfigError("cell_state.source must be fixed or file")
    try:
        value = CellState(_kind(cell_table, "value", str, "cell_state", CellState.INHIBITED.value))
    except ValueError as error:
        raise ConfigError(f"cell_state.value must be one of {', '.join(item.value for item in CellState)}") from error
    cell = CellStateConfig(source, value, _kind(cell_table, "path", str, "cell_state"), _kind(cell_table, "max_age_seconds", float, "cell_state", 30.0))
    if source == "file" and not cell.path:
        raise ConfigError("cell_state.path is required when the source is file")
    if cell.max_age_seconds <= 0:
        raise ConfigError("cell_state.max_age_seconds must be positive")

    gate_table = _table(data, "gate")
    _only(gate_table, {"spindle"}, "gate")
    spindle = _kind(gate_table, "spindle", str, "gate", SPINDLE_GATE_WHEN_KNOWN)
    if spindle not in SPINDLE_GATE_MODES:
        raise ConfigError(f"gate.spindle must be one of {', '.join(SPINDLE_GATE_MODES)}")
    mt_table = _table(data, "mtconnect")
    _only(mt_table, {"enabled"}, "mtconnect")
    idem = _table(data, "idempotency")
    _only(idem, {"path"}, "idempotency")
    stop_table = _table(data, "global_stop")
    _only(stop_table, {"enabled", "topic", "strict"}, "global_stop")
    try:
        global_stop = GlobalStopSettings(
            _kind(stop_table, "enabled", bool, "global_stop", False),
            _kind(stop_table, "topic", str, "global_stop", DEFAULT_TOPIC),
            _kind(stop_table, "strict", bool, "global_stop", False),
        )
    except ValueError as error:
        raise ConfigError(f"global_stop.topic: {error}") from error
    return ServiceConfig(
        _signal(data, "estop", ("the E-STOP is pressed", "")),
        _signal(data, "door", ("the door is open", "")),
        mqtt,
        controller,
        sharing,
        cell,
        spindle,
        _kind(mt_table, "enabled", bool, "mtconnect", False),
        _kind(idem, "path", str, "idempotency"),
        global_stop,
    )


def load_config(path: str | os.PathLike[str]) -> ServiceConfig:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error.strerror or error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from error
    return parse_config(data)


def _fresh_text(path: str, max_age_seconds: float, clock: Callable[[], float]) -> str | None:
    """The first word of a small file written by someone else; ``None`` when it is missing, unreadable, empty or stale."""

    try:
        age = clock() - os.stat(path).st_mtime
        if age > max_age_seconds or age < -max_age_seconds:
            return None
        with open(path, encoding="utf-8") as handle:
            words = handle.read(256).split()
    except (OSError, UnicodeDecodeError):
        return None
    return words[0] if words else None


def cell_state_reader(config: CellStateConfig, *, clock: Callable[[], float] = time.time) -> Callable[[], CellState]:
    """The ``cell_state`` callable of the bridge: always the current reading, and ``SAFE_STOP`` whenever it cannot be trusted."""

    if config.source == "fixed":
        value = config.value
        return lambda: value
    path, max_age = config.path or "", config.max_age_seconds

    def read() -> CellState:
        word = _fresh_text(path, max_age, clock)
        try:
            return CellState(word) if word is not None else CellState.SAFE_STOP
        except ValueError:
            return CellState.SAFE_STOP

    return read


def open_gpio_level(chip: str, line: int, unsafe_level: str, debounce_ms: float) -> Callable[[], bool]:
    """A reader of one GPIO line that answers True when the line is at its UNSAFE level. The only place gpiod is imported.

    The bias makes a cut wire read as unsafe: pull-up when the unsafe level is high, pull-down when it is low.
    """

    try:
        import gpiod  # type: ignore[import-untyped]
        from gpiod.line import Bias, Direction, Value
    except ImportError as error:
        raise RuntimeError("gpiod is not installed - install it to read the E-STOP and door lines of a real machine") from error
    from datetime import timedelta

    request = gpiod.request_lines(
        chip,
        consumer="hydra-umc-bridge-cnc",
        config={
            line: gpiod.LineSettings(
                direction=Direction.INPUT,
                bias=Bias.PULL_UP if unsafe_level == "high" else Bias.PULL_DOWN,
                debounce_period=timedelta(milliseconds=debounce_ms),
            )
        },
    )
    lock = threading.Lock()
    unsafe_value = Value.ACTIVE if unsafe_level == "high" else Value.INACTIVE

    def read() -> bool:
        with lock:
            return request.get_value(line) == unsafe_value

    return read


def unsafe_reader(config: SignalConfig, *, clock: Callable[[], float] = time.time, open_gpio=None) -> Callable[[], bool]:
    """A callable that answers True when the signal is at its unsafe level, and True too whenever it cannot be read."""

    if config.source == "none":
        return lambda: True
    if config.source == "gpio":
        raw = (open_gpio or open_gpio_level)(config.chip or "", config.line or 0, config.unsafe_level or "high", config.debounce_ms)

        def read_gpio() -> bool:
            try:
                return raw() is not False
            except Exception:  # noqa: BLE001 - a line that cannot be read is not known to be safe
                return True

        return read_gpio
    unsafe_word = "1" if config.unsafe_level == "high" else "0"
    path, max_age = config.path or "", config.max_age_seconds

    def read_file() -> bool:
        word = _fresh_text(path, max_age, clock)
        return word is None or word not in ("0", "1") or word == unsafe_word

    return read_file


def build_bridge(config: ServiceConfig, *, open_gpio=None, open_serial=None, open_cncjs=None, open_linuxcnc=None, clock: Callable[[], float] = time.time):
    """The bridge and what it has to close; the ``open_*`` arguments are for tests."""

    from hydra_umc_sdk.job_intake import IdempotencyStore

    from .cncjs_link import open_cncjs as real_open_cncjs
    from .linuxcnc_link import open_linuxcnc as real_open_linuxcnc
    from .mqtt_transport import CncMqttBridge
    from .multiplexer import share_serial_port
    from .serial_transport import ReconnectingSerialConnection, open_serial_port, open_serial_port_by_id

    estop_unsafe = unsafe_reader(config.estop, clock=clock, open_gpio=open_gpio)
    door_open = unsafe_reader(config.door, clock=clock, open_gpio=open_gpio)
    # the bridge asks "is the E-STOP pressed" and "is the door closed"
    estop = estop_unsafe
    door_closed = (lambda: not door_open())

    closers: list[Callable[[], None]] = []
    connection = None
    controller = None
    settings = config.controller
    if settings.kind == "grbl":
        device = settings.device or ""

        def connect():
            if open_serial is not None:
                return open_serial(device, settings.baud, settings.timeout_seconds)
            try:
                return open_serial_port_by_id(device, baud=settings.baud, timeout_seconds=settings.timeout_seconds)
            except RuntimeError:
                return open_serial_port(device, baud=settings.baud, timeout_seconds=settings.timeout_seconds)

        port = ReconnectingSerialConnection(connect)
        connection, multiplexer = share_serial_port(port, config.port_sharing)
        if multiplexer is not None:
            closers.append(multiplexer.close)
    elif settings.kind == "cncjs":
        controller = (open_cncjs or real_open_cncjs)(settings.url or "", settings.device or "", token=os.environ.get(CNCJS_TOKEN_ENVIRONMENT) or None)
    else:
        controller = (open_linuxcnc or real_open_linuxcnc)()
    idempotency = IdempotencyStore(config.idempotency_path) if config.idempotency_path else IdempotencyStore()
    bridge = CncMqttBridge(
        connection,
        estop,
        door_closed,
        cell_state_reader(config.cell_state, clock=clock),
        idempotency,
        clock=clock,
        controller=controller,
        spindle_gate=config.spindle_gate,
        mtconnect=config.mtconnect,
        global_stop=config.global_stop,
    )
    return bridge, closers


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hydra_umc_bridge_cnc", description="HYDRA-UMC CNC cell bridge service")
    parser.add_argument("--config", default=os.environ.get(CONFIG_ENVIRONMENT, DEFAULT_CONFIG_PATH), help=f"configuration file (default: ${CONFIG_ENVIRONMENT} or {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--check", action="store_true", help="read and validate the configuration, then exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"hydra-umc-bridge-cnc: configuration error: {error}", file=sys.stderr)
        return 2
    if args.check:
        print(f"CONFIG=PASS {args.config}")
        return 0

    from .mqtt_transport import run_forever

    try:
        bridge, closers = build_bridge(config)
    except (RuntimeError, OSError) as error:  # gpiod, pyserial or socketio missing; a port or server that cannot be reached
        print(f"hydra-umc-bridge-cnc: {error}", file=sys.stderr)
        return 3
    mqtt = config.mqtt
    # Restart=on-failure: a non-zero exit when the loop ends brings the service back
    try:
        run_forever(
            bridge,
            mqtt.host,
            mqtt.port,
            mqtt.client_id,
            max_connect_attempts=mqtt.max_connect_attempts,
            connect_retry_delay_seconds=mqtt.connect_retry_delay_seconds,
        )
    except Exception:  # noqa: BLE001
        _LOG.exception("the bridge stopped")
    finally:
        for close in closers:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass
    return 1
