"""Git worktrees for concurrent Task processes (dual dispatch).

A worktree is a second working folder of the same repository. Dispatch launches each Task
process with cwd set to its own worktree so two backends can build at once without sharing a
working tree. The primary checkout stays on the default branch and stays clean. After the
process exits the worktree is removed, which is what lets the merge tail check the Task branch
out in the primary: git refuses to check out a branch that another worktree still holds.
"""
import os

from . import gitread


class WorktreeError(RuntimeError):
    """Creating or removing a worktree failed."""


def path_for(store, task_id):
    """Where this Task's worktree lives, under the run's state directory."""
    return store.path("worktrees", task_id)


def add(repo, dest, sha, env=None):
    """Create a detached worktree at dest pointing at sha. dest's parent is created."""
    parent = os.path.dirname(dest)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    try:
        gitread.run(repo, ["worktree", "add", "--detach", dest, sha], env=env)
    except gitread.GitError as exc:
        raise WorktreeError("could not add worktree at %s: %s" % (dest, exc)) from exc
    return dest


def remove(repo, dest, env=None):
    """Remove a worktree. Missing dest is a prune, not an error: a crash mid remove
    should not strand the next merge."""
    if not dest:
        return
    if not os.path.exists(dest):
        gitread.run(repo, ["worktree", "prune"], check=False, env=env)
        return
    try:
        gitread.run(repo, ["worktree", "remove", "--force", dest], env=env)
    except gitread.GitError as exc:
        gitread.run(repo, ["worktree", "prune"], check=False, env=env)
        if os.path.exists(dest):
            raise WorktreeError("could not remove worktree at %s: %s" % (dest, exc)) from exc
