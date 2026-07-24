#!/usr/bin/env python3

import os
from unittest import mock

from codemcp.testing import MCPEndToEndTestCase
from codemcp.tools.git_blame import git_blame, git_blame_command
from codemcp.tools.git_diff import git_diff, git_diff_command
from codemcp.tools.git_log import git_log, git_log_command
from codemcp.tools.git_show import git_show, git_show_command


class TestGitTools(MCPEndToEndTestCase):
    """Test the git tools functionality."""

    async def asyncSetUp(self):
        # Use the parent class's asyncSetUp to set up test environment
        await super().asyncSetUp()

        # Create a sample file
        self.sample_file = os.path.join(self.temp_dir.name, "sample.txt")
        with open(self.sample_file, "w") as f:
            f.write("Sample content\nLine 2\nLine 3\n")

        # Add and commit the file (the base class already has git initialized)
        await self.git_run(["add", "sample.txt"])
        await self.git_run(["commit", "-m", "Initial commit"])

        # Modify the file and create another commit
        with open(self.sample_file, "a") as f:
            f.write("Line 4\nLine 5\n")

        await self.git_run(["add", "sample.txt"])
        await self.git_run(["commit", "-m", "Second commit"])

    async def test_git_log(self):
        """Test the git_log_command implementation."""
        # Test with no arguments
        result = await git_log_command(path=self.temp_dir.name)
        self.assertIn("Initial commit", result["output"])
        self.assertIn("Second commit", result["output"])

        # Test with arguments
        result = await git_log_command(
            arguments="--oneline -n 1", path=self.temp_dir.name
        )
        self.assertIn("Second commit", result["output"])
        self.assertNotIn("Initial commit", result["output"])

    async def test_git_diff(self):
        """Test the git_diff_command implementation."""
        # Create a change but don't commit it
        with open(self.sample_file, "a") as f:
            f.write("Uncommitted change\n")

        # Test with no arguments
        result = await git_diff_command(path=self.temp_dir.name)
        self.assertIn("Uncommitted change", result["output"])

        # Test with arguments
        result = await git_diff_command(
            arguments="HEAD~1 HEAD", path=self.temp_dir.name
        )
        self.assertIn("Line 4", result["output"])

    async def test_git_show(self):
        """Test the git_show_command implementation."""
        # Test with no arguments (should show the latest commit)
        result = await git_show_command(path=self.temp_dir.name)
        self.assertIn("Second commit", result["output"])

        # Test with arguments
        result = await git_show_command(arguments="HEAD~1", path=self.temp_dir.name)
        self.assertIn("Initial commit", result["output"])

    async def test_git_blame(self):
        """Test the git_blame_command implementation."""
        # Test with file argument
        result = await git_blame_command(
            arguments="sample.txt", path=self.temp_dir.name
        )
        self.assertIn(
            "A U Thor", result["output"]
        )  # MCPEndToEndTestCase sets this author
        self.assertIn("Line 2", result["output"])

        # Test with line range
        result = await git_blame_command(
            arguments="-L 4,5 sample.txt", path=self.temp_dir.name
        )
        self.assertIn("Line 4", result["output"])
        self.assertNotIn("Line 2", result["output"])

    async def test_invalid_path(self):
        """Test that the underlying *_command implementations handle invalid paths."""
        with mock.patch("codemcp.tools.git_log.is_git_repository", return_value=False):
            with self.assertRaises(ValueError):
                await git_log_command(path="/invalid/path")

        with mock.patch("codemcp.tools.git_diff.is_git_repository", return_value=False):
            with self.assertRaises(ValueError):
                await git_diff_command(path="/invalid/path")

        with mock.patch("codemcp.tools.git_show.is_git_repository", return_value=False):
            with self.assertRaises(ValueError):
                await git_show_command(path="/invalid/path")

        with mock.patch(
            "codemcp.tools.git_blame.is_git_repository", return_value=False
        ):
            with self.assertRaises(ValueError):
                await git_blame_command(path="/invalid/path")

    async def test_git_log_tool(self):
        """Test that the git_log MCP tool is registered and returns formatted text."""
        result = await git_log(path=self.temp_dir.name)
        self.assertIsInstance(result, str)
        self.assertIn("Second commit", result)

    async def test_git_diff_tool(self):
        """Test that the git_diff MCP tool is registered and returns formatted text."""
        with open(self.sample_file, "a") as f:
            f.write("Uncommitted change\n")

        result = await git_diff(path=self.temp_dir.name)
        self.assertIsInstance(result, str)
        self.assertIn("Uncommitted change", result)

    async def test_git_show_tool(self):
        """Test that the git_show MCP tool is registered and returns formatted text."""
        result = await git_show(path=self.temp_dir.name)
        self.assertIsInstance(result, str)
        self.assertIn("Second commit", result)

    async def test_git_blame_tool(self):
        """Test that the git_blame MCP tool is registered and returns formatted text."""
        result = await git_blame(arguments="sample.txt", path=self.temp_dir.name)
        self.assertIsInstance(result, str)
        self.assertIn("Line 2", result)

    async def test_git_tool_reports_errors_instead_of_raising(self):
        """MCP tool wrappers should return an error string, not raise, on failure."""
        with mock.patch("codemcp.tools.git_log.is_git_repository", return_value=False):
            result = await git_log(path="/invalid/path")
            self.assertIsInstance(result, str)
            self.assertIn("Error", result)

    async def test_git_tool_defaults_path_to_cwd(self):
        """The git_log MCP tool should default path to the current directory."""
        old_cwd = os.getcwd()
        try:
            os.chdir(self.temp_dir.name)
            result = await git_log()
            self.assertIn("Second commit", result)
        finally:
            os.chdir(old_cwd)
