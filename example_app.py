"""
Aplicación PySide6 de ejemplo para demostrar la integración con pyside-mcp.

Contiene varios tipos de widgets con objectName configurado para facilitar
la búsqueda por parte del agente.
"""

import sys

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class DemoWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Demo pyside-mcp")
        self.setMinimumSize(400, 500)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)

        # Etiqueta de estado
        self.status_label = QLabel("Esperando interacción del agente...")
        self.status_label.setObjectName("status_label")
        layout.addWidget(self.status_label)

        # Campo de texto de entrada
        self.input_field = QLineEdit()
        self.input_field.setObjectName("input_field")
        self.input_field.setPlaceholderText("Escribe algo aquí...")
        layout.addWidget(self.input_field)

        # Botón principal
        self.submit_btn = QPushButton("Enviar")
        self.submit_btn.setObjectName("submit_btn")
        self.submit_btn.clicked.connect(self._on_submit)
        layout.addWidget(self.submit_btn)

        # Selector desplegable
        self.combo = QComboBox()
        self.combo.setObjectName("combo")
        self.combo.addItems(["Opción A", "Opción B", "Opción C"])
        self.combo.currentTextChanged.connect(self._on_combo_changed)
        layout.addWidget(self.combo)

        # Botón secundario
        self.clear_btn = QPushButton("Limpiar log")
        self.clear_btn.setObjectName("clear_btn")
        self.clear_btn.clicked.connect(self._on_clear)
        layout.addWidget(self.clear_btn)

        # Área de log
        self.log_area = QTextEdit()
        self.log_area.setObjectName("log_area")
        self.log_area.setReadOnly(True)
        self.log_area.setPlaceholderText("El log de acciones aparecerá aquí...")
        layout.addWidget(self.log_area)

        self._log("App iniciada.")

    def _on_submit(self) -> None:
        text = self.input_field.text().strip()
        if text:
            self._log(f"Enviado: {text}")
            self.status_label.setText(f"Último envío: {text}")
            self.input_field.clear()
        else:
            self._log("Envío vacío ignorado.")
            self.status_label.setText("El campo estaba vacío.")

    def _on_combo_changed(self, text: str) -> None:
        self._log(f"Combo cambiado a: {text}")
        self.status_label.setText(f"Seleccionado: {text}")

    def _on_clear(self) -> None:
        self.log_area.clear()
        self.status_label.setText("Log limpiado.")

    def _log(self, message: str) -> None:
        self.log_area.append(message)
        print(message, flush=True)


def main() -> None:
    app = QApplication(sys.argv)
    window = DemoWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
