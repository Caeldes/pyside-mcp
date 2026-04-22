"""
Servidor de bridge que se ejecuta DENTRO de la aplicación PySide6 objetivo.

Expone una API TCP en localhost (puerto aleatorio) para que el servidor MCP
pueda inspeccionar e interactuar con la UI en tiempo real.

Uso desde la app objetivo:
    from pyside_mcp import install_bridge
    # Llamar después de crear QApplication:
    install_bridge()
"""

import json
import os
import queue
import socket
import tempfile
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

# ---------------------------------------------------------------------------
# Registro de widgets: id_corto (hex8) -> QWidget
# Se rellena al serializar el árbol y se usa para referenciar widgets por ID.
# ---------------------------------------------------------------------------
_widget_registry: dict[str, "QWidget"] = {}


def _register_widget(widget: QWidget) -> str:
    """Registra un widget en el registro global y retorna su ID único."""
    # Reusar ID si el widget ya está registrado
    for wid, w in _widget_registry.items():
        if w is widget:
            return wid
    wid = uuid.uuid4().hex[:8]
    _widget_registry[wid] = widget
    return wid


def _resolve_widget(widget_id: str) -> QWidget | None:
    """Recupera un widget del registro, comprobando que su objeto C++ sigue vivo."""
    widget = _widget_registry.get(widget_id)
    if widget is None:
        return None
    try:
        # objectName() lanza RuntimeError si el objeto C++ subyacente fue destruido
        widget.objectName()
        return widget
    except RuntimeError:
        del _widget_registry[widget_id]
        return None


# ---------------------------------------------------------------------------
# Servidor bridge
# ---------------------------------------------------------------------------

class BridgeServer(QObject):
    """
    Servidor TCP en localhost que acepta comandos JSON y los ejecuta
    en el hilo principal de Qt mediante un QTimer de sondeo.

    Protocolo: una petición JSON por línea (terminada en \\n).
    Respuesta:  una respuesta JSON por línea.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._request_queue: queue.Queue = queue.Queue()
        self._server_socket: socket.socket | None = None
        self._port: int | None = None

        # Timer que sondea la cola de peticiones en el hilo principal de Qt
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(5)
        self._poll_timer.timeout.connect(self._process_request_queue)

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def start(self) -> int:
        """Inicia el servidor bridge. Retorna el puerto TCP asignado."""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind(("127.0.0.1", 0))
        self._server_socket.listen(5)
        self._port = self._server_socket.getsockname()[1]

        # Publicar el puerto en un fichero temporal para que el MCP server lo descubra.
        port_text = str(self._port)
        tmp = Path(tempfile.gettempdir())
        # Fichero por PID propio
        (tmp / f"pyside_mcp_{os.getpid()}.port").write_text(port_text, encoding="utf-8")
        # Fichero por PID padre (Windows: el shim uv puede ser el parent)
        ppid = os.getppid()
        if ppid and ppid != os.getpid():
            (tmp / f"pyside_mcp_{ppid}.port").write_text(port_text, encoding="utf-8")
        # Fichero en ruta fija pasada por env var (más fiable en Windows)
        env_port_file = os.environ.get("PYSIDE_MCP_PORT_FILE")
        if env_port_file:
            Path(env_port_file).write_text(port_text, encoding="utf-8")

        # Hilo de aceptación de conexiones (daemon para no bloquear el cierre)
        threading.Thread(target=self._accept_loop, daemon=True).start()

        # Iniciar el sondeo de peticiones en el hilo Qt
        self._poll_timer.start()

        return self._port

    def stop(self) -> None:
        """Detiene el servidor bridge y elimina el fichero de puerto."""
        self._poll_timer.stop()
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
        tmp = Path(tempfile.gettempdir())
        (tmp / f"pyside_mcp_{os.getpid()}.port").unlink(missing_ok=True)
        ppid = os.getppid()
        if ppid and ppid != os.getpid():
            (tmp / f"pyside_mcp_{ppid}.port").unlink(missing_ok=True)
        env_port_file = os.environ.get("PYSIDE_MCP_PORT_FILE")
        if env_port_file:
            Path(env_port_file).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Bucle de red (hilo de fondo)
    # ------------------------------------------------------------------

    def _accept_loop(self) -> None:
        """Acepta conexiones entrantes. Solo permite conexiones desde localhost."""
        while True:
            try:
                conn, addr = self._server_socket.accept()
            except OSError:
                break
            # Seguridad: rechazar cualquier conexión que no sea loopback
            if addr[0] != "127.0.0.1":
                conn.close()
                continue
            threading.Thread(
                target=self._client_loop, args=(conn,), daemon=True
            ).start()

    def _client_loop(self, conn: socket.socket) -> None:
        """Atiende a un cliente conectado: lee peticiones y envía respuestas."""
        buffer = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                # Procesar todas las líneas completas del buffer
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    raw_line = raw_line.strip()
                    if raw_line:
                        response = self._enqueue_and_wait(raw_line.decode("utf-8"))
                        conn.sendall((json.dumps(response) + "\n").encode("utf-8"))

    def _enqueue_and_wait(self, raw: str) -> dict:
        """
        Encola una petición para ejecutarse en el hilo Qt y bloquea el hilo
        de red hasta recibir la respuesta (máximo 10 s).
        """
        try:
            request = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {"error": f"JSON inválido: {exc}"}

        if not isinstance(request, dict):
            return {"error": "La petición debe ser un objeto JSON"}

        event = threading.Event()
        result_holder: list[dict | None] = [None]

        def task() -> None:
            try:
                result_holder[0] = self._dispatch(request)
            except Exception as exc:  # noqa: BLE001
                result_holder[0] = {"error": f"Error interno: {exc}"}
            finally:
                event.set()

        self._request_queue.put(task)
        event.wait(timeout=10.0)
        return result_holder[0] or {"error": "Timeout: el event loop Qt no respondió"}

    # ------------------------------------------------------------------
    # Procesamiento en el hilo Qt (via QTimer)
    # ------------------------------------------------------------------

    def _process_request_queue(self) -> None:
        """Drena la cola de tareas ejecutándolas en el hilo principal de Qt."""
        while not self._request_queue.empty():
            try:
                task = self._request_queue.get_nowait()
                task()
            except queue.Empty:
                break

    # ------------------------------------------------------------------
    # Dispatch de comandos
    # ------------------------------------------------------------------

    def _dispatch(self, request: dict) -> dict:
        """Despacha un comando al handler correspondiente."""
        command = request.get("command", "")
        params = request.get("params", {})

        if not isinstance(params, dict):
            return {"error": "params debe ser un objeto JSON"}

        match command:
            case "ping":
                return {"status": "ok", "pid": os.getpid()}
            case "get_widget_tree":
                return self._cmd_get_widget_tree()
            case "find_widgets":
                return self._cmd_find_widgets(
                    params.get("object_name", ""),
                    params.get("widget_type", ""),
                )
            case "get_properties":
                return self._cmd_get_properties(params.get("widget_id", ""))
            case "click":
                return self._cmd_click(
                    params.get("widget_id", ""),
                    params.get("button", "left"),
                )
            case "double_click":
                return self._cmd_double_click(params.get("widget_id", ""))
            case "key_click":
                return self._cmd_key_click(
                    params.get("widget_id", ""),
                    params.get("key", ""),
                )
            case "set_text":
                return self._cmd_set_text(
                    params.get("widget_id", ""),
                    params.get("text", ""),
                )
            case _:
                return {"error": f"Comando desconocido: {command!r}"}

    # ------------------------------------------------------------------
    # Serialización de widgets
    # ------------------------------------------------------------------

    def _serialize_widget(
        self,
        widget: QWidget,
        depth: int = 0,
        max_depth: int = 6,
    ) -> dict:
        """
        Serializa un widget a un diccionario JSON-compatible.
        Registra el widget para poder referenciarlo por ID en comandos futuros.
        """
        wid = _register_widget(widget)
        info: dict = {
            "id": wid,
            "type": type(widget).__name__,
            "object_name": widget.objectName(),
            "visible": widget.isVisible(),
            "enabled": widget.isEnabled(),
            "geometry": {
                "x": widget.x(),
                "y": widget.y(),
                "width": widget.width(),
                "height": widget.height(),
            },
            "children": [],
        }

        # Propiedades de texto (comunes a QLabel, QPushButton, QLineEdit, etc.)
        for method_name, key in [
            ("text", "text"),
            ("title", "title"),
            ("toolTip", "tooltip"),
            ("placeholderText", "placeholder_text"),
        ]:
            if hasattr(widget, method_name):
                try:
                    info[key] = getattr(widget, method_name)()
                except Exception:  # noqa: BLE001
                    pass

        # Propiedades de estado/valor
        for method_name, key in [
            ("isChecked", "checked"),
            ("value", "value"),
            ("currentIndex", "current_index"),
            ("currentText", "current_text"),
            ("count", "item_count"),
        ]:
            if hasattr(widget, method_name):
                try:
                    info[key] = getattr(widget, method_name)()
                except Exception:  # noqa: BLE001
                    pass

        # Hijos directos (sin recursión si se alcanzó la profundidad máxima)
        if depth < max_depth:
            for child in widget.children():
                if isinstance(child, QWidget) and child.parent() is widget:
                    info["children"].append(
                        self._serialize_widget(child, depth + 1, max_depth)
                    )

        return info

    # ------------------------------------------------------------------
    # Implementación de comandos Qt
    # ------------------------------------------------------------------

    def _cmd_get_widget_tree(self) -> dict:
        """Retorna el árbol completo de widgets de la aplicación."""
        app = QApplication.instance()
        if app is None:
            return {"error": "No hay instancia de QApplication activa"}
        trees = [self._serialize_widget(w) for w in app.topLevelWidgets()]
        return {"widgets": trees}

    def _cmd_find_widgets(self, object_name: str, widget_type: str) -> dict:
        """Busca widgets por objectName y/o nombre de clase."""
        app = QApplication.instance()
        if app is None:
            return {"error": "No hay instancia de QApplication activa"}

        results = []
        for top in app.topLevelWidgets():
            candidates: list[QWidget] = [top, *top.findChildren(QWidget)]
            for w in candidates:
                name_match = not object_name or w.objectName() == object_name
                type_match = not widget_type or type(w).__name__ == widget_type
                if name_match and type_match:
                    results.append(self._serialize_widget(w, max_depth=0))

        return {"widgets": results}

    def _cmd_get_properties(self, widget_id: str) -> dict:
        """Retorna las propiedades detalladas de un widget por su ID."""
        if not widget_id:
            return {"error": "widget_id es requerido"}
        widget = _resolve_widget(widget_id)
        if widget is None:
            return {"error": f"Widget '{widget_id}' no encontrado o fue destruido"}
        return self._serialize_widget(widget, max_depth=1)

    def _cmd_click(self, widget_id: str, button: str) -> dict:
        """Simula un clic del ratón en un widget usando QTest."""
        if not widget_id:
            return {"error": "widget_id es requerido"}
        widget = _resolve_widget(widget_id)
        if widget is None:
            return {"error": f"Widget '{widget_id}' no encontrado"}

        qt_button = {
            "left": Qt.MouseButton.LeftButton,
            "right": Qt.MouseButton.RightButton,
            "middle": Qt.MouseButton.MiddleButton,
        }.get(button, Qt.MouseButton.LeftButton)

        QTest.mouseClick(widget, qt_button)
        return {"success": True}

    def _cmd_double_click(self, widget_id: str) -> dict:
        """Simula un doble clic en un widget usando QTest."""
        if not widget_id:
            return {"error": "widget_id es requerido"}
        widget = _resolve_widget(widget_id)
        if widget is None:
            return {"error": f"Widget '{widget_id}' no encontrado"}

        QTest.mouseDClick(widget, Qt.MouseButton.LeftButton)
        return {"success": True}

    def _cmd_key_click(self, widget_id: str, key: str) -> dict:
        """
        Simula la pulsación de una tecla en un widget usando QTest.
        El nombre de la tecla debe coincidir con Qt.Key.Key_<name>, sin el prefijo.
        Ejemplos válidos: "Return", "Escape", "Tab", "A", "F1".
        """
        if not widget_id:
            return {"error": "widget_id es requerido"}
        if not key:
            return {"error": "key es requerido"}

        widget = _resolve_widget(widget_id)
        if widget is None:
            return {"error": f"Widget '{widget_id}' no encontrado"}

        qt_key = getattr(Qt.Key, f"Key_{key}", None)
        if qt_key is None:
            return {
                "error": (
                    f"Tecla desconocida: '{key}'. "
                    "Usa nombres de Qt.Key sin el prefijo 'Key_' (ej: 'Return', 'Escape')."
                )
            }

        QTest.keyClick(widget, qt_key)
        return {"success": True}

    def _cmd_set_text(self, widget_id: str, text: str) -> dict:
        """Establece el texto en un widget de entrada (QLineEdit, QTextEdit, etc.)."""
        if not widget_id:
            return {"error": "widget_id es requerido"}

        widget = _resolve_widget(widget_id)
        if widget is None:
            return {"error": f"Widget '{widget_id}' no encontrado"}

        from PySide6.QtWidgets import QAbstractSpinBox, QAbstractSlider, QComboBox as _QComboBox

        if hasattr(widget, "setText"):
            widget.setText(text)
        elif hasattr(widget, "setPlainText"):
            widget.setPlainText(text)
        elif isinstance(widget, _QComboBox):
            # Seleccionar por texto exacto; si no existe, no hacer nada
            idx = widget.findText(text)
            if idx == -1:
                return {"error": f"Ítem '{text}' no encontrado en el ComboBox"}
            widget.setCurrentIndex(idx)
        elif isinstance(widget, (QAbstractSpinBox, QAbstractSlider)):
            try:
                val = int(text)
            except ValueError:
                return {"error": f"Valor '{text}' no es un entero válido"}
            widget.setValue(val)
        else:
            return {
                "error": (
                    f"El widget '{type(widget).__name__}' no soporta setText/setPlainText"
                )
            }

        return {"success": True}
