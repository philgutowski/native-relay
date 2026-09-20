"""GitHub Projects adapter, read side (U4).

Reads go through `gh`, which already holds the operator's authentication, so Relay carries no
secret of its own here. The `run` callable is injectable for the same reason as Jira's opener:
tests use recorded fixtures, and `gh` may be absent from the machine running them.

Terminal means one of two things, per the plan: the issue is CLOSED, or the item's status in the
project board equals the status field the manifest names. The second is what lets a project whose
"Done" column matters more than the issue state still land a task.
"""
import json
import hashlib
import subprocess

from . import NETWORK_TIMEOUT_SECONDS, OUTCOME_HALTED, OUTCOME_LANDED, reference_hit, skipped

# gh project item-list stops at 30 items unless told otherwise; a board past 30 made every
# later card read as absent from the board (2026-08-29).
PROJECT_ITEM_LIMIT = 500

# `gh project item-list` itself has a 500 item ceiling.  That is fine for the serial
# candidate browser, but it is not a safe authority for a triple allocation: a declared card
# at position 501 would otherwise appear to have disappeared.  The triple reader below starts
# from each declared issue and pages that issue's ProjectV2 memberships instead.
PROJECT_LIST_LIMIT = 1000
PROJECT_ITEM_PAGE_SIZE = 100
PROJECT_ITEM_PAGE_LIMIT = 1000
COMMENT_PAGE_SIZE = 100
COMMENT_DIGEST_LIMIT = 200

ISSUE_FIELDS = "id,title,body,state,comments"
# The ready read lists issues rather than board items: labels live on the issue, and `gh issue
# list` has no 500 item ceiling to fall off the end of.
READY_FIELDS = "number,title,labels,body"
READY_ISSUE_LIMIT = 1000
CLOSED_STATE = "CLOSED"

WRITE_BASH_PREFIXES = ("gh issue", "gh project item-edit")
CLOSEOUT_TOOLS = ("Bash",)


def _canonical(value):
    """A stable digest for persisted board evidence, independent of dict insertion order."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True).encode("utf-8")).hexdigest()


def _collision(reason, expected=None, observed=None):
    """One deliberately small, serialisable collision shape for the batch record.

    A coordinator stores this verbatim and never tries to repair it.  `expected` and
    `observed` are digests rather than card bodies so a status screen can prove divergence
    without repeating tracker content into logs.
    """
    result = {"kind": "card_collision", "reason": str(reason)}
    if expected is not None:
        result["expected_digest"] = _canonical(expected)
    if observed is not None:
        result["observed_digest"] = _canonical(observed)
    return result


def make_run(cwd):
    """The default transport: `gh` in the target repo, bounded, output captured."""

    def run(args, timeout=NETWORK_TIMEOUT_SECONDS):
        return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)

    return run


class GitHubAdapter:
    def __init__(self, manifest, run=None):
        self._owner = manifest.tracker.owner
        self._project_number = manifest.tracker.project_number
        self._status_field = manifest.tracker.status_field
        self._run = run or make_run(manifest.project.repo)

    # Transport.
    def _gh(self, args):
        """Returns (payload, None) or (None, reason). A nonzero exit is a reason, not a crash."""
        try:
            proc = self._run(list(args), timeout=NETWORK_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            return None, "gh could not run: %s" % exc
        if proc.returncode != 0:
            return None, "gh exited %d: %s" % (proc.returncode, (proc.stderr or "").strip())
        try:
            return json.loads(proc.stdout or "null"), None
        except ValueError as exc:
            return None, "gh returned output that is not JSON: %s" % exc

    def _items(self):
        payload, reason = self._gh([
            "gh", "project", "item-list", str(self._project_number),
            "--owner", str(self._owner), "--format", "json",
            # gh returns the first 30 items when no limit is given. The board crossed 30 on
            # 2026-08-29 and every later card read as absent, so a landed task whose card
            # had moved was still classified partial_landing.
            "--limit", str(PROJECT_ITEM_LIMIT),
        ])
        if payload is None:
            return [], reason
        return payload.get("items") or [], None

    def _issue(self, task_id):
        return self._gh(["gh", "issue", "view", str(task_id), "--json", ISSUE_FIELDS])

    def _project_status(self, task_id):
        """Returns (status, reason). The reason is what separates a board this adapter could not
        read from a board that genuinely does not carry the item: both used to come back as
        None, and None reads as `not terminal`, so an unreadable board looked exactly like a card
        that had not moved."""
        items, reason = self._items()
        if reason:
            return None, reason
        for item in items:
            content = item.get("content") or {}
            if str(content.get("number")) == str(task_id):
                return item.get("status"), None
        return None, None

    def _comments(self, task_id):
        payload, reason = self._issue(task_id)
        if payload is None:
            return [], reason
        return [{"id": str(entry.get("id")), "body": entry.get("body") or "",
                 "created": entry.get("createdAt")} for entry in payload.get("comments") or []], None

    # Triple snapshot transport.  These remain private methods so the nine-method generic
    # adapter interface stays usable by Jira and markdown.  The module functions after this
    # class are the opt-in GitHub-only coordinator seam.
    def _graphql(self, query, variables):
        args = ["gh", "api", "graphql", "-f", "query=" + query]
        for key in sorted(variables):
            value = variables[key]
            if value is not None:
                args.extend(["-F", "%s=%s" % (key, value)])
        payload, reason = self._gh(args)
        if payload is None:
            return None, reason
        if not isinstance(payload, dict):
            return None, "GitHub GraphQL returned a non-object payload"
        errors = payload.get("errors") or []
        if errors:
            messages = "; ".join(str(entry.get("message") or entry) for entry in errors)
            return None, "GitHub GraphQL returned errors: %s" % messages
        data = payload.get("data")
        if not isinstance(data, dict):
            return None, "GitHub GraphQL returned no data"
        return data, None

    def _repository_identity(self):
        payload, reason = self._gh(["gh", "repo", "view", "--json", "id,nameWithOwner"])
        if payload is None:
            return None, reason
        repository_id = str(payload.get("id") or "").strip()
        name = str(payload.get("nameWithOwner") or "").strip()
        if not repository_id or "/" not in name:
            return None, "GitHub repository identity is incomplete"
        owner, repository = name.split("/", 1)
        if not owner or not repository:
            return None, "GitHub repository identity is incomplete"
        return {"id": repository_id, "name": name, "owner": owner, "repository": repository}, None

    def _project_identity(self):
        payload, reason = self._gh([
            "gh", "project", "list", str(self._owner), "--format", "json",
            "--limit", str(PROJECT_LIST_LIMIT),
        ])
        if payload is None:
            return None, reason
        # gh has emitted a bare list and an object with `projects` across releases.  Accept both
        # rather than making a CLI presentation change silently turn into a false collision.
        projects = payload.get("projects") if isinstance(payload, dict) else payload
        if not isinstance(projects, list):
            return None, "GitHub project list is not a list"
        for project in projects:
            if str(project.get("number")) == str(self._project_number):
                project_id = str(project.get("id") or "").strip()
                if not project_id:
                    return None, "GitHub ProjectV2 has no node id"
                return {"id": project_id, "number": str(self._project_number)}, None
        return None, "GitHub ProjectV2 #%s was not found for owner %s" % (
            self._project_number, self._owner)

    def _project_item_for_issue(self, repository, project_id, task_id):
        """Read one declared issue's membership, rather than truncating the whole board."""
        query = """
query($owner: String!, $repository: String!, $number: Int!, $cursor: String, $statusField: String!) {
  repository(owner: $owner, name: $repository) {
    issue(number: $number) {
      projectItems(first: 100, after: $cursor) {
        nodes {
          id
          project { id number }
          fieldValueByName(name: $statusField) {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
        cursor = None
        seen = 0
        while True:
            data, reason = self._graphql(query, {
                "owner": repository["owner"], "repository": repository["repository"],
                "number": task_id, "cursor": cursor, "statusField": self._status_field,
            })
            if data is None:
                return None, reason
            issue = ((data.get("repository") or {}).get("issue"))
            items = (issue or {}).get("projectItems")
            if not isinstance(items, dict):
                return None, "GitHub issue #%s has no readable project memberships" % task_id
            for item in items.get("nodes") or []:
                seen += 1
                project = item.get("project") or {}
                if str(project.get("id") or "") == project_id:
                    status = (item.get("fieldValueByName") or {}).get("name")
                    item_id = str(item.get("id") or "").strip()
                    if not item_id:
                        return None, "GitHub project item for #%s has no node id" % task_id
                    return {"id": item_id, "status": status}, None
                if seen >= PROJECT_ITEM_PAGE_LIMIT:
                    return None, "GitHub issue #%s has too many project memberships to read safely" % task_id
            page = items.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return None, "GitHub issue #%s is not on the declared project" % task_id
            cursor = page.get("endCursor")
            if not cursor:
                return None, "GitHub project membership pagination lost its cursor"

    def _bounded_comments(self, repository, task_id):
        """Return a content-sensitive bounded comment snapshot.

        We deliberately refuse a card with more comments than the digest limit.  Dropping an
        older comment would make a later foreign edit indistinguishable from a Relay comment,
        which is a collision safety failure, not a performance optimisation.
        """
        query = """
query($owner: String!, $repository: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repository) {
    issue(number: $number) {
      comments(first: 100, after: $cursor) {
        nodes { id body createdAt }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""
        comments = []
        cursor = None
        while True:
            data, reason = self._graphql(query, {
                "owner": repository["owner"], "repository": repository["repository"],
                "number": task_id, "cursor": cursor,
            })
            if data is None:
                return None, reason
            issue = ((data.get("repository") or {}).get("issue"))
            page = (issue or {}).get("comments")
            if not isinstance(page, dict):
                return None, "GitHub issue #%s has no readable comments" % task_id
            for entry in page.get("nodes") or []:
                comment_id = str(entry.get("id") or "").strip()
                if not comment_id:
                    return None, "GitHub issue #%s returned a comment without an id" % task_id
                comments.append({"id": comment_id, "body": entry.get("body") or "",
                                 "created": entry.get("createdAt")})
                if len(comments) > COMMENT_DIGEST_LIMIT:
                    return None, "GitHub issue #%s has more than %d comments; snapshot is unsafe" % (
                        task_id, COMMENT_DIGEST_LIMIT)
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return comments, None
            cursor = info.get("endCursor")
            if not cursor:
                return None, "GitHub comment pagination lost its cursor"

    def _triple_card(self, repository, project, task_id):
        payload, reason = self._issue(task_id)
        if payload is None:
            return None, reason
        issue_id = str(payload.get("id") or "").strip()
        if not issue_id:
            return None, "GitHub issue #%s has no node id" % task_id
        item, reason = self._project_item_for_issue(repository, project["id"], task_id)
        if item is None:
            return None, reason
        comments, reason = self._bounded_comments(repository, task_id)
        if comments is None:
            return None, reason
        card = {
            "id": str(task_id), "item_id": item["id"], "content_id": issue_id,
            "title": payload.get("title") or "", "description": payload.get("body") or "",
            "status": item.get("status"), "issue_state": payload.get("state"),
            "comments": comments,
        }
        card["comments_digest"] = _canonical(comments)
        card["digest"] = _canonical(card)
        return card, None

    # Interface.
    def candidates(self):
        items, _ = self._items()
        found = []
        for item in items:
            content = item.get("content") or {}
            if content.get("number") is None:
                continue
            found.append({
                "id": str(content.get("number")),
                "title": content.get("title") or "",
                "description": content.get("body") or "",
                "status": item.get("status"),
            })
        return found

    def ready(self, source):
        """Open issues carrying every label in `source["labels"]`. With no labels configured the
        answer is a reason, never every open issue: a feeder with nothing to go on must not
        read the whole backlog as ready. Anything subtler than labels, such as a card that waits
        on another card, is project policy and belongs in the sidecar's ready command."""
        wanted = [str(name) for name in (source or {}).get("labels") or []]
        if not wanted:
            return [], "no ready labels are configured; set [ready] labels in the feeder sidecar"
        payload, reason = self._gh(["gh", "issue", "list", "--state", "open",
                                    "--limit", str(READY_ISSUE_LIMIT), "--json", READY_FIELDS])
        if payload is None:
            return [], reason
        found = []
        for issue in payload or []:
            labels = tuple(str((label or {}).get("name")) for label in issue.get("labels") or [])
            if all(name in labels for name in wanted):
                found.append({"id": str(issue.get("number")), "title": issue.get("title") or "",
                              "description": issue.get("body") or "", "labels": labels})
        return found, None

    def read(self, task_id):
        payload, reason = self._issue(task_id)
        if payload is None:
            return {"id": str(task_id), "title": "", "description": "", "status": None, "skipped": reason}
        return {
            "id": str(task_id),
            "title": payload.get("title") or "",
            "description": payload.get("body") or "",
            "status": payload.get("state"),
        }

    def status(self, task_id):
        payload, reason = self._issue(task_id)
        if payload is None:
            return skipped(reason)
        state = payload.get("state")
        if state == CLOSED_STATE:
            return {"status": state, "terminal": True, "reference": None, "skipped": None}
        if not self._status_field:
            return {"status": state, "terminal": False, "reference": None, "skipped": None}
        board, reason = self._project_status(task_id)
        if reason:
            return skipped("the project board could not be read: %s" % reason)
        terminal = bool(board) and str(board).lower() == str(self._status_field).lower()
        return {"status": board or state, "terminal": terminal, "reference": None, "skipped": None}

    def comments_since(self, task_id, baseline_comment_id):
        entries, _ = self._comments(task_id)
        if baseline_comment_id is None:
            return entries
        ids = [entry["id"] for entry in entries]
        baseline = str(baseline_comment_id)
        if baseline in ids:
            return entries[ids.index(baseline) + 1:]
        return []

    def closing_reference(self, task_id, ref):
        entries, _ = self._comments(task_id)
        for entry in entries:
            if reference_hit(entry["body"], ref):
                return entry["id"]
        return None

    def write_tool_patterns(self):
        """`gh pr create` is deliberately absent: in PR terminal mode the task process opens the
        pull request, and reading that as a denied tracker write would misclassify the run."""
        return {"tools": (), "bash": WRITE_BASH_PREFIXES, "paths": ()}

    def closeout_allowed_tools(self, backend=None):
        return CLOSEOUT_TOOLS

    def closeout_instructions(self, outcome, return_to=None, backend=None):
        """`return_to` (stale cards, 2026-09-08) is the status the card read before this run,
        supplied for a blocked or halted outcome when the runner wants the card returned there.
        The task process moved the item to the in review status at its first step, so without
        the return every blocked or halted card sits in progress with nobody on it."""
        if outcome == OUTCOME_LANDED:
            return ("Close the issue with `gh issue close <number>` and add one comment naming the "
                    "landing reference below, or move its project item to the terminal status.")
        move = ("Do not close the issue and do not move its project item" if not return_to else
                "Do not close the issue. Move its project item back to `%s`, the status it read "
                "before this run, since no process is working on it now; use `gh project "
                "item-edit` with the board's Status field" % return_to)
        if outcome == OUTCOME_HALTED:
            return ("Add one comment naming the halt class and the cause line below with `gh issue "
                    "comment`. %s: a halted task is not finished." % move)
        return ("Add one comment carrying the blocker digest below with `gh issue comment`. %s: a "
                "blocked task stays open." % move)


# Triple coordinator read and comparison primitives ------------------------------------------
#
# These are module functions, intentionally not methods on the generic adapter interface.  A
# triple batch is GitHub Projects specific; adding another required method would make an
# otherwise compatible Jira or markdown adapter pretend it can offer board identity fencing.


def read_triple_snapshot(adapter, task_ids):
    """Read exactly the declared cards into a deterministic, immutable board snapshot.

    The caller performs this once before remote claims and once after.  It must treat any
    non-None `reason` as a collision boundary: a partial snapshot is never an eligible
    allocation.  Card order follows the manifest declaration, while the digest itself is
    canonical and therefore insensitive to Python dict ordering.
    """
    if not isinstance(adapter, GitHubAdapter):
        return {"snapshot": None, "reason": "triple snapshots require GitHubAdapter"}
    task_ids = tuple(str(task_id) for task_id in task_ids)
    if len(task_ids) != 3 or len(set(task_ids)) != 3 or any(not task_id for task_id in task_ids):
        return {"snapshot": None, "reason": "triple snapshot requires exactly three distinct card ids"}
    repository, reason = adapter._repository_identity()
    if repository is None:
        return {"snapshot": None, "reason": reason}
    project, reason = adapter._project_identity()
    if project is None:
        return {"snapshot": None, "reason": reason}
    cards = []
    for task_id in task_ids:
        card, reason = adapter._triple_card(repository, project, task_id)
        if card is None:
            return {"snapshot": None, "reason": "card %s is unreadable: %s" % (task_id, reason)}
        cards.append(card)
    snapshot = {
        "repository_id": repository["id"], "repository": repository["name"],
        "project_id": project["id"], "project_number": project["number"], "cards": cards,
    }
    snapshot["digest"] = _canonical(snapshot)
    return {"snapshot": snapshot, "reason": None}


def card_state(card, phase="preread"):
    """Freeze all observable fields that matter for later collision detection."""
    if not isinstance(card, dict):
        raise ValueError("card snapshot must be an object")
    required = ("id", "item_id", "content_id", "title", "description", "status",
                "issue_state", "comments")
    missing = [key for key in required if key not in card]
    if missing:
        raise ValueError("card snapshot is missing %s" % ", ".join(missing))
    return {key: card[key] for key in required} | {"phase": phase}


def collision_evidence(expected, observed):
    """Return None only for an exact expected board state.

    This deliberately considers a foreign comment mixed with a valid Relay transition a
    collision.  The coordinator cannot prove who authored the foreign mutation, so accepting
    the valid half would be a false proof of exclusive card ownership.
    """
    if observed is None:
        return _collision("card is unreadable or evicted from the project", expected)
    try:
        wanted = card_state(expected, expected.get("phase", "preread"))
        actual = card_state(observed, wanted["phase"])
    except (AttributeError, ValueError) as exc:
        return _collision("invalid board snapshot: %s" % exc, expected, observed)
    # phase is local state, not a board field.
    wanted.pop("phase", None)
    actual.pop("phase", None)
    if wanted == actual:
        return None
    immutable = ("id", "item_id", "content_id", "title", "description")
    for key in immutable:
        if wanted[key] != actual[key]:
            return _collision("unexpected %s change" % key, expected, observed)
    if wanted["status"] != actual["status"] or wanted["issue_state"] != actual["issue_state"]:
        return _collision("unexpected status transition", expected, observed)
    if wanted["comments"] != actual["comments"]:
        return _collision("unexpected or mixed comment mutation", expected, observed)
    return _collision("unclassified board mutation", expected, observed)


def _same_card_except(expected, observed, ignored):
    wanted = card_state(expected)
    actual = card_state(observed)
    for key in ("phase",) + tuple(ignored):
        wanted.pop(key, None)
        actual.pop(key, None)
    return wanted == actual


def _new_comments(expected, observed):
    before = expected.get("comments") or []
    after = observed.get("comments") or []
    # A comment edit or deletion must not be accepted as an append.  GitHub comment node ids
    # are immutable, and preserving the exact prefix also protects bodies from later edits.
    if after[:len(before)] != before:
        return None
    return after[len(before):]


def capture_task_delta(expected, observed, in_review_status, branch_ref):
    """Capture the one Task-owned transition and its exact head-comment identity.

    Returns `(next_expected, evidence)`.  The coordinator persists `next_expected` immediately
    after the post-Task read.  Any evidence halts that card before it can be merged.
    """
    if observed is None:
        return None, _collision("card is unreadable after Task", expected)
    if not _same_card_except(expected, observed, ("status", "comments")):
        return None, _collision("Task changed immutable card fields", expected, observed)
    if str(observed.get("status") or "").lower() != str(in_review_status or "").lower():
        return None, _collision("Task did not make the declared in-review transition", expected, observed)
    added = _new_comments(expected, observed)
    if added is None or len(added) != 1 or not reference_hit(added[0].get("body") or "", branch_ref):
        return None, _collision("Task comment is missing, foreign, or does not name its branch head",
                                expected, observed)
    return card_state(observed, "task"), None


def capture_closeout_delta(expected, observed, outcome, terminal_status=None, landing_ref=None,
                           return_to=None):
    """Capture the one Closeout comment and its allowed terminal or return transition.

    `landing_ref` is mandatory for a landed closeout so a generic comment cannot masquerade as
    proof of the merge.  Blocked and halted outcomes must keep the issue open and, when a
    `return_to` status is supplied, return exactly there.
    """
    if observed is None:
        return None, _collision("card is unreadable after Closeout", expected)
    if not _same_card_except(expected, observed, ("status", "issue_state", "comments")):
        return None, _collision("Closeout changed immutable card fields", expected, observed)
    added = _new_comments(expected, observed)
    if added is None or len(added) != 1:
        return None, _collision("Closeout comment is missing, foreign, or mixed", expected, observed)
    if outcome == OUTCOME_LANDED:
        if not landing_ref or not reference_hit(added[0].get("body") or "", landing_ref):
            return None, _collision("Closeout comment does not name the landing reference", expected, observed)
        status_terminal = (terminal_status is not None and
                           str(observed.get("status") or "").lower() == str(terminal_status).lower())
        if observed.get("issue_state") != CLOSED_STATE and not status_terminal:
            return None, _collision("landed Closeout did not reach a terminal state", expected, observed)
    else:
        if observed.get("issue_state") == CLOSED_STATE:
            return None, _collision("non-landed Closeout closed the issue", expected, observed)
        if return_to is not None and str(observed.get("status") or "").lower() != str(return_to).lower():
            return None, _collision("non-landed Closeout did not return the card to its prior status",
                                    expected, observed)
    return card_state(observed, "closeout"), None
