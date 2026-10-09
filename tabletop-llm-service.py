#!/usr/bin/env python3
"""Самостоятельный запуск приватного сервиса, независимо от TUI."""

import os
from pathlib import Path
import sys

_VENV_DIR = Path(__file__).resolve().parent / ".venv"
_VENV_PYTHON = _VENV_DIR / "bin" / "python"
if _VENV_PYTHON.exists() and Path(sys.prefix) != _VENV_DIR:
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

from llm_service.runtime import entry_point

if __name__ == "__main__":
    raise SystemExit(entry_point())
