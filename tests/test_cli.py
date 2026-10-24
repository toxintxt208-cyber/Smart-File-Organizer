from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import main as cli
from src.config_manager import ConfigManager


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sfo-cli-")
        self.base = Path(self.temp.name)
        self.source = self.base / "Descargas"
        self.source.mkdir()
        self.config_path = self.base / "config.json"
        self.manager = ConfigManager(self.config_path)
        self.manager.save(
            {
                "sources": [
                    {"name": "Descargas", "path": str(self.source), "enabled": True}
                ],
                "categories": {
                    "Documentos": [".pdf"],
                    "Imagenes": [".png"],
                    "Otros": [],
                },
                "default_category": "Otros",
                "automation": {"enabled": False, "interval_minutes": 30},
                "history": {"enabled": True, "max_operations": 50},
            }
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        command = ["--config", str(self.config_path)]
        if any(arg in {"--analyze", "--dry-run", "--organize"} for arg in arguments):
            command.extend(["--source", str(self.source)])
        command.extend(arguments)
        with mock.patch("main._setup_logging_for", return_value=self.base / "cli.log"):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(command)
        return code, stdout.getvalue(), stderr.getvalue()

    def make_file(self, name: str, content: bytes = b"datos") -> Path:
        path = self.source / name
        path.write_bytes(content)
        return path

    def test_confirm_without_stdin_requires_yes(self) -> None:
        with mock.patch("main.sys.stdin", None):
            with self.assertRaises(cli.CliError):
                cli._confirm("¿Continuar?", False)
        self.assertTrue(cli._confirm("¿Continuar?", True))

    def test_parser_accepts_required_modes(self) -> None:
        parser = cli.build_parser()
        for mode in (
            "--analyze",
            "--dry-run",
            "--organize",
            "--stats",
            "--validate-config",
            "--watch",
            "--schedule",
            "--undo",
        ):
            with self.subTest(mode=mode):
                args = parser.parse_args([mode])
                self.assertTrue(getattr(args, mode.replace("--", "").replace("-", "_")))

    def test_analyze_lists_files_without_moving(self) -> None:
        self.make_file("informe.pdf")
        self.make_file("foto.png")
        self.make_file("LEEME")
        code, stdout, stderr = self.run_cli("--analyze")
        self.assertEqual(code, cli.EXIT_OK, stderr)
        self.assertIn("informe.pdf", stdout)
        self.assertIn("Documentos", stdout)
        self.assertIn("Resumen: 3 detectados", stdout)
        self.assertTrue((self.source / "informe.pdf").exists())
        self.assertFalse((self.source / "Documentos").exists())

    def test_dry_run_does_not_modify_anything(self) -> None:
        self.make_file("informe.pdf")
        code, stdout, _ = self.run_cli("--dry-run")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Simulación (no se ha modificado nada)", stdout)
        self.assertIn("1 archivos se moverían", stdout)
        self.assertTrue((self.source / "informe.pdf").exists())
        self.assertFalse((self.source / "Documentos").exists())
        self.assertFalse((self.base / "history.json").exists())

    def test_organize_moves_files_and_records_history(self) -> None:
        self.make_file("informe.pdf", b"pdf")
        self.make_file("foto.png", b"png")
        code, stdout, _ = self.run_cli("--organize")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Organización ejecutada", stdout)
        self.assertIn("Historial guardado: sí", stdout)
        self.assertEqual((self.source / "Documentos" / "informe.pdf").read_bytes(), b"pdf")
        self.assertEqual((self.source / "Imagenes" / "foto.png").read_bytes(), b"png")
        self.assertFalse((self.source / "informe.pdf").exists())

        history_path = self.base / "history.json"
        self.assertTrue(history_path.exists())
        data = json.loads(history_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["operations"]), 1)
        self.assertEqual(len(data["operations"][0]["entries"]), 2)

    def test_stats_reports_operations(self) -> None:
        self.make_file("informe.pdf")
        self.run_cli("--organize")
        code, stdout, _ = self.run_cli("--stats")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Operaciones          : 1", stdout)
        self.assertIn("Archivos movidos     : 1", stdout)
        self.assertIn(str(self.base / "history.json"), stdout)

    def test_validate_config(self) -> None:
        code, stdout, _ = self.run_cli("--validate-config")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Estado  : válida", stdout)
        self.assertIn("Orígenes configurados : 1", stdout)
        self.assertIn("Automático            : desactivado", stdout)

    def test_undo_restores_last_operation(self) -> None:
        original = self.make_file("informe.pdf", b"pdf")
        code, _, _ = self.run_cli("--organize")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertFalse(original.exists())

        code, stdout, _ = self.run_cli("--undo", "--yes")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Restaurados: 1", stdout)
        self.assertTrue(original.exists())
        self.assertEqual(original.read_bytes(), b"pdf")
        self.assertFalse((self.source / "Documentos" / "informe.pdf").exists())

        code, stdout, _ = self.run_cli("--undo", "--yes")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("No hay ningún movimiento reciente", stdout)

    def test_missing_source_returns_usage_error(self) -> None:
        stdout = io.StringIO()
        errors = io.StringIO()
        with mock.patch("main._setup_logging_for", return_value=self.base / "cli.log"):
            with redirect_stdout(stdout), redirect_stderr(errors):
                code = cli.main(
                    [
                        "--config",
                        str(self.config_path),
                        "--source",
                        str(self.base / "no-existe"),
                        "--analyze",
                    ]
                )
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("no existe", errors.getvalue())

    def test_schedule_refuses_when_disabled(self) -> None:
        code, _, stderr = self.run_cli("--schedule")
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("desactivada", stderr)

    def test_collision_option_requires_undo(self) -> None:
        with mock.patch("main._setup_logging_for", return_value=self.base / "cli.log"):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(
                        [
                            "--config",
                            str(self.config_path),
                            "--analyze",
                            "--collision",
                            "rename",
                        ]
                    )
        self.assertEqual(raised.exception.code, cli.EXIT_USAGE)

    def test_source_requires_a_mode(self) -> None:
        with mock.patch("main._setup_logging_for", return_value=self.base / "cli.log"):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(
                        [
                            "--config",
                            str(self.config_path),
                            "--source",
                            str(self.source),
                        ]
                    )
        self.assertEqual(raised.exception.code, cli.EXIT_USAGE)

    def test_config_is_created_when_missing(self) -> None:
        missing = self.base / "nueva" / "config.json"
        stdout = io.StringIO()
        with mock.patch("main._setup_logging_for", return_value=self.base / "cli.log"):
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = cli.main(["--config", str(missing), "--validate-config"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertTrue(missing.exists())


if __name__ == "__main__":
    unittest.main()
