"""Mutating git calls and the runner's git tail (U8).

Everything in this module can move the target repo. That is why it is a separate module from
gitread: a reader of the run loop can see at a glance which calls change something. Every
mutating wrapper takes an `ops` recorder (the state store, or anything with the same
`record_git_op(task_id, op, phase, detail)` method) and writes one `intent` entry before the
call and one `result` entry after, so a crash between them is a named state rather than a
mystery (R55, the plan's System-Wide Impact note).

The tail is the part of the pipeline the task process does not own (KTD5). In local merge mode
the task process exits on the Task branch (the Manifest prefix plus the Task id, default
`relay/<task-id>`), and the runner then runs the project's gate on that branch head, merges to
the default branch, and pushes. A gate refusal strands the branch instead of diverging the
default branch, which is the whole reason the merge lives here. Under `shipping.push = false`
the tail ends at the merge and nothing here pushes; `default_in_sync` is the one place the two
settings' remote checks differ.

Nothing here writes to a tracker (R19), and nothing here decides whether a task landed. That
verdict is verify.py, from git and the tracker alone.
"""
import hashlib
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field

from . import contracts, gitread

TASK_BRANCH_PREFIX = contracts.DEFAULT_TASK_BRANCH_PREFIX
MERGE_MESSAGE = "Merge relay task {task_id} from {branch}"

# `gh pr checks` exit codes, observed on the gh CLI: 0 every check passed, 8 checks still
# pending, anything else a failure or an error. Pending is the only code that keeps polling.
GH_CHECKS_PENDING = 8
DEFAULT_CI_POLL_INTERVAL_SECONDS = 60
SIGKILL_GRACE_SECONDS = 15


def _kill_group(proc, grace_seconds):
    """SIGTERM the whole group, then SIGKILL what is left."""
    import os as _os
    import signal as _signal

    try:
        _os.killpg(_os.getpgid(proc.pid), _signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    try:
        proc.wait(timeout=grace_seconds)
        return True
    except subprocess.TimeoutExpired:
        pass
    try:
        _os.killpg(_os.getpgid(proc.pid), _signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    return True


def task_branch_for(task_id, prefix=None):
    """Prefix plus Task id. `prefix is None` uses TASK_BRANCH_PREFIX. An empty string is the
    Task id alone, not an omit."""
    if prefix is None:
        prefix = TASK_BRANCH_PREFIX
    return prefix + task_id


def _record(ops, task_id, op, phase, detail=None):
    if ops is not None:
        ops.record_git_op(task_id, op, phase, detail)


def push_timeout_for(gate_timeout_seconds=None):
    """The bound for a push. A repository may run its gate inside a pre-push hook, so a push can
    legitimately take as long as the gate plus the transfer. gitread's read timeout is far too
    short for that, and applying it here kills the runner's own push mid-hook."""
    if gate_timeout_seconds is None:
        gate_timeout_seconds = contracts.DEFAULT_GATE_TIMEOUT_MINUTES * 60
    return gate_timeout_seconds + contracts.PUSH_NETWORK_MARGIN_SECONDS


def _mutate(repo, op, args, ops=None, task_id=None, env=None, check=False, timeout=None):
    """Run one mutating git command between an intent entry and a result entry."""
    _record(ops, task_id, op, "intent", {"args": list(args)})
    if timeout is None:
        proc = gitread.run(repo, args, check=check, env=env)
    else:
        proc = gitread.run(repo, args, check=check, env=env, timeout=timeout)
    output = (proc.stdout or "") + (proc.stderr or "")
    _record(ops, task_id, op, "result", {"returncode": proc.returncode, "output": output[-2000:]})
    return proc


@dataclass
class PushResult:
    ok: bool
    returncode: int
    output: str


@dataclass(frozen=True)
class RemoteLease:
    """One remote ref and the public object id that fences updates to it."""
    ref: str
    token: str


@dataclass
class RemoteLeaseResult:
    """Result of a remote claim transaction.

    `reason` is stable, actionable vocabulary for the coordinator.  The unabridged server
    response remains in `output`, while callers can safely put the ref names and object ids in
    durable state because neither is a credential.
    """
    ok: bool
    reason: str | None = None
    output: str = ""
    returncode: int | None = None
    claim_key: str | None = None
    card_leases: tuple = ()
    integration_lease: RemoteLease | None = None
    observed: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerClone:
    """An independently cloned Task workspace owned by one triple coordinator.

    The values retained here are deliberately sufficient to reject a later cleanup that is
    aimed at the wrong directory, a linked worktree, or a clone whose branch/remotes were
    altered by a worker.  They contain no remote URL or credentials because workers are
    intentionally disconnected before an agent runs.
    """
    path: str
    root: str
    task_id: str
    branch: str
    baseline_sha: str
    canonical_git_dir: str
    git_dir: str


@dataclass(frozen=True)
class WorkerProcess:
    """The process identity captured synchronously by the launcher for cleanup fencing."""
    pid: int
    process_group_id: int


@dataclass
class WorkerCloneResult:
    ok: bool
    worker: WorkerClone | None = None
    reason: str | None = None
    output: str = ""


@dataclass
class WorkerCleanupResult:
    removed: bool
    retained_reason: str | None = None


@dataclass
class MergeResult:
    ok: bool
    returncode: int
    output: str
    sha: str | None = None
    conflict: bool = False


@dataclass
class GateResult:
    ok: bool
    returncode: int | None
    log_path: str
    output_tail: str = ""
    timed_out: bool = False


@dataclass
class PreflightResult:
    ok: bool
    failed: str | None
    evidence: dict = field(default_factory=dict)


@dataclass
class ScopeResult:
    ok: bool
    offending: list = field(default_factory=list)
    changed: list = field(default_factory=list)
    halt_class: str | None = None
    reset_to: str | None = None
    # Untracked offenders survive the reset, because a reset moves tracked files and nothing
    # else. Named separately so the halt can tell the operator what is still on disk.
    untracked: list = field(default_factory=list)


@dataclass
class TimeoutDisposition:
    action: str
    tree: str
    branch: str


@dataclass
class CiResult:
    state: str
    elapsed_seconds: float
    polls: int
    output: str = ""
    halt_class: str | None = None


@dataclass
class TailResult:
    ok: bool
    halt_class: str | None = None
    stage: str | None = None
    merge_sha: str | None = None
    gate: GateResult | None = None
    evidence: dict = field(default_factory=dict)


# Mutating wrappers.

def fetch(repo, remote="origin", ops=None, task_id=None, env=None, check=True):
    return _mutate(repo, "fetch", ["fetch", "--quiet", remote], ops, task_id, env, check=check)


def checkout(repo, ref, ops=None, task_id=None, env=None):
    return _mutate(repo, "checkout", ["checkout", "--quiet", ref], ops, task_id, env, check=True)


def merge_no_ff(repo, branch, task_id, ops=None, env=None):
    """Merge the task branch into whatever is checked out, with a message naming the task."""
    message = MERGE_MESSAGE.format(task_id=task_id, branch=branch)
    proc = _mutate(repo, "merge", ["merge", "--no-ff", "--no-edit", "-m", message, branch],
                   ops, task_id, env)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return MergeResult(True, 0, output, sha=gitread.rev_parse(repo, "HEAD"))
    return MergeResult(False, proc.returncode, output, conflict=gitread.merge_head_exists(repo))


def merge_abort(repo, ops=None, task_id=None, env=None):
    return _mutate(repo, "merge_abort", ["merge", "--abort"], ops, task_id, env)


def abort_dangling_merge(repo, ops=None, task_id=None, env=None):
    """R55: a reclaimed lease may find a merge half done. True when one was aborted."""
    if not gitread.merge_head_exists(repo):
        return False
    merge_abort(repo, ops=ops, task_id=task_id, env=env)
    return True


def push(repo, args, ops=None, task_id=None, env=None, timeout=None):
    proc = _mutate(repo, "push", ["push"] + list(args), ops, task_id, env,
                   timeout=push_timeout_for(timeout))
    output = (proc.stdout or "") + (proc.stderr or "")
    return PushResult(proc.returncode == 0, proc.returncode, output)


# Triple worker clones ------------------------------------------------------
#
# A linked worktree is not a worker isolation boundary: its .git file resolves into the
# canonical checkout's common Git directory.  Triple mode therefore uses a normal local clone
# with Git's explicit no-hardlink switch, then removes every remote before the backend starts.
# The coordinator, not a worker, later imports the stopped branch into its canonical checkout.


def _within(parent, child):
    """True only when child is strictly below parent after resolving filesystem links."""
    try:
        return os.path.commonpath((parent, child)) == parent and parent != child
    except ValueError:
        return False


def worker_clone_path(root, task_id):
    """A stable, path-safe name for one worker beneath its coordinator-owned root."""
    task_id = _identity_text(task_id, "task_id")
    return os.path.join(os.path.realpath(root), "worker-" + hashlib.sha256(
        task_id.encode("utf-8")).hexdigest()[:24])


def _worker_clone_safety(worker, require_branch=True):
    """Return ``None`` only when this still looks like the clone we created.

    This intentionally treats uncertainty as unsafe.  In particular, a worker that adds a
    remote or switches branches leaves its clone for inspection instead of making cleanup
    erase the most useful evidence of what it did.
    """
    if not isinstance(worker, WorkerClone):
        return "worker_identity_missing"
    if not _within(worker.root, worker.path):
        return "worker_path_outside_owned_root"
    if _within(worker.path, worker.root) or os.path.islink(worker.path):
        return "worker_path_identity_changed"
    if not os.path.isdir(worker.path):
        return "worker_path_missing"
    dot_git = os.path.join(worker.path, ".git")
    if not os.path.isdir(dot_git) or os.path.islink(dot_git):
        return "worker_git_dir_not_private"
    if os.path.realpath(dot_git) != worker.git_dir:
        return "worker_git_dir_identity_changed"
    actual_git_dir = gitread.git_dir(worker.path)
    if actual_git_dir != worker.git_dir:
        return "worker_git_dir_identity_changed"
    if actual_git_dir == worker.canonical_git_dir:
        return "worker_git_dir_shared_with_canonical"
    try:
        if gitread.remotes(worker.path):
            return "worker_remote_present"
        if require_branch and gitread.current_branch(worker.path) != worker.branch:
            return "worker_branch_changed"
    except (gitread.GitError, OSError, subprocess.SubprocessError):
        return "worker_git_state_unreadable"
    return None


def create_worker_clone(repo, worker_root, task_id, branch, baseline_sha, default_branch,
                        ops=None, env=None):
    """Create a disconnected, independent clone prepared on one task branch.

    ``worker_root`` belongs to the coordinator and must sit outside the canonical checkout.
    The branch is created locally from the frozen build baseline.  No remote survives this
    function, so even an agent with broad tool access can only alter its own clone until the
    coordinator imports its stopped branch deliberately.
    """
    canonical = os.path.realpath(repo)
    root = os.path.realpath(worker_root)
    if not os.path.isdir(canonical):
        return WorkerCloneResult(False, reason="canonical_repo_missing")
    if _within(canonical, root) or root == canonical:
        return WorkerCloneResult(False, reason="worker_root_inside_canonical")
    if not isinstance(branch, str) or not branch.strip() or "\n" in branch:
        return WorkerCloneResult(False, reason="worker_branch_invalid")
    check_branch = gitread.run(canonical, ["check-ref-format", "--branch", branch], check=False,
                              env=env)
    if check_branch.returncode != 0:
        return WorkerCloneResult(False, reason="worker_branch_invalid",
                                 output=(check_branch.stdout or "") + (check_branch.stderr or ""))
    baseline = gitread.rev_parse(canonical, baseline_sha)
    if baseline is None:
        return WorkerCloneResult(False, reason="worker_baseline_missing")
    canonical_git_dir = gitread.git_dir(canonical)
    if canonical_git_dir is None:
        return WorkerCloneResult(False, reason="canonical_git_dir_missing")
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
    except OSError as exc:
        return WorkerCloneResult(False, reason="worker_root_unavailable", output=str(exc))
    path = worker_clone_path(root, task_id)
    if os.path.lexists(path):
        return WorkerCloneResult(False, reason="worker_path_exists")

    clone_args = ["git", "clone", "--quiet", "--no-hardlinks", "--branch", default_branch,
                  canonical, path]
    _record(ops, task_id, "create_worker_clone", "intent",
            {"path": path, "branch": branch, "baseline": baseline})
    try:
        proc = subprocess.run(clone_args, capture_output=True, text=True, env=env,
                              timeout=gitread.GIT_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        _record(ops, task_id, "create_worker_clone", "result",
                {"returncode": None, "output": str(exc)})
        return WorkerCloneResult(False, reason="worker_clone_failed", output=str(exc))
    output = (proc.stdout or "") + (proc.stderr or "")
    _record(ops, task_id, "create_worker_clone", "result",
            {"returncode": proc.returncode, "output": output[-2000:]})
    if proc.returncode != 0:
        return WorkerCloneResult(False, reason="worker_clone_failed", output=output)

    try:
        prepared = _mutate(path, "prepare_worker_branch",
                            ["checkout", "--quiet", "-B", branch, baseline], ops=ops,
                            task_id=task_id, env=env)
        if prepared.returncode != 0:
            return WorkerCloneResult(False, reason="worker_branch_prepare_failed",
                                     output=(prepared.stdout or "") + (prepared.stderr or ""))
        for remote in gitread.remotes(path):
            removed = _mutate(path, "disconnect_worker_remote", ["remote", "remove", remote],
                              ops=ops, task_id=task_id, env=env)
            if removed.returncode != 0:
                return WorkerCloneResult(False, reason="worker_remote_remove_failed",
                                         output=(removed.stdout or "") + (removed.stderr or ""))
    except (gitread.GitError, OSError, subprocess.SubprocessError) as exc:
        return WorkerCloneResult(False, reason="worker_prepare_failed", output=str(exc))

    worker_git_dir = gitread.git_dir(path)
    worker = WorkerClone(path=os.path.realpath(path), root=root, task_id=task_id, branch=branch,
                         baseline_sha=baseline, canonical_git_dir=canonical_git_dir,
                         git_dir=worker_git_dir or "")
    reason = _worker_clone_safety(worker)
    if reason is not None:
        return WorkerCloneResult(False, worker=worker, reason=reason)
    return WorkerCloneResult(True, worker=worker)


def worker_process_stopped(process):
    """Return ``(True, None)`` only after the detached worker group is gone.

    The launcher starts a new session, making the child pid its process-group id.  If that
    identity does not still hold we retain the clone.  A surviving group after its leader exits
    is also retained; it may contain a subagent that still has the clone open.
    """
    if not isinstance(process, WorkerProcess):
        return False, "worker_process_identity_missing"
    if process.pid <= 0 or process.process_group_id <= 0 or process.pid != process.process_group_id:
        return False, "worker_process_identity_invalid"
    try:
        current_group = os.getpgid(process.pid)
    except ProcessLookupError:
        current_group = None
    except (PermissionError, OSError):
        return False, "worker_process_identity_unreadable"
    if current_group is not None:
        if current_group != process.process_group_id:
            return False, "worker_process_identity_changed"
        return False, "worker_process_running"
    try:
        os.killpg(process.process_group_id, 0)
    except ProcessLookupError:
        return True, None
    except (PermissionError, OSError):
        return False, "worker_process_group_unreadable"
    return False, "worker_process_group_running"


def cleanup_worker_clone(worker, process):
    """Remove only an identity-verified clone whose complete worker group has stopped."""
    stopped, reason = worker_process_stopped(process)
    if not stopped:
        return WorkerCleanupResult(False, reason)
    reason = _worker_clone_safety(worker)
    if reason is not None:
        return WorkerCleanupResult(False, reason)
    try:
        shutil.rmtree(worker.path)
    except OSError as exc:
        return WorkerCleanupResult(False, "worker_cleanup_failed: %s" % exc)
    return WorkerCleanupResult(True)


def import_worker_branch(repo, worker, process, ops=None, task_id=None, env=None):
    """Import one stopped, disconnected worker branch into the canonical checkout.

    A worker never receives the canonical checkout's remote, but it can still rewrite its own
    branch.  Import is therefore deliberately later than process completion and refuses every
    uncertain identity.  ``git fetch <local-path>`` copies objects without making the worker a
    persistent remote in the coordinator repository.
    """
    stopped, reason = worker_process_stopped(process)
    if not stopped:
        return WorkerCloneResult(False, worker=worker, reason=reason)
    reason = _worker_clone_safety(worker)
    if reason is not None:
        return WorkerCloneResult(False, worker=worker, reason=reason)
    if gitread.branch_exists(repo, worker.branch):
        return WorkerCloneResult(False, worker=worker, reason="canonical_branch_exists")
    head = gitread.rev_parse(worker.path, worker.branch)
    if not head:
        return WorkerCloneResult(False, worker=worker, reason="worker_branch_missing")
    if not gitread.is_ancestor(worker.path, worker.baseline_sha, worker.branch):
        return WorkerCloneResult(False, worker=worker, reason="worker_branch_not_from_baseline")
    proc = _mutate(repo, "import_worker_branch",
                   ["fetch", "--quiet", "--no-tags", worker.path,
                    "%s:refs/heads/%s" % (worker.branch, worker.branch)],
                   ops=ops, task_id=task_id, env=env)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return WorkerCloneResult(False, worker=worker, reason="worker_import_failed",
                                 output=output)
    if gitread.rev_parse(repo, worker.branch) != head:
        return WorkerCloneResult(False, worker=worker, reason="worker_import_identity_changed")
    return WorkerCloneResult(True, worker=worker)


def rebase_worker_branch(repo, branch, onto, ops=None, task_id=None, env=None):
    """Rebase an imported worker branch onto the current serialized landing base.

    All triple workers begin from one frozen build baseline.  Later slots must not merge that
    stale base back over an earlier landing, so the coordinator rebases only after the worker
    has stopped and its branch has been imported.  A conflict is aborted and left as a named
    refusal with both branches intact for an operator to inspect.
    """
    checkout(repo, branch, ops=ops, task_id=task_id, env=env)
    proc = _mutate(repo, "rebase_worker_branch", ["rebase", onto], ops=ops,
                   task_id=task_id, env=env)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return MergeResult(True, 0, output, sha=gitread.rev_parse(repo, branch))
    # An aborted rebase restores the imported branch, so no half-rebased index blocks the next
    # operator action.  The original worker clone is retained independently by the coordinator.
    _mutate(repo, "rebase_abort", ["rebase", "--abort"], ops=ops, task_id=task_id, env=env)
    return MergeResult(False, proc.returncode, output, conflict=True)


# Remote triple ownership ---------------------------------------------------
#
# The local StateStore lease tells a second process on this machine to wait.  It cannot tell a
# coordinator in another clone or on another machine to wait, and GitHub Projects has no
# compare-and-swap mutation for a card.  These helpers use the Git server's all-or-nothing ref
# update instead.  Do not add a clock based expiry here: an old laptop with a wrong clock must
# never decide that it may take over a still running coordinator.


def _identity_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty canonical node id" % label)
    return value.strip()


def canonical_claim_key(repository_node_id, project_node_id):
    """Return an opaque, deterministic namespace for a GitHub repo plus ProjectV2.

    Repository names, URLs, and project numbers can be renamed or reused.  GitHub node ids are
    immutable, so this hash is both safe in a Git ref component and stable across those display
    changes.  Length prefixes make the encoding unambiguous without relying on a delimiter that
    might occur in a node id.
    """
    repository_node_id = _identity_text(repository_node_id, "repository_node_id")
    project_node_id = _identity_text(project_node_id, "project_node_id")
    payload = "%d:%s%d:%s" % (len(repository_node_id), repository_node_id,
                               len(project_node_id), project_node_id)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ref_component(value, label):
    """A node id cannot be trusted as a ref component, so use its full digest."""
    value = _identity_text(value, label)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def card_claim_ref(claim_key, project_item_node_id):
    """The deterministic remote claim ref for one immutable ProjectV2 item."""
    if not isinstance(claim_key, str) or len(claim_key) != 64:
        raise ValueError("claim_key must be the canonical claim key")
    return "%s/%s/cards/%s" % (contracts.REMOTE_CLAIM_REF_PREFIX, claim_key,
                                 _ref_component(project_item_node_id, "project_item_node_id"))


def integration_lease_ref(claim_key):
    """The deterministic repository integration fence for one GitHub Project namespace."""
    if not isinstance(claim_key, str) or len(claim_key) != 64:
        raise ValueError("claim_key must be the canonical claim key")
    return "%s/%s" % (contracts.REMOTE_INTEGRATION_REF_PREFIX, claim_key)


def _remote_refs(repo, refs, remote="origin", env=None):
    """Read only the exact requested remote refs, returning {ref: object-id}.

    The caller still attaches force-with-lease expectations to the subsequent push because
    another coordinator can race this read; this read makes the normal collision explanation
    precise. The read itself is `gitread.remote_refs`, shared with the Task branch check.
    """
    return gitread.remote_refs(repo, refs, remote=remote, env=env)


def _token_commit(repo, purpose, env=None, nonce=None):
    """Create an unreachable, unique, non-secret commit to use as a fencing token.

    This changes only the local object database, not HEAD, the index, or any branch.  The random
    nonce prevents two otherwise identical `commit-tree` invocations in one second from sharing
    an object id.  It is explicitly an ownership label, not authentication material.
    """
    if not isinstance(purpose, str) or not purpose.strip() or "\n" in purpose:
        raise ValueError("token purpose must be one non-empty line")
    tree = gitread.run(repo, ["rev-parse", "HEAD^{tree}"], check=False, env=env)
    if tree.returncode != 0 or not (tree.stdout or "").strip():
        return None, (tree.stdout or "") + (tree.stderr or "")
    parent = gitread.run(repo, ["rev-parse", "HEAD"], check=False, env=env)
    if parent.returncode != 0 or not (parent.stdout or "").strip():
        return None, (parent.stdout or "") + (parent.stderr or "")
    # uuid4 is public randomness.  The remote ref's object id is the durable fencing value; no
    # credential is generated, persisted, or required to release a lease.
    nonce = nonce or uuid.uuid4().hex
    message = "relay triple lease\npurpose: %s\nnonce: %s\n" % (purpose.strip(), nonce)
    proc = gitread.run(repo, ["commit-tree", (tree.stdout or "").strip(), "-p",
                              (parent.stdout or "").strip(), "-m", message],
                       check=False, env=env)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return None, output
    token = (proc.stdout or "").strip()
    return token or None, output


def _remote_failure_reason(output):
    folded = output.lower()
    if "atomic push" in folded and ("not support" in folded or "not available" in folded):
        return "atomic_push_unsupported"
    if "does not support --atomic" in folded or "atomic pushes are not supported" in folded:
        return "atomic_push_unsupported"
    if "remote rejected" in folded or "hook declined" in folded or "deny updating" in folded:
        return "custom_ref_rejected"
    if "stale info" in folded or "force-with-lease" in folded or "failed to push some refs" in folded:
        return "lease_conflict"
    return "remote_push_failed"


def _new_remote_leases(repo, card_refs, integration_ref_name, env=None):
    leases = []
    for ref in card_refs:
        token, output = _token_commit(repo, "card claim %s" % ref, env=env)
        if token is None:
            return None, output
        leases.append(RemoteLease(ref, token))
    token, output = _token_commit(repo, "integration lease %s" % integration_ref_name, env=env)
    if token is None:
        return None, output
    return (tuple(leases), RemoteLease(integration_ref_name, token)), ""


def acquire_remote_leases(repo, repository_node_id, project_node_id, project_item_node_ids,
                          remote="origin", ops=None, task_id=None, env=None):
    """Atomically create every card claim and the integration lease, or create none.

    The exact absence read makes a pre-existing holder understandable.  The empty
    force-with-lease expectations make that same absence a server-enforced condition during the
    atomic push, which closes the race between the read and the write.
    """
    item_ids = tuple(project_item_node_ids)
    if len(item_ids) != 3:
        raise ValueError("triple acquisition requires exactly three ProjectV2 item node ids")
    if len(set(item_ids)) != len(item_ids):
        raise ValueError("triple acquisition requires distinct ProjectV2 item node ids")
    claim_key = canonical_claim_key(repository_node_id, project_node_id)
    card_refs = tuple(card_claim_ref(claim_key, item) for item in item_ids)
    integration_ref_name = integration_lease_ref(claim_key)
    refs = card_refs + (integration_ref_name,)
    observed, returncode, output = _remote_refs(repo, refs, remote=remote, env=env)
    if observed is None:
        return RemoteLeaseResult(False, "remote_read_failed", output, returncode,
                                 claim_key=claim_key)
    if observed:
        reason = "integration_lease_held" if integration_ref_name in observed else "card_claim_held"
        return RemoteLeaseResult(False, reason, output, returncode, claim_key=claim_key,
                                 observed=observed)
    made, output = _new_remote_leases(repo, card_refs, integration_ref_name, env=env)
    if made is None:
        return RemoteLeaseResult(False, "token_creation_failed", output, claim_key=claim_key)
    card_leases, integration = made
    # A single push transaction gets no partial success.  The `ref:` form means the ref must
    # still be absent; Git rejects the whole batch if another coordinator creates any one ref.
    args = ["push", "--atomic"]
    args.extend("--force-with-lease=%s:" % ref for ref in refs)
    args.append(remote)
    args.extend("%s:%s" % (lease.token, lease.ref) for lease in card_leases + (integration,))
    proc = _mutate(repo, "acquire_remote_leases", args, ops=ops, task_id=task_id, env=env,
                   timeout=push_timeout_for())
    pushed = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return RemoteLeaseResult(False, _remote_failure_reason(pushed), pushed, proc.returncode,
                                 claim_key=claim_key, observed=observed)
    return RemoteLeaseResult(True, output=pushed, returncode=proc.returncode, claim_key=claim_key,
                             card_leases=card_leases, integration_lease=integration,
                             observed=observed)


def renew_integration_lease_and_push(repo, default_branch, expected_default_oid,
                                     integration_lease, remote="origin", ops=None,
                                     task_id=None, env=None, timeout=None):
    """Atomically push a default-branch change and rotate its exact integration fence.

    A caller must provide the remote default object id it based the merge or Closeout on and the
    current integration token.  Both are checked before and during the push.  A missing or
    replaced token therefore blocks landing even if the local StateStore lease still looks live.
    """
    if not isinstance(integration_lease, RemoteLease):
        raise ValueError("integration_lease must be a RemoteLease")
    default_ref = "refs/heads/%s" % default_branch
    refs = (default_ref, integration_lease.ref)
    observed, returncode, output = _remote_refs(repo, refs, remote=remote, env=env)
    if observed is None:
        return RemoteLeaseResult(False, "remote_read_failed", output, returncode,
                                 integration_lease=integration_lease)
    if observed.get(default_ref) != expected_default_oid:
        return RemoteLeaseResult(False, "default_branch_advanced", output, returncode,
                                 integration_lease=integration_lease, observed=observed)
    if observed.get(integration_lease.ref) != integration_lease.token:
        return RemoteLeaseResult(False, "integration_lease_lost", output, returncode,
                                 integration_lease=integration_lease, observed=observed)
    next_token, output = _token_commit(repo, "integration renewal %s" % integration_lease.ref,
                                       env=env)
    if next_token is None:
        return RemoteLeaseResult(False, "token_creation_failed", output,
                                 integration_lease=integration_lease, observed=observed)
    next_lease = RemoteLease(integration_lease.ref, next_token)
    local_default = gitread.rev_parse(repo, default_branch)
    if local_default is None:
        return RemoteLeaseResult(False, "local_default_missing", integration_lease=integration_lease,
                                 observed=observed)
    args = ["push", "--atomic",
            "--force-with-lease=%s:%s" % (default_ref, expected_default_oid),
            "--force-with-lease=%s:%s" % (integration_lease.ref, integration_lease.token),
            remote,
            "%s:%s" % (local_default, default_ref),
            "%s:%s" % (next_lease.token, next_lease.ref)]
    proc = _mutate(repo, "renew_integration_lease_and_push", args, ops=ops, task_id=task_id,
                   env=env, timeout=timeout or push_timeout_for())
    pushed = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return RemoteLeaseResult(False, _remote_failure_reason(pushed), pushed, proc.returncode,
                                 integration_lease=integration_lease, observed=observed)
    return RemoteLeaseResult(True, output=pushed, returncode=proc.returncode,
                             integration_lease=next_lease, observed=observed)


def release_remote_leases(repo, leases, remote="origin", ops=None, task_id=None, env=None):
    """Delete only refs that still equal these exact public fencing tokens.

    This is used for normal release and for an explicit operator break after the operator has
    independently established that the old coordinator is dead.  It intentionally has no
    timestamp takeover path and cannot erase a successor token.
    """
    leases = tuple(leases)
    if not leases or any(not isinstance(lease, RemoteLease) for lease in leases):
        raise ValueError("leases must be one or more RemoteLease values")
    refs = tuple(lease.ref for lease in leases)
    if len(set(refs)) != len(refs):
        raise ValueError("leases must name distinct refs")
    observed, returncode, output = _remote_refs(repo, refs, remote=remote, env=env)
    if observed is None:
        return RemoteLeaseResult(False, "remote_read_failed", output, returncode, observed={})
    mismatched = {lease.ref: observed.get(lease.ref) for lease in leases
                  if observed.get(lease.ref) != lease.token}
    if mismatched:
        return RemoteLeaseResult(False, "lease_token_mismatch", output, returncode,
                                 observed=mismatched)
    args = ["push", "--atomic"]
    args.extend("--force-with-lease=%s:%s" % (lease.ref, lease.token) for lease in leases)
    args.append(remote)
    args.extend(":%s" % lease.ref for lease in leases)
    proc = _mutate(repo, "release_remote_leases", args, ops=ops, task_id=task_id, env=env,
                   timeout=push_timeout_for())
    pushed = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return RemoteLeaseResult(False, _remote_failure_reason(pushed), pushed, proc.returncode,
                                 observed=observed)
    return RemoteLeaseResult(True, output=pushed, returncode=proc.returncode, observed=observed)


def break_remote_leases(repo, leases, remote="origin", ops=None, task_id=None, env=None):
    """The explicit operator-break primitive; aliases exact-token release by design."""
    return release_remote_leases(repo, leases, remote=remote, ops=ops, task_id=task_id, env=env)


def mirror_push(repo, mirror, ops=None, task_id=None, env=None, timeout=None):
    """R6: the mirror rule is an argument list the manifest supplies, run after closeout."""
    proc = _mutate(repo, "mirror_push", ["push"] + list(mirror), ops, task_id, env,
                   timeout=push_timeout_for(timeout))
    output = (proc.stdout or "") + (proc.stderr or "")
    return PushResult(proc.returncode == 0, proc.returncode, output)


def mirror_target(mirror):
    """The remote and destination branch a mirror argument list pushes to, so verify can read
    the mirror ref back. `["origin", "main:release"]` and `["origin", "release"]` both name
    `origin` and `release`. None when the list is empty or names no destination."""
    if not mirror:
        return None
    positional = [arg for arg in mirror if not arg.startswith("-")]
    if len(positional) < 2:
        return None
    remote = positional[0]
    destination = positional[-1].split(":")[-1]
    if destination.startswith("refs/heads/"):
        destination = destination[len("refs/heads/"):]
    return remote, destination


def delete_branch(repo, name, ops=None, task_id=None, env=None):
    """Delete the local task branch. Only called after a full verify has passed, so the commits
    are already on the remote default branch and `-d` refusing would be a real signal."""
    return _mutate(repo, "delete_branch", ["branch", "-D", name], ops, task_id, env)


def reset_hard(repo, ref, ops=None, task_id=None, env=None):
    return _mutate(repo, "reset_hard", ["reset", "--hard", ref], ops, task_id, env, check=True)


def keep_and_free_hint(branch):
    """The third way out of a stranded Task branch, for every message that refuses on one.
    Pre-flight's `no_task_branch` check asks `gitread.branch_exists`, which reads `refs/heads`
    only, so a branch moved to a tag stops blocking its card while every commit stays reachable.
    The message carries that mechanism and not only the command, because the mechanism is what an
    operator cannot see from the refusal. The tag is annotated, and `-m` is given, so the command
    never opens an editor."""
    return ("There is a third way. Pre flight reads refs/heads only, so tagging the branch and "
            "then deleting it keeps every commit and frees the card: "
            "git tag -a stranded/%s %s -m stranded && git branch -D %s. Rebuilding from the "
            "card's brief is often cheaper than resuming a branch whose recorded findings have "
            "gone stale." % (branch, branch, branch))


# Pre-flight (R16).

PREFLIGHT_CHECKS = ("tree_clean", "on_default", "head_equals_remote", "remote_is_ancestor",
                    "no_task_branch")


def _tree_is_clean(repo, evidence):
    """The `tree_clean` check, shared by pre-flight and the resume disposition. Records the
    first ten status lines as evidence either way."""
    porcelain = gitread.status_porcelain(repo)
    evidence["tree"] = porcelain.strip().splitlines()[:10]
    return not porcelain.strip()


def head_equals_remote(repo, default_branch, evidence):
    """The `head_equals_remote` check, shared by pre-flight, the resume disposition, and
    `run._note_halt`'s R6a guard. Both shas go into the evidence so a refusal can name them, and
    `None`-safety matters: an unreadable ref must never read as "equal" to another unreadable
    one."""
    local = gitread.rev_parse(repo, default_branch)
    remote = gitread.rev_parse(repo, "origin/" + default_branch)
    evidence.update(local_sha=local, remote_sha=remote)
    return local is not None and remote is not None and local == remote


def remote_is_ancestor(repo, default_branch, evidence):
    """The `shipping.push = false` counterpart of `head_equals_remote`. Local runs ahead of the
    remote by design there, so ahead and equal pass, while behind and diverged fail: the check
    still catches a remote that moved under the run, and permits the state the run creates.

    A remote tracking ref that does not resolve passes, whether the repo has no origin at all or
    has never fetched this branch, because there is no known remote state to have diverged from
    and nothing will be pushed to it. An unreadable local ref fails, never passes."""
    local = gitread.rev_parse(repo, default_branch)
    remote = gitread.rev_parse(repo, "origin/" + default_branch)
    evidence.update(local_sha=local, remote_sha=remote)
    if local is None:
        return False
    if remote is None:
        evidence["reason"] = ("origin/%s does not resolve, so there is no remote state to have "
                              "diverged from" % default_branch)
        return True
    return gitread.is_ancestor(repo, remote, local)


def default_in_sync(repo, default_branch, pushes, evidence):
    """Whether the default branch agrees with the remote, as the shipping setting defines
    agreement, and the name of the check that answered. Pre flight, the resume disposition, and
    `run._note_halt`'s R6a guard all ask through here, so the three cannot disagree about which
    rule applies."""
    if pushes:
        return head_equals_remote(repo, default_branch, evidence), "head_equals_remote"
    return remote_is_ancestor(repo, default_branch, evidence), "remote_is_ancestor"


def preflight(repo, default_branch, task_branch, env=None, pushes=True):
    """R16: a task process starts from a clean tree on the default branch, in sync with the
    remote, with no pre-existing task branch. Returns the name of the first check that failed,
    which is what the summary prints and what the halted record carries. `pushes` picks the remote
    check through `default_in_sync`."""
    evidence = {}
    if not _tree_is_clean(repo, evidence):
        return PreflightResult(False, "tree_clean", evidence)
    branch = gitread.current_branch(repo)
    evidence["branch"] = branch
    if branch != default_branch:
        return PreflightResult(False, "on_default", evidence)
    in_sync, check = default_in_sync(repo, default_branch, pushes, evidence)
    if not in_sync:
        return PreflightResult(False, check, evidence)
    if gitread.branch_exists(repo, task_branch):
        evidence["task_branch"] = task_branch
        return PreflightResult(False, "no_task_branch", evidence)
    return PreflightResult(True, None, evidence)


# The gate and its backstop.

def claude_dir_backstop(repo, baseline_sha, branch):
    """R41's second half: after the task process exits, a branch diff touching `.claude/` is
    refused before the gate. The pre-flight scan catches the intent in the brief; this catches
    what actually landed on the branch."""
    paths = gitread.diff_name_only(repo, baseline_sha, branch)
    return [path for path in paths if contracts.CLAUDE_DIR_PATH_REGEX.search("/" + path)]


def run_gate(repo, command, log_path, timeout_seconds=contracts.DEFAULT_GATE_TIMEOUT_MINUTES * 60,
             env=None):
    """R24: run the manifest's gate argument list in the repo and capture it to the log the
    summary points at. Never a shell string (R9); a nonzero exit strands the branch."""
    os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
    try:
        proc = subprocess.Popen(list(command), cwd=repo, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, env=env,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        text = "gate command could not run: %s" % exc
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return GateResult(False, None, log_path, text[-2000:])
    try:
        captured = proc.communicate(timeout=timeout_seconds)[0]
    except subprocess.TimeoutExpired:
        # The gate builds, so it spawns compilers and test runners. Killing only the process
        # named in the manifest would leave those running into the next task.
        _kill_group(proc, SIGKILL_GRACE_SECONDS)
        captured = ""
        try:
            captured = proc.communicate(timeout=5)[0] or ""
        except (subprocess.TimeoutExpired, ValueError):
            pass
        text = "gate timed out after %d seconds\n%s" % (timeout_seconds, captured)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return GateResult(False, None, log_path, text[-2000:], timed_out=True)
    except OSError as exc:
        text = "gate command could not run: %s" % exc
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return GateResult(False, None, log_path, text[-2000:])
    text = captured or ""
    with open(log_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return GateResult(proc.returncode == 0, proc.returncode, log_path, text[-2000:])


# The tail.

def _unpushed_base_refusal(repo, default_branch, baseline_sha, branch, gate, ops, task_id, env):
    """The tail's remote check under `shipping.push = false` (KTD3 of the no push plan), or None
    when the merge may go ahead.

    Two movers, both `remote_advanced` because both are the landing base moving under the task.
    The local default branch is the landing target here, so it has to sit where the Task started;
    a concurrent session committing to it is the first mover. The remote is the second: fetched
    when an origin exists, and a fetch that fails or hangs is ignored, because an offline machine
    is a legitimate place for a run that pushes nothing. The ancestor check then reads whatever
    `origin/<default>` the repo knows. `reason` says which mover it was, since the class's Cause
    line names both."""
    local_sha = gitread.rev_parse(repo, default_branch)
    if local_sha != baseline_sha:
        return TailResult(False, contracts.HALT_REMOTE_ADVANCED, "baseline", gate=gate,
                          evidence={"sha": local_sha, "local_sha": local_sha,
                                    "baseline_sha": baseline_sha, "branch": branch,
                                    "reason": "the local %s moved from the baseline during the "
                                              "task" % default_branch})
    if "origin" in gitread.remotes(repo):
        try:
            fetch(repo, ops=ops, task_id=task_id, env=env, check=False)
        except subprocess.TimeoutExpired:
            pass
    evidence = {}
    if not remote_is_ancestor(repo, default_branch, evidence):
        remote_sha = evidence.get("remote_sha")
        return TailResult(False, contracts.HALT_REMOTE_ADVANCED, "fetch", gate=gate,
                          evidence={"sha": remote_sha, "remote_sha": remote_sha,
                                    "baseline_sha": baseline_sha, "branch": branch,
                                    "reason": "origin/%s has diverged from the local %s"
                                              % (default_branch, default_branch)})
    return None


def local_merge_tail(repo, task_id, default_branch, baseline_sha, gate_command, gate_log_path,
                     ops=None, env=None, gate_timeout_seconds=None, still_ours=None,
                     branch=None, pushes=True, expected_default=None):
    """The fixed local merge sequence of R50, from the task process's exit to a pushed default
    branch. Stops at the first refusal and names the halt class; every stop leaves the task
    branch in place so the operator can repair by hand and resume.

    `pushes` false ends the sequence at the merge, with stage `merged` and `pushed` false in the
    evidence, and swaps the fetch and remote compare for `_unpushed_base_refusal`.

    `expected_default` is the SHA the coordinator believes the default branch sits at after its
    own earlier landings in this dispatch. When set, the remote_advanced check compares against
    that SHA rather than the Task's launch baseline, so a sibling landing is not a foreign
    mover. Serial `run` leaves it unset and behaviour is unchanged.
    """
    if branch is None:
        branch = task_branch_for(task_id, None)
    if gate_timeout_seconds is None:
        gate_timeout_seconds = contracts.DEFAULT_GATE_TIMEOUT_MINUTES * 60

    # A process can report success and leave nothing behind. The runner decides from git, so a
    # missing branch is a named refusal here rather than an exception out of the checkout.
    if not gitread.branch_exists(repo, branch):
        return TailResult(False, contracts.HALT_UNCLEAN_EXIT, "branch",
                          evidence={"branch": branch,
                                    "reason": "the task branch does not exist; nothing to merge"})

    checkout(repo, branch, ops=ops, task_id=task_id, env=env)

    hits = claude_dir_backstop(repo, baseline_sha, branch)
    if hits:
        # Its own sentence, not classify's (issue #8). The Task finished and the Runner is the
        # one refusing, so this Cause line names the merge repair; `stage` is what run.py
        # persists so the record says which raiser fired.
        return TailResult(False, contracts.HALT_PATH_GATE, contracts.TAIL_STAGE_BACKSTOP,
                          evidence={"paths": hits, "branch": branch,
                                    "detail": contracts.PATH_GATE_CLAUDE_DIR_BACKSTOP.format(
                                        branch=branch, paths=", ".join(hits))})

    gate = run_gate(repo, gate_command, gate_log_path, gate_timeout_seconds, env=env)
    if not gate.ok:
        return TailResult(False, contracts.HALT_GATE_REFUSED, "gate", gate=gate,
                          evidence={"branch": branch, "sha": gitread.rev_parse(repo, branch),
                                    "log": gate.log_path, "returncode": gate.returncode})
    if not gitread.is_clean(repo):
        return TailResult(False, contracts.HALT_UNCLEAN_EXIT, "gate", gate=gate,
                          evidence={"branch": branch,
                                    "tree": gitread.status_porcelain(repo).strip().splitlines()[:10]})

    if still_ours is not None and not still_ours():
        return TailResult(False, contracts.HALT_RUNNER_CRASHED, "lease",
                          evidence={"branch": branch, "status_before": contracts.STATUS_MERGING,
                                    "reason": "the lease was lost while the gate ran"})

    compare_sha = expected_default if expected_default is not None else baseline_sha

    if pushes:
        fetch(repo, ops=ops, task_id=task_id, env=env)
        remote_sha = gitread.rev_parse(repo, "origin/" + default_branch)
        if remote_sha != compare_sha:
            return TailResult(False, contracts.HALT_REMOTE_ADVANCED, "fetch", gate=gate,
                              evidence={"remote_sha": remote_sha, "baseline_sha": baseline_sha,
                                        "expected_default": compare_sha,
                                        "sha": remote_sha, "branch": branch})
    else:
        refused = _unpushed_base_refusal(repo, default_branch, compare_sha, branch, gate, ops,
                                         task_id, env)
        if refused is not None:
            return refused
        remote_sha = gitread.rev_parse(repo, "origin/" + default_branch)

    checkout(repo, default_branch, ops=ops, task_id=task_id, env=env)
    merge = merge_no_ff(repo, branch, task_id, ops=ops, env=env)
    if not merge.ok:
        if merge.conflict:
            merge_abort(repo, ops=ops, task_id=task_id, env=env)
        return TailResult(False, contracts.HALT_REMOTE_ADVANCED, "merge", gate=gate,
                          evidence={"sha": gitread.rev_parse(repo, default_branch),
                                    "conflict": merge.conflict, "merge_output": merge.output,
                                    "branch": branch, "baseline_sha": baseline_sha,
                                    "remote_sha": remote_sha})

    if not pushes:
        # R2: the landing is the local default branch, and the sequence ends here.
        return TailResult(True, None, "merged", merge_sha=merge.sha, gate=gate,
                          evidence={"branch": default_branch, "sha": merge.sha, "pushed": False})

    if still_ours is not None and not still_ours():
        return TailResult(False, contracts.HALT_RUNNER_CRASHED, "lease", merge_sha=merge.sha,
                          evidence={"branch": default_branch,
                                    "status_before": contracts.STATUS_MERGING,
                                    "reason": "the lease was lost before the push"})

    pushed = push(repo, ["origin", default_branch], ops=ops, task_id=task_id, env=env,
                  timeout=gate_timeout_seconds)
    if not pushed.ok:
        return TailResult(False, contracts.HALT_GATE_REFUSED, "push", merge_sha=merge.sha, gate=gate,
                          evidence={"branch": default_branch, "sha": merge.sha,
                                    "log": gate.log_path, "push_output": pushed.output})
    return TailResult(True, None, "pushed", merge_sha=merge.sha, gate=gate,
                      evidence={"branch": default_branch, "sha": merge.sha})


def blocked_path(repo, default_branch, branch, ops=None, task_id=None, env=None):
    """R50's blocked route: return to the default branch and leave the task branch stranded,
    recording its name and head so the summary can point the operator at it."""
    head = gitread.rev_parse(repo, branch) if gitread.branch_exists(repo, branch) else None
    if gitread.current_branch(repo) != default_branch:
        checkout(repo, default_branch, ops=ops, task_id=task_id, env=env)
    return {"branch": branch if head else None, "head": head}


def timeout_disposition(repo, default_branch, branch, tree=None, current=None):
    """R35 and R50: after a timeout kill, a clean tree on the task branch or the default branch
    takes the blocked path and the run continues; a dirty tree halts.

    Dispatch snapshots the worktree before removing it and passes `tree` and `current` so the
    primary checkout, which stayed clean, does not hide a dirty build.
    """
    if tree is None:
        tree = "clean" if gitread.is_clean(repo) else "dirty"
    if current is None:
        current = gitread.current_branch(repo)
    if tree == "clean" and current in (branch, default_branch):
        return TimeoutDisposition("blocked", tree, current)
    return TimeoutDisposition("halt", tree, current)


def resume_disposition(repo, default_branch, ops=None, task_id=None, env=None, pushes=True):
    """Issue #15: after a halt the manifest may continue past, could the next task start from
    here? The same three reads pre-flight makes, in the order that keeps evidence intact: a
    dirty tree refuses before any checkout, so whatever the halted task left is exactly where
    it left it; a clean tree on the task branch is returned to the default, as `blocked_path`
    does for a blocked task; then the default has to sit at the remote's head, which is the
    check that tells a failed gate (default untouched) from a failed push (default ahead).

    Never resets, stashes, or deletes. A repo that needs that is the operator's to repair, and
    the refusal names the check so the record can say why the run stopped. The task branch is
    left in place either way; pre-flight checks only the next task's own branch name."""
    evidence = {}
    if not _tree_is_clean(repo, evidence):
        return PreflightResult(False, "tree_clean", evidence)
    branch = gitread.current_branch(repo)
    evidence["branch"] = branch
    if branch != default_branch:
        checkout(repo, default_branch, ops=ops, task_id=task_id, env=env)
        evidence["checked_out_from"] = branch
    in_sync, check = default_in_sync(repo, default_branch, pushes, evidence)
    if not in_sync:
        return PreflightResult(False, check, evidence)
    return PreflightResult(True, None, evidence)


def path_allowed(path, allowed_paths):
    """An entry ending in `/` is a directory prefix; anything else is an exact file path."""
    for entry in allowed_paths:
        if entry.endswith("/"):
            if path == entry.rstrip("/") or path.startswith(entry):
                return True
        elif path == entry:
            return True
    return False


def task_scope_offenders(repo, baseline_sha, branch, allowed_paths):
    """Paths any Task commit touched that fall outside the Task path bound.

    Returns the list and mutates nothing. Do not call closeout_scope_check for this:
    its failure path calls reset_hard, which would destroy the Task branch.
    """
    paths = gitread.paths_touched_in_range(repo, baseline_sha, branch)
    return [path for path in paths if not path_allowed(path, allowed_paths)]


def closeout_scope_check(repo, pre_closeout_head, allowed_paths, ops=None, task_id=None, env=None):
    """R53 and KTD15: the closeout commits docs and never pushes, so the runner checks what it
    produced before its own push. A path outside the allowed set resets the branch to the
    pre-closeout head, which is why this runs before the push and not after it.

    The working tree counts, not only the commits. A closeout that edited a file and left it
    uncommitted, or dropped an untracked file, changed the repository just as much as one that
    committed; reading the commit diff alone let both through, and the next task's pre flight
    then refused on a tree this check had already called clean.
    """
    committed = gitread.diff_name_only(repo, pre_closeout_head, "HEAD")
    working, untracked = gitread.status_paths(repo)
    changed = committed + [path for path in working if path not in committed]
    offending = [path for path in changed if not path_allowed(path, allowed_paths)]
    if offending:
        reset_hard(repo, pre_closeout_head, ops=ops, task_id=task_id, env=env)
        return ScopeResult(False, offending, changed, contracts.HALT_CLOSEOUT_OUT_OF_SCOPE,
                           reset_to=pre_closeout_head,
                           untracked=[path for path in untracked if path in offending])
    if working:
        # In scope but uncommitted, which is a different failure and gets the class that names
        # it. Nothing here would have been pushed, and leaving it in place refuses the next
        # task at pre flight, so the tree goes back to where the closeout found it.
        reset_hard(repo, pre_closeout_head, ops=ops, task_id=task_id, env=env)
        return ScopeResult(False, [], changed, contracts.HALT_UNCLEAN_EXIT,
                           reset_to=pre_closeout_head, untracked=untracked)
    return ScopeResult(True, [], changed)


# PR terminal mode (R12). Both helpers take the adapter's injectable run callable, so no test
# needs `gh` installed.

def find_pr(run, branch, timeout=30):
    """The open pull request for the task branch, or None."""
    import json

    proc = run(["gh", "pr", "list", "--head", branch, "--json", "url,number"], timeout=timeout)
    if proc.returncode != 0:
        return None
    try:
        entries = json.loads(proc.stdout or "[]")
    except ValueError:
        return None
    return entries[0] if entries else None


def poll_ci(run, branch, bound_seconds, interval_seconds=DEFAULT_CI_POLL_INTERVAL_SECONDS,
            sleep=time.sleep, monotonic=time.monotonic, timeout=30):
    """R12: poll `gh pr checks` until it decides or the bound expires. The clock is monotonic,
    which excludes host sleep (KTD10), and both the clock and the runner are injectable so the
    undecided case is a fast test rather than a real wait."""
    started = monotonic()
    deadline = started + bound_seconds
    polls = 0
    output = ""
    while True:
        proc = run(["gh", "pr", "checks", branch], timeout=timeout)
        polls += 1
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            return CiResult("pass", monotonic() - started, polls, output)
        if proc.returncode != GH_CHECKS_PENDING:
            return CiResult("fail", monotonic() - started, polls, output)
        if monotonic() >= deadline:
            return CiResult("undecided", monotonic() - started, polls, output,
                            contracts.HALT_CI_UNDECIDED)
        sleep(interval_seconds)
