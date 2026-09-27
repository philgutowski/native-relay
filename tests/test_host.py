"""Issue #32: the host snapshot a task record carries at start and end.

Nothing here reads the real host; every probe is fed a recorded reading, including the one that
runs through this platform's own branch of `snapshot()`.
"""
import subprocess
import sys
import unittest
from types import SimpleNamespace

import _paths  # noqa: F401  (imports _nonet, and puts the runner package on the path)
from relay import host

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                    60487.
Pages active:                                 367020.
Pages inactive:                               459988.
Pages speculative:                             62379.
"Translation faults":                     1254986958.
Pages stored in compressor:                   190429.
Pages occupied by compressor:                  68127.
Swapins:                                          12.
Swapouts:                                         34.
"""

MEMINFO = """MemTotal:       16318756 kB
MemFree:         1024000 kB
MemAvailable:    8000000 kB
Inactive:        2048000 kB
Inactive(anon):  1548000 kB
Inactive(file):   500000 kB
"""

VMSTAT = """nr_free_pages 256000
pswpin 5
pswpout 7
"""


def ran(stdout, returncode=0):
    def run(args, **kwargs):
        return SimpleNamespace(stdout=stdout, returncode=returncode)
    return run


class Parse(unittest.TestCase):
    def test_vm_stat_scales_pages_by_the_header_page_size_and_keeps_swap_counters_in_pages(self):
        self.assertEqual(host.parse_vm_stat(VM_STAT), {
            "free_bytes": 60487 * 16384, "inactive_bytes": 459988 * 16384,
            "compressed_bytes": 68127 * 16384, "swapins": 12, "swapouts": 34})

    def test_vm_stat_without_a_page_size_leaves_the_memory_fields_out(self):
        text = VM_STAT.replace("(page size of 16384 bytes)", "")
        self.assertEqual(host.parse_vm_stat(text), {"swapins": 12, "swapouts": 34})

    def test_linux_counts_only_file_backed_inactive_memory_as_headroom(self):
        # Inactive anonymous pages leave memory only by swapping, so the total `Inactive:` line
        # would report 2 GB of headroom on a host with half a gigabyte.
        self.assertEqual(host.parse_linux(MEMINFO, VMSTAT), {
            "free_bytes": 1024000 * 1024, "inactive_bytes": 500000 * 1024,
            "swapins": 5, "swapouts": 7})


class Snapshot(unittest.TestCase):
    def test_darwin_carries_every_field(self):
        snap = host.snapshot(platform="darwin", run=ran(VM_STAT), loadavg=lambda: (2.345, 1, 1))
        self.assertEqual(snap, {"load_1m": 2.35, "free_bytes": 60487 * 16384,
                                "inactive_bytes": 459988 * 16384,
                                "compressed_bytes": 68127 * 16384, "swapins": 12, "swapouts": 34})

    def test_linux_reads_proc(self):
        files = {"/proc/meminfo": MEMINFO, "/proc/vmstat": VMSTAT}
        snap = host.snapshot(platform="linux", read=files.__getitem__, loadavg=lambda: (1.0, 1, 1))
        self.assertEqual(snap["free_bytes"], 1024000 * 1024)
        self.assertEqual(snap["swapouts"], 7)

    def test_a_failing_vm_stat_keeps_the_load_and_leaves_the_rest_none(self):
        def run(args, **kwargs):
            raise subprocess.TimeoutExpired(args, 5)
        snap = host.snapshot(platform="darwin", run=run, loadavg=lambda: (3.0, 1, 1))
        self.assertEqual(snap, dict(host.empty(), load_1m=3.0))

    def test_a_nonzero_vm_stat_is_ignored(self):
        snap = host.snapshot(platform="darwin", run=ran(VM_STAT, returncode=1),
                             loadavg=lambda: (3.0, 1, 1))
        self.assertIsNone(snap["free_bytes"])

    def test_nothing_readable_is_every_field_none_rather_than_a_raise(self):
        def loadavg():
            raise OSError("no load average here")
        self.assertEqual(host.snapshot(platform="sunos", loadavg=loadavg), host.empty())

    def test_this_hosts_platform_branch_parses_a_recorded_reading(self):
        # `set(host.snapshot()) == set(host.FIELDS)` passed even when every read failed, since
        # `empty()` already carries the full key set before a single field is read. Feed the
        # parser a recorded reading through whichever branch this platform selects and check the
        # numbers, so a parser that stopped reading would fail here.
        if sys.platform == "darwin":
            snap = host.snapshot(run=ran(VM_STAT), loadavg=lambda: (2.345, 1, 1))
            self.assertEqual(snap, {"load_1m": 2.35, "free_bytes": 60487 * 16384,
                                    "inactive_bytes": 459988 * 16384,
                                    "compressed_bytes": 68127 * 16384, "swapins": 12, "swapouts": 34})
        elif sys.platform.startswith("linux"):
            files = {"/proc/meminfo": MEMINFO, "/proc/vmstat": VMSTAT}
            snap = host.snapshot(read=files.__getitem__, loadavg=lambda: (1.0, 1, 1))
            self.assertEqual(snap, {"load_1m": 1.0, "free_bytes": 1024000 * 1024,
                                    "inactive_bytes": 500000 * 1024, "compressed_bytes": None,
                                    "swapins": 5, "swapouts": 7})
        else:
            self.skipTest("no recorded reading for platform %r" % sys.platform)


class Line(unittest.TestCase):
    START = {"load_1m": 1.5, "free_bytes": 2 << 30, "inactive_bytes": 4 << 30,
             "swapins": 10, "swapouts": 100}
    END = {"load_1m": 6.25, "free_bytes": 1 << 29, "inactive_bytes": 1 << 30,
           "swapins": 10, "swapouts": 4100}

    def test_the_line_reads_start_to_end_and_swap_as_the_difference(self):
        self.assertEqual(
            host.line(self.START, self.END),
            "host: load 1.50 to 6.25, free 2.0 GiB to 0.5 GiB, inactive 4.0 GiB to 1.0 GiB, "
            "0 swapins and 4000 swapouts during the task")

    def test_the_compressor_prints_when_either_side_read_it(self):
        text = host.line(dict(self.START, compressed_bytes=1 << 30),
                         dict(self.END, compressed_bytes=6 << 30))
        self.assertIn("compressed 1.0 GiB to 6.0 GiB", text)
        self.assertNotIn("compressed", host.line(self.START, self.END))

    def test_no_snapshot_at_all_is_no_line(self):
        self.assertIsNone(host.line(None, None))

    def test_snapshots_holding_no_reading_are_no_line_rather_than_a_row_of_question_marks(self):
        self.assertIsNone(host.line(host.empty(), host.empty()))

    def test_a_missing_side_prints_question_marks_rather_than_a_false_zero(self):
        text = host.line(self.START, None)
        self.assertIn("load 1.50 to ?", text)
        self.assertIn("? swapins and ? swapouts", text)


if __name__ == "__main__":
    unittest.main()
