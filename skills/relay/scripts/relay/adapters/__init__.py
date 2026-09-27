"""Tracker adapters, read side only (U4, KTD16).

An adapter is the whole of what Relay knows about a tracker. Everything project specific and
tracker specific lives behind this interface, which is what keeps the runner project agnostic
(R1) and keeps `mcp__atlassian__` and `gh` out of the classifier and the closeout template.

No method here writes. That is the point of R19: the runner reads the tracker to decide whether
a task landed, and every write goes through a Task or Closeout process instead, so a defect in
the runner can never move a card. The shared test suite asserts that the public surface of every
adapter is exactly the nine methods below.

The interface, with the shapes each method returns:

    candidates()                  -> [{"id", "title", "description", "status"}, ...]
    ready(source)                 -> ([{"id", "title", "description", "labels"}, ...], reason)
    read(id)                      -> {"id", "title", "description", "status"}
    status(id)                    -> {"status", "terminal", "reference", "skipped"}
    comments_since(id, baseline)  -> [{"id", "body", "created"}, ...] newer than baseline, in order
    closing_reference(id, ref)    -> the comment id naming ref, else None
    write_tool_patterns()         -> {"tools": (...), "bash": (...), "paths": (...)}
    closeout_allowed_tools(backend=None) -> (tool name, ...) explicit, never a wildcard
    closeout_instructions(outcome, return_to=None, backend=None, baseline_unknown=False)
                                  -> the duty one text for the closeout brief

`ready` is the feeder's read (feeder plan, KTD3): the cards that can start now, by the tracker's
own account. `source` is the `[ready]` table of the feeder's sidecar file, so what ready means is
the project's data and never this package's code: labels for GitHub, a JQL query for Jira, and
nothing at all for markdown, where an unchecked box is the whole answer. It returns a reason
beside an empty list rather than raising, because "the tracker could not be read" and "nothing
is ready" send the feeder down different paths and an empty list alone cannot tell them apart.

`status` returning a `skipped` reason rather than raising is deliberate: a tracker that cannot be
read must never be mistaken for either a landing or a failure to land, so verify turns a skip
into a blocking unknown and the run halts with something an operator can act on. Every network
or subprocess call is bounded by NETWORK_TIMEOUT_SECONDS.

Every adapter takes an injectable transport, so tests run against recorded fixtures and never
touch a network or invoke `gh`: an opener for Jira, a `run` callable for `gh`, and the git read
wrapper for markdown.
"""

import re

from .. import contracts

NETWORK_TIMEOUT_SECONDS = 30

# A commit sha as a comment body abbreviates it. Seven characters is git's own short sha floor.
SHA_TOKEN_RE = re.compile(r"\b[0-9a-f]{7,40}\b")

INTERFACE = (
    "candidates",
    "ready",
    "read",
    "status",
    "comments_since",
    "closing_reference",
    "write_tool_patterns",
    "closeout_allowed_tools",
    "closeout_instructions",
)

OUTCOME_LANDED = "landed"
OUTCOME_BLOCKED = "blocked"
OUTCOME_HALTED = "halted"


class ConfigurationError(ValueError):
    """A manifest or environment problem the operator must fix before any run. Raised at
    construction, before a single request, so `relay validate` names the missing variable."""


def reference_hit(body, ref):
    """True when a body names a landing reference: the full string (a PR URL), or a commit sha
    the body abbreviated (`abc1234` in the body for a full sha on the record). Shared by all
    three adapters because it is a property of git and of URLs, not of any one tracker."""
    if not ref or not body:
        return False
    if ref in body:
        return True
    return any(ref.startswith(token) for token in SHA_TOKEN_RE.findall(body))


def skipped(reason):
    """The status shape for a read that could not be completed."""
    return {"status": None, "terminal": False, "reference": None, "skipped": str(reason)}


def unknown_baseline_move(in_review, thing):
    """The Closeout's move sentence for a blocked or halted card whose status the runner never
    read before the run (issue #51). "Keeps its current status" is false here whenever the Task's
    start step ran, and there is no known status to return the card to, so the Closeout is told
    both halves and asked to hand the move to the operator rather than guess one. The runner reads
    the card back afterwards and lists it as a check by hand if it still reads in review."""
    status = "`%s`" % in_review if in_review else "its in review status"
    return ("The runner could not read this %s's status before this run, so it has no status "
            "to return it to, and this run's task process may have moved it to %s at its first "
            "step with no process working on it now. Leave the %s where it is rather than guess a "
            "status, and say in your comment that it needs moving back to %s by hand"
            % (thing, status, thing, contracts.UNKNOWN_RETURN))


def status(adapter, task_id, cache=None):
    """`adapter.status(task_id)`, sharing GitHub's one full board read across a batch of calls
    when the caller passes a `cache` dict (issue #61). An open, non closed GitHub issue with
    `status_field` declared reads the project board inside `status()` itself, so the run end
    audit's per task loop, which calls this for every card whether or not it later checks a
    landed item's lag, made one board read per card before this. Every other adapter's `status`
    takes `task_id` alone, so a `TypeError` from the extra keyword falls back to the plain call
    rather than assuming every adapter shares the cache."""
    if cache is None:
        return adapter.status(task_id)
    try:
        return adapter.status(task_id, cache=cache)
    except TypeError:
        return adapter.status(task_id)


def board_lag(adapter, task_id, cache=None):
    """Returns (lag, reason) for a landed task's board item, or (None, None) for an adapter
    with nothing to check (issue #43).

    Only GitHub keeps two truths about one card, the issue's state and its project item's
    status, and `status` answers terminal from the first alone. The read lives behind a private
    method rather than a tenth interface method because it is GitHub's alone: Jira and markdown
    have one status per card, so there is nothing for them to disagree with.

    `cache`, when given, is a dict the adapter may keep its one full board read in and reuse
    across a batch of calls (issue #61): the run end audit checks every landed task's item in
    one pass, and without a shared cache that pass made one full board read per card rather than
    one for the whole audit. `None`, the default, is every existing single-card caller: a
    Closeout confirming the one card it just landed reads the board itself, same as before."""
    read = getattr(adapter, "_board_lag", None)
    if read is None:
        return None, None
    # A raise is a reason like any other failed read, so both callers report it rather than
    # turning a report into a halt.
    try:
        if cache is None:
            return read(task_id)
        return read(task_id, cache=cache)
    except Exception as exc:
        return None, "the board read raised: %s" % exc


def task_tracker_steps(manifest, branch, backend=None):
    """The three places the task brief tells the process to touch the tracker: the start step
    before any other work, the review step before the envelope, and the comment when it cannot
    finish. Resolved by adapter name rather than by building the adapter, so a brief renders
    without a tracker credential.

    The markdown adapter is the reason this exists (first live run, 2026-08-26). Its tracker is
    a file the closeout edits and the runner reads at the remote head, so a task told to "move
    the card to in review" had no card to move and blocked on that step with the code done and
    the gate green. Under markdown the task makes no tracker write at all.

    The start step exists so the board shows a task as in progress the moment its process
    launches, rather than only near the end of the session (relay task 50). The review step no
    longer moves the card itself, since the start step already did, so it carries only the comment
    duty now.
    """
    name = manifest.tracker.adapter
    if name == "jira" and manifest.execution.mode == "triple":
        owned = ("The triple coordinator owns Jira transitions and comments for this card. Do not "
                 "call Atlassian tools, transition the card, or add a Jira comment yourself.")
        return {"start_step": owned, "review_step": owned, "blocked_step": owned +
                " Print the envelope with `status: blocked` and the blockers listed."}
    in_review = manifest.tracker.in_review_status or "its in review status"
    if name == "markdown":
        path = manifest.tracker.file or "the tracker file"
        return {
            "start_step": ("There is no tracker write for you to make yet. This project's tracker "
                           "is `%s` in the repository, which only the runner's closeout process "
                           "edits; there is no in-progress mark for you to set." % path),
            "review_step": ("There is no tracker write for you to make. This project's tracker is `%s` "
                            "in the repository, which the runner's own closeout process edits after "
                            "you exit. Do not edit `%s` yourself; confirm the head of `%s` is "
                            "committed and go to the next step." % (path, path, branch)),
            "blocked_step": ("Do not edit `%s`; the runner records the blocker there after you exit. "
                             "Print the envelope with `status: blocked` and the blockers listed."
                             % path),
        }
    steps = {
        "start_step": ("Move the tracker card to `%s` now, before anything else in this session. "
                       "This is your first tracker write of the run." % in_review),
        "review_step": ("Comment the head commit of `%s` on the tracker card. This is the last "
                        "tracker write you make; the runner launches a separate process to close "
                        "the card once the merge exists." % branch),
        "blocked_step": ("Comment the blocker on the tracker card, then print the envelope with "
                         "`status: blocked` and the blockers listed."),
    }
    if name == "jira" and backend == "grok":
        how = (" Use the Atlassian MCP tools atlassian__getJiraIssue, "
               "atlassian__getTransitionsForJiraIssue, atlassian__transitionJiraIssue, and "
               "atlassian__addCommentToJiraIssue. Pass the tracker site as cloudId. Never call "
               "getAccessibleAtlassianResources. Do not use JIRA_API_TOKEN; it is not in this "
               "process.")
        return {key: value + how for key, value in steps.items()}
    return steps


def build(manifest, env=None, opener=None, run=None, read=None):
    """The adapter the manifest names. Imports are local so a machine without one tracker's
    dependencies can still use the others."""
    name = manifest.tracker.adapter
    if name == "jira":
        from .jira import JiraAdapter

        return JiraAdapter(manifest, opener=opener, env=env)
    if name == "github":
        from .github import GitHubAdapter

        return GitHubAdapter(manifest, run=run)
    if name == "markdown":
        from .markdown import MarkdownAdapter

        return MarkdownAdapter(manifest, read=read)
    raise ConfigurationError("unknown tracker.adapter %r; expected jira, github, or markdown" % name)
