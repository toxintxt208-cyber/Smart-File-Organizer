"""Genera assets/app.ico para el ejecutable de Windows.

Se ejecuta durante la compilación (build.bat). Pillow solo se necesita para
crear el icono; la aplicación en tiempo de ejecución no depende de Pillow.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_icon(output: Path) -> Path:
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover - depende del entorno de build
        raise SystemExit(
            "Pillow no está instalado. Instala las dependencias de compilación con:\n"
            "  python -m pip install -r requirements-build.txt"
        ) from exc

    size = 256
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Fondo redondeado azul.
    draw.rounded_rectangle((8, 8, size - 8, size - 8), radius=54, fill=(18, 50, 74, 255))

    # Carpeta blanca.
    draw.rounded_rectangle((48, 82, 208, 196), radius=18, fill=(245, 249, 252, 255))
    draw.rounded_rectangle((54, 62, 126, 104), radius=14, fill=(245, 249, 252, 255))

    # Documento y check verde.
    draw.rounded_rectangle((92, 108, 174, 176), radius=10, fill=(210, 228, 242, 255))
    draw.line((104, 128, 160, 128), fill=(18, 50, 74, 255), width=7)
    draw.line((104, 146, 148, 146), fill=(18, 50, 74, 255), width=7)
    draw.line((104, 164, 138, 164), fill=(18, 50, 74, 255), width=7)
    draw.line((150, 170, 170, 190, 212, 144), fill=(34, 197, 94, 255), width=12, joint="curve")

    output.parent.mkdir(parents=True, exist_ok=True)
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    image.save(output, format="ICO", sizes=sizes)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera el icono de Smart File Organizer.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "assets" / "app.ico",
        help="Ruta del archivo .ico de salida.",
    )
    args = parser.parse_args(argv)
    try:
        path = build_icon(args.output)
    except OSError as exc:
        print(f"ERROR: no se pudo crear el icono: {exc}", file=sys.stderr)
        return 1
    print(f"Icono creado: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
