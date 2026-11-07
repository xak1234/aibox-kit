"""Local development entry point; the Sites supervisor owns this process."""
import os
from pathlib import Path

root = Path(__file__).resolve().parent.parent
python = root / '.venv/bin/python'
if not python.exists():
    raise SystemExit('Create .venv and install requirements-dev.txt first.')
os.chdir(root)
os.execv(str(python), [str(python), '-m', 'uvicorn', 'terminal_app.app:app', '--host', '0.0.0.0', '--port', '4173'])
