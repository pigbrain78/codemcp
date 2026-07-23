#!/usr/bin/env python3

import tempfile
from pathlib import Path

from click.testing import CliRunner

from codemcp.main import cli, infer_commands_from_project


def test_infer_commands_from_node_project():
    """Test that infer_commands_from_project reads npm scripts from package.json."""
    with tempfile.TemporaryDirectory() as temp_dir:
        project_path = Path(temp_dir)
        (project_path / "package.json").write_text(
            '{"scripts": {"format": "prettier --write .", "test": "jest", "build": "tsc"}}'
        )

        commands = infer_commands_from_project(project_path)

        assert commands["format"] == ["npm", "run", "format"]
        assert commands["test"] == ["npm", "run", "test"]
        assert commands["build"] == ["npm", "run", "build"]
        assert "lint" not in commands


def test_infer_commands_from_python_project():
    """Test that infer_commands_from_project reads tool sections from pyproject.toml."""
    with tempfile.TemporaryDirectory() as temp_dir:
        project_path = Path(temp_dir)
        (project_path / "pyproject.toml").write_text("""
[tool.ruff]
line-length = 88

[tool.pyright]
typeCheckingMode = "strict"

[tool.pytest.ini_options]
testpaths = ["tests"]
""")

        commands = infer_commands_from_project(project_path)

        assert commands["format"] == ["ruff", "format"]
        assert commands["lint"] == ["ruff", "check", "--fix"]
        assert commands["typecheck"] == ["pyright"]
        assert commands["test"] == ["pytest"]


def test_infer_commands_from_makefile():
    """Test that infer_commands_from_project reads targets from a Makefile."""
    with tempfile.TemporaryDirectory() as temp_dir:
        project_path = Path(temp_dir)
        (project_path / "Makefile").write_text("""
test:
\tgo test ./...

lint:
\tgolangci-lint run
""")

        commands = infer_commands_from_project(project_path)

        assert commands["test"] == ["make", "test"]
        assert commands["lint"] == ["make", "lint"]
        assert "format" not in commands


def test_infer_commands_no_manifests():
    """Test that infer_commands_from_project returns an empty dict when nothing is detected."""
    with tempfile.TemporaryDirectory() as temp_dir:
        commands = infer_commands_from_project(Path(temp_dir))
        assert commands == {}


def test_infer_config_cli_writes_toml():
    """Test that `codemcp infer-config` writes a codemcp.toml with inferred commands."""
    with tempfile.TemporaryDirectory() as temp_dir:
        project_path = Path(temp_dir)
        (project_path / "package.json").write_text('{"scripts": {"test": "jest"}}')

        runner = CliRunner()
        result = runner.invoke(cli, ["infer-config", str(project_path)])

        assert result.exit_code == 0, result.output
        config_path = project_path / "codemcp.toml"
        assert config_path.exists()
        content = config_path.read_text()
        assert "[commands]" in content
        assert 'test = ["npm", "run", "test"]' in content


def test_infer_config_cli_refuses_to_overwrite():
    """Test that `codemcp infer-config` refuses to run if codemcp.toml already exists."""
    with tempfile.TemporaryDirectory() as temp_dir:
        project_path = Path(temp_dir)
        config_path = project_path / "codemcp.toml"
        config_path.write_text("# existing config\n")

        runner = CliRunner()
        result = runner.invoke(cli, ["infer-config", str(project_path)])

        assert result.exit_code != 0
        assert config_path.read_text() == "# existing config\n"
