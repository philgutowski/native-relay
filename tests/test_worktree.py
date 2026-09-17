"""Git worktree helpers used by dispatch."""
import os
import tempfile
import unittest

import _paths
import _repo
from relay import gitread, state, worktree


class Worktree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = _repo.make_repo(self.tmp.name)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)
        self.store = state.StateStore(os.path.join(self.tmp.name, "m.toml"), self.repo,
                                      home=self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_creates_a_detached_checkout_at_the_sha_without_moving_the_primary(self):
        sha = gitread.rev_parse(self.repo, "main")
        dest = worktree.path_for(self.store, "T-1")
        worktree.add(self.repo, dest, sha)
        self.assertTrue(os.path.isdir(dest))
        self.assertEqual(gitread.rev_parse(dest, "HEAD"), sha)
        self.assertEqual(gitread.current_branch(dest), "HEAD")
        self.assertEqual(gitread.current_branch(self.repo), "main")
        self.assertTrue(gitread.is_clean(self.repo))

    def test_two_worktrees_can_exist_at_the_same_sha(self):
        sha = gitread.rev_parse(self.repo, "main")
        a = worktree.path_for(self.store, "T-1")
        b = worktree.path_for(self.store, "T-2")
        worktree.add(self.repo, a, sha)
        worktree.add(self.repo, b, sha)
        self.assertEqual(gitread.rev_parse(a, "HEAD"), sha)
        self.assertEqual(gitread.rev_parse(b, "HEAD"), sha)

    def test_remove_deletes_the_directory_and_prune_is_idempotent(self):
        sha = gitread.rev_parse(self.repo, "main")
        dest = worktree.path_for(self.store, "T-1")
        worktree.add(self.repo, dest, sha)
        worktree.remove(self.repo, dest)
        self.assertFalse(os.path.exists(dest))
        worktree.remove(self.repo, dest)

    def test_a_branch_created_in_the_worktree_survives_remove(self):
        sha = gitread.rev_parse(self.repo, "main")
        dest = worktree.path_for(self.store, "T-1")
        worktree.add(self.repo, dest, sha)
        _repo.git(dest, "checkout", "-q", "-b", "relay/T-1", "main")
        with open(os.path.join(dest, "src_t1.py"), "w") as handle:
            handle.write("x = 1\n")
        _repo.git(dest, "add", "-A")
        _repo.git(dest, "commit", "-q", "-m", "T-1 work")
        worktree.remove(self.repo, dest)
        self.assertTrue(gitread.branch_exists(self.repo, "relay/T-1"))
        self.assertEqual(gitread.current_branch(self.repo), "main")
