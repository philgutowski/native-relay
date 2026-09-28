"""U2 of the browser test loop plan: the sidecar's `[test_loop]` table, read by `load_config`
with the defaults the plan names, and refused key by key the way every other sidecar key is."""
import os
import tempfile
import unittest

import _paths
from relay import feeder, testloop

FULL = """\
[models]
allowed = ["fable", "opus", "sonnet"]
[test_loop]
enabled = true
report_only = true
tour = "docs/tour.md"
url = "http://127.0.0.1:5173"
prepare = ["scripts/serve-at.sh", "--restart"]
prepare_timeout_seconds = 300
model = "sonnet"
effort = "medium"
timeout_minutes = 45
max_rounds = 4
max_hours = 12
max_cards_per_pass = 5
max_patches_per_area = 2
max_cards_total = 20
labels = ["test-loop", "ready"]
allowed_tools = ["Bash", "Read"]
design_model = "fable"
design_note = "Build on the design skill the project names."
"""

# The keys a loop that is on cannot run without, as a table that has them all.
ON = ('[test_loop]\nenabled = true\ntour = "docs/tour.md"\nurl = "http://127.0.0.1:5173"\n'
      'prepare = ["scripts/serve-at.sh"]\n')


class TestLoopConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "run.feeder.toml")

    def load(self, text):
        with open(self.path, "w") as handle:
            handle.write(text)
        return feeder.load_config(self.path)

    def refused(self, text):
        with self.assertRaises(feeder.ConfigError) as caught:
            self.load(text)
        return str(caught.exception)

    def test_no_test_loop_table_loads_as_today(self):
        # Covers AE8: the loop's default is part of the default Config, so nothing else moves.
        text = '[feeder]\nbatch = 5\n[models]\ndefault = "sonnet"\n'
        config = self.load(text)
        self.assertEqual(config, feeder.Config(batch=5, default_model="sonnet"))
        self.assertEqual(config.test_loop, feeder.TestLoop())
        self.assertFalse(config.test_loop.enabled)
        self.assertEqual(feeder.load_config(os.path.join(self.tmp.name, "absent.toml")),
                         feeder.Config())

    def test_the_defaults_are_the_plans(self):
        loop = feeder.TestLoop()
        self.assertEqual(
            (loop.enabled, loop.report_only, loop.tour, loop.url, loop.prepare,
             loop.prepare_timeout_seconds, loop.model, loop.effort, loop.timeout_minutes,
             loop.max_rounds, loop.max_hours, loop.max_cards_per_pass,
             loop.max_patches_per_area, loop.max_cards_total, loop.labels, loop.allowed_tools,
             loop.design_model, loop.design_note),
            (False, False, "", "", (), 600, "", "", 60, 6, 24, 10, 3, 30, (),
             ("Bash", "Read", "Grep", "Glob"), "", ""))

    def test_an_empty_table_is_the_default(self):
        self.assertEqual(self.load("[test_loop]\n"), feeder.Config())

    def test_a_full_table_loads_every_value(self):
        loop = self.load(FULL).test_loop
        self.assertEqual(loop, feeder.TestLoop(
            enabled=True, report_only=True, tour="docs/tour.md", url="http://127.0.0.1:5173",
            prepare=("scripts/serve-at.sh", "--restart"), prepare_timeout_seconds=300,
            model="sonnet", effort="medium", timeout_minutes=45, max_rounds=4, max_hours=12,
            max_cards_per_pass=5, max_patches_per_area=2, max_cards_total=20,
            labels=("test-loop", "ready"), allowed_tools=("Bash", "Read"),
            design_model="fable", design_note="Build on the design skill the project names."))

    def test_model_and_effort_default_to_the_models_values(self):
        config = self.load('[models]\ndefault = "sonnet"\neffort = "low"\n' + ON)
        self.assertEqual((config.test_loop.model, config.test_loop.effort), ("", ""))
        self.assertEqual((config.test_model, config.test_effort), ("sonnet", "low"))
        config = self.load(FULL)
        self.assertEqual((config.test_model, config.test_effort), ("sonnet", "medium"))

    def test_a_loop_that_is_on_with_what_it_needs_loads(self):
        loop = self.load(ON).test_loop
        self.assertTrue(loop.enabled)
        self.assertEqual(loop.prepare, ("scripts/serve-at.sh",))

    def test_a_misspelt_key_is_not_a_feeder_setting(self):
        message = self.refused("[test_loop]\nenable = true\n")
        self.assertIn("test_loop.enable is not a feeder setting", message)

    def test_enabled_without_prepare_is_refused_naming_prepare(self):
        message = self.refused(ON.replace('prepare = ["scripts/serve-at.sh"]\n', ""))
        self.assertIn("test_loop.enabled needs test_loop.prepare", message)
        self.assertNotIn("test_loop.tour", message)
        self.assertNotIn("test_loop.url", message)

    def test_enabled_with_nothing_names_every_missing_key(self):
        message = self.refused("[test_loop]\nenabled = true\nprepare = []\nurl = \"\"\n")
        for need in ("tour", "url", "prepare"):
            self.assertIn("test_loop.enabled needs test_loop.%s" % need, message)

    def test_a_loop_that_is_off_needs_nothing(self):
        self.assertFalse(self.load("[test_loop]\nreport_only = true\n").test_loop.enabled)

    def test_prepare_as_a_shell_string_is_refused(self):
        message = self.refused(ON.replace('["scripts/serve-at.sh"]', '"make serve"'))
        self.assertIn("test_loop.prepare must be an array of strings (an argument list, never a "
                      "shell string)", message)
        # Named once, for its shape, not again as missing.
        self.assertNotIn("needs test_loop.prepare", message)

    def test_a_design_model_outside_the_allowed_set_is_refused(self):
        message = self.refused('[test_loop]\ndesign_model = "haiku"\n')
        self.assertIn("test_loop.design_model 'haiku' is not in models.allowed", message)
        self.assertEqual(self.load('[models]\nallowed = ["opus", "haiku"]\n'
                                   '[test_loop]\ndesign_model = "haiku"\n').test_loop.design_model,
                         "haiku")

    def test_a_loop_model_outside_the_allowed_set_is_refused(self):
        message = self.refused('[test_loop]\nmodel = "haiku"\n')
        self.assertIn("test_loop.model 'haiku' is not in models.allowed", message)

    def test_a_count_that_is_not_a_positive_integer_is_refused(self):
        message = self.refused("[test_loop]\nmax_cards_per_pass = 0\n")
        self.assertIn("test_loop.max_cards_per_pass must be a positive integer", message)
        for key in ("prepare_timeout_seconds", "timeout_minutes", "max_rounds", "max_hours",
                    "max_patches_per_area", "max_cards_total"):
            for bad in ("-1", "true", '"6"', "1.5"):
                message = self.refused("[test_loop]\n%s = %s\n" % (key, bad))
                self.assertIn("test_loop.%s must be a positive integer" % key, message)

    def test_labels_as_a_string_is_refused(self):
        message = self.refused('[test_loop]\nlabels = "loop"\n')
        self.assertIn("test_loop.labels must be an array of strings", message)
        self.assertNotIn("argument list", message)

    def test_allowed_tools_must_be_a_non_empty_array_of_strings(self):
        self.assertIn("test_loop.allowed_tools must be an array of strings",
                      self.refused('[test_loop]\nallowed_tools = "Bash"\n'))
        self.assertIn("test_loop.allowed_tools must name at least one tool",
                      self.refused("[test_loop]\nallowed_tools = []\n"))

    def test_a_switch_that_is_not_a_boolean_is_refused(self):
        for key in ("enabled", "report_only"):
            message = self.refused('[test_loop]\n%s = "yes"\n' % key)
            self.assertIn("test_loop.%s must be true or false" % key, message)

    def test_a_string_setting_given_another_type_is_refused(self):
        for key in ("tour", "url", "model", "effort", "design_model", "design_note"):
            message = self.refused("[test_loop]\n%s = 3\n" % key)
            self.assertIn("test_loop.%s must be a string" % key, message)

    def test_a_tour_outside_the_target_repository_is_refused(self):
        for bad in ("/tmp/tour.md", "../other/tour.md", "docs/../../tour.md", "~/tour.md"):
            message = self.refused(ON.replace('"docs/tour.md"', '"%s"' % bad))
            self.assertIn("test_loop.tour must be a path relative to the target repository",
                          message)
        self.assertEqual(self.load(ON.replace('"docs/tour.md"', '"docs/../tour.md"'))
                         .test_loop.tour, "docs/../tour.md")

    def test_a_url_without_an_http_scheme_and_host_is_refused(self):
        for bad in ("localhost:5173", "http//x", "file:///tmp/app.html", "https://"):
            message = self.refused(ON.replace('"http://127.0.0.1:5173"', '"%s"' % bad))
            self.assertIn("test_loop.url must be an http or https URL with a host", message)

    def test_blank_strings_are_refused_where_they_would_mean_nothing(self):
        for key in ("model", "effort", "design_model", "tour", "url"):
            self.assertIn("test_loop.%s must not be blank" % key,
                          self.refused('[test_loop]\n%s = " "\n' % key))
        self.assertIn("test_loop.prepare must start with the program to run",
                      self.refused('[test_loop]\nprepare = ["", "--restart"]\n'))
        for key in ("labels", "allowed_tools"):
            self.assertIn("test_loop.%s must not hold a blank string" % key,
                          self.refused('[test_loop]\n%s = ["Read", " "]\n' % key))
        self.assertEqual(self.load('[test_loop]\ndesign_note = " "\n').test_loop.design_note, " ")

    def test_the_cap_defaults_are_the_rules_own(self):
        loop, settings = feeder.TestLoop(), testloop.Settings()
        for name in ("max_rounds", "max_hours", "max_cards_per_pass", "max_patches_per_area",
                     "max_cards_total"):
            self.assertEqual(getattr(loop, name), getattr(settings, name))

    def test_test_loop_that_is_not_a_table_is_refused(self):
        message = self.refused("test_loop = true\n")
        self.assertIn("[test_loop] is not a feeder table", message)

    def test_loop_problems_are_named_with_the_other_sidecar_problems(self):
        message = self.refused("[feeder]\nbatch = 0\n[test_loop]\nmax_rounds = 0\n"
                               'labels = "loop"\nmodel = "haiku"\n')
        for expected in ("batch must be a positive integer",
                         "test_loop.max_rounds must be a positive integer",
                         "test_loop.labels must be an array of strings",
                         "test_loop.model 'haiku' is not in models.allowed"):
            self.assertIn(expected, message)

    def test_a_wrong_models_allowed_is_named_once_and_skips_the_loop_model_check(self):
        message = self.refused('[models]\nallowed = "opus"\n[test_loop]\nmodel = "haiku"\n')
        self.assertIn("allowed_models must be an array of strings", message)
        self.assertNotIn("test_loop.model", message)


if __name__ == "__main__":
    unittest.main()
