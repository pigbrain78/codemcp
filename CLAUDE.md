# codemcp

codemcp is an MCP (Model Context Protocol) server that lets Claude Desktop
(or any MCP client) directly read, edit, and run commands in a Git repository
on the user's machine. It's Python, targets 3.12+, uses `uv` for dependency
management, and is distributed as the `codemcp` CLI/package.

The author considers this project largely superseded by Claude Code (see the
NOTICE at the top of README.md), but still maintains it for its Git-versioning
design ideas. See `CONTRIBUTING.md` for the project's stance on accepting
patches (small hand-reviewable diffs, or a documented prompt + manual test
evidence).

## Project-specific instructions

`codemcp.toml` (repo root) is the actual source of truth for how an agent
should behave in this repo — it defines `project_prompt` (etch rules) and the
runnable `[commands]`. Read it before doing anything else. Key rules from it:

- Write a short haiku before beginning work on a feature.
- When done, run lint, then submit a PR using the `ghstack` command.
- Only write end-to-end tests — do NOT use mocks.
- When adding a new function argument, prefer updating all call sites over
  giving the argument a default, if that's feasible.
- New tool prompts go in `system_prompt` in `codemcp/tools/init_project.py`
  (this is also duplicated conceptually in `codemcp/main.py` — the system
  prompt there is a different, Claude-Code-style prompt used by `agno.py`;
  they should be kept in sync per the `NB` comment in `init_project.py`).
- Never wrap a failing operation in try/except to suppress and fall back;
  let exceptions propagate to the top level. If a test fails because of a
  thrown exception, figure out what invariant was violated instead of
  catching it.
- Never remove a failing `assert` to "fix" a test — diagnose the underlying
  bug, or ask the user and halt.
- End-to-end tests that call into codemcp functionality go in `e2e/`; pure
  unit tests for functional code go in `tests/`.

## Architecture

See `ARCHITECTURE.md` for more detail. Summary:

- `codemcp.toml` in a project root configures that project: `project_prompt`
  (injected into the system prompt) and `[commands]` (named shell commands
  the agent may invoke, e.g. `format`, `test`, `lint`). The `format` command
  is special — it runs automatically after every file edit.
- `codemcp/mcp.py` defines the shared `mcp` FastMCP instance; tools register
  themselves onto it with `@mcp.tool()`.
- `codemcp/main.py` is the CLI entrypoint (`codemcp` console script via
  `[project.scripts]` in `pyproject.toml`). It wires up all tool modules,
  configures logging (to `~/.codemcp/codemcp.log` by default, level from
  `~/.codemcprc`), and exposes subcommands: default (`serve` the stdio MCP
  server), `init` (scaffold a new project + git repo), `run` (execute a
  `codemcp.toml` command directly without the chat/commit flow), and `serve`
  (mount the MCP server over SSE/HTTP with CORS, default-restricted to
  `https://claude.ai`).
- `codemcp/tools/` — one module per tool exposed to the LLM: `read_file`,
  `write_file`, `edit_file`, `ls`, `grep`, `glob`, `rm`, `mv`, `chmod`,
  `run_command`, `think`, `init_project`, plus git-inspection tools
  (`git_blame`, `git_diff`, `git_log`, `git_show`) and `commit_utils`.
- `codemcp/access.py` enforces the core safety invariant: a file is only
  editable if it lives inside a Git repository *and* that repo's root
  contains a `codemcp.toml`. `get_git_base_dir` also guards against path
  traversal outside the repo.
- `codemcp/git*.py` (`git.py`, `git_commit.py`, `git_message.py`,
  `git_parse_message.py`, `git_query.py`) implement the "every LLM edit is a
  Git commit" model: each chat gets a `chat_id` (see `init_project.py`,
  format `N-slugified-description`, counter stored at
  `.git/codemcp/counter`), tracked via a `codemcp-id:` trailer in commit
  messages and a `refs/codemcp/<chat_id>` ref, so changes can be attributed
  and rolled back per-chat.
- `codemcp/rules.py`, `codemcp/glob_pattern.py`, `codemcp/common.py`,
  `codemcp/file_utils.py`, `codemcp/async_file_utils.py`,
  `codemcp/line_endings.py`, `codemcp/shell.py` — supporting utilities
  (path normalization, gitignore-aware globbing, async file I/O, line-ending
  handling, shell command execution).
- `codemcp/config.py` reads the optional global `~/.codemcprc` (logger path
  and verbosity).
- `codemcp/agno.py` integrates with the `agno` agent framework as an
  alternative runtime (used by `codemcp/main.py`'s SSE app path).
- `codemcp/templates/{blank,python}/` are project scaffolds used by
  `codemcp init` (with `--python` for the Python variant); placeholders like
  `__PROJECT_NAME__` / `__PACKAGE_NAME__` are substituted in file contents
  and paths.
- `stubs/` holds custom type stubs (`mcp_stubs`, `editorconfig`) referenced
  via `stubPath`/`stubPackages` in `pyproject.toml` for strict Pyright.

## Commands

Defined in `pyproject.toml` / the shell scripts at the repo root, and also
declared for the agent in `codemcp.toml`'s `[commands]` section:

- `./run_format.sh` — `ruff format .` (uses the repo's `.venv`).
- `./run_lint.sh` — `ruff check` with autofix, plus an unsafe-fix pass for
  `F401,F841,I`, plus a check that `e2e/*.py` never call `session.call_tool`
  directly (must use `call_tool_assert_success`/`call_tool_assert_error`
  helpers instead — the script fails the build if it finds a violation).
- `./run_test.sh [pytest-args]` — runs `pytest` (accepts a pytest-style test
  selector, e.g. `./run_test.sh e2e/test_edit_file.py::TestName::test_foo`).
- `EXPECTTEST_ACCEPT=1 ./run_test.sh [pytest-args]` (the `accept` command in
  `codemcp.toml`) — regenerate `expecttest` golden values.
- `./run_typecheck.sh` — Pyright in strict mode (`pyproject.toml`'s
  `[tool.pyright]`).

All four scripts invoke `.venv/bin/python`, so a `uv`-managed virtualenv must
exist at the repo root first (`uv sync`).

CI (`.github/workflows/test.yml`) runs `uv run --frozen pytest` on
`pull_request` and on push to `main`/`prod`.

## Testing conventions

- Test layout: `tests/` for pure unit tests, `e2e/` for tests that exercise
  the MCP tools end-to-end (see `pyproject.toml`'s `testpaths`). Tests run
  in parallel via `pytest-xdist` (`-n auto`).
- Use `expecttest`'s `assertExpectedInline` for multiline/golden-style
  assertions instead of `assertIn` on unstable output (see
  `.cursor/rules/expecttest.mdc`). If such a test fails, run the `accept`
  command to regenerate the expected value — never loosen the assertion. If
  accepted output still looks nondeterministic, stop and ask the user.
- We ONLY write end-to-end tests for tool behavior; do not introduce mocks.
- Never delete/weaken a failing `assert` to make a test pass — find the root
  cause.

## Type checking

Pyright strict mode, configured in `pyproject.toml`. See `CONTRIBUTING.md`
for the stub/ignore strategy: custom stubs live in `stubs/` (mapped via
`stubPackages`), and hard-to-type files get targeted
`[[tool.pyright.ignoreExtraErrors]]` entries rather than inline ignores.
Run `./run_typecheck.sh` before considering type-affecting changes done.

## Security invariants

- `check_edit_permission` (`codemcp/access.py`) must remain the gate for any
  file-modifying tool: no edits outside a Git repo, and no edits in a repo
  that lacks a `codemcp.toml` at its root.
- `get_git_base_dir` must keep rejecting paths that resolve outside the
  repository root (path traversal / symlink escape).
- Never introduce code that logs or exposes secrets/keys; never commit
  secrets.
- The SSE server (`codemcp serve`) defaults CORS to `https://claude.ai` only
  — don't widen this default without being asked.
