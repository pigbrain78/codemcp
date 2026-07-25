#!/usr/bin/env python3

"""Structural test: every @mcp.tool()-decorated function must actually be
registered (imported) in codemcp/main.py, or it is never reachable on a
running server.

Most e2e tests dispatch tool calls by calling the Python function directly
(see testing.py's `_dispatch_to_subtool`, used whenever `in_process` is True,
the default for MCPEndToEndTestCase), which bypasses the registration path
entirely and cannot catch a missing import. This is exactly how git_diff,
git_log, git_show, and git_blame went unregistered despite being fully
implemented and covered by e2e tests -- nothing exercised the actual
`main.py` import list until this test was added.
"""

import ast
import asyncio
import re
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / "codemcp" / "tools"
MAIN_PY = Path(__file__).resolve().parent.parent / "codemcp" / "main.py"
TESTING_PY = Path(__file__).resolve().parent.parent / "codemcp" / "testing.py"


def _mcp_tool_names_defined_in(file_path: Path) -> set[str]:
    """Return the names of top-level functions decorated with @mcp.tool() in a file."""
    tree = ast.parse(file_path.read_text(), filename=str(file_path))
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id == "mcp"
            ):
                names.add(node.name)
    return names


def _all_defined_tool_names() -> set[str]:
    defined: set[str] = set()
    for py_file in sorted(TOOLS_DIR.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        defined |= _mcp_tool_names_defined_in(py_file)
    return defined


def _names_imported_from_tools_in_main() -> set[str]:
    """Return the bound names of everything main.py imports from codemcp.tools.*."""
    tree = ast.parse(MAIN_PY.read_text(), filename=str(MAIN_PY))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "tools" in node.module:
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _dispatch_targets_in_testing() -> dict[str, str]:
    """Return {subtool_name: python_function_name} pairs from testing.py's
    `_dispatch_to_subtool`, i.e. every subtool the e2e test harness knows how
    to call directly in-process, and which function it calls to do so.
    """
    source = TESTING_PY.read_text()
    pattern = re.compile(r'subtool == "(\w+)":\s*\n(?:.*\n)*?\s*return await (\w+)\(')
    return {m.group(1): m.group(2) for m in pattern.finditer(source)}


class ToolRegistrationTest(unittest.TestCase):
    """Verify codemcp/tools/*.py and codemcp/main.py agree on what's registered."""

    def test_every_defined_tool_is_imported_in_main(self):
        """Static check: every @mcp.tool() function must be imported in main.py."""
        missing = sorted(
            _all_defined_tool_names() - _names_imported_from_tools_in_main()
        )
        self.assertEqual(
            missing,
            [],
            "Tool(s) defined with @mcp.tool() but not imported in codemcp/main.py, "
            f"so they are never registered on a running server: {missing}",
        )

    def test_every_registered_tool_matches_a_defined_tool(self):
        """Dynamic check: mcp.list_tools() must match what's statically discoverable.

        Imports codemcp.main to trigger the real registration side effects,
        then compares the live tool list against the @mcp.tool() functions
        found in codemcp/tools/*.py, as a cross-check on the AST-based scan.
        """
        import codemcp.main  # noqa: F401  (side effect: registers all tools)
        from codemcp.mcp import mcp

        async def get_registered_names() -> set[str]:
            tools = await mcp.list_tools()
            return {t.name for t in tools}

        registered = asyncio.run(get_registered_names())

        self.assertEqual(
            registered,
            _all_defined_tool_names(),
            "The live set of registered MCP tools does not match the set of "
            "@mcp.tool()-decorated functions found in codemcp/tools/*.py.",
        )

    def test_every_testing_dispatch_target_is_a_registered_tool(self):
        """Every subtool the test harness's `_dispatch_to_subtool` knows how to
        call directly must correspond to a live registered MCP tool.

        This is the check that would have caught git_diff/git_log/git_show/
        git_blame going unregistered: those functions had no @mcp.tool()
        decorator at all, so the two checks above (which only look at
        decorated functions) saw nothing to flag as missing -- both the
        static "defined" set and the dynamic "registered" set agreed on
        excluding them. Only cross-referencing against what the test harness
        actually calls (which is not gated on the decorator) exposes the gap.
        """
        import codemcp.main  # noqa: F401  (side effect: registers all tools)
        from codemcp.mcp import mcp

        async def get_registered_names() -> set[str]:
            tools = await mcp.list_tools()
            return {t.name for t in tools}

        registered = asyncio.run(get_registered_names())
        dispatch_targets = _dispatch_targets_in_testing()

        self.assertGreater(
            len(dispatch_targets), 0, "Failed to parse any subtool dispatch targets"
        )

        missing = sorted(
            f"{subtool} -> {func_name}"
            for subtool, func_name in dispatch_targets.items()
            if func_name not in registered
        )
        self.assertEqual(
            missing,
            [],
            "Subtool(s) callable via the test harness's direct dispatch but not "
            f"registered as a live MCP tool, so unreachable on a real server: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
