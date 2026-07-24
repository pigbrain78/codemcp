#!/usr/bin/env python3

import logging
import shlex
from typing import Any

from ..common import normalize_file_path
from ..git import is_git_repository
from ..mcp import mcp
from ..shell import run_command
from .commit_utils import append_commit_hash

__all__ = [
    "git_log",
    "git_log_command",
    "render_result_for_assistant",
    "TOOL_NAME_FOR_PROMPT",
    "DESCRIPTION",
]

TOOL_NAME_FOR_PROMPT = "GitLog"
DESCRIPTION = """
Shows commit logs using git log.
This tool is read-only and safe to use with any arguments.
The arguments parameter should be a string and will be interpreted as space-separated
arguments using shell-style tokenization (spaces separate arguments, quotes can be used
for arguments containing spaces, etc.).

Example:
  git log --oneline -n 5  # Show the last 5 commits in oneline format
  git log --author="John Doe" --since="2023-01-01"  # Show commits by an author since a date
  git log -- path/to/file  # Show commit history for a specific file
"""


async def git_log_command(
    arguments: str | None = None,
    path: str | None = None,
    chat_id: str | None = None,
) -> dict[str, Any]:
    """Execute git log with the provided arguments.

    Args:
        arguments: Optional arguments to pass to git log as a string
        path: The directory to execute the command in (must be in a git repository)
        chat_id: The unique ID of the current chat session

    Returns:
        A dictionary with git log output
    """

    if path is None:
        raise ValueError("Path must be provided for git log")

    # Normalize the directory path
    absolute_path = normalize_file_path(path)

    # Verify this is a git repository
    if not await is_git_repository(absolute_path):
        raise ValueError(f"The provided path is not in a git repository: {path}")

    # Build command
    cmd = ["git", "log"]

    # Add additional arguments if provided
    if arguments:
        parsed_args = shlex.split(arguments)
        cmd.extend(parsed_args)

    logging.debug(f"Executing git log command: {' '.join(cmd)}")

    # Execute git log command asynchronously
    result = await run_command(
        cmd=cmd,
        cwd=absolute_path,
        capture_output=True,
        text=True,
        check=True,  # Allow exception if git log fails to propagate up
    )

    # Prepare output
    output = {
        "output": result.stdout,
    }

    # Add formatted result for assistant
    output["resultForAssistant"] = render_result_for_assistant(output)

    return output


def render_result_for_assistant(output: dict[str, Any]) -> str:
    """Render the results in a format suitable for the assistant.

    Args:
        output: The git log output dictionary

    Returns:
        A formatted string representation of the results
    """
    return output.get("output", "")


@mcp.tool()
async def git_log(
    arguments: str | None = None,
    path: str | None = None,
    chat_id: str | None = None,
    commit_hash: str | None = None,
) -> str:
    """Shows commit logs using git log.
    This tool is read-only and safe to use with any arguments.
    The arguments parameter should be a string and will be interpreted as space-separated
    arguments using shell-style tokenization (spaces separate arguments, quotes can be used
    for arguments containing spaces, etc.).

    Example:
      GitLog arguments="--oneline -n 5"  # Show the last 5 commits in oneline format
      GitLog arguments="--author=\\"John Doe\\" --since=\\"2023-01-01\\""  # Show commits by an author since a date
      GitLog arguments="-- path/to/file"  # Show commit history for a specific file

    Args:
        arguments: Optional arguments to pass to git log as a string
        path: The directory to execute the command in (must be in a git repository); defaults to the current directory
        chat_id: The unique ID of the current chat session
        commit_hash: Optional Git commit hash for version tracking

    Returns:
        A string with the git log output
    """
    try:
        chat_id = "" if chat_id is None else chat_id
        normalized_path = normalize_file_path("." if path is None else path)

        output = await git_log_command(
            arguments=arguments, path=normalized_path, chat_id=chat_id
        )
        result_for_assistant = output["resultForAssistant"]

        result_for_assistant, _ = await append_commit_hash(
            result_for_assistant, normalized_path, commit_hash
        )
        return result_for_assistant
    except Exception as e:
        logging.error(f"Error in git_log: {e}", exc_info=True)
        return f"Error executing git log: {e}"
