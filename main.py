"""
Servidor MCP para depuración de aplicaciones PySide6 en ejecución.

Expone herramientas para que agentes de IA puedan:
  - Lanzar apps PySide6 con el bridge de depuración inyectado
  - Conectarse a apps que ya tienen el bridge instalado
  - Inspeccionar el árbol de widgets
  - Interactuar con la UI mediante eventos sintéticos (QTest)
  - Capturar la salida stdout/stderr de la app
"""

import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from pyside_mcp.client import BridgeClient, find_bridge_port

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Directorio raíz del proyecto MCP (se usa para inyectar el bridge en el path)
_PROJECT_DIR = Path(__file__).parent

# Registro de procesos lanzados por este servidor:
#   pid -> (proceso, deque_stdout, deque_stderr)
_launched_processes: dict[
    int, tuple[subprocess.Popen, deque[str], deque[str]]
] = {}

# Capacidad máxima de las colas de salida por proceso
_OUTPUT_DEQUE_MAXLEN = 10_000

# ---------------------------------------------------------------------------
# Servidor MCP
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "pyside-mcp",
    instructions="""
    Servidor de depuración para aplicaciones PySide6 en ejecución.
    Permite a los agentes de IA inspeccionar y controlar la UI de apps PySide6.

    Flujo típico:
    1. Lanzar la app con `launch_app` (o conectar a una existente con `connect_to_app`).
    2. Inspeccionar la UI con `get_widget_tree` o `find_widgets`.
    3. Obtener detalles con `get_widget_properties`.
    4. Interactuar: `click_widget`, `double_click_widget`, `press_key`, `set_widget_text`.
    5. Revisar la salida de la app con `get_app_output`.
    6. Detener la app con `stop_app` (solo si fue lanzada con `launch_app`).

    Los IDs de widget son temporales y pueden cambiar si se recarga el árbol.
    """,
)

# ---------------------------------------------------------------------------
# Utilidades internas
# ---------------------------------------------------------------------------


def _get_connected_client(pid: int) -> BridgeClient:
    """
    Crea y conecta un BridgeClient para el PID dado.
    Lanza ValueError si el bridge no está disponible.
    """
    port = find_bridge_port(pid)
    if port is None:
        raise ValueError(
            f"No se encontró el bridge para PID {pid}. "
            "Asegúrate de que la app esté en ejecución y tenga el bridge instalado. "
            "Usa launch_app para lanzarla con el bridge automático, o añade "
            "`from pyside_mcp import install_bridge; install_bridge()` a tu app."
        )
    client = BridgeClient(port)
    client.connect()
    return client


def _drain_pipe(pipe, output_deque: deque[str]) -> None:
    """Drena un pipe de subprocess en segundo plano y almacena las líneas."""
    try:
        for raw_line in pipe:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
            output_deque.append(line)
    except Exception:  # noqa: BLE001
        pass


def _build_launcher_code(script: Path, argv: list[str]) -> str:
    """
    Genera el código Python del launcher que inyecta el bridge en la app objetivo.
    Todos los valores de usuario se serializan con repr() para evitar inyección de código.
    """
    project_dir_repr = repr(str(_PROJECT_DIR))
    argv_repr = repr(argv)
    script_repr = repr(str(script))

    return (
        "import sys, os\n"
        f"sys.path.insert(0, {project_dir_repr})\n"
        f"sys.argv = {argv_repr}\n"
        "\n"
        "# Monkey-patch QApplication para instalar el bridge en el hilo principal\n"
        "# en cuanto se crea la instancia, antes de que arranque el event loop.\n"
        "from PySide6.QtWidgets import QApplication as _OrigQApplication\n"
        "class _BridgedQApplication(_OrigQApplication):\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        super().__init__(*args, **kwargs)\n"
        "        try:\n"
        "            from pyside_mcp import install_bridge\n"
        "            install_bridge()\n"
        "        except Exception as _e:\n"
        "            import sys as _sys\n"
        "            print(f'pyside-mcp bridge error: {_e}', file=_sys.stderr, flush=True)\n"
        "import PySide6.QtWidgets as _qt_widgets\n"
        "_qt_widgets.QApplication = _BridgedQApplication\n"
        "\n"
        f"_script_globals = {{'__name__': '__main__', '__file__': {script_repr}}}\n"
        f"exec(compile(open({script_repr}, 'rb').read(), {script_repr}, 'exec'), _script_globals)\n"
    )


# ---------------------------------------------------------------------------
# Herramientas MCP
# ---------------------------------------------------------------------------


def _resolve_real_python() -> str:
    """
    Devuelve la ruta al ejecutable Python real del venv.

    En Windows, uv crea un proxy ligero (.venv/Scripts/python.exe) que NO
    propaga los pipes de stdout/stderr al proceso hijo. Se busca pythonw.exe
    o python.exe en el directorio Scripts del venv como alternativa,
    o se usa el proxy directamente si no hay otra opción.
    """
    scripts_dir = Path(sys.executable).parent
    # Intentar python3.exe o python3.X.exe en Scripts del venv
    for candidate in sorted(scripts_dir.glob("python3*.exe")):
        if candidate.name.lower() not in ("python3t.exe",):
            return str(candidate)
    return sys.executable


def _build_venv_env() -> dict:
    """
    Construye las variables de entorno para el proceso hijo.
    Usa el mismo entorno del servidor, sin modificaciones adicionales,
    ya que el venv proxy de uv ya tiene el entorno correcto.
    """
    import os as _os
    return dict(_os.environ)


@mcp.tool()
def launch_app(script_path: str, args: list[str] | None = None) -> dict:
    """
    Lanza una aplicación PySide6 como subproceso con el bridge de depuración activo.

    El bridge se inyecta automáticamente: no es necesario modificar la app objetivo.
    La salida stdout/stderr queda capturada y disponible via get_app_output.

    Args:
        script_path: Ruta al script Python (.py) de la app PySide6 a lanzar.
        args: Argumentos de línea de comandos para pasar a la app (opcional).

    Returns:
        Diccionario con pid, port y estado inicial del bridge.
    """
    script = Path(script_path).resolve()

    if not script.exists():
        return {"error": f"El script '{script_path}' no existe."}
    if script.suffix != ".py":
        return {"error": "Solo se pueden lanzar scripts Python (.py)."}

    argv = [str(script)] + (args or [])
    launcher_code = _build_launcher_code(script, argv)

    # Ruta fija para el port file de este lanzamiento (más fiable que descubrir por PID)
    port_file = Path(tempfile.gettempdir()) / f"pyside_mcp_launch_{os.getpid()}.port"
    port_file.unlink(missing_ok=True)

    # Escribir launcher en fichero temporal
    launcher_file = Path(tempfile.gettempdir()) / f"pyside_mcp_launcher_{os.getpid()}.py"
    try:
        launcher_file.write_text(launcher_code, encoding="utf-8")
    except OSError as exc:
        return {"error": f"No se pudo crear el launcher temporal: {exc}"}

    # Resolver el ejecutable Python real (en Windows, el .venv/Scripts/python.exe
    # puede ser un proxy de uv que no propaga los pipes al proceso hijo real).
    python_exe = _resolve_real_python()
    env = _build_venv_env()
    # Pasar la ruta del port file al proceso hijo via variable de entorno
    env["PYSIDE_MCP_PORT_FILE"] = str(port_file)

    # Lanzar la app como subproceso.
    # stdin=DEVNULL evita que el proceso hijo herede el pipe stdio del servidor MCP
    # (que usa stdio para el protocolo MCP), lo que causaría un deadlock en Windows.
    # CREATE_NO_WINDOW evita que se abra una ventana de consola extra en Windows.
    _popen_kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if sys.platform == "win32":
        _popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.Popen([python_exe, str(launcher_file)], **_popen_kwargs)
    except Exception as exc:  # noqa: BLE001
        launcher_file.unlink(missing_ok=True)
        port_file.unlink(missing_ok=True)
        return {"error": f"Error al lanzar la app: {exc}"}

    pid = process.pid

    # Iniciar hilos de drenaje de salida (compatibles con Windows)
    stdout_deque: deque[str] = deque(maxlen=_OUTPUT_DEQUE_MAXLEN)
    stderr_deque: deque[str] = deque(maxlen=_OUTPUT_DEQUE_MAXLEN)
    threading.Thread(
        target=_drain_pipe, args=(process.stdout, stdout_deque), daemon=True
    ).start()
    threading.Thread(
        target=_drain_pipe, args=(process.stderr, stderr_deque), daemon=True
    ).start()

    _launched_processes[pid] = (process, stdout_deque, stderr_deque)

    # Esperar a que el bridge esté disponible (máximo 15 segundos)
    # Primero se intenta la ruta fija por env var, luego la búsqueda por PID
    port: int | None = None
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if port_file.exists():
            try:
                port = int(port_file.read_text(encoding="utf-8").strip())
                break
            except (ValueError, OSError):
                pass
        if port is None:
            port = find_bridge_port(pid)
            if port is not None:
                break
        # Verificar si la app terminó prematuramente
        if process.poll() is not None:
            launcher_file.unlink(missing_ok=True)
            port_file.unlink(missing_ok=True)
            return {
                "error": "La app terminó antes de que el bridge estuviera listo.",
                "pid": pid,
                "returncode": process.returncode,
                "stderr_tail": list(stderr_deque)[-20:],
            }
        time.sleep(0.1)

    launcher_file.unlink(missing_ok=True)
    port_file.unlink(missing_ok=True)

    if port is None:
        return {
            "error": (
                "Timeout: el bridge no se inició en 15 segundos. "
                "Verifica que la app crea un QApplication."
            ),
            "pid": pid,
        }

    # Verificar conexión con el bridge
    try:
        with BridgeClient(port) as client:
            ping = client.send_command("ping")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Bridge disponible pero no responde: {exc}", "pid": pid, "port": port}

    return {"pid": pid, "port": port, "status": "running", "bridge": ping}


@mcp.tool()
def connect_to_app(pid: int) -> dict:
    """
    Conecta al bridge de una aplicación PySide6 ya en ejecución.

    La app debe haber instalado el bridge previamente:
        from pyside_mcp import install_bridge
        install_bridge()  # llamar después de crear QApplication

    Args:
        pid: PID del proceso de la aplicación PySide6.

    Returns:
        Estado de la conexión con el bridge.
    """
    if pid <= 0:
        return {"error": "pid debe ser un número entero positivo."}

    port = find_bridge_port(pid)
    if port is None:
        return {
            "error": (
                f"No se encontró el bridge para PID {pid}. "
                "¿La app tiene el bridge instalado? "
                "Añade: from pyside_mcp import install_bridge; install_bridge()"
            )
        }

    try:
        with BridgeClient(port) as client:
            ping = client.send_command("ping")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"No se pudo conectar al bridge: {exc}", "pid": pid, "port": port}

    return {"pid": pid, "port": port, "connected": True, "bridge": ping}


@mcp.tool()
def get_widget_tree(pid: int) -> dict:
    """
    Obtiene el árbol completo de widgets de la aplicación con todas sus propiedades.

    Úsalo para explorar la estructura de la UI y obtener los IDs de widget
    necesarios para las herramientas de interacción.

    Args:
        pid: PID de la aplicación PySide6.

    Returns:
        Árbol jerárquico de widgets con id, tipo, objectName, geometría, etc.
    """
    try:
        with _get_connected_client(pid) as client:
            return client.send_command("get_widget_tree")
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def find_widgets(
    pid: int,
    object_name: str = "",
    widget_type: str = "",
) -> dict:
    """
    Busca widgets por objectName y/o tipo de clase Qt.

    Al menos uno de los filtros debe ser no vacío.
    Ejemplos de widget_type: "QPushButton", "QLineEdit", "QLabel", "QComboBox".

    Args:
        pid: PID de la aplicación PySide6.
        object_name: Valor exacto del objectName del widget. Vacío para ignorar.
        widget_type: Nombre de clase del widget (ej: "QPushButton"). Vacío para ignorar.

    Returns:
        Lista de widgets que coinciden, con sus propiedades y IDs.
    """
    if not object_name and not widget_type:
        return {"error": "Debes especificar al menos object_name o widget_type."}

    try:
        with _get_connected_client(pid) as client:
            return client.send_command(
                "find_widgets",
                {"object_name": object_name, "widget_type": widget_type},
            )
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def get_widget_properties(pid: int, widget_id: str) -> dict:
    """
    Obtiene las propiedades detalladas de un widget específico.

    Args:
        pid: PID de la aplicación PySide6.
        widget_id: ID del widget (obtenido de get_widget_tree o find_widgets).

    Returns:
        Propiedades del widget: texto, visibilidad, estado, geometría, hijos directos.
    """
    if not widget_id or not widget_id.strip():
        return {"error": "widget_id no puede estar vacío."}

    try:
        with _get_connected_client(pid) as client:
            return client.send_command("get_properties", {"widget_id": widget_id})
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def click_widget(pid: int, widget_id: str, button: str = "left") -> dict:
    """
    Simula un clic del ratón en un widget usando QTest.

    Args:
        pid: PID de la aplicación PySide6.
        widget_id: ID del widget a clickear.
        button: Botón del ratón: "left" (por defecto), "right" o "middle".

    Returns:
        {"success": true} si el evento se envió correctamente.
    """
    if not widget_id or not widget_id.strip():
        return {"error": "widget_id no puede estar vacío."}
    if button not in ("left", "right", "middle"):
        return {"error": "button debe ser 'left', 'right' o 'middle'."}

    try:
        with _get_connected_client(pid) as client:
            return client.send_command("click", {"widget_id": widget_id, "button": button})
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def double_click_widget(pid: int, widget_id: str) -> dict:
    """
    Simula un doble clic (botón izquierdo) en un widget usando QTest.

    Args:
        pid: PID de la aplicación PySide6.
        widget_id: ID del widget.

    Returns:
        {"success": true} si el evento se envió correctamente.
    """
    if not widget_id or not widget_id.strip():
        return {"error": "widget_id no puede estar vacío."}

    try:
        with _get_connected_client(pid) as client:
            return client.send_command("double_click", {"widget_id": widget_id})
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def press_key(pid: int, widget_id: str, key: str) -> dict:
    """
    Simula la pulsación de una tecla en un widget usando QTest.

    El nombre de la tecla debe ser el nombre Qt.Key sin el prefijo 'Key_'.
    Ejemplos válidos: "Return", "Escape", "Tab", "Space", "A", "F1", "Delete".

    Args:
        pid: PID de la aplicación PySide6.
        widget_id: ID del widget que recibirá el evento de teclado.
        key: Nombre de la tecla (ej: "Return", "Escape", "Tab", "A").

    Returns:
        {"success": true} si el evento se envió correctamente.
    """
    if not widget_id or not widget_id.strip():
        return {"error": "widget_id no puede estar vacío."}
    if not key or not key.strip():
        return {"error": "key no puede estar vacío."}

    try:
        with _get_connected_client(pid) as client:
            return client.send_command("key_click", {"widget_id": widget_id, "key": key})
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def set_widget_text(pid: int, widget_id: str, text: str) -> dict:
    """
    Establece el texto en un widget de entrada (QLineEdit, QTextEdit, QPlainTextEdit).

    Esta operación reemplaza el contenido actual del widget con el texto indicado.

    Args:
        pid: PID de la aplicación PySide6.
        widget_id: ID del widget de entrada.
        text: Texto a establecer en el widget.

    Returns:
        {"success": true} si el texto se estableció correctamente.
    """
    if not widget_id or not widget_id.strip():
        return {"error": "widget_id no puede estar vacío."}

    try:
        with _get_connected_client(pid) as client:
            return client.send_command("set_text", {"widget_id": widget_id, "text": text})
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


@mcp.tool()
def get_app_output(pid: int, max_lines: int = 100) -> dict:
    """
    Retorna las últimas líneas de stdout/stderr de una app lanzada con launch_app.

    Solo funciona con apps iniciadas mediante la herramienta launch_app de este servidor.

    Args:
        pid: PID de la aplicación.
        max_lines: Número máximo de líneas a retornar por stream (1-500, por defecto 100).

    Returns:
        Diccionario con listas de líneas de stdout y stderr, y estado del proceso.
    """
    if not (1 <= max_lines <= 500):
        return {"error": "max_lines debe estar entre 1 y 500."}

    entry = _launched_processes.get(pid)
    if entry is None:
        return {"error": f"PID {pid} no fue lanzado por este servidor MCP."}

    process, stdout_deque, stderr_deque = entry
    returncode = process.poll()

    return {
        "pid": pid,
        "running": returncode is None,
        "returncode": returncode,
        "stdout": list(stdout_deque)[-max_lines:],
        "stderr": list(stderr_deque)[-max_lines:],
    }


@mcp.tool()
def stop_app(pid: int) -> dict:
    """
    Detiene una aplicación lanzada previamente con launch_app.

    Solo puede detener procesos iniciados por este servidor MCP.
    Intenta SIGTERM y espera 5 s; si no termina, envía SIGKILL.

    Args:
        pid: PID de la aplicación a detener.

    Returns:
        Estado de la terminación del proceso.
    """
    entry = _launched_processes.get(pid)
    if entry is None:
        return {"error": f"PID {pid} no fue lanzado por este servidor MCP."}

    process, _, _ = entry
    returncode = process.poll()

    if returncode is not None:
        del _launched_processes[pid]
        return {"pid": pid, "status": "already_stopped", "returncode": returncode}

    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

    del _launched_processes[pid]

    # Limpiar el fichero de puerto del bridge
    port_file = Path(tempfile.gettempdir()) / f"pyside_mcp_{pid}.port"
    port_file.unlink(missing_ok=True)

    return {"pid": pid, "status": "stopped", "returncode": process.returncode}


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------


def main() -> None:
    """Arranca el servidor MCP (transporte stdio por defecto)."""
    mcp.run()


@mcp.tool()
def debug_env() -> dict:
    """Devuelve información de entorno del servidor para diagnóstico."""
    import glob as _glob
    port_files = sorted(_glob.glob(str(Path(tempfile.gettempdir()) / "pyside_mcp_*.port")))
    return {
        "sys_executable": sys.executable,
        "real_python": _resolve_real_python(),
        "venv_env_pythonpath": _build_venv_env().get("PYTHONPATH", ""),
        "venv_env_virtual_env": _build_venv_env().get("VIRTUAL_ENV", ""),
        "existing_port_files": port_files,
    }


@mcp.tool()
def debug_launch_test() -> dict:
    """
    Ejecuta un test de lanzamiento directamente desde el servidor MCP para diagnóstico.
    """
    real_py = _resolve_real_python()
    env = _build_venv_env()
    results = {"real_python": real_py, "sys_executable": sys.executable}

    _kw: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        _kw["creationflags"] = subprocess.CREATE_NO_WINDOW

    # Test 1: proxy python con stdin=DEVNULL
    proc = subprocess.Popen([sys.executable, "-c", "print('proxy python ok')"], **_kw)
    try:
        o, e = proc.communicate(timeout=5)
        results["proxy_python"] = {"rc": proc.returncode, "out": o.decode("utf-8","replace"), "err": e.decode("utf-8","replace")[:200]}
    except subprocess.TimeoutExpired:
        proc.kill()
        results["proxy_python"] = {"rc": "TIMEOUT"}

    # Test 2: python resuelto con stdin=DEVNULL
    proc2 = subprocess.Popen([real_py, "-c", "print('real python ok')"], **_kw)
    try:
        o2, e2 = proc2.communicate(timeout=5)
        results["real_python_test"] = {"rc": proc2.returncode, "out": o2.decode("utf-8","replace"), "err": e2.decode("utf-8","replace")[:200]}
    except subprocess.TimeoutExpired:
        proc2.kill()
        results["real_python_test"] = {"rc": "TIMEOUT"}

    # Listar Scripts del venv
    scripts = list(Path(sys.executable).parent.glob("python*.exe"))
    results["venv_scripts_python_exes"] = [str(s) for s in scripts]

    return results


if __name__ == "__main__":
    main()
