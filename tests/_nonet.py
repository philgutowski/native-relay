"""Turns "no test touches a network" from a sentence into a failure.

Every adapter takes an injectable transport, and a test is supposed to hand it a fake. Nothing
enforced that. A test that drove `relay validate` through the CLI without one fell through to the
factory's defaults, the real `urllib` opener and the real `gh`, and reached a live site on every
run. It still passed, because an unreadable card is only a warning, so the one visible trace was
a ResourceWarning in the suite's output.

Importing this module closes both doors for the whole process. `_paths` imports it, and every
test module imports `_paths` first, so there is no test it does not cover. A process a test
launches is outside its reach, which is fine: those run the stub CLIs with a temporary `HOME`.
"""
import os
import subprocess
import urllib.request

# The executables that exist to talk to a remote. `git` is absent on purpose: the suite drives
# real git against temporary local repositories all day.
NETWORK_EXECUTABLES = ("gh", "curl", "wget", "ssh")


class NetworkReached(AssertionError):
    """A test got through to a real transport. An AssertionError, so neither adapter's
    `except OSError` turns it into an ordinary tracker failure and hides it again."""


def _refuse_open(self, fullurl, *args, **kwargs):
    url = fullurl.get_full_url() if hasattr(fullurl, "get_full_url") else str(fullurl)
    raise NetworkReached("a test reached the real urllib opener for %s; inject a fake opener "
                         "(see FakeOpener in test_adapters)" % url)


_popen_init = subprocess.Popen.__init__


def _guarded_popen_init(self, args, *rest, **kwargs):
    argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
    name = os.path.basename(os.fsdecode(argv[0])) if argv else ""
    if name in NETWORK_EXECUTABLES:
        raise NetworkReached("a test launched the real `%s` (%s); inject a fake run callable "
                             "(see DispatchRun in test_adapters and FakeRun in _fakes)"
                             % (name, " ".join(os.fsdecode(part) for part in argv)))
    return _popen_init(self, args, *rest, **kwargs)


def install():
    urllib.request.OpenerDirector.open = _refuse_open
    subprocess.Popen.__init__ = _guarded_popen_init


install()
