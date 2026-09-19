"""Feeder plan, KTD2: the manifest edits code makes, tested as text in, text out.

The feeder is the first runner code that writes a manifest, so each edit is pinned here apart
from the loop that calls it: what an append and an exclusion produce, that both leave every
hand written line alone, and that `commit` never lets an edit that fails validation reach the
file.
"""
import os
import stat
import tomllib
import unittest

import _paths
from relay import manifest as mf, manifestedit as me
from test_run import MANIFEST, RunCase

STAMP = "2026-09-19 08:50"


def entry(task_id, model="opus", effort="high", title="a card"):
    return {"id": task_id, "model": model, "effort": effort, "title": title}


class Append(unittest.TestCase):
    def test_an_append_adds_the_tasks_in_order_and_touches_nothing_else(self):
        out = me.append_tasks(MANIFEST, [entry("T-9"), entry("T-10", model="fable")], STAMP)
        self.assertTrue(out.startswith(MANIFEST))
        self.assertEqual(me.task_ids(out), ["T-1", "T-2", "T-3", "T-9", "T-10"])
        self.assertEqual(tomllib.loads(out)["tasks"][-1],
                         {"id": "T-10", "model": "fable", "effort": "high"})
        self.assertIn("# appended by the feeder 2026-09-19 08:50: a card\n[[tasks]]", out)

    def test_a_manifest_with_no_tasks_yet_takes_its_first(self):
        head = MANIFEST.split("[[tasks]]")[0]
        self.assertEqual(me.task_ids(me.append_tasks(head, [entry("112")], STAMP)), ["112"])

    def test_an_id_already_listed_is_refused(self):
        with self.assertRaises(me.EditError):
            me.append_tasks(MANIFEST, [entry("T-2")], STAMP)
        with self.assertRaises(me.EditError):
            me.append_tasks(MANIFEST, [entry("T-9"), entry("T-9")], STAMP)

    def test_a_title_cannot_break_out_of_its_comment_line(self):
        out = me.append_tasks(MANIFEST, [entry("T-9", title='x\n[[tasks]]\nid = "evil"')], STAMP)
        self.assertEqual(me.task_ids(out), ["T-1", "T-2", "T-3", "T-9"])

    def test_a_string_with_quotes_and_backslashes_survives_the_parser(self):
        value = 'say "no" to C:\\path\nand a second line\x01'
        parsed = tomllib.loads("x = " + me.toml_string(value))["x"]
        self.assertEqual(parsed, 'say "no" to C:\\path and a second line')


class Exclude(unittest.TestCase):
    def test_an_exclusion_writes_two_keys_into_that_block_only(self):
        out = me.exclude_task(MANIFEST, "T-2", "excluded by the feeder after 2 halts")
        tasks = {task["id"]: task for task in tomllib.loads(out)["tasks"]}
        self.assertEqual(tasks["T-2"], {"id": "T-2", "model": "sonnet", "effort": "low",
                                        "excluded": True,
                                        "reason": "excluded by the feeder after 2 halts"})
        self.assertNotIn("excluded", tasks["T-1"])
        self.assertNotIn("excluded", tasks["T-3"])

    def test_the_keys_land_above_the_next_blocks_own_comment(self):
        text = me.append_tasks(MANIFEST, [entry("T-9", title="nine")], STAMP)
        out = me.exclude_task(text, "T-3", "why")
        self.assertIn('effort = "low"\nexcluded = true\nreason = "why"\n\n# appended by the '
                      'feeder 2026-09-19 08:50: nine\n[[tasks]]', out)

    def test_an_already_excluded_task_is_left_alone(self):
        out = me.exclude_task(MANIFEST, "T-2", "first")
        self.assertIsNone(me.exclude_task(out, "T-2", "second"))

    def test_a_reason_the_task_already_carried_is_kept_inside_the_new_one(self):
        text = MANIFEST.replace('id = "T-2"', 'id = "T-2"\nreason = "mechanical, so grok"\n'
                                              'excluded = false')
        out = me.exclude_task(text, "T-2", "halted twice")
        task = tomllib.loads(out)["tasks"][1]
        self.assertIs(task["excluded"], True)
        self.assertEqual(task["reason"],
                         "halted twice; the reason it carried before: mechanical, so grok")

    def test_an_id_is_found_whatever_quoting_or_key_order_the_operator_used(self):
        text = MANIFEST.replace('id = "T-2"\nmodel = "sonnet"',
                                "model = 'sonnet'\ndeclared_paths = [\n  \"src/\",\n]\nid = 'T-2'")
        out = me.exclude_task(text, "T-2", "why")
        self.assertIs(tomllib.loads(out)["tasks"][1]["excluded"], True)

    def test_a_cause_line_full_of_quotes_still_parses(self):
        out = me.exclude_task(MANIFEST, "T-1", 'class x: the gate said "no"\nand \\ more')
        self.assertEqual(tomllib.loads(out)["tasks"][0]["reason"],
                         'class x: the gate said "no" and \\ more')

    def test_an_unknown_id_is_an_error_not_a_silent_no_op(self):
        with self.assertRaises(me.EditError):
            me.exclude_task(MANIFEST, "T-404", "why")


class Commit(RunCase):
    def read(self):
        with open(self.manifest_path, encoding="utf-8") as handle:
            return handle.read()

    def leftovers(self):
        return [name for name in os.listdir(self.tmp.name) if name.endswith(".tmp")]

    def test_a_valid_edit_replaces_the_file_and_leaves_no_temporary_behind(self):
        os.chmod(self.manifest_path, 0o640)
        before = self.read()
        after = me.append_tasks(before, [entry("T-9")], STAMP)
        me.commit(self.manifest_path, before, after)
        self.assertEqual(self.read(), after)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(stat.S_IMODE(os.stat(self.manifest_path).st_mode), 0o640)
        self.assertTrue(mf.validate(mf.load(self.manifest_path)).ok)

    def test_an_edit_that_fails_validate_never_reaches_the_file(self):
        # grok-4.6 belongs to grok and this manifest's tasks run on claude, which is the
        # coherence check routing gets for free by committing through validate.
        before = self.read()
        after = me.append_tasks(before, [entry("T-9", model="grok-4.6")], STAMP)
        with self.assertRaises(me.EditError) as caught:
            me.commit(self.manifest_path, before, after)
        self.assertIn("belongs to backend grok", str(caught.exception))
        self.assertEqual(self.read(), before)
        self.assertEqual(self.leftovers(), [])

    def test_a_manifest_edited_underneath_is_not_overwritten(self):
        before = self.read()
        after = me.append_tasks(before, [entry("T-9")], STAMP)
        with open(self.manifest_path, "a", encoding="utf-8") as handle:
            handle.write("\n# the operator was here\n")
        with self.assertRaises(me.EditError):
            me.commit(self.manifest_path, before, after)
        self.assertIn("the operator was here", self.read())
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
