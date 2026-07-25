#!/usr/bin/env python3

"""Tests for the RegexEdit subtool."""

import os

from codemcp.testing import MCPEndToEndTestCase


class RegexEditTest(MCPEndToEndTestCase):
    """Test the RegexEdit subtool."""

    async def get_chat_id_for_test(self, session, description: str) -> str:
        init_result_text = await self.call_tool_assert_success(
            session,
            "codemcp",
            {
                "subtool": "InitProject",
                "path": self.temp_dir.name,
                "user_prompt": description,
                "subject_line": f"test: {description}",
                "reuse_head_chat_id": False,
            },
        )
        return self.extract_chat_id_from_text(init_result_text)

    async def test_regex_edit_replaces_all_matches(self):
        """RegexEdit should replace every match in the file by default."""
        test_file_path = os.path.join(self.temp_dir.name, "regex_edit.txt")
        with open(test_file_path, "w") as f:
            f.write("foo bar\nfoo baz\nfoo qux\n")

        await self.git_run(["add", "regex_edit.txt"], check=False)
        await self.git_run(["commit", "-m", "Add file for regex editing"], check=False)

        async with self.create_client_session() as session:
            chat_id = await self.get_chat_id_for_test(session, "regex edit all matches")

            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "RegexEdit",
                    "path": test_file_path,
                    "pattern": r"foo",
                    "replacement": "FOO",
                    "description": "Rename foo to FOO",
                    "chat_id": chat_id,
                },
            )

            self.assertIn("made 3 replacements", result_text)

            with open(test_file_path) as f:
                content = f.read()
            self.assertEqual(content, "FOO bar\nFOO baz\nFOO qux\n")

    async def test_regex_edit_respects_count(self):
        """RegexEdit should only replace up to `count` matches when given."""
        test_file_path = os.path.join(self.temp_dir.name, "regex_edit_count.txt")
        with open(test_file_path, "w") as f:
            f.write("foo\nfoo\nfoo\n")

        await self.git_run(["add", "regex_edit_count.txt"], check=False)
        await self.git_run(["commit", "-m", "Add file for count test"], check=False)

        async with self.create_client_session() as session:
            chat_id = await self.get_chat_id_for_test(session, "regex edit count")

            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "RegexEdit",
                    "path": test_file_path,
                    "pattern": r"foo",
                    "replacement": "bar",
                    "count": 2,
                    "description": "Replace first two",
                    "chat_id": chat_id,
                },
            )

            self.assertIn("made 2 replacements", result_text)

            with open(test_file_path) as f:
                content = f.read()
            self.assertEqual(content, "bar\nbar\nfoo\n")

    async def test_regex_edit_backreferences(self):
        """RegexEdit should support \\1-style backreferences in the replacement."""
        test_file_path = os.path.join(self.temp_dir.name, "regex_edit_backref.txt")
        with open(test_file_path, "w") as f:
            f.write("first_name = 'Alice'\nlast_name = 'Smith'\n")

        await self.git_run(["add", "regex_edit_backref.txt"], check=False)
        await self.git_run(["commit", "-m", "Add file for backref test"], check=False)

        async with self.create_client_session() as session:
            chat_id = await self.get_chat_id_for_test(session, "regex edit backref")

            await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "RegexEdit",
                    "path": test_file_path,
                    "pattern": r"(\w+)_name",
                    "replacement": r"\1Name",
                    "description": "Convert to camelCase",
                    "chat_id": chat_id,
                },
            )

            with open(test_file_path) as f:
                content = f.read()
            self.assertEqual(content, "firstName = 'Alice'\nlastName = 'Smith'\n")

    async def test_regex_edit_no_matches(self):
        """RegexEdit should report no changes and not commit when nothing matches."""
        test_file_path = os.path.join(self.temp_dir.name, "regex_edit_nomatch.txt")
        with open(test_file_path, "w") as f:
            f.write("hello world\n")

        await self.git_run(["add", "regex_edit_nomatch.txt"], check=False)
        await self.git_run(["commit", "-m", "Add file for no-match test"], check=False)

        async with self.create_client_session() as session:
            chat_id = await self.get_chat_id_for_test(session, "regex edit no match")

            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "RegexEdit",
                    "path": test_file_path,
                    "pattern": r"nonexistent",
                    "replacement": "x",
                    "description": "No-op",
                    "chat_id": chat_id,
                },
            )

            self.assertIn("No matches found", result_text)

            with open(test_file_path) as f:
                content = f.read()
            self.assertEqual(content, "hello world\n")

    async def test_regex_edit_invalid_pattern(self):
        """RegexEdit should report an error for an invalid regular expression."""
        test_file_path = os.path.join(self.temp_dir.name, "regex_edit_invalid.txt")
        with open(test_file_path, "w") as f:
            f.write("hello world\n")

        await self.git_run(["add", "regex_edit_invalid.txt"], check=False)
        await self.git_run(
            ["commit", "-m", "Add file for invalid pattern test"], check=False
        )

        async with self.create_client_session() as session:
            chat_id = await self.get_chat_id_for_test(
                session, "regex edit invalid pattern"
            )

            error_text = await self.call_tool_assert_error(
                session,
                "codemcp",
                {
                    "subtool": "RegexEdit",
                    "path": test_file_path,
                    "pattern": r"(unclosed",
                    "replacement": "x",
                    "description": "Should fail",
                    "chat_id": chat_id,
                },
            )

            self.assertIn("Invalid regular expression", error_text)

    async def test_regex_edit_nonexistent_file(self):
        """RegexEdit should raise an error for a file that doesn't exist."""
        missing_path = os.path.join(self.temp_dir.name, "does_not_exist.txt")

        async with self.create_client_session() as session:
            chat_id = await self.get_chat_id_for_test(
                session, "regex edit missing file"
            )

            error_text = await self.call_tool_assert_error(
                session,
                "codemcp",
                {
                    "subtool": "RegexEdit",
                    "path": missing_path,
                    "pattern": r"foo",
                    "replacement": "bar",
                    "description": "Should fail",
                    "chat_id": chat_id,
                },
            )

            self.assertIn("File does not exist", error_text)

    async def test_regex_edit_untracked_file(self):
        """RegexEdit should refuse to edit a file that isn't tracked by git."""
        untracked_path = os.path.join(self.temp_dir.name, "untracked.txt")
        with open(untracked_path, "w") as f:
            f.write("foo\n")

        async with self.create_client_session() as session:
            chat_id = await self.get_chat_id_for_test(
                session, "regex edit untracked file"
            )

            error_text = await self.call_tool_assert_error(
                session,
                "codemcp",
                {
                    "subtool": "RegexEdit",
                    "path": untracked_path,
                    "pattern": r"foo",
                    "replacement": "bar",
                    "description": "Should fail",
                    "chat_id": chat_id,
                },
            )

            self.assertIn("not tracked by git", error_text)

    async def test_regex_edit_refuses_codemcp_toml(self):
        """RegexEdit should refuse to edit codemcp.toml for security reasons."""
        toml_path = os.path.join(self.temp_dir.name, "codemcp.toml")

        async with self.create_client_session() as session:
            chat_id = await self.get_chat_id_for_test(
                session, "regex edit codemcp.toml"
            )

            error_text = await self.call_tool_assert_error(
                session,
                "codemcp",
                {
                    "subtool": "RegexEdit",
                    "path": toml_path,
                    "pattern": r"format",
                    "replacement": "x",
                    "description": "Should fail",
                    "chat_id": chat_id,
                },
            )

            self.assertIn("not allowed", error_text)
