"""Puts the runner package on sys.path so `python3 -m unittest discover -s tests` finds it.

Every test module imports this first. The package lives under the skill directory because a
Claude Code plugin ships its scripts next to the skill that calls them, not on the Python path.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "skills", "relay", "scripts")
STUB_DIR = os.path.join(REPO_ROOT, "tests", "stub-claude")
FIXTURES_DIR = os.path.join(REPO_ROOT, "tests", "fixtures")

if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# Every test module imports this one first, which makes it the one place a suite wide rule can
# live. `_nonet` fails any test that gets through to a real network transport.
import _nonet  # noqa: E402,F401
