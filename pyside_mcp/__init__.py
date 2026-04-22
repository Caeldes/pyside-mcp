"""
pyside-mcp: Bridge de depuración para aplicaciones PySide6.

Para instalar el bridge en tu app PySide6, añade esto DESPUÉS de crear QApplication:

    from pyside_mcp import install_bridge
    port = install_bridge()
    print(f"Bridge escuchando en el puerto {port}")

El servidor MCP se conectará automáticamente usando el PID del proceso.
"""

# Instancia singleton del servidor bridge (se crea con install_bridge)
_bridge_instance = None


def install_bridge() -> int:
    """
    Instala y arranca el bridge de depuración en la aplicación actual.

    Debe llamarse después de que QApplication haya sido creada.
    Si el bridge ya está activo, retorna el puerto existente sin crear uno nuevo.

    Returns:
        Puerto TCP en el que el bridge está escuchando.

    Raises:
        RuntimeError: Si no hay ninguna instancia de QApplication activa.
    """
    global _bridge_instance  # noqa: PLW0603

    if _bridge_instance is not None:
        return _bridge_instance._port  # type: ignore[return-value]

    # Importación diferida: PySide6 solo se carga cuando esta función se llama,
    # lo que permite importar pyside_mcp en el servidor MCP sin necesitar PySide6.
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    if QApplication.instance() is None:
        raise RuntimeError(
            "install_bridge() debe llamarse después de crear QApplication."
        )

    from pyside_mcp.bridge import BridgeServer  # noqa: PLC0415

    _bridge_instance = BridgeServer()
    port = _bridge_instance.start()
    return port
