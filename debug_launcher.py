import sys, os, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import QApplication as _Orig

_LOGFILE = Path(tempfile.gettempdir()) / "bridge_debug.log"


class _Bridged(_Orig):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import traceback as _tb
        log = open(_LOGFILE, "w", encoding="utf-8")
        try:
            from pyside_mcp import install_bridge
            port = install_bridge()
            log.write(f"OK port={port} pid={os.getpid()}\n")
        except Exception:
            log.write(_tb.format_exc())
        finally:
            log.close()


import PySide6.QtWidgets as _w
_w.QApplication = _Bridged

_script = str(Path(__file__).parent / "example_app.py")
_g = {"__name__": "__main__", "__file__": _script}
exec(compile(open(_script, "rb").read(), _script, "exec"), _g)
