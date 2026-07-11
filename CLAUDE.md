# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

codemcp is an MCP (Model Context Protocol) server that turns Claude Desktop/Claude.ai
into a pair-programming assistant: it exposes filesystem, git, and shell tools that Claude
calls to read/edit files and run project commands. Two defining design choices:

- **No unrestricted shell.** Agents cannot run arbitrary shell commands; the commands they
  may invoke (`format`, `lint`, `test`, `typecheck`, ...) must be predeclared per-project in
  that project's `codemcp.toml`.
- **Every LLM edit is a git commit.** Each chat gets a `chat_id` (via `InitProject`) and edits
  made during that chat amend a single commit, so changes are fully reversible on a
  fine-grained basis. See `ARCHITECTURE.md` for the config file format and tool list.

See `README.md` for the installation/usage story (this project targets Claude Pro's $20/mo
subscription, is IDE-agnostic, and forbids unrestricted shell by design) and `TODO.md` for
known gaps. Note the top of `README.md`: the author considers this project superseded by
Claude Code itself, kept mainly for its git-versioning design ideas.

## Commands

All commands assume a `uv`-managed venv at `.venv/` (see `run_*.sh` — they invoke
`.venv/bin/python` directly).

```bash
./run_test.sh                        # pytest (tests/ + e2e/), runs with -n auto (parallel)
./run_test.sh path/to/test_file.py   # single test file
./run_test.sh path/to/test_file.py::TestClass::test_method   # single test
EXPECTTEST_ACCEPT=1 ./run_test.sh path/to/test_file.py   # regenerate expecttest golden output

./run_lint.sh                        # ruff check (with autofix) + forbids raw session.call_tool in e2e/
./run_format.sh                      # ruff format .
./run_typecheck.sh                   # pyright, strict mode, configured in pyproject.toml
```

`codemcp run <command> [args...]` executes a command defined in a target project's
`codemcp.toml` `[commands]` section directly (used by the tools above internally, and useful
for manually invoking a project's declared commands without going through git-commit flow).

## Testing conventions

- **Unit tests** (`tests/`) test pure/functional code (e.g. `git_message.py` parsing,
  `glob_pattern.py`, line-ending handling) with no MCP server involved.
- **End-to-end tests** (`e2e/`) spin up the actual MCP server and call tools through an MCP
  client session, via `codemcp.testing.MCPEndToEndTestCase`. Always use the
  `call_tool_assert_success` / `call_tool_assert_error` helpers instead of calling
  `session.call_tool` directly — `run_lint.sh` greps for and fails on direct
  `session.call_tool` usage in `e2e/*.py`.
- We use **expecttest** (`assertExpectedInline`) for golden-output assertions instead of
  hand-written `assertIn`/`assertEqual` on multiline/generated strings. When one fails
  legitimately, regenerate it with `EXPECTTEST_ACCEPT=1 ./run_test.sh <test>` rather than
  hand-editing the literal. If a run with `accept` still fails, the test is nondeterministic —
  stop and investigate rather than relaxing it.
- We only write end-to-end tests for tool behavior — no mocks. See
  `.cursor/rules/expecttest.mdc` for the fuller expecttest rationale.
- If an `assert` is failing, don't remove it — figure out what invariant broke.
- Don't wrap failures in try/except to suppress and fall back; let exceptions propagate to the
  top so real errors are visible.

## Architecture

**Entry point / process model** (`codemcp/main.py`, `codemcp/__init__.py`):
`codemcp` is a Click CLI (`codemcp/main.py:cli`) with subcommands `init` (scaffold a new
project + git repo from `codemcp/templates/{blank,python}/`), `run` (execute a
`codemcp.toml` command directly), and the default/no-subcommand path which starts the MCP
server — either over stdio (`run()` → `mcp.run()`) or as an SSE/Starlette app mounted via
`serve` (`create_sse_app`, CORS-restricted to `https://claude.ai` by default).

**Tool registration** (`codemcp/mcp.py`, `codemcp/tools/`): `mcp.py` creates the single
shared `FastMCP("codemcp")` instance. Each file under `codemcp/tools/` (e.g. `edit_file.py`,
`read_file.py`, `run_command.py`, `init_project.py`) defines one `@mcp.tool()`-registered
async function; `codemcp/__init__.py` imports all of them (for side-effecting registration)
before the server starts. `codemcp/tools/code_command.py` (`__init__.py`) is a
sub-namespace that re-exports plain async helpers (`chmod`, `mv`, `rm`, `git_blame`,
`git_diff`, `git_log`, `git_show`) that are not themselves separately-registered tools in the
same way — check individual files before assuming a tool is user-facing.

**Project config** (`codemcp.toml`, `codemcp/config.py`, `codemcp/code_command.py`):
Every project that codemcp operates on has its own `codemcp.toml` in the git root, with an
optional `project_prompt` (folded into the system prompt on `InitProject`) and a `[commands]`
table mapping command names to shell argv lists (or `{command, doc}` for tool-specific docs).
`codemcp/access.py:check_edit_permission` gates all file edits on this file's presence — no
`codemcp.toml` in the repo root means no edits are permitted. `format` is special: it is
invoked automatically after every file edit (see `code_command.py:run_code_command` /
`run_formatter_without_commit`).

**Git-commit-per-edit flow** (`codemcp/git.py`, `codemcp/git_commit.py`,
`codemcp/git_message.py`, `codemcp/git_parse_message.py`, `codemcp/git_query.py`,
`codemcp/tools/commit_utils.py`): `InitProject` (`codemcp/tools/init_project.py`) allocates a
`chat_id` (persisted as a counter in `.git/codemcp/counter`) that's threaded through
subsequent tool calls. Each edit tool commits (or amends) using that `chat_id` embedded in
the commit trailer/message (parsed/generated by `git_message.py` / `git_parse_message.py`),
so a whole chat's edits collapse into one amendable commit and can be rolled back
independently of other chats' work.

**Security boundary** (`codemcp/access.py`): file paths are resolved and checked to ensure
they stay inside the git repository root (defends against path traversal / symlink escapes)
in addition to the `codemcp.toml`-presence check above.

**Supporting modules**: `shell.py` (subprocess execution used by `run_command` and the
`[commands]` runners), `file_utils.py` / `async_file_utils.py` (file I/O helpers),
`line_endings.py` (CRLF/LF preservation on edits), `glob_pattern.py` (gitignore-aware glob
matching, also used by `main.py:get_files_respecting_gitignore` for template scaffolding),
`rules.py` (Cursor `.cursor/rules/*.mdc` discovery, surfaced to tools like `read_file`/`ls` so
Claude sees project-specific rule files), `agno.py` (integration with the `agno` agent
framework), `common.py` (path normalization, output truncation shared across tools).

## Type checking

Strict `pyright` mode (see `pyproject.toml` `[tool.pyright]`). Type stubs for third-party libs
without their own types live in `stubs/` (`stubPackages` maps `tomli`→`tomli_stubs`,
`mcp`→`mcp_stubs`). A few files with heavy dynamic typing (`testing.py`, `main.py`, `agno.py`,
`config.py`) have file-scoped `ignoreExtraErrors` entries in `pyproject.toml` rather than
inline `# type: ignore` — prefer that pattern over scattering inline ignores if you hit a
similar case.

## Argument design convention

When adding a new argument to an existing function, prefer updating **all** call sites to pass
it explicitly rather than giving it a default — only default it if genuinely optional for some
callers.

## Contributing

The maintainer does not review large LLM-generated patches by hand. Per `CONTRIBUTING.md`,
contributions should be either small hand-reviewable patches, or a prompt (with evidence of
manual testing) that can be used to regenerate the diff. See that file for details.
