# =============================================================================
# HYDRA-UMC-BRIDGE-CNC - Serial port multiplexer for GRBL
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Share the one serial port of a GRBL controller between the bridge and a G-code sender.

A GRBL board has ONE serial port. This module lets the bridge keep it open and still leave the machine to the program
sender (UGS, bCNC, CNCjs, LaserGRBL): the multiplexer owns the real port and offers a TCP front on which ONE sender at a
time connects (a "socket://" or "raw TCP" connection of the sender, instead of the serial device).

    sender  <--TCP-->  SerialMultiplexer  <--serial-->  GRBL
                            |
                       MuxBridgeLink  (what the bridge reads and writes: ``SerialLike``)

What goes where:

* everything the controller writes goes to the sender as it came, and every complete line of it is also kept for the
  bridge (its status queries read from there);
* everything the sender writes goes to the controller as it came: the multiplexer never rewrites, delays or inspects a
  program (except the optional ``sender_allowed`` gate below);
* the bridge writes ONLY GRBL's own real-time bytes (``?`` ``!`` ``~`` and Ctrl-X). Anything else it tries to write is
  refused with a ``ValueError``: the bridge never streams a program, with or without a multiplexer. A real-time byte goes
  out at once, between two bytes of the sender, which is exactly what GRBL's real-time channel is for;
* ``sender_allowed`` (optional) is asked before a non-real-time line of the sender is written. While it says no, that
  line is not written and the sender gets ``error:9`` (GRBL's "G-code locked out") so it does not wait for an ``ok``.
  Real-time bytes of the sender are never held: whoever runs the machine can always stop it.

The configuration is conservative: the multiplexer is OFF unless asked for, its front listens on the loopback address only,
and a second sender is refused while one is connected. Nothing here has run against a real GRBL board; the tests run it
against the GRBL emulator of the test suite.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from hydra_umc_sdk.serial_link import SerialLike

__all__ = [
    "MuxBridgeLink",
    "PortSharingConfig",
    "SerialMultiplexer",
    "port_sharing_from_environment",
    "share_serial_port",
]

MODE_ENVIRONMENT = "HYDRA_UMC_CNC_PORT_SHARING"
LISTEN_ENVIRONMENT = "HYDRA_UMC_CNC_PORT_SHARING_LISTEN"
ALLOW_REMOTE_ENVIRONMENT = "HYDRA_UMC_CNC_PORT_SHARING_ALLOW_REMOTE"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5700
REALTIME_BYTES = frozenset({0x3F, 0x21, 0x7E, 0x18})  # ? ! ~ Ctrl-X
LOCKED_OUT_REPLY = b"error:9\r\n"
MAX_BRIDGE_LINES = 256
MAX_LINE_BYTES = 4096
_READ_IDLE_SECONDS = 0.01


@dataclass(frozen=True)
class PortSharingConfig:
    """How the port is shared. ``enabled`` False (the default) is the old behaviour: the bridge holds the port alone."""

    enabled: bool = False
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allow_remote: bool = False
    read_timeout_seconds: float = 1.0

    def validate(self) -> None:
        if not (0 <= self.port <= 65535):
            raise ValueError(f"the sharing port must be 0 to 65535, not {self.port}")
        if not self.allow_remote and self.host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError(
                f"the multiplexer front listens on {self.host}, which is not the loopback address: "
                f"a G-code front on the network needs {ALLOW_REMOTE_ENVIRONMENT}=1"
            )


def port_sharing_from_environment(environment: dict[str, str] | None = None) -> PortSharingConfig:
    """``HYDRA_UMC_CNC_PORT_SHARING`` (``off`` or ``multiplexer``, default ``off``), ``..._LISTEN`` (``host:port``) and
    ``..._ALLOW_REMOTE`` (``1``). A word it does not know is ``off``: the safe answer."""

    source = environment if environment is not None else os.environ
    mode = source.get(MODE_ENVIRONMENT, "").strip().lower()
    host, port = DEFAULT_HOST, DEFAULT_PORT
    listen = source.get(LISTEN_ENVIRONMENT, "").strip()
    if listen:
        name, _, number = listen.rpartition(":")
        try:
            host, port = (name or DEFAULT_HOST), int(number)
        except ValueError:
            return PortSharingConfig(enabled=False)
    return PortSharingConfig(
        enabled=mode == "multiplexer",
        host=host,
        port=port,
        allow_remote=source.get(ALLOW_REMOTE_ENVIRONMENT, "").strip() == "1",
    )


class MuxBridgeLink:
    """The bridge's side of the multiplexer, as a ``SerialLike``: it reads the lines of the controller and writes only real-time bytes."""

    def __init__(self, multiplexer: "SerialMultiplexer", timeout_seconds: float = 1.0) -> None:
        self._mux = multiplexer
        self._timeout = timeout_seconds

    def write(self, data: bytes) -> int:
        return self._mux.write_realtime(data)

    def readline(self) -> bytes:
        return self._mux.next_bridge_line(self._timeout)

    def reset_input_buffer(self) -> None:
        self._mux.clear_bridge_lines()

    @property
    def in_waiting(self) -> int:
        return self._mux.bridge_lines_waiting()

    def close(self) -> None:
        # the bridge letting go of its link does not close the port: the multiplexer owns it
        return None


class SerialMultiplexer:
    """Owns the real serial port, serves one sender over TCP and gives the bridge its own link."""

    def __init__(
        self,
        port: SerialLike,
        config: PortSharingConfig | None = None,
        *,
        sender_allowed: Callable[[], bool] | None = None,
    ) -> None:
        self._config = config or PortSharingConfig(enabled=True)
        self._config.validate()
        self._port = port
        self._sender_allowed = sender_allowed
        self._write_lock = threading.Lock()
        self._lines: deque[bytes] = deque(maxlen=MAX_BRIDGE_LINES)
        self._lines_ready = threading.Condition()
        self._sender: socket.socket | None = None
        self._sender_lock = threading.Lock()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._port_error: Exception | None = None

    # -- life cycle -------------------------------------------------------------------------------------------------

    def start(self) -> tuple[str, int]:
        """Start listening and reading; returns the address the sender connects to (the port is the real one if 0 was asked)."""
        listener = socket.socket(socket.AF_INET6 if ":" in self._config.host else socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self._config.host, self._config.port))
        listener.listen(1)
        listener.settimeout(0.2)
        self._listener = listener
        for target, name in ((self._read_controller, "mux-controller-reader"), (self._accept_senders, "mux-sender-accept")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        host, port = listener.getsockname()[:2]
        return str(host), int(port)

    def close(self) -> None:
        self._stop.set()
        with self._sender_lock:
            sender, self._sender = self._sender, None
        for sock in (sender, self._listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        for thread in self._threads:
            thread.join(timeout=2.0)
        with self._lines_ready:
            self._lines_ready.notify_all()

    def bridge_link(self) -> MuxBridgeLink:
        return MuxBridgeLink(self, self._config.read_timeout_seconds)

    @property
    def sender_connected(self) -> bool:
        with self._sender_lock:
            return self._sender is not None

    @property
    def port_error(self) -> Exception | None:
        """The last failure of the port (None once it reads again); while it lasts the bridge reads nothing and fails closed."""
        return self._port_error

    # -- the bridge's side ------------------------------------------------------------------------------------------

    def write_realtime(self, data: bytes) -> int:
        if not data or any(byte not in REALTIME_BYTES for byte in data):
            raise ValueError("the bridge may write only GRBL real-time bytes (? ! ~ and Ctrl-X), never a program")
        with self._write_lock:
            written = self._port.write(data)
        return written if isinstance(written, int) else 0

    def next_bridge_line(self, timeout_seconds: float) -> bytes:
        deadline = time.monotonic() + timeout_seconds
        with self._lines_ready:
            while not self._lines:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    return b""
                self._lines_ready.wait(remaining)
            return self._lines.popleft()

    def clear_bridge_lines(self) -> None:
        with self._lines_ready:
            self._lines.clear()

    def bridge_lines_waiting(self) -> int:
        with self._lines_ready:
            return len(self._lines)

    # -- the controller's side --------------------------------------------------------------------------------------

    def _read_controller(self) -> None:
        pending = b""
        while not self._stop.is_set():
            try:
                chunk = self._port.readline()
            except Exception as error:  # noqa: BLE001 - a failing port is reported and tried again; meanwhile the bridge reads nothing and fails closed
                self._port_error = error
                time.sleep(0.5)
                continue
            self._port_error = None
            if not chunk:
                time.sleep(_READ_IDLE_SECONDS)
                continue
            self._to_sender(chunk)
            pending += chunk
            while b"\n" in pending:
                line, _, pending = pending.partition(b"\n")
                with self._lines_ready:
                    self._lines.append(line + b"\n")
                    self._lines_ready.notify_all()
            if len(pending) > MAX_LINE_BYTES:
                pending = b""  # noise without a line end: not worth keeping

    def _to_sender(self, data: bytes) -> None:
        with self._sender_lock:
            sender = self._sender
        if sender is None:
            return
        try:
            sender.sendall(data)
        except OSError:
            self._drop_sender(sender)

    # -- the sender's side ------------------------------------------------------------------------------------------

    def _accept_senders(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._sender_lock:
                busy = self._sender is not None
                if not busy:
                    self._sender = client
            if busy:
                try:
                    client.sendall(b"[MSG:another sender is already connected]\r\n")
                finally:
                    client.close()
                continue
            thread = threading.Thread(target=self._serve_sender, args=(client,), name="mux-sender", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _serve_sender(self, client: socket.socket) -> None:
        client.settimeout(0.2)
        pending = bytearray()
        while not self._stop.is_set():
            try:
                data = client.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            for byte in data:
                if byte in REALTIME_BYTES:
                    self._write_sender_bytes(bytes([byte]))
                    continue
                pending.append(byte)
                if byte == 0x0A or len(pending) >= MAX_LINE_BYTES:
                    self._write_sender_line(client, bytes(pending))
                    pending.clear()
        self._drop_sender(client)

    def _write_sender_line(self, client: socket.socket, line: bytes) -> None:
        if self._sender_allowed is not None and not self._allowed():
            try:
                client.sendall(LOCKED_OUT_REPLY)
            except OSError:
                pass
            return
        self._write_sender_bytes(line)

    def _allowed(self) -> bool:
        try:
            return bool(self._sender_allowed())  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - a gate that cannot answer holds the line: unknown is not allowed
            return False

    def _write_sender_bytes(self, data: bytes) -> None:
        with self._write_lock:
            try:
                self._port.write(data)
            except Exception as error:  # noqa: BLE001
                self._port_error = error

    def _drop_sender(self, client: socket.socket) -> None:
        with self._sender_lock:
            if self._sender is client:
                self._sender = None
        try:
            client.close()
        except OSError:
            pass


def share_serial_port(
    port: SerialLike,
    config: PortSharingConfig | None = None,
    *,
    sender_allowed: Callable[[], bool] | None = None,
) -> tuple[SerialLike, SerialMultiplexer | None]:
    """The connection the bridge should use for ``port`` under ``config`` (the environment's when not given).

    Sharing off (the default): the port itself and no multiplexer, exactly as before. Sharing on: the bridge link of a
    started multiplexer and the multiplexer (close it when the bridge stops).
    """

    config = config if config is not None else port_sharing_from_environment()
    if not config.enabled:
        return port, None
    multiplexer = SerialMultiplexer(port, config, sender_allowed=sender_allowed)
    multiplexer.start()
    return multiplexer.bridge_link(), multiplexer
