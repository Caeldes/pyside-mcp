# pyside-mcp — Agent Instructions

Servidor MCP (Model Context Protocol) escrito en Python con `uv`.  
Objetivo: puente de depuración seguro para aplicaciones PySide6 en ejecución.

## Build & Run

```sh
uv sync                  # instalar dependencias
uv run main.py           # arrancar el servidor
uv add <package>         # añadir dependencia
uv run pytest            # ejecutar tests (cuando existan)
```

## Architecture

- `main.py` — punto de entrada del servidor MCP
- Las herramientas MCP exponen operaciones de depuración al cliente (IDE/agente)
- El servidor se conecta a una app PySide6 en ejecución via su PID
- Toda interacción con la UI objetivo ocurre por canales seguros (ver Conventions)

## Conventions

### Nomenclatura
- Variables, métodos, clases, módulos, parámetros: **snake_case en inglés** (`widget_tree`, `get_children`, `find_by_object_name`)
- Comentarios y docstrings: **en español**
- Constantes: `UPPER_SNAKE_CASE`

### Seguridad — Principio de Menor Privilegio (OBLIGATORIO)
- **Prohibido** usar `os.system`, `os.popen`, `shutil.rmtree` u otras llamadas arbitrarias al sistema
- Las operaciones de ficheros deben restringirse al **directorio del proyecto** (`Path(__file__).parent`)
- Las operaciones de proceso deben restringirse al **PID registrado de la app objetivo**
- Nunca ejecutar comandos shell construidos con entrada del usuario sin validación estricta
- Validar y sanitizar todos los parámetros recibidos desde el cliente MCP antes de usarlos

### Inspección de UI
- **Prohibido** usar visión artificial, capturas de pantalla o cámaras para inspeccionar la UI
- Usar `QObject.findChildren()` para obtener el árbol de widgets
- Acceder a propiedades de widgets mediante la API Qt (`.text()`, `.isVisible()`, `.objectName()`, etc.)

### Interacción con la UI
- **Prohibido** usar automatización de ratón/teclado físico (`pyautogui`, `pynput` o similares)
- Usar exclusivamente `QTest` para eventos sintéticos:
  ```python
  # correcto
  QTest.mouse_click(widget, Qt.MouseButton.LeftButton)
  QTest.key_click(widget, Qt.Key.Key_Return)

  # prohibido
  # pyautogui.click(x, y)
  ```

### Captura de Logs
- Capturar stdout/stderr de la app objetivo mediante `subprocess.Popen`:
  ```python
  process = subprocess.Popen(
      cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
  )
  ```
- No leer ficheros de log del sistema operativo directamente

## Key Dependencies

| Paquete | Uso |
|---------|-----|
| `mcp` (MCP SDK) | Framework del servidor MCP |
| `PySide6` | Interacción con la app objetivo (QTest, QObject) |

## Common Pitfalls

- `requires-python = ">=3.14"` — usar únicamente sintaxis y APIs de Python 3.14+
- La app PySide6 objetivo debe estar en ejecución antes de llamar a cualquier herramienta que opere sobre su PID
- `QTest` requiere que la app objetivo tenga el event loop Qt activo
