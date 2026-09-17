"""Pair split, load, and validate."""
import os
import unittest

import _paths
import _repo
from relay import manifest as mf, pair
from test_run import MANIFEST, RunCase


class PairSplit(RunCase):
    def mixed_text(self):
        text = MANIFEST.replace("__REPO__", self.repo)
        text += '''
[defaults]
backend = "claude"

[[tasks]]
id = "T-4"
model = "grok-4.6"
effort = "low"
backend = "grok"
reason = "mechanical work, a good use of the grok account"
'''
        # Rewrite T-2 onto grok, keep T-1 and T-3 on claude.
        text = text.replace(
            '''[[tasks]]
id = "T-2"
model = "sonnet"
effort = "low"
''',
            '''[[tasks]]
id = "T-2"
model = "grok-4.6"
effort = "low"
backend = "grok"
reason = "mechanical work, a good use of the grok account"
''')
        return text

    def load_mixed(self):
        path = os.path.join(self.tmp.name, "mixed.toml")
        with open(path, "w") as handle:
            handle.write(self.mixed_text())
        return mf.load(path)

    def test_queues_preserve_source_order_and_group_by_backend(self):
        manifest = self.load_mixed()
        grouped = pair.queues(manifest)
        self.assertEqual(grouped["order"], ("T-1", "T-2", "T-3", "T-4"))
        self.assertEqual(grouped["claude"], ("T-1", "T-3"))
        self.assertEqual(grouped["grok"], ("T-2", "T-4"))
        self.assertEqual(grouped["other"], ())

    def test_from_manifest_refuses_a_single_backend_list(self):
        with self.assertRaises(pair.PairError) as raised:
            pair.from_manifest(self.manifest)
        self.assertIn("at least one claude task and one grok task", str(raised.exception))

    def test_split_writes_members_and_a_pair_file_that_round_trips(self):
        manifest = self.load_mixed()
        loaded = pair.split(manifest, out_dir=self.tmp.name)
        self.assertTrue(os.path.exists(loaded.path))
        self.assertEqual(loaded.order, ("T-1", "T-2", "T-3", "T-4"))
        self.assertEqual([task.id for task in loaded.claude.tasks], ["T-1", "T-3"])
        self.assertEqual([task.id for task in loaded.grok.tasks], ["T-2", "T-4"])
        self.assertTrue(all(task.backend == "claude" for task in loaded.claude.tasks))
        self.assertTrue(all(task.backend == "grok" for task in loaded.grok.tasks))
        combined = pair.combine(loaded)
        self.assertEqual([task.id for task in combined.tasks], ["T-1", "T-2", "T-3", "T-4"])
        self.assertEqual([task.backend for task in combined.tasks],
                         ["claude", "grok", "claude", "grok"])
        self.assertEqual(pair.validate(loaded, env=self.base_env()), [])

    def test_a_task_omitting_backend_follows_the_source_default_into_the_claude_member(self):
        manifest = self.load_mixed()
        loaded = pair.split(manifest, out_dir=self.tmp.name)
        self.assertEqual(loaded.claude.tasks[0].id, "T-1")
        self.assertEqual(loaded.claude.tasks[0].backend, "claude")
