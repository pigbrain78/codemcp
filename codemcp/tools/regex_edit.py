#!/usr/bin/env python3

import logging
import os
import re

from ..code_command import run_formatter_without_commit
from ..common import normalize_file_path
from ..file_utils import (
    async_open_text,
    check_file_path_and_permissions,
    check_git_tracking_for_existing_file,
    write_text_content,
)
from ..git import commit_changes
from ..line_endings import detect_line_endings
from ..mcp import mcp
from .commit_utils import append_commit_hash

__all__ = [
    "regex_edit",
]


@mcp.tool()
async def regex_edit(
    path: str,
    pattern: str,
    replacement: str,
    count: int = 0,
    description: str | None = None,
    chat_id: str | None = None,
    commit_hash: str | None = None,
) -> str:
    r"""Performs a regular-expression search-and-replace across a single file.

    Unlike EditFile (which requires old_string to uniquely identify one exact
    location), this tool matches a Python regular expression and replaces every
    match in the file in one call. Use it for refactor-y, pattern-based changes
    (e.g. renaming a symbol used many times, or updating an import style) where
    writing out each occurrence's surrounding context for EditFile would be
    tedious or error-prone.

    The replacement string may reference capture groups (\1, \2, ...) using
    Python re.sub syntax.

    Prefer EditFile for a single, well-understood change — this tool trades
    EditFile's uniqueness safety net for the ability to change every match at
    once, so review the returned summary and, if unsure, follow up with
    GitDiff before continuing.

    Args:
        path: The absolute path to the file to modify (must be absolute, not relative)
        pattern: A Python regular expression to search for
        replacement: The replacement string (supports \1, \2, ... backreferences)
        count: Maximum number of replacements to make (0 means replace all matches)
        description: Short description of the change
        chat_id: The unique ID of the current chat session
        commit_hash: Optional Git commit hash for version tracking

    Returns:
        A message describing how many replacements were made, or an explanation
        if the pattern didn't match anything

    Note:
        This tool only operates on files that already exist and are tracked by
        git; it does not create new files.
    """
    description = "" if description is None else description
    chat_id = "" if chat_id is None else chat_id

    full_file_path = normalize_file_path(path)

    # Prevent editing codemcp.toml for security reasons
    if os.path.basename(full_file_path) == "codemcp.toml":
        raise ValueError("Editing codemcp.toml is not allowed for security reasons.")

    # Check file path and permissions
    is_valid, error_message = await check_file_path_and_permissions(full_file_path)
    if not is_valid:
        raise ValueError(error_message)

    if not os.path.exists(full_file_path):
        raise FileNotFoundError(f"File does not exist: {full_file_path}")

    # Check git tracking status and commit any pending changes
    is_tracked, track_error = await check_git_tracking_for_existing_file(
        full_file_path,
        chat_id=chat_id,
    )
    if not is_tracked:
        raise ValueError(track_error)

    try:
        compiled_pattern = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Invalid regular expression {pattern!r}: {e}") from e

    if count < 0:
        raise ValueError(
            f"count must be >= 0 (0 means replace all matches), got {count}"
        )

    # Use UTF-8 encoding and detect line endings
    line_endings = await detect_line_endings(full_file_path, return_format="format")

    # Read the original file
    content = await async_open_text(full_file_path, encoding="utf-8")

    try:
        updated_content, num_replacements = compiled_pattern.subn(
            replacement, content, count=count
        )
    except re.error as e:
        raise ValueError(f"Invalid replacement string {replacement!r}: {e}") from e

    if num_replacements == 0:
        return (
            f"No matches found for pattern {pattern!r} in {full_file_path}. "
            "No changes made."
        )

    # Write the modified content back to the file
    await write_text_content(full_file_path, updated_content, "utf-8", line_endings)

    # Try to run the formatter on the file
    format_message = ""
    formatter_success, formatter_output = await run_formatter_without_commit(
        full_file_path
    )
    if formatter_success:
        logging.info(f"Auto-formatted {full_file_path}")
        if formatter_output.strip():
            format_message = "\nAuto-formatted the file"
    else:
        if "No format command configured" not in formatter_output:
            logging.warning(
                f"Failed to auto-format {full_file_path}: {formatter_output}"
            )

    # Commit the changes
    git_message = ""
    success, message = await commit_changes(full_file_path, description, chat_id)
    if success:
        git_message = f"\n\nChanges committed to git: {description}"
        if "previous commit was" in message:
            git_message = f"\n\n{message}"
    else:
        git_message = f"\n\nFailed to commit changes to git: {message}"

    replacement_word = "replacement" if num_replacements == 1 else "replacements"
    result = (
        f"Successfully edited {full_file_path}: made {num_replacements} "
        f"{replacement_word} for pattern {pattern!r}{format_message}{git_message}"
    )

    # Append commit hash
    result, _ = await append_commit_hash(result, full_file_path, commit_hash)
    return result
