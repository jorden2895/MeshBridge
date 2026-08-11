from __future__ import annotations

import socket
import threading
from collections.abc import Callable


HOST = "127.0.0.1"
PORT = 47833
SIGNATURE = b"MeshBridge-v3\n"
ACKNOWLEDGEMENT = b"MeshBridge-v3-ack\n"


class SingleInstance:
    """Small loopback command channel used only to activate the primary GUI."""

    def __init__(self, on_command: Callable[[str], None]) -> None:
        self.on_command = on_command
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def acquire(self, secondary_command: str = "show:dashboard") -> bool:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((HOST, PORT))
        except OSError:
            listener.close()
            try:
                with socket.create_connection((HOST, PORT), timeout=2) as client:
                    client.settimeout(2)
                    client.sendall(SIGNATURE + secondary_command.encode("utf-8") + b"\n")
                    if client.makefile("rb").readline() == ACKNOWLEDGEMENT:
                        return False
            except OSError:
                pass
            # The port belongs to an unrelated or unresponsive process. Continue
            # without the activation listener instead of silently exiting.
            return True
        listener.listen(2)
        listener.settimeout(0.5)
        self._socket = listener
        self._thread = threading.Thread(target=self._serve, name="single-instance", daemon=True)
        self._thread.start()
        return True

    def _serve(self) -> None:
        while not self._stop.is_set() and self._socket is not None:
            try:
                client, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with client:
                try:
                    payload = client.recv(1024)
                    if payload.startswith(SIGNATURE):
                        command = payload[len(SIGNATURE):].decode("utf-8").strip()
                        self.on_command(command or "show:dashboard")
                        client.sendall(ACKNOWLEDGEMENT)
                except Exception:
                    continue

    def close(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
