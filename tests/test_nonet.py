"""The suite's network guard guards. If `_nonet` stops refusing, every other test goes back to
being able to reach a live tracker without anybody noticing, so the refusal itself is tested."""
import subprocess
import sys
import unittest
import urllib.request

import _paths  # noqa: F401
import _nonet


class NetworkGuard(unittest.TestCase):
    def test_the_real_urllib_opener_refuses(self):
        opener = urllib.request.build_opener()
        with self.assertRaises(_nonet.NetworkReached) as caught:
            opener.open(urllib.request.Request("https://example.invalid/rest"), timeout=1)
        self.assertIn("example.invalid", str(caught.exception))

    def test_urlopen_refuses_too(self):
        with self.assertRaises(_nonet.NetworkReached):
            urllib.request.urlopen("https://example.invalid/", timeout=1)

    def test_every_network_executable_refuses_by_name_or_by_path(self):
        for name in _nonet.NETWORK_EXECUTABLES:
            for argv in ([name, "--version"], ["/opt/anywhere/bin/" + name, "--version"]):
                with self.subTest(argv=argv):
                    with self.assertRaises(_nonet.NetworkReached) as caught:
                        subprocess.run(argv, capture_output=True)
                    self.assertIn(name, str(caught.exception))

    def test_the_refusal_is_not_an_oserror(self):
        # Both adapters catch OSError around their transport and report an ordinary tracker
        # failure. A refusal that were one would be swallowed right there.
        self.assertFalse(issubclass(_nonet.NetworkReached, OSError))
        self.assertTrue(issubclass(_nonet.NetworkReached, AssertionError))

    def test_every_test_module_imports_paths_and_so_the_guard(self):
        # The guard covers a module only because that module imports `_paths`. Running one
        # module alone, `python3 -m unittest test_<name>`, is the case this protects.
        import glob
        import os
        import re

        here = os.path.dirname(os.path.abspath(__file__))
        for path in sorted(glob.glob(os.path.join(here, "test_*.py"))):
            with open(path) as handle:
                text = handle.read()
            self.assertTrue(re.search(r"^import _paths\b", text, flags=re.M),
                            "%s does not import _paths" % os.path.basename(path))

    def test_an_ordinary_local_process_still_runs(self):
        done = subprocess.run([sys.executable, "-c", "print('ok')"], capture_output=True, text=True)
        self.assertEqual(done.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
