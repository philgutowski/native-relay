"""A headless browser driver for a Test process, run from the shell (browser test loop plan, U8).

This is the project's own tooling, not Relay's. Copy it into the app's repository, install its
one dependency there, and name it in the tour document. The Runner never imports it and stays
standard library only (R9).

    python3 -m pip install playwright
    python3 -m playwright install chromium

Two modes.

`signin` opens a headed browser once, for the operator. Sign in by hand, come back to the
terminal, press Enter, and the session is written to the storage state file (KTD14). Nothing
else writes that file, and a Test process never runs this mode.

    python3 drive.py signin --url http://127.0.0.1:8765/login \
        --state ~/.example-app/storage-state.json

`visit` is what a Test process runs. It starts a headless browser with that session, opens one
page, runs the steps in the order given, and prints what it saw: the final address, the title,
every console error and page error, every failed request, and with `--text` the page's visible
text. `--screenshot` writes a full page image the Test process can read back. The browser is
closed before the command returns, whatever happened, so nothing outlives the command.

    python3 drive.py visit --url http://127.0.0.1:8765/search \
        --state ~/.example-app/storage-state.json --signin-marker form#signin \
        --step fill:#query=lamp --step press:Enter --step wait:.result \
        --screenshot /tmp/search.png --text

A step is `kind:argument`, and the kinds are:

    goto:PATH           open PATH, relative to --url, or a full address on the same host
    click:SELECTOR      click the first element the selector matches
    fill:SELECTOR=TEXT  type TEXT into the field the selector matches
    press:KEY           press one key, such as Enter or Tab, on the focused element
    wait:SELECTOR       wait until the selector matches a visible element
    sleep:MILLISECONDS  wait that long, for an animation that has no selector to wait on

Exit codes: 0 the page was driven, 1 a step failed or the page did not load, 2 the command line
is wrong or the browser package is missing, 3 the storage state file is missing, 4 the page
showed the sign in page (`--signin-marker`). A Test process reports the pass as not run on 3 or
4, since both mean the session the operator signed in is gone.
"""
import argparse
import os
import sys
import urllib.parse

EXIT_OK = 0
EXIT_STEP = 1
EXIT_USAGE = 2
EXIT_NO_STATE = 3
EXIT_SIGNED_OUT = 4

STEP_KINDS = ("goto", "click", "fill", "press", "wait", "sleep")
TEXT_LIMIT = 20000          # characters of visible text printed, so a long page stays readable
TIMEOUT_MS = 15000          # per step and per page load


def parse_step(value):
    """`kind:argument` as (kind, argument), refused by argparse when it is neither."""
    kind, sep, argument = value.partition(":")
    if not sep or kind not in STEP_KINDS or not argument:
        raise argparse.ArgumentTypeError(
            "a step is kind:argument, with kind one of %s" % ", ".join(STEP_KINDS))
    if kind == "fill" and "=" not in argument:
        raise argparse.ArgumentTypeError("a fill step is fill:SELECTOR=TEXT")
    if kind == "sleep" and not argument.isdigit():
        raise argparse.ArgumentTypeError("a sleep step is sleep:MILLISECONDS")
    return kind, argument


def build_parser():
    parser = argparse.ArgumentParser(description="Drive the app under test in a browser.")
    modes = parser.add_subparsers(dest="mode", required=True)

    signin = modes.add_parser("signin", help="open a headed browser once for the operator to "
                                             "sign in, then write the storage state file")
    signin.add_argument("--url", required=True, help="the app's sign in page")
    signin.add_argument("--state", required=True, help="the storage state file to write")

    visit = modes.add_parser("visit", help="open one page headless with the signed in session, "
                                           "run the steps, and print what the page showed")
    visit.add_argument("--url", required=True, help="the page to open first")
    visit.add_argument("--state", required=True, help="the storage state file to load")
    visit.add_argument("--step", action="append", default=[], type=parse_step,
                       metavar="KIND:ARG", help="one step, in order; repeat for more")
    visit.add_argument("--screenshot", metavar="PATH", help="write a full page image here")
    visit.add_argument("--text", action="store_true", help="print the page's visible text")
    visit.add_argument("--signin-marker", metavar="SELECTOR",
                       help="a selector only the sign in page matches; finding it exits 4")
    visit.add_argument("--width", type=int, default=1280)
    visit.add_argument("--height", type=int, default=800)
    return parser


def load_playwright():
    """The browser package, imported only when a mode runs, so `--help` works without it."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("the playwright package is not installed: python3 -m pip install playwright && "
              "python3 -m playwright install chromium", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    return sync_playwright


def same_host(base, target):
    """True when `target` is on the host `base` names. `goto` never leaves the app."""
    return urllib.parse.urlsplit(base).netloc == urllib.parse.urlsplit(target).netloc


def signin(args):
    sync_playwright = load_playwright()
    state = os.path.expanduser(args.state)
    os.makedirs(os.path.dirname(state) or ".", exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(args.url)
            input("Sign in in the browser window, then press Enter here to save the session. ")
            context.storage_state(path=state)
        finally:
            browser.close()
    os.chmod(state, 0o600)          # it holds a live session, so it is the operator's alone
    print("wrote %s" % state)
    return EXIT_OK


def run_step(page, base, kind, argument):
    if kind == "goto":
        target = urllib.parse.urljoin(base, argument)
        if not same_host(base, target):
            raise ValueError("goto %s leaves the app under test" % target)
        page.goto(target, timeout=TIMEOUT_MS)
    elif kind == "click":
        page.click(argument, timeout=TIMEOUT_MS)
    elif kind == "fill":
        selector, _, text = argument.partition("=")
        page.fill(selector, text, timeout=TIMEOUT_MS)
    elif kind == "press":
        page.keyboard.press(argument)
    elif kind == "wait":
        page.wait_for_selector(argument, state="visible", timeout=TIMEOUT_MS)
    elif kind == "sleep":
        page.wait_for_timeout(int(argument))


def visit(args):
    state = os.path.expanduser(args.state)
    if not os.path.isfile(state):
        print("no signed in session: %s does not exist; the operator runs signin once"
              % state)
        return EXIT_NO_STATE
    sync_playwright = load_playwright()
    errors, failed = [], []
    code = EXIT_OK
    with sync_playwright() as driver:
        browser = driver.chromium.launch(headless=True)
        try:
            context = browser.new_context(storage_state=state,
                                          viewport={"width": args.width, "height": args.height})
            page = context.new_page()
            page.on("console", lambda message: errors.append("console: " + message.text)
                    if message.type == "error" else None)
            page.on("pageerror", lambda error: errors.append("page error: %s" % error))
            page.on("requestfailed", lambda request: failed.append(
                "%s %s" % (request.method, request.url)))
            try:
                page.goto(args.url, timeout=TIMEOUT_MS)
                if args.signin_marker and page.query_selector(args.signin_marker):
                    print("signed out: the page shows the sign in page (%s)"
                          % args.signin_marker)
                    code = EXIT_SIGNED_OUT
                else:
                    for number, (kind, argument) in enumerate(args.step, 1):
                        print("step %d %s:%s" % (number, kind, argument))
                        run_step(page, args.url, kind, argument)
            except Exception as exc:        # a step that fails is a finding, not a crash
                print("step failed: %s" % exc)
                code = EXIT_STEP
            print("url: %s" % page.url)
            print("title: %s" % page.title())
            if args.screenshot:
                page.screenshot(path=args.screenshot, full_page=True)
                print("screenshot: %s" % args.screenshot)
            if args.text:
                text = page.inner_text("body")
                print("text:")
                print(text[:TEXT_LIMIT])
                if len(text) > TEXT_LIMIT:
                    print("(text cut at %d characters)" % TEXT_LIMIT)
        finally:
            browser.close()
    for line in errors:
        print(line)
    for line in failed:
        print("failed request: " + line)
    if not errors and not failed:
        print("no console errors and no failed requests")
    return code


def main(argv=None):
    args = build_parser().parse_args(argv)
    return signin(args) if args.mode == "signin" else visit(args)


if __name__ == "__main__":
    sys.exit(main())
