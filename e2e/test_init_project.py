#!/usr/bin/env python3

"""End-to-end tests for InitProject subtool."""

import os
import unittest

from codemcp.testing import MCPEndToEndTestCase


class InitProjectTest(MCPEndToEndTestCase):
    """Test the InitProject subtool functionality."""

    async def test_reuse_head_chat_id(self):
        """Test that reuse_head_chat_id=True reuses the chat ID from the HEAD commit."""
        # Set up a git repository in the temp dir
        from codemcp.git import (
            commit_changes,
            get_head_commit_chat_id,
            get_ref_commit_chat_id,
        )

        # Create a simple codemcp.toml file
        toml_path = os.path.join(self.temp_dir.name, "codemcp.toml")
        with open(toml_path, "w") as f:
            f.write("""
project_prompt = "Test reuse chat ID"
[commands]
test = ["./run_test.sh"]
""")

        # Set up a git repository
        await self.git_run(["init"])
        await self.git_run(["config", "user.email", "test@example.com"])
        await self.git_run(["config", "user.name", "Test User"])

        # Create an initial commit to have a HEAD reference
        test_file = os.path.join(self.temp_dir.name, "test_file.txt")
        with open(test_file, "w") as f:
            f.write("Initial content")

        await self.git_run(["add", "."])
        await self.git_run(["commit", "-m", "Initial commit"])

        # First InitProject call to create a reference with a chat ID
        async with self.create_client_session() as session:
            result_text1 = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "InitProject",
                    "path": self.temp_dir.name,
                    "user_prompt": "First initialization",
                    "subject_line": "feat: first commit",
                },
            )

            # Extract the chat ID from the result
            original_chat_id = self.extract_chat_id_from_text(result_text1)

            # Verify the reference contains the chat ID
            ref_name = f"refs/codemcp/{original_chat_id}"
            ref_chat_id = await get_ref_commit_chat_id(self.temp_dir.name, ref_name)
            self.assertEqual(
                original_chat_id, ref_chat_id, "Chat ID not found in reference"
            )

            # The HEAD commit doesn't have the chat ID yet since it's only in the reference
            head_chat_id = await get_head_commit_chat_id(self.temp_dir.name)
            self.assertNotEqual(
                original_chat_id, head_chat_id, "HEAD shouldn't have the chat ID yet"
            )

            # Make a change and commit it to add the chat ID to HEAD
            with open(test_file, "a") as f:
                f.write("\nAdded some content")

            await commit_changes(
                self.temp_dir.name,
                description="Adding content",
                chat_id=original_chat_id,
            )

            # Now verify the HEAD has the chat ID
            head_chat_id = await get_head_commit_chat_id(self.temp_dir.name)
            self.assertEqual(
                original_chat_id,
                head_chat_id,
                "Chat ID should be in HEAD commit after changes",
            )

        # Second InitProject call with reuse_head_chat_id=True
        async with self.create_client_session() as session:
            result_text2 = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "InitProject",
                    "path": self.temp_dir.name,
                    "user_prompt": "Continue working on the same feature",
                    "subject_line": "feat: continue working",
                    "reuse_head_chat_id": True,
                },
            )

            # Extract the chat ID from the result
            reused_chat_id = self.extract_chat_id_from_text(result_text2)

            # Verify the chat ID is the same as the original
            self.assertEqual(original_chat_id, reused_chat_id, "Chat ID not reused")

    async def test_init_project_basic(self):
        """Test basic InitProject functionality with simple TOML file."""
        # The basic codemcp.toml file is already created in the base test setup
        # We'll test that InitProject can read it correctly

        async with self.create_client_session() as session:
            # Call the InitProject tool with our new helper method
            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "InitProject",
                    "path": self.temp_dir.name,
                    "user_prompt": "Test initialization",
                    "subject_line": "feat: test basic functionality",
                    "reuse_head_chat_id": False,
                },
            )

            # Verify the result contains expected system prompt elements
            self.assertIn("You are an AI assistant", result_text)

    async def test_init_project_with_claude_md(self):
        """Test that InitProject folds a project-root CLAUDE.md into the system prompt."""
        claude_md_path = os.path.join(self.temp_dir.name, "CLAUDE.md")
        with open(claude_md_path, "w") as f:
            f.write("# CLAUDE.md\n\nAlways write haikus before editing files.\n")

        async with self.create_client_session() as session:
            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "InitProject",
                    "path": self.temp_dir.name,
                    "user_prompt": "Test with CLAUDE.md",
                    "subject_line": "feat: test claude.md support",
                    "reuse_head_chat_id": False,
                },
            )

            self.assertIn("Project instructions from CLAUDE.md", result_text)
            self.assertIn("Always write haikus before editing files.", result_text)

    async def test_init_project_without_claude_md(self):
        """Test that InitProject omits the CLAUDE.md section when no CLAUDE.md exists."""
        async with self.create_client_session() as session:
            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "InitProject",
                    "path": self.temp_dir.name,
                    "user_prompt": "Test without CLAUDE.md",
                    "subject_line": "feat: test missing claude.md",
                    "reuse_head_chat_id": False,
                },
            )

            self.assertNotIn("Project instructions from CLAUDE.md", result_text)

    async def test_init_project_complex_toml(self):
        """Test InitProject with a more complex TOML file that exercises all parsing features."""
        # Create a more complex codemcp.toml file with various data types
        complex_toml_path = os.path.join(self.temp_dir.name, "codemcp.toml")
        with open(complex_toml_path, "w") as f:
            f.write("""# Complex TOML file with various data types
project_prompt = \"\"\"
This is a multiline string
with multiple lines
of text.
\"\"\"

[commands]
format = ["./run_format.sh"]
lint = ["./run_lint.sh"]

[commands.test]
command = ["./run_test.sh"]
doc = "Run tests with optional arguments"
""")

        async with self.create_client_session() as session:
            # Call the InitProject tool with our new helper method
            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "InitProject",
                    "path": self.temp_dir.name,
                    "user_prompt": "Test with complex TOML",
                    "subject_line": "feat: test complex TOML parsing",
                    "reuse_head_chat_id": False,
                },
            )

            # Verify the result contains expected elements from the complex TOML
            self.assertIn("This is a multiline string", result_text)
            self.assertIn("format", result_text)
            self.assertIn("lint", result_text)
            self.assertIn("Run tests with optional arguments", result_text)

    async def test_init_project_with_binary_characters(self):
        """Test InitProject with TOML containing binary/non-ASCII characters to ensure proper handling."""
        # Create a TOML file with non-ASCII characters and binary data
        binary_toml_path = os.path.join(self.temp_dir.name, "codemcp.toml")
        with open(binary_toml_path, "wb") as f:
            f.write(b"""project_prompt = "Testing binary data handling \xc2\xa9\xe2\x84\xa2\xf0\x9f\x98\x8a"

[commands]
format = ["./run_format.sh"]
""")

        async with self.create_client_session() as session:
            # Call the InitProject tool with our new helper method
            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "InitProject",
                    "path": self.temp_dir.name,
                    "user_prompt": "Test with binary characters",
                    "subject_line": "feat: test binary character handling",
                    "reuse_head_chat_id": False,
                },
            )

            # Verify the result contains expected elements, ensuring binary data was handled properly
            self.assertIn("format", result_text)

    async def test_chat_id_from_subject_line(self):
        """Test that the chat ID uses the subject line for the human-readable part."""
        # Create a simple codemcp.toml file
        toml_path = os.path.join(self.temp_dir.name, "codemcp.toml")
        with open(toml_path, "w") as f:
            f.write("""
project_prompt = "Test project"
[commands]
test = ["./run_test.sh"]
""")

        # Set up a git repository
        from codemcp.git import get_head_commit_hash, get_ref_commit_chat_id

        await self.git_run(["init"])
        await self.git_run(["config", "user.email", "test@example.com"])
        await self.git_run(["config", "user.name", "Test User"])

        # Create an initial commit to have a HEAD reference
        test_file = os.path.join(self.temp_dir.name, "test_file.txt")
        with open(test_file, "w") as f:
            f.write("Initial content")

        await self.git_run(["add", "."])
        await self.git_run(["commit", "-m", "Initial commit"])

        # Get the hash of the initial commit
        initial_hash = await get_head_commit_hash(self.temp_dir.name, short=False)

        async with self.create_client_session() as session:
            # Call InitProject with a conventional commit style subject line
            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "InitProject",
                    "path": self.temp_dir.name,
                    "user_prompt": "Test initialize with subject line",
                    "subject_line": "feat: add new feature with spaces!",
                },
            )

            # Verify that the chat ID contains the slugified subject line
            # The chat ID should be something like "1-feat-add-new-feature-with-spaces"
            # Check that it's not using "untitled"
            self.assertNotIn("untitled", result_text)
            # Check that a slugified version of the subject line is in the chat ID
            self.assertIn("feat-add-new-feature-with-spaces", result_text)

            # Extract the chat ID from the result
            chat_id = self.extract_chat_id_from_text(result_text)

            # Verify that HEAD hasn't changed - it should still point to the initial commit
            head_hash_after = await get_head_commit_hash(
                self.temp_dir.name, short=False
            )
            self.assertEqual(
                initial_hash,
                head_hash_after,
                "HEAD should not change after InitProject",
            )

            # Verify the reference was created with the chat ID
            ref_name = f"refs/codemcp/{chat_id}"
            ref_chat_id = await get_ref_commit_chat_id(self.temp_dir.name, ref_name)
            self.assertEqual(
                chat_id,
                ref_chat_id,
                f"Chat ID {chat_id} should be in reference {ref_name}",
            )

    async def test_cherry_pick_reference_commit(self):
        """Test that commit_changes cherry-picks the reference commit when needed."""
        from codemcp.git import (
            commit_changes,
            get_head_commit_chat_id,
            get_head_commit_hash,
            get_head_commit_message,
        )

        # Set up a git repository
        await self.git_run(["init"])
        await self.git_run(["config", "user.email", "test@example.com"])
        await self.git_run(["config", "user.name", "Test User"])

        # Create an initial commit
        initial_file = os.path.join(self.temp_dir.name, "initial.txt")
        with open(initial_file, "w") as f:
            f.write("Initial content")

        await self.git_run(["add", "."])
        await self.git_run(["commit", "-m", "Initial commit"])

        # Get the hash of the initial commit
        initial_hash = await get_head_commit_hash(self.temp_dir.name, short=False)

        # Define the test subject and body for InitProject
        test_subject = "feat: test reference cherry-pick"
        test_body = "Test initialize for cherry-pick test"

        # Call InitProject which should create a reference without changing HEAD
        async with self.create_client_session() as session:
            result_text = await self.call_tool_assert_success(
                session,
                "codemcp",
                {
                    "subtool": "InitProject",
                    "path": self.temp_dir.name,
                    "user_prompt": test_body,
                    "subject_line": test_subject,
                },
            )

            # Extract the chat ID from the result
            chat_id = self.extract_chat_id_from_text(result_text)

            # Verify HEAD is unchanged
            head_hash_after_init = await get_head_commit_hash(
                self.temp_dir.name, short=False
            )
            self.assertEqual(
                initial_hash,
                head_hash_after_init,
                "HEAD should not change after InitProject",
            )

            # Now make a change and commit it
            test_file = os.path.join(self.temp_dir.name, "test_file.txt")
            with open(test_file, "w") as f:
                f.write("Test content for cherry-pick test")

            # Commit the changes - this should cherry-pick the reference commit first
            success, message = await commit_changes(
                path=self.temp_dir.name,
                description="Testing cherry-pick",
                chat_id=chat_id,
            )

            self.assertTrue(success, f"Commit failed: {message}")

            # Verify HEAD has a new commit with the right chat ID
            head_chat_id = await get_head_commit_chat_id(self.temp_dir.name)
            self.assertEqual(
                chat_id,
                head_chat_id,
                "HEAD commit should have the correct chat ID after cherry-pick",
            )

            # Verify the commit message contains the original subject and body from InitProject
            head_commit_msg = await get_head_commit_message(self.temp_dir.name)

            # Check that both the subject and body are in the commit message
            self.assertIn(
                test_subject,
                head_commit_msg,
                "Commit message should contain the original subject line",
            )
            self.assertIn(
                test_body,
                head_commit_msg,
                "Commit message should contain the original body",
            )

            # Check that the new description is also included since this is an amended commit
            self.assertIn(
                "Testing cherry-pick",
                head_commit_msg,
                "Commit message should contain the new change description",
            )

            # Get commit count to verify we have more than just the initial commit
            commit_count_output = await self.git_run(
                ["rev-list", "--count", "HEAD"], capture_output=True, text=True
            )
            commit_count = int(commit_count_output.strip())
            self.assertGreater(
                commit_count, 1, "Should have more than one commit after changes"
            )


if __name__ == "__main__":
    # Run the tests
    unittest.main()
