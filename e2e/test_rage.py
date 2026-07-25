#!/usr/bin/env python3

import tempfile
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from codemcp.main import cli, generate_rage_report


def test_generate_rage_report_includes_versions():
    """Test that the rage report includes version information."""
    report = generate_rage_report()

    assert "== Versions ==" in report
    assert "Python:" in report
    assert "Git:" in report


def test_generate_rage_report_includes_config_section():
    """Test that the rage report reports on the config file, present or absent."""
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = Path(temp_dir) / "codemcprc"
        with mock.patch("codemcp.config.get_config_path", return_value=config_path):
            report = generate_rage_report()
            assert "== Config ==" in report
            assert "No config file found" in report

        config_path.write_text("[logger]\nverbosity = 'DEBUG'\n")
        with mock.patch("codemcp.config.get_config_path", return_value=config_path):
            report = generate_rage_report()
            assert str(config_path) in report
            assert "verbosity = 'DEBUG'" in report


def test_generate_rage_report_includes_log_tail():
    """Test that the rage report includes the tail of the log file when present."""
    with tempfile.TemporaryDirectory() as temp_dir:
        log_lines = [f"log line {i}" for i in range(300)]
        (Path(temp_dir) / "codemcp.log").write_text("\n".join(log_lines) + "\n")

        with mock.patch("codemcp.config.get_logger_path", return_value=temp_dir):
            report = generate_rage_report(log_tail_lines=200)

        assert "log line 299" in report
        assert "log line 100" in report
        assert "log line 99" not in report


def test_generate_rage_report_no_log_file():
    """Test that the rage report handles a missing log file gracefully."""
    with tempfile.TemporaryDirectory() as temp_dir:
        with mock.patch("codemcp.config.get_logger_path", return_value=temp_dir):
            report = generate_rage_report()

        assert "No log file found" in report


def test_rage_cli_command():
    """Test that `codemcp rage` runs and prints a report."""
    runner = CliRunner()
    result = runner.invoke(cli, ["rage"])

    assert result.exit_code == 0, result.output
    assert "== Versions ==" in result.output
