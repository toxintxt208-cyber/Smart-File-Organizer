"""Registro de eventos en un archivo rotativo del directorio de usuario."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .paths import get_log_path


def setup_logging(
    *,
    log_path: Path | str | None = None,
    level: int = logging.INFO,
    console: bool = True,
) -> Path:
    """Configura el logger raíz una sola vez y devuelve la ruta del archivo."""

    path = Path(log_path) if log_path else get_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    target = str(path.resolve())

    already_configured = False
    for handler in root.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                if Path(handler.baseFilename).resolve() == Path(target):
                    already_configured = True
                    break
            except (OSError, AttributeError):
                continue

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not already_configured:
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        handler.setLevel(level)
        root.addHandler(handler)

    if console and not any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in root.handlers
    ):
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.setLevel(level)
        root.addHandler(stream)

    return path
