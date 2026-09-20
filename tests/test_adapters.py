"""U4: the three read side tracker adapters behind one interface.

Every adapter takes an injectable transport (an opener for Jira, a run callable for `gh`, the
git read wrapper for markdown), so no test here touches a network or invokes `gh`, and `gh` may
be absent from the machine entirely. The shared contract runs against all three.
"""
import os
import re
import copy
import json
import tempfile
import unittest
from unittest import mock

import _paths
import _repo
from relay import adapters, manifest as mf
from relay.adapters import github as gh_adapter, jira as jira_adapter, markdown as md_adapter

FIXTURE = os.path.join(_paths.FIXTURES_DIR, "manifests", "complete.toml")
TRACKER = os.path.join(_paths.FIXTURES_DIR, "tracker")

TRACKER_MD = """# Tasks

- [ ] T-1 Add the brief renderer
  - 2026-08-24 picked this up
  - 2026-08-25 still going
- [x] T-2 Wire the run loop (abc1234)
- [ ] T-3 Nothing yet
"""


def fixture(name):
    with open(os.path.join(TRACKER, name)) as handle:
        return handle.read()


class FakeOpener:
    """Stands in for urllib's opener. Routes a URL substring to a fixture file."""

    def __init__(self, routes, error=None):
        self.routes = dict(routes)
        self.error = error
        self.requests = []

    def open(self, request, timeout=None):
        url = request.get_full_url() if hasattr(request, "get_full_url") else str(request)
        self.requests.append((url, dict(getattr(request, "headers", {})), timeout))
        if self.error:
            raise self.error
        for fragment, name in self.routes.items():
            if fragment in url:
                return _Body(fixture(name))
        raise AssertionError("no fixture routed for %s" % url)


class _Body:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DispatchRun:
    """A `run(args, timeout=None)` for `gh` that answers from fixtures by subcommand."""

    def __init__(self, issues=None, items=None, failure=None, items_failure=None):
        self.issues = dict(issues or {})
        self.items = items
        self.failure = failure
        # A board that fails on its own, with the issue read still working. That is the shape
        # that matters: `gh issue view` needs no project scope and `gh project item-list` does.
        self.items_failure = items_failure
        self.calls = []

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        if self.failure is not None:
            code, err = self.failure
            return _Proc(code, "", err)
        if "project" in args:
            if self.items_failure is not None:
                code, err = self.items_failure
                return _Proc(code, "", err)
            return _Proc(0, fixture(self.items), "")
        number = args[args.index("view") + 1]
        name = self.issues.get(str(number))
        if name is None:
            return _Proc(1, "", "no issue %s" % number)
        return _Proc(0, fixture(name), "")


class _Proc:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = _repo.make_repo(self.tmp.name, files={"tracker.md": TRACKER_MD})
        with open(FIXTURE) as handle:
            self.toml = handle.read().replace("__REPO__", self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, text=None, name="manifest.toml"):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w") as handle:
            handle.write(text if text is not None else self.toml)
        return mf.load(path)

    def jira_manifest(self):
        text = self.toml.replace('adapter = "markdown"', 'adapter = "jira"')
        text = text.replace('file = "tracker.md"', 'site = "example.atlassian.net"\nproject_key = "EX"')
        return self.manifest(text.replace('done_statuses = ["done"]', 'done_statuses = ["Done", "Closed"]'),
                             name="jira.toml")

    def github_manifest(self, status_field="Shipped"):
        text = self.toml.replace('adapter = "markdown"', 'adapter = "github"')
        text = text.replace('file = "tracker.md"',
                            'owner = "example-org"\nproject_number = 4\nstatus_field = "%s"' % status_field)
        return self.manifest(text, name="github.toml")

    def jira(self, opener, env=None):
        return jira_adapter.JiraAdapter(self.jira_manifest(),
                                        opener=opener,
                                        env=env or {"JIRA_API_TOKEN": "t", "JIRA_EMAIL": "e@x.invalid"})

    def github(self, run):
        return gh_adapter.GitHubAdapter(self.github_manifest(), run=run)

    def markdown(self):
        return md_adapter.MarkdownAdapter(self.manifest())


class SharedContract(AdapterCase):
    """Every adapter answers the same nine methods and exposes nothing that writes."""

    def each(self):
        yield "jira", self.jira(FakeOpener({"/issue/": "jira_issue_done.json", "search": "jira_search.json"}))
        yield "github", self.github(DispatchRun(issues={"12": "github_issue_closed.json"},
                                                items="github_project_items.json"))
        yield "markdown", self.markdown()

    def test_every_adapter_implements_the_whole_interface(self):
        for name, adapter in self.each():
            for method in adapters.INTERFACE:
                self.assertTrue(callable(getattr(adapter, method, None)), "%s lacks %s" % (name, method))

    def test_no_adapter_exposes_a_method_outside_the_read_side_interface(self):
        for name, adapter in self.each():
            public = {attr for attr in dir(adapter)
                      if not attr.startswith("_") and callable(getattr(adapter, attr))}
            self.assertEqual(public, set(adapters.INTERFACE), "%s exposes more than the interface" % name)

    def test_every_adapter_returns_the_status_shape_verify_reads(self):
        ids = {"jira": "ABC-83", "github": "12", "markdown": "T-2"}
        for name, adapter in self.each():
            result = adapter.status(ids[name])
            for key in ("status", "terminal", "reference", "skipped"):
                self.assertIn(key, result, "%s status is missing %s" % (name, key))
            self.assertTrue(result["terminal"], name)

    def test_every_adapter_names_its_closeout_tools_without_a_wildcard(self):
        for name, adapter in self.each():
            tools = adapter.closeout_allowed_tools()
            self.assertTrue(tools, name)
            for tool in tools:
                self.assertNotIn("*", tool, "%s closeout tools carry a wildcard" % name)

    def test_every_adapter_writes_closeout_instructions_for_both_outcomes(self):
        for name, adapter in self.each():
            for outcome in ("landed", "blocked"):
                text = adapter.closeout_instructions(outcome)
                self.assertTrue(text.strip(), "%s has no %s instructions" % (name, outcome))
            self.assertNotEqual(adapter.closeout_instructions("landed"),
                                adapter.closeout_instructions("blocked"), name)

    def test_every_adapter_writes_a_halted_instruction_that_moves_nothing(self):
        for name, adapter in self.each():
            text = adapter.closeout_instructions("halted")
            self.assertTrue(text.strip(), "%s has no halted instructions" % name)
            self.assertNotEqual(text, adapter.closeout_instructions("landed"), name)
            self.assertNotEqual(text, adapter.closeout_instructions("blocked"), name)
            self.assertNotIn("`[x]`", text, "%s halted instructions check the box" % name)


class ReturnTo(SharedContract):
    """Stale cards, R1 to R3: the move sentence each adapter renders when the runner asks for
    a blocked or halted card back, and the two that never move anything."""

    def test_github_and_jira_move_the_card_back_and_stop_saying_not_to(self):
        for name, adapter in self.each():
            if name == "markdown":
                continue
            for outcome in ("blocked", "halted"):
                text = adapter.closeout_instructions(outcome, return_to="Todo")
                self.assertIn("`Todo`", text, "%s %s names no destination" % (name, outcome))
                self.assertIn("before this run", text, name)
                self.assertNotIn("Do not transition the card", text, name)
                self.assertNotIn("do not move its project item", text, name)
                self.assertNotEqual(text, adapter.closeout_instructions(outcome), name)

    def test_without_a_destination_the_sentences_are_unchanged(self):
        for name, adapter in self.each():
            for outcome in ("landed", "blocked", "halted"):
                self.assertEqual(adapter.closeout_instructions(outcome),
                                 adapter.closeout_instructions(outcome, return_to=None), name)

    def test_a_landed_outcome_ignores_the_destination(self):
        for name, adapter in self.each():
            self.assertEqual(adapter.closeout_instructions("landed"),
                             adapter.closeout_instructions("landed", return_to="Todo"), name)

    def test_markdown_has_no_status_to_return_to(self):
        adapter = self.markdown()
        for outcome in ("blocked", "halted"):
            self.assertEqual(adapter.closeout_instructions(outcome),
                             adapter.closeout_instructions(outcome, return_to="Todo"))

    def test_the_interface_is_nine_methods_and_the_ninth_is_a_read(self):
        # Eight until the feeder plan of 2026-09-19 added `ready`, which reads and never writes.
        self.assertEqual(len(adapters.INTERFACE), 9)
        self.assertIn("ready", adapters.INTERFACE)


class _JsonOpener:
    """One canned JSON answer for every request, with the URLs kept for the assertions."""

    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def open(self, request, timeout=None):
        self.urls.append(request.get_full_url())
        return _Body(json.dumps(self.payload))


class ReadySource(AdapterCase):
    """Feeder plan, KTD3. What ready means is the sidecar's data; the adapter only reads it."""

    ISSUES = [
        {"number": 7, "title": "a unit", "body": "**Model:** fable",
         "labels": [{"name": "unit"}, {"name": "ready"}]},
        {"number": 8, "title": "not ready yet", "body": "", "labels": [{"name": "unit"}]},
        {"number": 9, "title": "ready and attended", "body": "",
         "labels": [{"name": "unit"}, {"name": "ready"}, {"name": "attended"}]},
    ]

    def gh(self, code=0, payload=None, err=""):
        calls = []

        def run(args, timeout=None):
            calls.append(list(args))
            return _Proc(code, json.dumps(self.ISSUES if payload is None else payload), err)
        return run, calls

    def test_github_returns_open_issues_carrying_every_configured_label(self):
        run, calls = self.gh()
        cards, reason = self.github(run).ready({"labels": ["unit", "ready"]})
        self.assertIsNone(reason)
        self.assertEqual([card["id"] for card in cards], ["7", "9"])
        self.assertEqual(cards[0]["description"], "**Model:** fable")
        self.assertEqual(cards[1]["labels"], ("unit", "ready", "attended"))
        self.assertEqual(calls[0][:5], ["gh", "issue", "list", "--state", "open"])

    def test_github_with_no_labels_configured_is_a_reason_and_never_the_whole_backlog(self):
        run, calls = self.gh()
        cards, reason = self.github(run).ready({})
        self.assertEqual(cards, [])
        self.assertIn("no ready labels", reason)
        self.assertEqual(calls, [])

    def test_a_github_read_failure_is_a_reason_not_an_empty_board(self):
        run, _ = self.gh(code=1, err="HTTP 502")
        cards, reason = self.github(run).ready({"labels": ["ready"]})
        self.assertEqual(cards, [])
        self.assertIn("502", reason)

    def test_jira_runs_the_configured_query_and_drops_a_done_card(self):
        opener = _JsonOpener({"issues": [
            {"key": "EX-1", "fields": {"summary": "one", "status": {"name": "Ready"},
                                       "labels": ["unit"], "description": None}},
            {"key": "EX-2", "fields": {"summary": "two", "status": {"name": "Done"}}},
        ]})
        cards, reason = self.jira(opener).ready({"jql": "project = EX AND status = Ready"})
        self.assertIsNone(reason)
        self.assertEqual([(card["id"], card["labels"]) for card in cards], [("EX-1", ("unit",))])
        self.assertIn("status+%3D+Ready", opener.urls[0])

    def test_jira_with_no_query_configured_is_a_reason(self):
        opener = _JsonOpener({"issues": []})
        cards, reason = self.jira(opener).ready({})
        self.assertEqual(cards, [])
        self.assertIn("no ready query", reason)
        self.assertEqual(opener.urls, [])

    def test_markdown_ready_is_every_unchecked_box(self):
        cards, reason = self.markdown().ready({})
        self.assertIsNone(reason)
        self.assertEqual([card["id"] for card in cards], ["T-1", "T-3"])


class Jira(AdapterCase):
    def opener(self, **routes):
        return FakeOpener(routes or {"/issue/": "jira_issue_done.json", "search": "jira_search.json"})

    def test_a_done_issue_reads_terminal_with_its_flattened_description(self):
        adapter = self.jira(self.opener())
        card = adapter.read("ABC-83")
        self.assertEqual(card["id"], "ABC-83")
        self.assertEqual(card["title"], "Add the brief renderer")
        self.assertIn("Render the task brief from a template.", card["description"])
        self.assertIn("Second paragraph", card["description"])
        self.assertEqual(card["status"], "Done")
        self.assertTrue(adapter.status("ABC-83")["terminal"])

    def test_a_backlog_issue_is_not_terminal(self):
        adapter = self.jira(self.opener(**{"/issue/": "jira_issue_open.json"}))
        self.assertFalse(adapter.status("ABC-84")["terminal"])

    def test_closing_reference_finds_the_comment_naming_the_sha_prefix(self):
        adapter = self.jira(self.opener())
        self.assertEqual(adapter.closing_reference("ABC-83", "abc1234def56789"), "10002")

    def test_closing_reference_returns_none_when_no_comment_names_it(self):
        adapter = self.jira(self.opener())
        self.assertIsNone(adapter.closing_reference("ABC-83", "9999999"))

    def test_comments_since_a_baseline_returns_exactly_the_newer_ones_in_order(self):
        adapter = self.jira(self.opener())
        newer = adapter.comments_since("ABC-83", "10001")
        self.assertEqual([entry["id"] for entry in newer], ["10002", "10003"])
        self.assertIn("Landed on main", newer[0]["body"])

    def test_comments_since_none_returns_every_comment(self):
        adapter = self.jira(self.opener())
        self.assertEqual(len(adapter.comments_since("ABC-83", None)), 3)

    def test_comments_since_an_absent_baseline_is_unresolved_not_everything(self):
        adapter = self.jira(self.opener())
        self.assertEqual(adapter.comments_since("ABC-83", "99999"), [])

    def test_a_missing_token_env_var_is_a_named_configuration_error_before_any_request(self):
        opener = self.opener()
        with self.assertRaises(adapters.ConfigurationError) as caught:
            jira_adapter.JiraAdapter(self.jira_manifest(), opener=opener, env={"JIRA_EMAIL": "e@x.invalid"})
        self.assertIn("JIRA_API_TOKEN", str(caught.exception))
        self.assertEqual(opener.requests, [])

    def test_the_request_carries_basic_auth_and_the_thirty_second_timeout(self):
        opener = self.opener()
        self.jira(opener).read("ABC-83")
        url, headers, timeout = opener.requests[0]
        self.assertIn("example.atlassian.net/rest/api/3/issue/ABC-83", url)
        self.assertEqual(timeout, adapters.NETWORK_TIMEOUT_SECONDS)
        self.assertTrue(any(key.lower() == "authorization" for key in headers))

    def test_a_successful_empty_jira_transition_response_is_not_a_json_failure(self):
        adapter = self.jira(self.opener())
        adapter._opener = mock.Mock()
        adapter._opener.open.return_value = _Body("")
        payload, reason = adapter._post("/rest/api/3/issue/ABC-83/transitions", {"transition": {"id": "1"}})
        self.assertEqual(payload, {})
        self.assertIsNone(reason)

    def test_a_read_that_raises_becomes_a_skipped_result_rather_than_an_exception(self):
        adapter = self.jira(FakeOpener({}, error=OSError("connection refused")))
        result = adapter.status("ABC-83")
        self.assertIn("connection refused", result["skipped"])
        self.assertFalse(result["terminal"])
        self.assertEqual(adapter.comments_since("ABC-83", None), [])
        self.assertIsNone(adapter.closing_reference("ABC-83", "abc1234"))

    def test_an_http_error_status_becomes_a_skipped_result_and_its_response_is_closed(self):
        # urllib's HTTPError is the open response as well as the exception: it holds the error
        # body in a file. Dropped unclosed, the interpreter cleans it up later and says so with a
        # ResourceWarning, which is how a live 404 once showed up in this suite's output.
        import io
        import urllib.error

        body = io.BytesIO(b'{"errorMessages": ["Issue does not exist"]}')
        error = urllib.error.HTTPError("https://example.atlassian.net/rest/api/3/issue/ABC-83",
                                       404, "Not Found", {}, body)
        adapter = self.jira(FakeOpener({}, error=error))
        result = adapter.status("ABC-83")
        self.assertIn("jira returned 404", result["skipped"])
        self.assertFalse(result["terminal"])
        self.assertTrue(body.closed)

    def test_candidates_lists_the_project_issues_that_are_not_done(self):
        """Issue #24: done cards filled the first page of 50 on a real board."""
        opener = self.opener()
        found = self.jira(opener).candidates()
        self.assertEqual([entry["id"] for entry in found], ["ABC-84"])
        url = opener.requests[0][0]
        self.assertIn("status+not+in+%28%22done%22%2C+%22closed%22%29", url)
        self.assertIn("maxResults=100", url)

    def test_the_write_patterns_name_the_atlassian_mcp_and_nothing_else(self):
        patterns = self.jira(self.opener()).write_tool_patterns()
        self.assertEqual(patterns["tools"],
                         (jira_adapter.WRITE_TOOL_PREFIX, jira_adapter.GROK_WRITE_TOOL_PREFIX))
        self.assertEqual(patterns["bash"], ())

    def test_the_closeout_tools_are_explicit_and_carry_no_confluence_tool(self):
        tools = self.jira(self.opener()).closeout_allowed_tools()
        self.assertEqual(tools, jira_adapter.CLOSEOUT_TOOLS)
        self.assertEqual(tools, (
            "mcp__atlassian__getJiraIssue",
            "mcp__atlassian__getTransitionsForJiraIssue",
            "mcp__atlassian__transitionJiraIssue",
            "mcp__atlassian__addCommentToJiraIssue",
        ))
        self.assertNotIn("mcp__atlassian__getAccessibleAtlassianResources", tools)
        for tool in tools:
            self.assertNotIn("Confluence", tool)
            self.assertNotIn("*", tool)

    def test_grok_closeout_tools_use_the_native_allow_form_and_carry_no_wildcard(self):
        tools = self.jira(self.opener()).closeout_allowed_tools(backend="grok")
        self.assertEqual(tools, jira_adapter.GROK_CLOSEOUT_TOOLS)
        for tool in tools:
            self.assertTrue(tool.startswith("MCPTool(atlassian__"), tool)
            self.assertNotIn("*", tool)
            self.assertNotIn("mcp__", tool)

    def test_closeout_instructions_name_the_site_as_cloud_id(self):
        adapter = self.jira(self.opener())
        for outcome in ("landed", "blocked", "halted"):
            text = adapter.closeout_instructions(outcome)
            self.assertIn("example.atlassian.net", text, outcome)
            self.assertIn("cloudId", text, outcome)
            self.assertIn("getAccessibleAtlassianResources", text, outcome)
            self.assertRegex(text, r"(?i)never call getAccessibleAtlassianResources")

    def test_triple_snapshot_uses_immutable_jira_issue_and_project_ids(self):
        adapter = self.jira(self.opener())
        def issue(key):
            number = key.rsplit("-", 1)[1]
            return ({"id": "issue-" + number, "key": key, "fields": {
                "project": {"id": "project-9", "key": "EX"},
                "summary": "Card " + number, "description": {"type": "doc", "content": []},
                "status": {"name": "To Do"}}}, None)
        with mock.patch.object(adapter, "_issue", side_effect=issue), \
             mock.patch.object(adapter, "_all_comments", return_value=([], None)):
            result = jira_adapter.read_triple_snapshot(adapter, ("EX-1", "EX-2", "EX-3"))
        self.assertIsNone(result["reason"])
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["project_id"], "jira-project:project-9")
        self.assertEqual([card["item_id"] for card in snapshot["cards"]],
                         ["issue-1", "issue-2", "issue-3"])

    def test_triple_transition_uses_label_and_requires_its_exact_destination_status(self):
        adapter = self.jira(self.opener())
        self.assertEqual(adapter._authorize_triple_writes({"cards": [
            {"item_id": "issue-1"}, {"item_id": "issue-2"}, {"item_id": "issue-3"}]}),
            (True, None))
        transitions = {"transitions": [{"id": "7", "name": "In Review",
                                          "to": {"name": "In Progress"}}]}
        with mock.patch.object(adapter, "_get", return_value=(transitions, None)), \
             mock.patch.object(adapter, "_post", return_value=({}, None)) as post:
            ok, reason = adapter._triple_transition({"item_id": "issue-1"}, "In Review", "In Progress")
        self.assertTrue(ok, reason)
        self.assertEqual(post.call_args.args[0], "/rest/api/3/issue/issue-1/transitions")

    def test_triple_transition_fails_closed_for_missing_ambiguous_or_mismatched_labels(self):
        adapter = self.jira(self.opener())
        adapter._authorize_triple_writes({"cards": [
            {"item_id": "issue-1"}, {"item_id": "issue-2"}, {"item_id": "issue-3"}]})
        cases = (
            ([], "has 0 transitions labelled"),
            ([{"id": "1", "name": "In Review", "to": {"name": "In Progress"}},
              {"id": "2", "name": "In Review", "to": {"name": "In Progress"}}],
             "has 2 transitions labelled"),
            ([{"id": "1", "name": "In Review", "to": {"name": "Ready"}}], "not configured status"),
        )
        for transitions, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(
                    adapter, "_get", return_value=({"transitions": transitions}, None)), \
                    mock.patch.object(adapter, "_post") as post:
                ok, reason = adapter._triple_transition(
                    {"item_id": "issue-1"}, "In Review", "In Progress")
            self.assertFalse(ok)
            self.assertIn(expected, reason)
            post.assert_not_called()

    def test_triple_write_scope_refuses_unclaimed_issue_ids(self):
        adapter = self.jira(self.opener())
        adapter._authorize_triple_writes({"cards": [
            {"item_id": "issue-1"}, {"item_id": "issue-2"}, {"item_id": "issue-3"}]})
        with mock.patch.object(adapter, "_post") as post:
            ok, reason = adapter._triple_comment({"item_id": "issue-outside"}, "nope")
        self.assertFalse(ok)
        self.assertIn("outside claimed immutable issue ids", reason)
        post.assert_not_called()


class GitHub(AdapterCase):
    def run_for(self, **kwargs):
        kwargs.setdefault("issues", {"12": "github_issue_closed.json", "13": "github_issue_open.json"})
        kwargs.setdefault("items", "github_project_items.json")
        return DispatchRun(**kwargs)

    def test_a_closed_issue_is_terminal_and_an_open_one_is_not(self):
        adapter = self.github(self.run_for())
        self.assertTrue(adapter.status("12")["terminal"])
        self.assertFalse(adapter.status("13")["terminal"])

    def test_candidates_come_from_the_project_item_list(self):
        run = self.run_for()
        found = self.github(run).candidates()
        self.assertEqual([entry["id"] for entry in found], ["12", "13"])
        self.assertEqual(found[0]["title"], "Add the brief renderer")
        self.assertIn("--owner", run.calls[0])
        self.assertIn("example-org", run.calls[0])

    def test_the_item_list_asks_for_more_than_the_thirty_gh_returns_by_default(self):
        run = self.run_for()
        self.github(run).candidates()
        call = run.calls[0]
        self.assertIn("--limit", call, "gh project item-list returns only 30 items unless told otherwise")
        self.assertGreater(int(call[call.index("--limit") + 1]), 30)

    def test_an_open_issue_whose_project_status_matches_the_manifest_field_is_terminal(self):
        run = self.run_for()
        adapter = gh_adapter.GitHubAdapter(self.github_manifest(status_field="Done"), run=run)
        self.assertTrue(adapter.status("13")["terminal"],
                        "the project status field named in the manifest was not consulted")
        self.assertEqual(adapter.status("13")["status"], "Done")

    def test_candidates_carry_the_project_status_of_each_item(self):
        found = {entry["id"]: entry for entry in self.github(self.run_for()).candidates()}
        self.assertEqual(found["13"]["status"], "Done")
        self.assertEqual(found["12"]["status"], "Todo")

    def test_closing_reference_finds_the_comment_naming_the_sha(self):
        adapter = self.github(self.run_for())
        self.assertEqual(adapter.closing_reference("12", "abc1234def"), "IC_2")
        self.assertIsNone(adapter.closing_reference("12", "0000000"))

    def test_comments_since_a_baseline_returns_the_newer_comment(self):
        adapter = self.github(self.run_for())
        newer = adapter.comments_since("12", "IC_1")
        self.assertEqual([entry["id"] for entry in newer], ["IC_2"])

    def test_comments_since_an_absent_baseline_is_unresolved_not_everything(self):
        adapter = self.github(self.run_for())
        self.assertEqual(adapter.comments_since("12", "IC_999"), [])

    def test_a_nonzero_gh_exit_is_skipped_with_the_stderr_text(self):
        adapter = self.github(self.run_for(failure=(1, "gh: not authenticated\n")))
        result = adapter.status("12")
        self.assertIn("not authenticated", result["skipped"])
        self.assertEqual(adapter.candidates(), [])

    def test_an_unreadable_project_board_is_skipped_rather_than_reported_not_terminal(self):
        """A board this adapter could not read and a card that has not moved both used to come
        back as `terminal: False, skipped: None`, which verify reads as a card that stayed put.
        The first is an unknown and has to reach verify as a blocking skip instead."""
        run = self.run_for(items_failure=(1, "gh: your token has no project scope\n"))
        adapter = gh_adapter.GitHubAdapter(self.github_manifest(status_field="Done"), run=run)
        result = adapter.status("13")
        self.assertIn("project board could not be read", result["skipped"])
        self.assertIn("no project scope", result["skipped"])
        self.assertFalse(result["terminal"])

    def test_a_closed_issue_is_terminal_even_when_the_board_cannot_be_read(self):
        """The board is not consulted for a closed issue, so an unreadable one changes nothing."""
        run = self.run_for(items_failure=(1, "gh: your token has no project scope\n"))
        adapter = gh_adapter.GitHubAdapter(self.github_manifest(status_field="Done"), run=run)
        result = adapter.status("12")
        self.assertTrue(result["terminal"])
        self.assertIsNone(result["skipped"])

    def test_an_issue_absent_from_the_board_is_not_terminal_and_is_not_skipped(self):
        """The other half of the same distinction: the board read worked and the card is simply
        not on it. That is a real answer, not an unknown."""
        run = self.run_for(issues={"99": "github_issue_open.json"})
        adapter = gh_adapter.GitHubAdapter(self.github_manifest(status_field="Done"), run=run)
        result = adapter.status("99")
        self.assertFalse(result["terminal"])
        self.assertIsNone(result["skipped"])

    def test_a_pr_create_command_does_not_match_the_write_patterns(self):
        from relay import classify

        patterns = self.github(self.run_for()).write_tool_patterns()
        pr_create = {"name": "Bash", "input": {"command": "gh pr create --fill"}}
        issue_close = {"name": "Bash", "input": {"command": "gh issue close 12 --comment landed"}}
        self.assertFalse(classify.matches_write_pattern(pr_create, patterns))
        self.assertTrue(classify.matches_write_pattern(issue_close, patterns))

    def test_every_gh_call_carries_the_thirty_second_timeout(self):
        run = self.run_for()
        self.github(run).status("12")
        self.assertTrue(run.calls)


class TripleGitHubSnapshot(AdapterCase):
    """The triple reader is deliberately outside the generic eight-method interface."""

    class Run:
        def __init__(self):
            self.calls = []

        def __call__(self, args, timeout=None):
            self.calls.append(list(args))
            if args[:4] == ["gh", "repo", "view", "--json"]:
                return _Proc(0, json.dumps({"id": "R_repo", "nameWithOwner": "example-org/relay"}), "")
            if args[:3] == ["gh", "project", "list"]:
                return _Proc(0, json.dumps([{"id": "PVT_board", "number": 4}]), "")
            if args[:3] == ["gh", "issue", "view"]:
                task_id = args[args.index("view") + 1]
                return _Proc(0, json.dumps({
                    "id": "I_" + task_id, "title": "Card " + task_id,
                    "body": "Body " + task_id, "state": "OPEN",
                    "comments": [{"id": "IC_base_" + task_id, "body": "baseline",
                                  "createdAt": "2026-09-18T00:00:00Z"}],
                }), "")
            if args[:3] == ["gh", "api", "graphql"]:
                query = next(value for value in args if value.startswith("query="))
                number = next(value.split("=", 1)[1] for value in args if value.startswith("number="))
                if "projectItems" in query:
                    payload = {"data": {"repository": {"issue": {"projectItems": {
                        "nodes": [{"id": "PVTI_" + number,
                                   "project": {"id": "PVT_board", "number": 4},
                                   "fieldValueByName": {"name": "Todo"}}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }}}}}
                else:
                    payload = {"data": {"repository": {"issue": {"comments": {
                        "nodes": [{"id": "IC_base_" + number, "body": "baseline",
                                   "createdAt": "2026-09-18T00:00:00Z"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }}}}}
                return _Proc(0, json.dumps(payload), "")
            raise AssertionError("unexpected gh call: %r" % (args,))

    def adapter(self):
        return gh_adapter.GitHubAdapter(self.github_manifest(), run=self.Run())

    def test_snapshot_reads_exact_declared_cards_without_the_board_list_ceiling(self):
        adapter = self.adapter()
        result = gh_adapter.read_triple_snapshot(adapter, ("12", "13", "501"))
        self.assertIsNone(result["reason"])
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["repository_id"], "R_repo")
        self.assertEqual(snapshot["project_id"], "PVT_board")
        self.assertEqual([card["item_id"] for card in snapshot["cards"]],
                         ["PVTI_12", "PVTI_13", "PVTI_501"])
        self.assertTrue(snapshot["digest"])
        self.assertFalse(any("item-list" in call for call in adapter._run.calls),
                         "triple allocation must not truncate at gh project item-list's ceiling")

    def test_snapshot_refuses_an_unreadable_declared_card(self):
        adapter = self.adapter()
        original = adapter._triple_card
        adapter._triple_card = lambda repository, project, task_id: (None, "no project scope") if task_id == "13" else original(repository, project, task_id)
        result = gh_adapter.read_triple_snapshot(adapter, ("12", "13", "14"))
        self.assertIsNone(result["snapshot"])
        self.assertIn("13 is unreadable", result["reason"])

    def test_expected_state_rejects_identity_and_mixed_foreign_comment_changes(self):
        card = gh_adapter.read_triple_snapshot(self.adapter(), ("12", "13", "14"))["snapshot"]["cards"][0]
        expected = gh_adapter.card_state(card)
        changed = copy.deepcopy(card)
        changed["item_id"] = "PVTI_replaced"
        evidence = gh_adapter.collision_evidence(expected, changed)
        self.assertEqual(evidence["kind"], "card_collision")
        self.assertIn("item_id", evidence["reason"])

        changed = copy.deepcopy(card)
        changed["comments"].append({"id": "IC_foreign", "body": "unrelated", "created": None})
        evidence = gh_adapter.collision_evidence(expected, changed)
        self.assertIn("comment", evidence["reason"])

    def test_task_and_closeout_capture_exact_comment_ids_and_expected_transitions(self):
        card = gh_adapter.read_triple_snapshot(self.adapter(), ("12", "13", "14"))["snapshot"]["cards"][0]
        expected = gh_adapter.card_state(card)
        task = copy.deepcopy(card)
        task["status"] = "In review"
        task["comments"].append({"id": "IC_task", "body": "head abc1234def", "created": None})
        expected, evidence = gh_adapter.capture_task_delta(expected, task, "In review", "abc1234def")
        self.assertIsNone(evidence)
        self.assertEqual(expected["phase"], "task")
        self.assertEqual(expected["comments"][-1]["id"], "IC_task")

        closeout = copy.deepcopy(task)
        closeout["issue_state"] = "CLOSED"
        closeout["comments"].append({"id": "IC_closeout", "body": "landed abc1234def", "created": None})
        expected, evidence = gh_adapter.capture_closeout_delta(expected, closeout, "landed",
                                                                 terminal_status="Done",
                                                                 landing_ref="abc1234def")
        self.assertIsNone(evidence)
        self.assertEqual(expected["phase"], "closeout")

    def test_valid_task_change_mixed_with_foreign_comment_is_collision_evidence(self):
        card = gh_adapter.read_triple_snapshot(self.adapter(), ("12", "13", "14"))["snapshot"]["cards"][0]
        task = copy.deepcopy(card)
        task["status"] = "In review"
        task["comments"].extend([
            {"id": "IC_task", "body": "head abc1234def", "created": None},
            {"id": "IC_foreign", "body": "also changed", "created": None},
        ])
        _, evidence = gh_adapter.capture_task_delta(gh_adapter.card_state(card), task,
                                                     "In review", "abc1234def")
        self.assertEqual(evidence["kind"], "card_collision")
        self.assertIn("missing, foreign", evidence["reason"])


class Markdown(AdapterCase):
    def test_a_closed_line_reads_closed_with_its_reference(self):
        result = self.markdown().status("T-2")
        self.assertEqual(result["status"], "closed")
        self.assertTrue(result["terminal"])
        self.assertEqual(result["reference"], "abc1234")

    def test_an_open_line_is_not_terminal(self):
        result = self.markdown().status("T-1")
        self.assertEqual(result["status"], "open")
        self.assertFalse(result["terminal"])
        self.assertIsNone(result["reference"])

    def test_comments_are_the_indented_lines_under_the_task(self):
        adapter = self.markdown()
        self.assertEqual(len(adapter.comments_since("T-1", None)), 2)
        newer = adapter.comments_since("T-1", 1)
        self.assertEqual(len(newer), 1)
        self.assertIn("still going", newer[0]["body"])

    def test_a_task_with_no_comments_returns_an_empty_list(self):
        self.assertEqual(self.markdown().comments_since("T-3", None), [])

    def test_the_file_is_read_at_the_remote_default_branch_not_the_working_tree(self):
        with open(os.path.join(self.repo, "tracker.md"), "w") as handle:
            handle.write("- [x] T-1 Add the brief renderer (deadbeef)\n")
        self.assertFalse(self.markdown().status("T-1")["terminal"],
                         "the adapter read the working tree instead of origin/main")

    def test_closing_reference_matches_the_reference_on_the_closed_line(self):
        adapter = self.markdown()
        self.assertEqual(adapter.closing_reference("T-2", "abc1234def"), "T-2")
        self.assertIsNone(adapter.closing_reference("T-2", "9999999"))
        self.assertIsNone(adapter.closing_reference("T-1", "abc1234def"))

    def test_candidates_are_the_open_lines(self):
        found = self.markdown().candidates()
        self.assertEqual([entry["id"] for entry in found], ["T-1", "T-3"])

    def test_the_write_patterns_name_the_tracker_file_for_edit_and_write(self):
        from relay import classify

        patterns = self.markdown().write_tool_patterns()
        edit = {"name": "Edit", "input": {"file_path": os.path.join(self.repo, "tracker.md")}}
        other = {"name": "Edit", "input": {"file_path": os.path.join(self.repo, "src/x.py")}}
        self.assertTrue(classify.matches_write_pattern(edit, patterns))
        self.assertFalse(classify.matches_write_pattern(other, patterns))

    def test_a_missing_tracker_file_is_skipped_rather_than_a_crash(self):
        _repo.git(self.repo, "rm", "-q", "tracker.md")
        _repo.git(self.repo, "commit", "-q", "-m", "drop the tracker")
        _repo.git(self.repo, "push", "-q", "origin", "main")
        result = self.markdown().status("T-1")
        self.assertIsNotNone(result["skipped"])
        self.assertEqual(self.markdown().candidates(), [])


class MarkdownWithoutPush(AdapterCase):
    """Issue #15. Under shipping.push = false the closeout's commit never reaches origin, so the
    adapter reads the local default branch head, and still never the working tree."""

    def no_push(self):
        text = self.toml.replace('mode = "local_merge"', 'mode = "local_merge"\npush = false')
        return md_adapter.MarkdownAdapter(self.manifest(text, name="no-push.toml"))

    def close_t1(self, commit=True):
        path = os.path.join(self.repo, "tracker.md")
        with open(path) as handle:
            text = handle.read()
        with open(path, "w") as handle:
            handle.write(re.sub(r"^- \[ \] T-1 (.*)$", r"- [x] T-1 \1 (cafe123)", text,
                                count=1, flags=re.M))
        if commit:
            _repo.git(self.repo, "commit", "-qam", "close T-1 locally")

    def test_the_file_is_read_at_the_local_default_branch_head(self):
        self.close_t1()
        self.assertTrue(self.no_push().status("T-1")["terminal"])
        self.assertFalse(self.markdown().status("T-1")["terminal"],
                         "push true must still read origin/main")

    def test_the_working_tree_is_still_not_read(self):
        self.close_t1(commit=False)
        self.assertFalse(self.no_push().status("T-1")["terminal"])

    def test_the_landed_instruction_does_not_promise_a_push(self):
        local = self.no_push().closeout_instructions(md_adapter.OUTCOME_LANDED)
        self.assertIn("do not push", local)
        self.assertNotIn("runner pushes", local)
        self.assertIn("runner pushes",
                      self.markdown().closeout_instructions(md_adapter.OUTCOME_LANDED))


class Factory(AdapterCase):
    def test_the_manifest_adapter_name_selects_the_implementation(self):
        self.assertIsInstance(adapters.build(self.manifest()), md_adapter.MarkdownAdapter)
        self.assertIsInstance(
            adapters.build(self.jira_manifest(), env={"JIRA_API_TOKEN": "t", "JIRA_EMAIL": "e@x.invalid"},
                           opener=FakeOpener({})),
            jira_adapter.JiraAdapter)
        self.assertIsInstance(adapters.build(self.github_manifest(), run=DispatchRun()),
                              gh_adapter.GitHubAdapter)

    def test_an_unknown_adapter_name_is_a_configuration_error(self):
        text = self.toml.replace('adapter = "markdown"', 'adapter = "trello"')
        with self.assertRaises(adapters.ConfigurationError):
            adapters.build(self.manifest(text, name="bad.toml"))


if __name__ == "__main__":
    unittest.main()
