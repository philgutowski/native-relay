"""U1: the read only git wrappers against a real temp repo."""
import os
import tempfile
import unittest

import _paths
import _repo
from relay import gitread


class GitRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = _repo.make_repo(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def commit_file(self, rel, text, message="change"):
        path = os.path.join(self.repo, rel)
        with open(path, "w") as handle:
            handle.write(text)
        _repo.git(self.repo, "add", rel)
        _repo.git(self.repo, "commit", "-q", "-m", message)
        return gitread.rev_parse(self.repo, "HEAD")

    def test_git_error_carries_args_code_and_stderr(self):
        with self.assertRaises(gitread.GitError) as ctx:
            gitread.run(self.repo, ["rev-parse", "--verify", "nope^{commit}"])
        self.assertNotEqual(ctx.exception.returncode, 0)
        self.assertIn("rev-parse", ctx.exception.args_list)
        self.assertTrue(ctx.exception.stderr)
        proc = gitread.run(self.repo, ["rev-parse", "--verify", "nope^{commit}"], check=False)
        self.assertNotEqual(proc.returncode, 0)

    def test_repo_identity_matches_the_working_tree_for_a_regular_checkout(self):
        self.assertEqual(gitread.repo_identity(self.repo), os.path.realpath(self.repo))

    def test_a_worktree_hashes_the_same_identity_as_the_main_checkout(self):
        dest = os.path.join(self.tmp.name, "wt")
        sha = gitread.rev_parse(self.repo, "HEAD")
        _repo.git(self.repo, "worktree", "add", "--detach", dest, sha)
        self.assertEqual(gitread.repo_identity(dest), gitread.repo_identity(self.repo))
        self.assertEqual(gitread.common_dir(dest), gitread.common_dir(self.repo))

    def test_rev_parse_and_branch_reads(self):
        head = gitread.rev_parse(self.repo, "HEAD")
        self.assertEqual(len(head), 40)
        self.assertEqual(gitread.rev_parse(self.repo, "main"), head)
        self.assertIsNone(gitread.rev_parse(self.repo, "relay/T-9"))
        self.assertEqual(gitread.current_branch(self.repo), "main")
        self.assertFalse(gitread.branch_exists(self.repo, "relay/T-1"))
        _repo.git(self.repo, "branch", "relay/T-1")
        self.assertTrue(gitread.branch_exists(self.repo, "relay/T-1"))
        _repo.git(self.repo, "checkout", "-q", "--detach")
        self.assertEqual(gitread.current_branch(self.repo), "HEAD")

    def test_status_and_clean(self):
        self.assertTrue(gitread.is_clean(self.repo))
        with open(os.path.join(self.repo, "README.md"), "a") as handle:
            handle.write("dirty\n")
        self.assertFalse(gitread.is_clean(self.repo))
        self.assertIn("README.md", gitread.status_porcelain(self.repo))

    def test_show_diff_log_and_fetch(self):
        base = gitread.rev_parse(self.repo, "HEAD")
        head = self.commit_file("notes.md", "hello\n")
        self.assertEqual(gitread.show(self.repo, "HEAD", "notes.md"), "hello\n")
        self.assertIsNone(gitread.show(self.repo, "HEAD", "missing.md"))
        self.assertIsNone(gitread.show(self.repo, "no-such-ref", "notes.md"))
        self.assertEqual(gitread.diff_name_only(self.repo, base, head), ["notes.md"])
        self.assertEqual(len(gitread.log_oneline(self.repo, base, head)), 1)
        self.assertNotEqual(gitread.rev_parse(self.repo, "origin/main"), head)
        _repo.git(self.repo, "push", "-q", "origin", "main")
        gitread.fetch(self.repo)
        self.assertEqual(gitread.rev_parse(self.repo, "origin/main"), head)

    def test_remotes_default_branch_and_config(self):
        self.assertEqual(gitread.remotes(self.repo), ["origin"])
        self.assertEqual(gitread.default_branch(self.repo), "main")
        self.assertEqual(gitread.config_get(self.repo, "user.name"), "Relay Test")
        self.assertIsNone(gitread.config_get(self.repo, "relay.nothing"))
        bare = _repo.make_repo(self.tmp.name, name="lonely", origin=False)
        self.assertEqual(gitread.remotes(bare), [])
        self.assertIsNone(gitread.default_branch(bare))

    def test_remote_heads_answers_from_the_remote_and_matches_exact_names_only(self):
        bare = os.path.join(self.tmp.name, "repo.git")
        _repo.git(self.repo, "push", "-q", "origin", "main:refs/heads/relay/T-1")
        _repo.git(self.repo, "push", "-q", "origin", "main:refs/heads/old/relay/T-2")
        found, reason = gitread.remote_heads(self.repo, ["relay/T-1", "relay/T-2", "relay/T-3"])
        self.assertIsNone(reason)
        self.assertEqual(found, {"relay/T-1"})
        _repo.git(bare, "branch", "-D", "relay/T-1")
        found, _ = gitread.remote_heads(self.repo, ["relay/T-1"])
        self.assertEqual(found, set())

    def test_remote_heads_with_no_names_asks_nothing(self):
        self.assertEqual(gitread.remote_heads(self.repo, [], remote="unreachable"), (set(), None))

    def test_remote_heads_names_a_remote_it_could_not_read(self):
        _repo.git(self.repo, "remote", "set-url", "origin", os.path.join(self.tmp.name, "gone.git"))
        found, reason = gitread.remote_heads(self.repo, ["relay/T-1"])
        self.assertIsNone(found)
        self.assertTrue(reason)
        self.assertNotIn("\n", reason)

    def test_merge_head_exists(self):
        self.assertFalse(gitread.merge_head_exists(self.repo))
        _repo.git(self.repo, "checkout", "-q", "-b", "side")
        self.commit_file("a.txt", "side\n")
        _repo.git(self.repo, "checkout", "-q", "main")
        self.commit_file("a.txt", "main\n")
        proc = _repo.git(self.repo, "merge", "side", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(gitread.merge_head_exists(self.repo))
        _repo.git(self.repo, "merge", "--abort")
        self.assertFalse(gitread.merge_head_exists(self.repo))


if __name__ == "__main__":
    unittest.main()
