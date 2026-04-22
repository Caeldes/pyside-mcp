# pyside-mcp

Servidor MCP para que agentes de IA puedan inspeccionar e interactuar con aplicaciones PySide6 en ejecución.

## Inicio rápido

### 1. Instalar dependencias
```sh
uv sync
```

### 2. Configurar en Claude Desktop (o cualquier cliente MCP)

Añade esta entrada a tu `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pyside-mcp": {
      "command": "uv",
      "args": ["run", "main.py"],
      "cwd": "C:/Users/User/newProyects/pyside-mcp"
    }
  }
}
```

### 3. Probar con el MCP Inspector
```sh
uv run mcp dev main.py
```

---

## Flujo de uso con el agente

### Opción A — La app la lanza el agente (recomendado)

El agente usa `launch_app` y el bridge se inyecta automáticamente:

```
Agente: launch_app("example_app.py")
→ { pid: 1234, port: 54321, status: "running" }
```

### Opción B — App ya en ejecución con bridge manual

Añade esto a tu app PySide6 **después de crear QApplication**:

```python
from PySide6.QtWidgets import QApplication
import sys

app = QApplication(sys.argv)

# Instalar bridge AQUÍ
from pyside_mcp import install_bridge
install_bridge()

# ... resto de tu app
```

Luego el agente conecta con el PID del proceso:
```
Agente: connect_to_app(pid=1234)
```

---

## Herramientas disponibles

| Herramienta | Descripción |
|---|---|
| `launch_app(script_path, args?)` | Lanza una app PySide6 con bridge automático |
| `connect_to_app(pid)` | Conecta a una app ya en ejecución |
| `get_widget_tree(pid)` | Árbol completo de widgets con IDs |
| `find_widgets(pid, object_name?, widget_type?)` | Busca widgets por nombre o tipo |
| `get_widget_properties(pid, widget_id)` | Propiedades detalladas de un widget |
| `click_widget(pid, widget_id, button?)` | Clic del ratón (QTest) |
| `double_click_widget(pid, widget_id)` | Doble clic (QTest) |
| `press_key(pid, widget_id, key)` | Pulsación de tecla (QTest) |
| `set_widget_text(pid, widget_id, text)` | Establece texto en campos de entrada |
| `get_app_output(pid, max_lines?)` | Lee stdout/stderr de la app |
| `stop_app(pid)` | Detiene la app |

---

## Ejemplo de sesión con el agente

```
1. launch_app("example_app.py")
   → pid: 1234

2. get_widget_tree(1234)
   → [{ id: "a1b2c3d4", type: "DemoWindow", children: [...] }]

3. find_widgets(1234, widget_type="QPushButton")
   → [{ id: "e5f6a7b8", object_name: "submit_btn", text: "Enviar" }]

4. find_widgets(1234, object_name="input_field")
   → [{ id: "c9d0e1f2", type: "QLineEdit" }]

5. set_widget_text(1234, "c9d0e1f2", "Hola desde el agente!")

6. click_widget(1234, "e5f6a7b8")

7. get_app_output(1234)
   → { stdout: ["Enviado: Hola desde el agente!"] }

8. stop_app(1234)
```
