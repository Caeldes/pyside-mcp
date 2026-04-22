"""
Cliente TCP para comunicarse con el bridge de depuración de una app PySide6.

Este módulo se usa desde el servidor MCP (sin PySide6) para enviar comandos
al bridge que corre dentro de la aplicación objetivo.
"""

import json
import socket
import tempfile
from pathlib import Path


def find_bridge_port(pid: int) -> int | None:
    """
    Retorna el puerto TCP del bridge para el PID dado, o None si no está disponible.
    El bridge escribe su puerto en un fichero temporal al arrancar.
    """
    port_file = Path(tempfile.gettempdir()) / f"pyside_mcp_{pid}.port"
    if not port_file.exists():
        return None
    try:
        return int(port_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


class BridgeClient:
    """
    Cliente TCP que se conecta al bridge corriendo en la app PySide6 objetivo.
    Soporta uso como gestor de contexto (with BridgeClient(...) as c: ...).
    """

    def __init__(self, port: int, timeout: float = 10.0) -> None:
        self._port = port
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._recv_buffer = b""

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Establece la conexión con el bridge en localhost."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self._timeout)
        self._sock.connect(("127.0.0.1", self._port))
        self._recv_buffer = b""

    def disconnect(self) -> None:
        """Cierra la conexión de forma segura."""
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "BridgeClient":
        self.connect()
        return self

    def __exit__(self, *_args) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Protocolo: JSON por línea
    # ------------------------------------------------------------------

    def send_command(self, command: str, params: dict | None = None) -> dict:
        """
        Envía un comando al bridge y retorna la respuesta como diccionario.
        Lanza ConnectionError si la conexión no está establecida o se pierde.
        """
        if self._sock is None:
            raise ConnectionError("No hay conexión con el bridge. Llama a connect() primero.")

        payload = json.dumps({"command": command, "params": params or {}}) + "\n"
        self._sock.sendall(payload.encode("utf-8"))

        # Leer hasta encontrar un salto de línea completo
        while b"\n" not in self._recv_buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("El bridge cerró la conexión inesperadamente.")
            self._recv_buffer += chunk

        raw_line, self._recv_buffer = self._recv_buffer.split(b"\n", 1)
        return json.loads(raw_line.decode("utf-8"))
