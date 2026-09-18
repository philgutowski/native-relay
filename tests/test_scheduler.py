"""The same-backend scheduler is pure, deterministic, and conservative."""
import os
import tempfile
import unittest
from types import SimpleNamespace

import _paths
from relay import scheduler


def task(task_id, paths):
    return SimpleNamespace(id=task_id, declared_paths=paths)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, tasks, text=None, **kwargs):
        text = text if text is not None else {item.id: "" for item in tasks}
        return scheduler.build_schedule(tasks, self.tmp.name, text, **kwargs)

    def test_parallel_places_disjoint_narrow_paths_in_one_wave_in_manifest_order(self):
        result = self.plan([task("T-1", ["src/one.py"]), task("T-2", ["src/two.py"]),
                            task("T-3", ["lib/three.py"])], policy="parallel")
        self.assertEqual(result.waves, (("T-1", "T-2", "T-3"),))
        self.assertEqual(result.edges, ())

    def test_serial_policy_serializes_every_pair(self):
        result = self.plan([task("T-1", ["src/one.py"]), task("T-2", ["src/two.py"]),
                            task("T-3", ["lib/three.py"])])
        self.assertEqual(result.waves, (("T-1",), ("T-2",), ("T-3",)))
        self.assertEqual([edge.reason for edge in result.edges],
                         ["policy_serial", "policy_serial", "policy_serial"])

    def test_missing_or_invalid_paths_are_not_a_proof_of_independence(self):
        result = self.plan([task("T-1", []), task("T-2", ["src/two.py"]),
                            task("T-3", ["../escape.py"])], policy="parallel")
        self.assertEqual(result.waves, (("T-1",), ("T-2",), ("T-3",)))
        self.assertEqual([(edge.first, edge.second, edge.reason) for edge in result.edges], [
            ("T-1", "T-2", "declared_paths_missing"),
            ("T-1", "T-3", "declared_paths_missing"),
            ("T-2", "T-3", "declared_path_invalid"),
        ])

    def test_exact_file_and_parent_directory_overlap(self):
        result = self.plan([task("T-1", ["src/"]), task("T-2", ["src/parse.py"]),
                            task("T-3", ["lib/ok.py"])], policy="parallel")
        self.assertEqual(result.waves, (("T-1", "T-3"), ("T-2",)))
        self.assertEqual([(edge.first, edge.second, edge.reason) for edge in result.edges],
                         [("T-1", "T-2", "declared_paths_overlap")])

    def test_global_surfaces_serialize_even_when_the_other_path_is_disjoint(self):
        result = self.plan([task("T-1", ["pyproject.toml"]), task("T-2", ["src/two.py"]),
                            task("T-3", ["docs/guide.md"])], policy="parallel")
        self.assertEqual(result.waves, (("T-1",), ("T-2",), ("T-3",)))
        self.assertEqual([edge.reason for edge in result.edges], [
            "declared_path_global", "declared_path_global", "declared_path_global",
        ])

    def test_unavailable_text_and_global_or_undeclared_text_path_serialize(self):
        tasks = [task("T-1", ["src/one.py"]), task("T-2", ["src/two.py"]),
                 task("T-3", ["src/three.py"])]
        unavailable = self.plan(tasks, {"T-1": "", "T-2": ""}, policy="parallel")
        self.assertIn(("T-1", "T-3", "task_text_unavailable"),
                      [(edge.first, edge.second, edge.reason) for edge in unavailable.edges])
        result = self.plan(tasks, {"T-1": "update shared API", "T-2": "edit docs/other.md",
                                   "T-3": ""}, policy="parallel")
        self.assertEqual(result.waves, (("T-1",), ("T-2",), ("T-3",)))
        reasons = [edge.reason for edge in result.edges]
        self.assertIn("task_text_global_signal", reasons)
        self.assertIn("task_text_undeclared_path", reasons)

    def test_explicit_task_dependency_creates_only_that_edge(self):
        result = self.plan([task("T-1", ["src/one.py"]), task("T-2", ["src/two.py"]),
                            task("T-3", ["src/three.py"])],
                           {"T-1": "", "T-2": "depends on T-1", "T-3": ""},
                           policy="parallel")
        self.assertEqual(result.waves, (("T-1", "T-3"), ("T-2",)))
        self.assertEqual([(edge.first, edge.second, edge.reason) for edge in result.edges],
                         [("T-1", "T-2", "task_text_dependency:T-1")])

    def test_literal_repository_reference_only_adds_a_serial_edge(self):
        os.makedirs(os.path.join(self.tmp.name, "src"))
        with open(os.path.join(self.tmp.name, "src", "one.py"), "w") as handle:
            handle.write("# uses src/two.py\n")
        result = self.plan([task("T-1", ["src/one.py"]), task("T-2", ["src/two.py"]),
                            task("T-3", ["lib/three.py"])], policy="parallel")
        self.assertEqual(result.waves, (("T-1", "T-3"), ("T-2",)))
        self.assertEqual([(edge.first, edge.second, edge.reason) for edge in result.edges],
                         [("T-1", "T-2", "repository_path_reference")])

    def test_semantic_input_can_add_but_never_remove_a_deterministic_edge(self):
        tasks = [task("T-1", ["src/one.py"]), task("T-2", ["src/two.py"]),
                 task("T-3", ["src/three.py"])]
        result = self.plan(tasks, policy="parallel",
                           semantic_edges=[("T-2", "T-3", "shared concept")])
        self.assertEqual(result.waves, (("T-1", "T-2"), ("T-3",)))
        self.assertEqual([(edge.first, edge.second, edge.reason) for edge in result.edges],
                         [("T-2", "T-3", "semantic:shared concept")])

    def test_renderer_is_complete_and_stable(self):
        result = self.plan([task("T-1", ["src/one.py"]), task("T-2", ["src/one.py"])],
                           policy="parallel")
        self.assertEqual(scheduler.render(result), "\n".join((
            "run policy: parallel", "wave 1: T-1", "wave 2: T-2", "serialized edges:",
            "- T-1 -> T-2: declared_paths_overlap",
        )))


if __name__ == "__main__":
    unittest.main()
