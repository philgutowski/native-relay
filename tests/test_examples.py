"""U11: the shipped skill and the example manifests.

Two things are proved here. Every example validates against a real temp repo, so a fresh clone
can run `relay validate` on one without editing Relay (R38). And nothing in what ships names a
real project, tracker site, or person (R40).
"""
import ast
import contextlib
import dataclasses
import glob
import json
import os
import re
import unittest

import _paths
import _repo
from relay import adapters, cli, feeder, manifest as mf, testbrief, testloop
from test_adapters import DispatchRun, FakeOpener

REPO_ROOT = _paths.REPO_ROOT
EXAMPLES = os.path.join(REPO_ROOT, "docs", "examples")
SKILL = os.path.join(REPO_ROOT, "skills", "relay", "SKILL.md")

# Every verb the plan's runner subcommand table names.
VERBS = ("validate", "run", "status", "tail", "summary", "audit", "verify", "lease",
         "pair", "dispatch", "feed", "test")

# What must never appear in anything Relay ships (R40). These are the shapes a real project
# leaks in: a Jira key, the operator's own repo, a live Atlassian site, and the operator's own
# account and workspace names. The planning ladder and the solutions store are history and are
# deliberately outside SHIPPED.
LEAK_PATTERNS = (
    r"IW-[0-9]+",
    r"support-workbench",
    r"\b(?!example\.)[a-z0-9-]+\.atlassian\.net/[a-z]",
    r"pgutowski",
    r"PhilAI",
)
SHIPPED = ("skills", "docs/examples", "README.md", "CONCEPTS.md", "CLAUDE.md", ".claude-plugin",
           "tests")


def example_paths():
    return sorted(glob.glob(os.path.join(EXAMPLES, "*.toml")))


REAL_BUILD = adapters.build  # held before the test patches the name it lives under


def offline_build(manifest, env=None):
    """The real adapter factory over fixture transports. `relay validate` reads every card, and
    the factory's defaults are the real `urllib` opener and the real `gh`, so a test that drives
    the verb without this reaches `example.atlassian.net` and GitHub on every run."""
    issues = {task.id: "github_issue_open.json" for task in manifest.tasks}
    return REAL_BUILD(manifest, env=env,
                      opener=FakeOpener({"/issue/": "jira_issue_open.json"}),
                      run=DispatchRun(issues=issues, items="github_project_items.json"))


class Examples(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        # The markdown example names `tasks.md` and lists T-1 and T-2, so the repo carries both.
        self.repo = _repo.make_repo(self.tmp.name, files={
            "tasks.md": "- [ ] T-1 A task\n- [ ] T-2 Another task\n"})

    def tearDown(self):
        self.tmp.cleanup()

    def localised(self, path):
        """Point an example at the temp repo, the way an operator points it at their own."""
        with open(path) as handle:
            text = handle.read()
        text = re.sub(r'^repo = ".*"$', 'repo = "%s"' % self.repo, text, count=1, flags=re.M)
        target = os.path.join(self.tmp.name, os.path.basename(path))
        with open(target, "w") as handle:
            handle.write(text)
        return target

    def test_three_examples_ship_one_per_adapter(self):
        adapters = set()
        for path in example_paths():
            adapters.add(mf.load(path).tracker.adapter)
        self.assertEqual(adapters, {"jira", "github", "markdown"})

    def test_every_example_loads_and_validates_against_a_real_repo(self):
        env = {"JIRA_API_TOKEN": "placeholder", "JIRA_EMAIL": "placeholder@example.invalid"}
        for path in example_paths():
            with self.subTest(example=os.path.basename(path)):
                manifest = mf.load(self.localised(path))
                result = mf.validate(manifest, env=dict(os.environ, **env))
                self.assertEqual(result.errors, [], os.path.basename(path))

    def test_every_example_names_its_four_qualifying_satisfiers(self):
        for path in example_paths():
            manifest = mf.load(path)
            for key in mf.QUALIFYING_KEYS:
                self.assertTrue(getattr(manifest.qualifying, key).strip(),
                                "%s has no %s satisfier" % (os.path.basename(path), key))

    def test_every_example_gate_and_mirror_are_argument_lists(self):
        for path in example_paths():
            manifest = mf.load(path)
            self.assertIsInstance(manifest.gate.command, tuple, os.path.basename(path))
            self.assertIsInstance(manifest.project.mirror, tuple, os.path.basename(path))

    def test_no_example_names_bypass_permissions_or_a_permission_mode(self):
        for path in example_paths():
            with open(path) as handle:
                text = handle.read()
            self.assertNotIn("bypassPermissions", text)
            self.assertNotIn("permission_mode", text)

    def test_the_validate_verb_accepts_every_example_through_the_cli(self):
        import io
        from unittest import mock

        env = dict(os.environ, JIRA_API_TOKEN="placeholder", JIRA_EMAIL="p@example.invalid")
        for path in example_paths():
            with self.subTest(example=os.path.basename(path)):
                out = io.StringIO()
                with mock.patch.object(cli.adapters, "build", offline_build):
                    code = cli.main(["validate", self.localised(path)], env=env, out=out)
                self.assertEqual(code, cli.EXIT_OK, out.getvalue())
                # An unreadable card is only a warning, which is how this test once passed while
                # every tracker read in it was failing against a live site.
                self.assertNotIn("could not be read", out.getvalue())


class FeederExamples(unittest.TestCase):
    """The example sidecar says every value in it is the default unless it is marked as an
    example, so a default that moves in code has to move in the file a new operator copies."""

    STEM = os.path.join(EXAMPLES, "feeder", "manifest-github-projects")

    def test_the_example_sidecar_loads_and_its_settings_are_the_defaults(self):
        config = feeder.load_config(self.STEM + ".feeder.toml")
        defaults = feeder.Config()
        for name in ("batch", "max_halts", "caffeinate", "quick_death_seconds",
                     "limit_wait_seconds", "limit_waits_max", "idle_wait_seconds",
                     "idle_waits_max", "lease_wait_seconds", "default_model", "default_effort",
                     "allowed_models"):
            self.assertEqual(getattr(config, name), getattr(defaults, name), name)
        self.assertEqual(config.ready_source, {"labels": ["ready"]})
        self.assertEqual(config.denied_labels, ("attended",))

    def test_the_example_sidecar_is_named_the_way_the_feeder_derives_it(self):
        paths = feeder.paths_for(self.STEM + ".toml")
        for path in (paths.config, paths.order, paths.routing):
            self.assertTrue(os.path.isfile(path), path)

    def test_the_example_order_and_routing_files_parse(self):
        with open(self.STEM + ".order") as handle:
            self.assertEqual(feeder.read_order(handle.read()), {"12": 0, "14": 1, "13": 2})
        with open(self.STEM + ".models") as handle:
            chosen, notes = feeder.read_routing(handle.read(), feeder.Config().allowed_models)
        self.assertEqual((chosen, notes), ({"12": "fable"}, []))


LOOP_EXAMPLE = os.path.join(EXAMPLES, "browser-test-loop")
AUTHORING = os.path.join(REPO_ROOT, "docs", "manifest-authoring.md")
CONCEPTS = os.path.join(REPO_ROOT, "CONCEPTS.md")
README = os.path.join(REPO_ROOT, "README.md")
STOP_REASONS = (testloop.STOP_CLEAN, testloop.STOP_OPEN_FINDINGS, testloop.STOP_ROUNDS,
                testloop.STOP_CLOCK, testloop.STOP_BUDGET, testloop.STOP_REPORT_ONLY)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@contextlib.contextmanager
def tempfile_path(content=None):
    """A path in a fresh temporary directory, holding `content` when it is given."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        if content is not None:
            with open(path, "w") as handle:
                handle.write(content)
        yield path


def toml_value(value):
    """A `TestLoop` default as the documentation's TOML block spells it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, tuple):
        return json.dumps(list(value))
    return json.dumps(value)


class BrowserTestLoopExample(unittest.TestCase):
    """Browser test loop plan, U8: the shipped example and the documentation (R9, R25)."""

    def test_the_example_ships_its_four_files(self):
        for name in ("README.md", "example.feeder.toml", "tour-template.md", "drive.py"):
            self.assertTrue(os.path.isfile(os.path.join(LOOP_EXAMPLE, name)), name)

    def test_the_example_sidecar_loads_with_the_loop_on(self):
        config = feeder.load_config(os.path.join(LOOP_EXAMPLE, "example.feeder.toml"))
        loop = config.test_loop
        self.assertTrue(loop.enabled)
        self.assertFalse(loop.report_only)
        for need in ("tour", "url", "prepare"):
            self.assertTrue(getattr(loop, need), need)
        self.assertIsInstance(loop.prepare, tuple)
        # The GitHub rule `relay test` enforces: every ready label is also a loop label, and
        # the loop's planning cards stay out of every batch.
        for label in config.ready_source["labels"]:
            self.assertIn(label, loop.labels)
        self.assertIn("attended", config.denied_labels)
        self.assertIn(loop.design_model, config.allowed_models)

    def test_relay_test_accepts_the_example_sidecar_beside_the_github_example(self):
        """The pass's own refusal is the authority on what a loop sidecar needs, so a rule it
        gains reaches the shipped example here rather than at an operator's first pass."""
        import tempfile

        from relay import testpass

        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo.make_repo(tmp)
            with open(os.path.join(EXAMPLES, "manifest-github-projects.toml")) as handle:
                text = handle.read()
            text = re.sub(r'^repo = ".*"$', 'repo = "%s"' % repo, text, count=1, flags=re.M)
            path = os.path.join(tmp, "example.toml")
            with open(path, "w") as handle:
                handle.write(text)
            manifest = mf.load(path)
            config = feeder.load_config(os.path.join(LOOP_EXAMPLE, "example.feeder.toml"))
            for request in (testpass.Request(kind=testloop.TOUR),
                            testpass.Request(kind=testloop.TOUR, report_only=True)):
                self.assertIsNone(testpass.refusal(manifest, config, request))

    def test_the_driver_parses_its_steps_and_keeps_goto_on_the_app(self):
        import argparse
        import types

        # Compiled from its source rather than imported, so no bytecode cache is written into
        # the shipped example folder.
        path = os.path.join(LOOP_EXAMPLE, "drive.py")
        drive = types.ModuleType("example_drive")
        drive.__file__ = path
        exec(compile(read(path), path, "exec"), drive.__dict__)
        # An attribute selector keeps its `=`: the fill separator is `=>`.
        self.assertEqual(drive.parse_step("fill:input[name=q]=>lamp"),
                         ("fill", "input[name=q]=>lamp"))
        self.assertEqual(drive.parse_step("click:#send"), ("click", "#send"))
        self.assertEqual(drive.parse_step("sleep:250"), ("sleep", "250"))
        for bad in ("fill:input[name=q]", "fill:=>lamp", "sleep:soon", "hover:#x", "click:",
                    "nocolon"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                drive.parse_step(bad)
        self.assertTrue(drive.same_host("http://127.0.0.1:8765/a", "http://127.0.0.1:8765/b"))
        self.assertFalse(drive.same_host("http://127.0.0.1:8765/a", "https://example.com/b"))
        with tempfile_path() as missing:
            self.assertIn("does not exist", drive.read_state(missing))
        with tempfile_path('{"cookies": [], "origins": []}') as good:
            self.assertIsNone(drive.read_state(good))
        with tempfile_path("not json") as bad:
            self.assertIn("could not be read", drive.read_state(bad))

    def test_the_example_sidecar_writes_every_key_and_its_caps_are_the_defaults(self):
        """A default that moves in code has to move in the file an operator copies."""
        text = read(os.path.join(LOOP_EXAMPLE, "example.feeder.toml"))
        loaded = feeder.load_config(os.path.join(LOOP_EXAMPLE, "example.feeder.toml")).test_loop
        defaults = feeder.TestLoop()
        for spec in dataclasses.fields(feeder.TestLoop):
            self.assertRegex(text, r"(?m)^%s = " % spec.name, spec.name)
        for name in ("prepare_timeout_seconds", "model", "effort", "timeout_minutes",
                     "max_rounds", "max_hours", "max_cards_per_pass", "max_patches_per_area",
                     "max_cards_total", "allowed_tools", "report_only"):
            self.assertEqual(getattr(loaded, name), getattr(defaults, name), name)

    def test_the_documentation_names_every_test_loop_key_with_its_default(self):
        text = read(AUTHORING)
        section = text[text.index("## 12. The browser test loop"):text.index("## 13.")]
        for spec in dataclasses.fields(feeder.TestLoop):
            default = getattr(feeder.TestLoop(), spec.name)
            self.assertRegex(section, r"(?m)^%s = %s(\s|$)" % (
                re.escape(spec.name), re.escape(toml_value(default))), spec.name)
            self.assertIn("`%s`" % spec.name, section, spec.name)

    def test_the_documentation_names_every_stop_reason(self):
        for path in (AUTHORING, os.path.join(LOOP_EXAMPLE, "README.md")):
            text = read(path)
            for reason in STOP_REASONS:
                self.assertIn("`%s`" % reason, text, "%s lacks %s" % (path, reason))

    def test_the_documentation_carries_the_ready_source_sign_in_and_outbound_rules(self):
        for path in (AUTHORING, os.path.join(LOOP_EXAMPLE, "README.md"), SKILL):
            text = read(path)
            with self.subTest(path=os.path.relpath(path, REPO_ROOT)):
                self.assertRegex(text, r"(?i)ready\s+source\s+must\s+admit\s+a\s+card\s+carrying")
                self.assertRegex(text, r"(?i)outbound\s+integrations")
                self.assertRegex(text, r"(?i)storage\s+state\s+file")
                self.assertRegex(text, r"(?i)never\s+type")

    def test_concepts_defines_the_loop_vocabulary(self):
        text = read(CONCEPTS)
        for term in ("Test pass", "Test process", "Filing process", "Tour document",
                     "Generation"):
            self.assertRegex(text, r"(?m)^### %s$" % term, term)

    def test_the_readme_and_the_skill_point_at_the_section_and_the_example(self):
        for path in (README, SKILL):
            text = read(path)
            self.assertIn("[test_loop]", text, path)
            self.assertRegex(text, r"[Ss]ection 12 of\s+`docs/manifest-authoring.md`", path)
            self.assertIn("docs/examples/browser-test-loop/", text, path)

    def test_the_tour_template_opens_without_a_heading_and_lists_its_areas(self):
        text = read(os.path.join(LOOP_EXAMPLE, "tour-template.md"))
        self.assertFalse(text.lstrip().startswith("#"))
        areas = testbrief.headings(text)
        self.assertEqual(areas, ("Search", "Orders", "Settings"))
        self.assertEqual(text.count("Approval steps not to pass"), len(areas))
        opening = text[:text.index("## ")]
        for fact in ("drive.py", "storage state file", "--signin-marker"):
            self.assertIn(fact, opening)

    def test_the_driver_is_never_imported_by_the_runner(self):
        scripts = os.path.join(REPO_ROOT, "skills", "relay", "scripts")
        for root, _, names in os.walk(scripts):
            for name in names:
                if not name.endswith(".py"):
                    continue
                text = read(os.path.join(root, name))
                self.assertNotRegex(text, r"(?m)^\s*(import|from)\s+drive\b", name)
                self.assertNotIn("browser-test-loop", text, name)

    def test_the_driver_parses_and_imports_its_browser_package_only_when_it_runs(self):
        """The runner stays standard library only, and so does the driver's own `--help`."""
        tree = ast.parse(read(os.path.join(LOOP_EXAMPLE, "drive.py")))
        for node in tree.body:
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name.startswith("playwright")
                                     for alias in node.names))
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("playwright"))
        functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertTrue({"signin", "visit", "main"} <= functions)

    def test_the_loop_documentation_uses_no_dashes(self):
        paths = [AUTHORING, CONCEPTS, README, SKILL] + sorted(
            path for path in glob.glob(os.path.join(LOOP_EXAMPLE, "*")) if os.path.isfile(path))
        for path in paths:
            text = read(path)
            for dash in ("–", "—"):
                self.assertNotIn(dash, text, os.path.relpath(path, REPO_ROOT))
            self.assertIsNone(re.search(r"\w -{1,2} \w", text), os.path.relpath(path, REPO_ROOT))


class Skill(unittest.TestCase):
    def setUp(self):
        with open(SKILL) as handle:
            self.text = handle.read()

    def test_the_runner_script_is_resolved_from_the_skill_directory(self):
        self.assertIn("scripts/relay_cli.py", self.text)
        self.assertRegex(self.text, r"(?i)this skill's (own )?directory")

    def test_every_runner_verb_appears_with_an_invocation(self):
        """The skill resolves the script path once and then invokes it as <runner>, so an
        invocation is a line running <runner> with the verb."""
        for verb in VERBS:
            self.assertRegex(self.text, r"(?m)^python3 <runner> %s\b" % verb,
                             "SKILL.md has no invocation for the %s verb" % verb)

    def test_the_skill_launches_detached_the_way_ktd14_names(self):
        for fragment in ("setsid", "caffeinate", "runner.log"):
            self.assertIn(fragment, self.text)
        self.assertRegex(self.text, r"(?i)lid close")

    def test_the_skill_refuses_to_launch_without_the_four_qualifying_satisfiers(self):
        for key in mf.QUALIFYING_KEYS:
            self.assertIn("qualifying.%s" % key, self.text)
        self.assertRegex(self.text, r"(?i)refuse")

    def test_the_skill_diagnoses_from_state_rather_than_from_a_transcript(self):
        self.assertNotRegex(self.text, r"(?i)read the transcript")
        self.assertRegex(self.text, r"(?i)do not open a session transcript")
        self.assertRegex(self.text, r"(?m)^python3 <runner> summary <manifest> --json")

    def test_the_skill_names_no_permission_mode_but_dont_ask(self):
        self.assertNotIn("bypassPermissions", self.text)

    def test_the_skill_points_at_the_backend_rubric_in_its_own_directory(self):
        self.assertIn("references/backend-rubric.md", self.text)
        self.assertRegex(self.text, r"(?i)do not write a backend the operator")
        self.assertRegex(self.text, r"(?i)nothing re-applies the rubric")
        self.assertRegex(self.text, r"(?i)unenforced_acceptance")
        self.assertRegex(self.text, r"(?i)never invent")
        with open(os.path.join(REPO_ROOT, "skills", "relay", "references", "backend-rubric.md")) as handle:
            rubric = handle.read()
        for name in ("claude", "codex", "grok"):
            self.assertIn("`%s`" % name, rubric)
        self.assertRegex(rubric, r"(?i)detects rather than prevents")
        self.assertRegex(rubric, r"(?i)commit scope")

    def test_the_skill_still_carries_its_frontmatter_name_and_description(self):
        self.assertRegex(self.text, r"(?m)^name: relay$")
        self.assertRegex(self.text, r"(?m)^description: .{40,}$")
        self.assertNotRegex(self.text, r"(?i)stub")

    def test_the_skill_asks_whether_the_run_pushes_and_never_runs_the_push_itself(self):
        """Issue #15. The skill authors manifests, so it has to ask about shipping.push rather
        than leave the default to decide, and a push false run's closing command is the
        operator's to run."""
        self.assertIn("shipping.push", self.text)
        self.assertRegex(self.text, r"(?i)ask whether the\s+runner pushes")
        self.assertRegex(self.text, r"(?i)whether the run pushes")
        self.assertRegex(self.text, r"(?i)never this skill's")


class NoProjectLeakage(unittest.TestCase):
    def shipped_files(self):
        for entry in SHIPPED:
            path = os.path.join(REPO_ROOT, entry)
            if os.path.isfile(path):
                yield path
                continue
            for root, _, names in os.walk(path):
                if "__pycache__" in root:
                    continue
                for name in names:
                    full = os.path.join(root, name)
                    # This file carries the patterns themselves.
                    if os.path.samefile(full, __file__):
                        continue
                    yield full

    def test_nothing_shipped_names_a_real_project_tracker_or_person(self):
        for path in self.shipped_files():
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            for pattern in LEAK_PATTERNS:
                found = re.search(pattern, text)
                self.assertIsNone(found, "%s names %r" % (os.path.relpath(path, REPO_ROOT),
                                                          found.group(0) if found else ""))


class Readme(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO_ROOT, "README.md")) as handle:
            self.text = handle.read()

    def test_the_readme_tells_a_fresh_machine_how_to_install_and_validate(self):
        self.assertRegex(self.text, r"(?i)install")
        self.assertIn("relay_cli.py", self.text)
        self.assertIn("docs/examples/", self.text)

    def test_the_readme_points_at_the_plan_and_the_vocabulary(self):
        self.assertIn("docs/plans/2026-08-25-1346-feat-relay-outer-loop-plan.md", self.text)
        self.assertIn("CONCEPTS.md", self.text)


if __name__ == "__main__":
    unittest.main()
