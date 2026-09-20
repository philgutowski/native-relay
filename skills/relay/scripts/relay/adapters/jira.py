"""Jira adapter, read side (U4, KTD9).

Reads go straight to the Jira REST API rather than through a model. A read that routes through a
`claude -p` call is a report of the thing being verified, not the thing itself, and landing is
exactly the claim Relay refuses to take on trust.

Credentials are read from the environment once, at construction, and never leave this object.
The launcher scrubs the same variables out of every child process env, so no task process, gate,
or push ever sees the token.
"""
import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

from . import (ConfigurationError, NETWORK_TIMEOUT_SECONDS, OUTCOME_HALTED, OUTCOME_LANDED,
               reference_hit, skipped)

ISSUE_FIELDS = "summary,description,status,comment,project"
# The issue endpoint is the one the plan pins. Enhanced search is the current Jira Cloud path for
# a JQL query and backs `validate --list` only, so a change there degrades to a skipped listing
# rather than a wrong landing verdict.
ISSUE_PATH = "/rest/api/3/issue/%s"
SEARCH_PATH = "/rest/api/3/search/jql"
SEARCH_PAGE_SIZE = 100
SEARCH_PAGE_LIMIT = 20
COMMENT_PAGE_SIZE = 100
COMMENT_DIGEST_LIMIT = 200

WRITE_TOOL_PREFIX = "mcp__atlassian__"
# Grok's Atlassian MCP tools carry no mcp__ prefix. Classify matches on startswith, so both
# spellings have to live here: a grok Closeout denied on atlassian__transitionJiraIssue would
# otherwise read as a plain denied_tool and not as tracker_write_denied.
GROK_WRITE_TOOL_PREFIX = "atlassian__"
CLOSEOUT_TOOLS = (
    "mcp__atlassian__getJiraIssue",
    "mcp__atlassian__getTransitionsForJiraIssue",
    "mcp__atlassian__transitionJiraIssue",
    "mcp__atlassian__addCommentToJiraIssue",
)
# Native grok allow form (Grok 1.0.25). The mcp__ spelling is rewritten onto the same matcher,
# but --allow with a Claude tool name is accepted and does not grant grok's real tools, so
# Closeout on grok names the form this CLI actually enforces.
GROK_CLOSEOUT_TOOLS = (
    "MCPTool(atlassian__getJiraIssue)",
    "MCPTool(atlassian__getTransitionsForJiraIssue)",
    "MCPTool(atlassian__transitionJiraIssue)",
    "MCPTool(atlassian__addCommentToJiraIssue)",
)
GROK_TOOL_NAMES = (
    "atlassian__getJiraIssue",
    "atlassian__getTransitionsForJiraIssue",
    "atlassian__transitionJiraIssue",
    "atlassian__addCommentToJiraIssue",
)


def _canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=True).encode("utf-8")).hexdigest()


def _adf_text(node):
    """Flatten Atlassian Document Format, the nested JSON Jira stores rich text in, to plain
    text. The brief passes a description verbatim, so it has to be text, not a document tree."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(item) for item in node)
    if isinstance(node, dict):
        kind = node.get("type")
        if kind == "text":
            return str(node.get("text", ""))
        if kind == "hardBreak":
            return "\n"
        inner = _adf_text(node.get("content"))
        if kind in ("paragraph", "heading", "listItem", "blockquote", "codeBlock", "rule"):
            return inner + "\n"
        return inner
    return ""


class JiraAdapter:
    def __init__(self, manifest, opener=None, env=None):
        import os

        env = os.environ if env is None else env
        tracker = manifest.tracker
        self._site = (tracker.site or "").rstrip("/")
        self._project_key = tracker.project_key
        self._done = tuple(str(name).lower() for name in tracker.done_statuses)
        token = env.get(tracker.token_env)
        email = env.get(tracker.email_env)
        if not token:
            raise ConfigurationError(
                "the jira adapter needs an API token in %s and it is not set" % tracker.token_env)
        if not email:
            raise ConfigurationError(
                "the jira adapter needs an account email in %s and it is not set" % tracker.email_env)
        pair = ("%s:%s" % (email, token)).encode("utf-8")
        self._authorization = "Basic " + base64.b64encode(pair).decode("ascii")
        self._opener = opener or urllib.request.build_opener()
        # Set only after remote claims and a project-validated re-read.  Triple REST writes may
        # never address an issue outside this frozen immutable-ID set.
        self._triple_write_issue_ids = frozenset()

    # Transport.
    def _request(self, method, path, params=None, payload=None):
        """Returns (payload, None) or (None, reason). Never raises for a transport failure."""
        url = "https://%s%s" % (self._site, path)
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": self._authorization,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        try:
            with self._opener.open(request, timeout=NETWORK_TIMEOUT_SECONDS) as body:
                raw = body.read().decode("utf-8")
                # Jira transitions conventionally succeed with HTTP 204.  Treat an empty
                # successful body as an empty response rather than turning the write into a
                # false collision merely because it is not JSON.
                return (json.loads(raw) if raw.strip() else {}), None
        except urllib.error.HTTPError as exc:
            # An HTTPError is also an open response holding the error body.  Close it, or the
            # interpreter warns when it has to clean the handle up itself.
            exc.close()
            return None, "jira returned %s for %s" % (exc.code, path)
        except (OSError, ValueError) as exc:
            return None, "jira read failed: %s" % exc

    def _get(self, path, params=None):
        return self._request("GET", path, params=params)

    def _post(self, path, payload):
        return self._request("POST", path, payload=payload)

    def _issue(self, task_id):
        return self._get(ISSUE_PATH % task_id, {"fields": ISSUE_FIELDS})

    def _comments(self, task_id):
        payload, reason = self._issue(task_id)
        if payload is None:
            return [], reason
        raw = ((payload.get("fields") or {}).get("comment") or {}).get("comments") or []
        return [{"id": str(entry.get("id")), "body": _adf_text(entry.get("body")).strip(),
                 "created": entry.get("created")} for entry in raw], None

    def _all_comments(self, issue_id):
        """Read the complete bounded history: a truncated history cannot fence a foreign edit."""
        found, start = [], 0
        while True:
            payload, reason = self._get("/rest/api/3/issue/%s/comment" % issue_id,
                                        {"startAt": start, "maxResults": COMMENT_PAGE_SIZE})
            if payload is None:
                return None, reason
            for entry in payload.get("comments") or []:
                comment_id = str(entry.get("id") or "").strip()
                if not comment_id:
                    return None, "jira returned a comment without an id"
                found.append({"id": comment_id, "body": _adf_text(entry.get("body")).strip(),
                              "created": entry.get("created")})
                if len(found) > COMMENT_DIGEST_LIMIT:
                    return None, "jira issue has more than %d comments; snapshot is unsafe" % COMMENT_DIGEST_LIMIT
            total = int(payload.get("total", len(found)) or 0)
            start += int(payload.get("maxResults", COMMENT_PAGE_SIZE) or COMMENT_PAGE_SIZE)
            if start >= total:
                return found, None

    def _transition(self, issue_id, label, expected_status):
        payload, reason = self._get("/rest/api/3/issue/%s/transitions" % issue_id)
        if payload is None:
            return False, reason
        hits = [entry for entry in payload.get("transitions") or []
                if str(entry.get("name") or "") == str(label)]
        if len(hits) != 1:
            return False, "jira has %d transitions labelled %r" % (len(hits), label)
        actual = str((hits[0].get("to") or {}).get("name") or "")
        if actual != str(expected_status):
            return False, "jira transition %r targets %r, not configured status %r" % (
                label, actual, expected_status)
        _payload, reason = self._post("/rest/api/3/issue/%s/transitions" % issue_id,
                                      {"transition": {"id": hits[0].get("id")}})
        return (reason is None), reason

    def _authorize_triple_writes(self, snapshot):
        """Fence REST writes to IDs taken from the claimed, project-validated snapshot."""
        cards = (snapshot or {}).get("cards") or []
        issue_ids = [str(card.get("item_id") or "") for card in cards]
        if len(issue_ids) != 3 or len(set(issue_ids)) != 3 or any(not value for value in issue_ids):
            return False, "Jira triple write scope needs exactly three immutable issue ids"
        self._triple_write_issue_ids = frozenset(issue_ids)
        return True, None

    def _triple_issue_id(self, card):
        issue_id = str((card or {}).get("item_id") or "")
        if issue_id not in self._triple_write_issue_ids:
            return None, "Jira coordinator write refused outside claimed immutable issue ids"
        return issue_id, None

    def _triple_transition(self, card, label, expected_status):
        issue_id, reason = self._triple_issue_id(card)
        if issue_id is None:
            return False, reason
        return self._transition(issue_id, label, expected_status)

    def _triple_comment(self, card, text):
        issue_id, reason = self._triple_issue_id(card)
        if issue_id is None:
            return False, reason
        payload = {"body": {"type": "doc", "version": 1,
                            "content": [{"type": "paragraph", "content": [
                                {"type": "text", "text": str(text)}]}]}}
        _result, reason = self._post("/rest/api/3/issue/%s/comment" % issue_id, payload)
        return (reason is None), reason

    # Interface.
    def candidates(self):
        """The project's cards that are not done, oldest first (issue #24). The done statuses go
        into the JQL, because the search returns one page at a time and a project's done cards
        filled the first page of 50 on a real board; the same filter runs again on what comes
        back, so a status Jira matched loosely cannot slip through. Pages are followed to a
        bound rather than without end."""
        jql = "project = %s" % self._project_key
        if self._done:
            jql += " AND status not in (%s)" % ", ".join(
                '"%s"' % name.replace('"', '\\"') for name in self._done)
        params = {"jql": jql + " ORDER BY created ASC", "fields": "summary,status",
                  "maxResults": SEARCH_PAGE_SIZE}
        found = []
        for _ in range(SEARCH_PAGE_LIMIT):
            payload, reason = self._get(SEARCH_PATH, params)
            if payload is None:
                break
            for issue in payload.get("issues") or []:
                fields = issue.get("fields") or {}
                status = (fields.get("status") or {}).get("name")
                if status and status.lower() in self._done:
                    continue
                found.append({
                    "id": issue.get("key"),
                    "title": fields.get("summary") or "",
                    "description": "",
                    "status": status,
                })
            token = payload.get("nextPageToken")
            if payload.get("isLast", True) or not token:
                break
            params = dict(params, nextPageToken=token)
        return found

    def ready(self, source):
        """The cards a configured JQL query returns, in the query's own order. JQL is Jira's
        search language, and what ready means on a Jira board (a status, a label, no open
        blocker link) differs per project, so the query is the project's and none is assumed.
        A done status is filtered again on what comes back, the same belt `candidates` wears."""
        jql = str((source or {}).get("jql") or "").strip()
        if not jql:
            return [], "no ready query is configured; set [ready] jql in the feeder sidecar"
        params = {"jql": jql, "fields": "summary,status,labels,description",
                  "maxResults": SEARCH_PAGE_SIZE}
        found = []
        for _ in range(SEARCH_PAGE_LIMIT):
            payload, reason = self._get(SEARCH_PATH, params)
            if payload is None:
                return [], reason
            for issue in payload.get("issues") or []:
                fields = issue.get("fields") or {}
                status = (fields.get("status") or {}).get("name")
                if status and status.lower() in self._done:
                    continue
                found.append({"id": issue.get("key"), "title": fields.get("summary") or "",
                              "description": _adf_text(fields.get("description")).strip(),
                              "labels": tuple(str(name) for name in fields.get("labels") or [])})
            token = payload.get("nextPageToken")
            if payload.get("isLast", True) or not token:
                break
            params = dict(params, nextPageToken=token)
        return found, None

    def read(self, task_id):
        payload, reason = self._issue(task_id)
        if payload is None:
            return {"id": task_id, "title": "", "description": "", "status": None, "skipped": reason}
        fields = payload.get("fields") or {}
        return {
            "id": payload.get("key") or task_id,
            "title": fields.get("summary") or "",
            "description": _adf_text(fields.get("description")).strip(),
            "status": (fields.get("status") or {}).get("name"),
        }

    def status(self, task_id):
        payload, reason = self._issue(task_id)
        if payload is None:
            return skipped(reason)
        name = ((payload.get("fields") or {}).get("status") or {}).get("name")
        return {
            "status": name,
            "terminal": bool(name) and name.lower() in self._done,
            "reference": None,
            "skipped": None,
        }

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
        return {"tools": (WRITE_TOOL_PREFIX, GROK_WRITE_TOOL_PREFIX), "bash": (), "paths": ()}

    def closeout_allowed_tools(self, backend=None):
        if backend == "grok":
            return GROK_CLOSEOUT_TOOLS
        return CLOSEOUT_TOOLS

    def closeout_instructions(self, outcome, return_to=None, backend=None):
        """`return_to` (stale cards, 2026-09-08) is the status the card read before this run,
        supplied for a blocked or halted outcome when the runner wants the card returned there.
        The task process transitioned the card to the in review status at its first step, so
        without the return every blocked or halted card sits in progress with nobody on it."""
        if outcome == OUTCOME_LANDED:
            text = ("Transition the card to its terminal status, then add one comment naming the "
                    "landing reference below. Use the Jira tools on your allowlist and nothing else.")
        elif outcome == OUTCOME_HALTED:
            move = ("Do not transition the card: a halted task keeps its current status."
                    if not return_to else
                    "Transition the card back to `%s`, the status it read before this run, since "
                    "no process is working on it now; a halted task is not finished." % return_to)
            text = "Add one comment naming the halt class and the cause line below. %s" % move
        else:
            move = ("Do not transition the card: a blocked task keeps its current status so the "
                    "board still shows it as open." if not return_to else
                    "Transition the card back to `%s`, the status it read before this run, since "
                    "no process is working on it now; a blocked task stays open." % return_to)
            text = "Add one comment carrying the blocker digest below. %s" % move
        tail = (" Pass %s as cloudId on every Atlassian call. Never call "
                "getAccessibleAtlassianResources." % self._site)
        if backend == "grok":
            tail += (" This CLI names the tools %s. Use those, not mcp__atlassian__ names, "
                     "and not JIRA_API_TOKEN; the token is not in this process."
                     % ", ".join(GROK_TOOL_NAMES))
        return text + tail


# Triple coordinator snapshot ---------------------------------------------------------------
#
# This remains opt-in: Jira's normal adapter surface is deliberately read-only and serial runs
# continue to hand tracker writes to their Task/Closeout process.  A triple batch needs one
# deterministic view for all three backends because Codex has no Atlassian MCP closeout path.


def read_triple_snapshot(adapter, task_ids):
    if not isinstance(adapter, JiraAdapter):
        return {"snapshot": None, "reason": "triple snapshots require JiraAdapter"}
    task_ids = tuple(str(task_id) for task_id in task_ids)
    if len(task_ids) != 3 or len(set(task_ids)) != 3 or any(not task_id for task_id in task_ids):
        return {"snapshot": None, "reason": "triple snapshot requires exactly three distinct card ids"}
    cards, project_id = [], None
    for task_id in task_ids:
        payload, reason = adapter._issue(task_id)
        if payload is None:
            return {"snapshot": None, "reason": "card %s is unreadable: %s" % (task_id, reason)}
        fields = payload.get("fields") or {}
        project = fields.get("project") or {}
        issue_id = str(payload.get("id") or "").strip()
        actual_key = str(payload.get("key") or "").strip()
        actual_project = str(project.get("key") or "").strip()
        actual_project_id = str(project.get("id") or "").strip()
        if not issue_id or not actual_key or not actual_project_id:
            return {"snapshot": None, "reason": "card %s has incomplete immutable Jira identity" % task_id}
        if actual_key != task_id or actual_project != str(adapter._project_key):
            return {"snapshot": None, "reason": "card %s is not in Jira project %s" % (task_id, adapter._project_key)}
        if project_id is None:
            project_id = actual_project_id
        elif project_id != actual_project_id:
            return {"snapshot": None, "reason": "declared Jira cards belong to different projects"}
        comments, reason = adapter._all_comments(issue_id)
        if comments is None:
            return {"snapshot": None, "reason": "card %s comments are unreadable: %s" % (task_id, reason)}
        card = {"id": task_id, "item_id": issue_id, "content_id": issue_id,
                "title": fields.get("summary") or "",
                "description": _adf_text(fields.get("description")).strip(),
                "status": (fields.get("status") or {}).get("name"),
                "issue_state": "OPEN", "comments": comments}
        card["comments_digest"] = _canonical(comments)
        card["digest"] = _canonical(card)
        cards.append(card)
    snapshot = {"repository_id": "jira:%s" % adapter._site.lower(),
                "project_id": "jira-project:%s" % project_id,
                "project_key": str(adapter._project_key), "cards": cards}
    snapshot["digest"] = _canonical(snapshot)
    return {"snapshot": snapshot, "reason": None}
