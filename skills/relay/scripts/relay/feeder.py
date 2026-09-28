"""The feeder: an outer loop that turns one Relay run into a continuous one (feeder plan).

Relay reads its manifest once per run, the task list is fixed for that run, and a resumed run
skips what landed. So a continuous run is a loop around `relay run` that grows the manifest
between runs:

    stop file? -> checkout usable? -> pre cycle hook -> read the READY cards -> take a small
    batch -> pick a model per card -> append [[tasks]] -> relay run -> read the summary ->
    exclude what halted twice -> repeat

Three rules carry it, each for a failure that would otherwise cost a day:

1.  Only READY cards are appended. Ready is the tracker's own derivation that a card can start
    now, read through the adapter's `ready` method or the sidecar's ready command. The batch is
    small on purpose, so a dependency that lands in one cycle releases its dependants in the
    next rather than after a fifty task pass.
2.  A task that halts twice is excluded, with the reason written into the manifest. Relay
    relaunches a halted task on every run, so without this one deterministic failure is retried
    for ever at a session's cost each time.
3.  A usage limit is run as one state machine (usage limit plan, #68), decided in `limits.py`
    and applied here. Relay's Task process has no limit handling of its own: the headless
    process exits, and the record reads halted or blocked. The Feeder reads why.

    Each death the run launched this Cycle, known by a `started_at` the record did not carry
    before the run, is read from its own log (`limits.read_death`). A 429 in the last attempt's
    `result` line confirms a usage limit, any other `result` line refutes it, and a quick death
    with no `result` line is unconfirmed. A record the run refused before launch, or never
    reached, is never read as a limit: its log is an older attempt's.

    A confirmed death marks its model, in the state file's `exhausted`, until the reset time
    the CLI printed when the log carries one ahead, else `fallback_hours` after the death. A
    later confirmed death replaces the mark. A landing beside it does not prevent it. The
    operator is told when a model goes from unmarked to marked. The death is never counted as
    a halt, and a blocked one is queued for a `--retry-blocked` of its own.

    No Task is launched on a marked model. At the start of every Cycle each unsettled Task and
    queued retry the manifest lists on a marked model is moved to the first free model along
    its `[models] fallback` chain. With none free it is held: a held retry keeps its place in
    the queue, any other held Task is named to the run with `--defer`, and a held Task takes no
    room in the batch. A fresh card routed to a held model is not appended. When held work is
    all that is left, the feeder waits, reason `model_held`, no longer than the earliest mark
    that holds it. The mark is the bound: a Task on a model that stays limited is launched at
    most once per mark.

    An unconfirmed death never marks, moves, or holds. It acts only through the whole Cycle
    wait: a Cycle where nothing landed, every death was quick, and one death was unconfirmed
    is waited out, reason `usage_limit`, those deaths are not counted, and the blocked ones are
    queued for a retry after the wait. That wait is still a reading of timing, and
    `limit_waits` counts it. Past `limit_waits_max` in a row the feeder leaves with exit 2 and
    reports the blocked ones. A Cycle where something landed, a death was slow, or nothing
    died clears the count. The count survives a restart, since a restart is no evidence about
    the account; leaving on it clears it, and so does `feed --clear-limits`, which empties the
    marks too.

    Every mark, move, and hold is one `limit` event in the events file.

The feeder never merges, pushes, moves a card, or edits the target repository. It writes five
things, all beside the manifest: the manifest itself, through `manifestedit`; its own state
file; its log; its events file; and its post cycle hook's output. The tracker is read only here
too, so the invariant that the runner never writes to a tracker on a normal manifest holds for
the feeder as well.

The two hooks are the operator's, and what they do is theirs: `pre_cycle` runs before the
ready cards are read, for a board whose ready labels are derived, and `post_cycle` (issue #37)
runs after each `relay run` the feeder settles, with the cycle's landed, halted, blocked, and
skipped ids and the default branch's merge range in its environment. A blocking one is waited
on and can hold the feeder on a nonzero exit; a detached one is started and left to run, and
reaped at a later cycle's start with its exit code logged. A hold is a record in the state file,
not only the exit (issue #53): every later feeder refuses to start a cycle while it is set, by
hand, by `--restart`, or by a cron line running `--once`, until `feed <manifest> --release`.

A feeder answers for itself (issue #36). The state file carries a `process` record, its pid,
host, start, runner tree, and current cycle, stamped with the exit and the reason when it
leaves, and `liveness` checks that pid against this manifest's own lock file, so no watcher has
to guess from a process listing that cannot tell two manifests apart. The events file,
`<stem>.feeder.events.jsonl`, holds one JSON object per line for each start, cycle, result, a
post cycle hook starting or finishing, wait, and leave, every one naming its manifest and pid,
and `feed --follow` streams it. The log stays the human account; nothing reads its sentences.

Everything project specific is data in the sidecar file, `<manifest stem>.feeder.toml`. It is a
sidecar and not a manifest table because the feeder rewrites the manifest while older pinned
runners must still load it, and a runner that met an unknown table would be within its rights
to refuse it.

Every outside effect is reached through `Deps`, a record of callables, so the suite drives the
whole loop with a fake runner and a sleep that does not sleep.
"""
import errno
import fcntl
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
import urllib.parse
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta

from . import (adapters, brief, contracts, gitread, limits, manifest as manifest_module,
               manifestedit, run as run_module, state as state_module,
               summary as summary_module, testloop)

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_HALTED = 2
EXIT_LEASE = 3
EXIT_INTERRUPTED = 130

# The state counts about the queue that mean "this many times in a row", reset when a feeder
# starts. The third such count, `limit_waits`, is about the account, so it is handed to the next
# feeder and cleared only by evidence: a Cycle that breaks the row, the leave on
# `limit_waits_exhausted`, or `feed --clear-limits` (usage limit plan, KTD8, R9).
QUEUE_STREAKS = ("idle_waits", "unreadable_waits")

# A task in one of these statuses is one the next run will not launch, so it holds no room in
# the batch. `skipped` is here because a skip costs no session: the runner rechecks the card at
# every launch and steps over it in a moment. It is settled for room and still reported, which
# is the half the original script left out.
SETTLED = ("landed", "blocked", "excluded", "skipped")
STATUS_HALTED = "halted"
STATUS_LANDED = "landed"
STATUS_BLOCKED = "blocked"
STATUS_SKIPPED = "skipped"

COMMAND_TIMEOUT_SECONDS = 900
RESTART_POLL_SECONDS = 20
RESTART_POLLS_MAX = 4320          # a day of polling at twenty seconds
UNREADABLE_WAITS_MAX = 2          # waits on a ready source that fails to read, then stop
UNRANKED = 10 ** 9
DRY_RUN_LINES = 12
MODEL_LINE_RE = re.compile(r"^\*\*Model:\*\*\s*(\S+)", re.MULTILINE)
EVENTS_CHUNK_BYTES = 64 * 1024    # how far `end_offset` reads back per step for a newline

# The events file's `event` words (issue #36). A watcher keys on these, so they are a contract:
# add one if a new kind of moment needs it, never rename one.
EVENT_STARTED = "started"
EVENT_CYCLE_STARTED = "cycle_started"
EVENT_CYCLE_RESULT = "cycle_result"
EVENT_WAITING = "waiting"
EVENT_LEAVING = "leaving"
EVENT_POST_CYCLE = "post_cycle"
EVENT_POST_CYCLE_STARTED = "post_cycle_started"
# One per mark, move, or hold (usage limit plan, R12). Its `action` is one of the three words.
EVENT_LIMIT = "limit"
LIMIT_MARK = "mark"
LIMIT_MOVE = "move"
LIMIT_HOLD = "hold"
# The browser test loop's two words (browser test loop plan, U6, KTD10). One `test_pass` per
# pass the feeder started, whatever its status, and one `test_loop_stopped` when the loop ends.
EVENT_TEST_PASS = "test_pass"
EVENT_TEST_LOOP_STOPPED = "test_loop_stopped"
# Written by `feed --follow`, never by a feeder: the follower's own line for a feeder it found
# gone without a `leaving` event, killed or never started.
EVENT_NOT_RUNNING = "not_running"
FOLLOW_POLL_SECONDS = 2
FOLLOW_GRACE_POLLS = 5            # how long a follower waits for a feeder that is starting
# The stop file's content when a restart is waiting to take over, and the leaving reason then.
RESTART_WORD = "restart"
LOCK_ATTEMPTS = 3
LOCK_RETRY_SECONDS = 0.05
# The cap `announce_holds` gives `limits.hold_wait`, so the answer is the earliest expiry along a
# held model's chain however far off: a year.
HOLD_SECONDS_MAX = 365 * 24 * 60 * 60
# The post cycle hook's two modes. Blocking is waited on, its exit code logged, and with
# `post_cycle_hold` a nonzero one stops the feeder; detached is started and left to run.
HOOK_BLOCKING = "blocking"
HOOK_DETACHED = "detached"
HOOK_MODES = (HOOK_BLOCKING, HOOK_DETACHED)
HOLD_WORD = "post_cycle_held"
# Said after any sentence that the state file could not be read, by every reader that refuses.
STATE_HINT = "It holds the halt counts, so fix or remove it by hand."


class ConfigError(ValueError):
    """The sidecar file is wrong. Every problem found is in the message."""


@dataclass(frozen=True)
class Pending:
    """A wait the rules asked for and `settle` has not taken yet, because the post cycle hook
    runs first and a hold it asks for replaces the wait."""
    seconds: int
    reason: str


@dataclass(frozen=True)
class Launch:
    """What the start of a Cycle leaves for the run, once `limits.plan_cycle_start`'s moves are
    written. `running` the listed ids the run will launch, in order, the retries among them;
    `retry` the ids for `--retry-blocked` and `defer` those for `--defer`; `held` (id, model)
    for every held Task; `queue` every id queued for a retry, held or not."""
    running: tuple = ()
    retry: tuple = ()
    defer: tuple = ()
    held: tuple = ()
    queue: frozenset = frozenset()


@dataclass(frozen=True)
class CycleContext:
    """What `settle` needs from before the run: every record as it read then, the ids the run
    was told to defer, the retry queue it started with, and the time the terminal record the
    state held then was written, so a run that wrote none is not read through the one before."""
    before: dict = field(default_factory=dict)
    deferred: frozenset = frozenset()
    retries: frozenset = frozenset()
    terminal: object = None


@dataclass(frozen=True)
class PassDone:
    """What one browser test pass left the feeder: `new` the cards it confirmed filed and
    recorded, `ready` those of them the ready source returns, for the drain tour's choice
    between going round and leaving."""
    new: tuple = ()
    ready: tuple = ()


@dataclass(frozen=True)
class Paths:
    """Every file the feeder touches, all derived from the manifest's own path, so two
    manifests in one directory can never share a stop file or a state file."""
    manifest: str
    config: str
    stop: str
    state: str
    order: str
    routing: str
    log: str
    lock: str
    out: str
    events: str
    hook_out: str


def paths_for(manifest_path):
    manifest_path = os.path.abspath(manifest_path)
    stem = os.path.splitext(manifest_path)[0]
    return Paths(manifest=manifest_path, config=stem + ".feeder.toml", stop=stem + ".feeder.stop",
                 state=stem + ".feeder.state.json", order=stem + ".order",
                 routing=stem + ".models", log=stem + ".feeder.log", lock=stem + ".feeder.lock",
                 out=stem + ".feeder.out", events=stem + ".feeder.events.jsonl",
                 hook_out=stem + ".feeder.hook.out")


def listed_ids(paths):
    """The task ids the manifest currently lists (issue #46). `cmd_feed` calls this before it
    detaches or asks a live feeder to leave, so a `--retry-blocked` id can be checked without
    touching a lock, a stop file, or a config. Goes through `manifest_module.load` first, the
    same call `cycle` makes before it ever reaches `manifestedit`, so a manifest that will not
    parse raises the caught `ManifestError` here too rather than a bare `EditError` from reading
    the raw text unvalidated."""
    manifest = manifest_module.load(paths.manifest, allow_no_tasks=True)
    return [task.id for task in manifest.tasks]


@dataclass(frozen=True)
class TestLoop:
    """The sidecar's `[test_loop]` table (browser test loop plan, U2). Off unless `enabled`, and
    with no table at all it is this default, so a Config without the loop is today's Config
    (R1, AE8). An empty `model` or `effort` means the `[models]` value, read through
    `Config.test_model` and `Config.test_effort`; an empty `design_model` means none."""
    enabled: bool = False
    report_only: bool = False
    tour: str = ""                    # a path relative to the target repository
    url: str = ""
    prepare: tuple = ()               # an argument list, never a shell string (KTD7)
    prepare_timeout_seconds: int = 600
    model: str = ""
    effort: str = ""
    timeout_minutes: int = 60
    # The caps take their defaults from `testloop.Settings`, so the rules and the sidecar
    # can never disagree on one.
    max_rounds: int = testloop.Settings.max_rounds
    max_hours: int = testloop.Settings.max_hours
    max_cards_per_pass: int = testloop.Settings.max_cards_per_pass
    max_patches_per_area: int = testloop.Settings.max_patches_per_area
    max_cards_total: int = testloop.Settings.max_cards_total
    labels: tuple = ()                # the labels every filed card carries (R13)
    allowed_tools: tuple = ("Bash", "Read", "Grep", "Glob")
    design_model: str = ""
    design_note: str = ""


# The `[test_loop]` keys a loop that is on cannot run without (KTD7).
_TEST_LOOP_NEEDS = ("tour", "url", "prepare")


@dataclass(frozen=True)
class Config:
    """The sidecar's settings. Every default is the value the Cratekit script ran on."""
    batch: int = 3
    max_halts: int = 2
    caffeinate: bool = True
    quick_death_seconds: int = 600
    limit_wait_seconds: int = 1800
    limit_waits_max: int = 16         # eight hours of waiting on a suspected usage limit
    idle_wait_seconds: int = 1800
    idle_waits_max: int = 0           # an empty queue with no run to wait on: leave at once
    lease_wait_seconds: int = 600
    default_model: str = "opus"
    default_effort: str = "high"
    allowed_models: tuple = ("fable", "opus", "sonnet")
    denied_ids: tuple = ()
    denied_labels: tuple = ()
    ready_source: dict = field(default_factory=dict)
    ready_command: tuple = ()
    pre_cycle_command: tuple = ()
    post_cycle_command: tuple = ()
    post_cycle_mode: str = HOOK_BLOCKING
    post_cycle_hold: bool = False
    post_cycle_timeout_seconds: int = 3600
    model_fallback: dict = field(default_factory=dict)   # empty: no per model fallback
    fallback_hours: int = 5
    test_loop: TestLoop = field(default_factory=TestLoop)

    @property
    def test_model(self):
        """The model a Test process runs on: the loop's own, else the `[models]` default."""
        return self.test_loop.model or self.default_model

    @property
    def test_effort(self):
        """The effort a Test process runs at: the loop's own, else the `[models]` effort."""
        return self.test_loop.effort or self.default_effort


# Sidecar table and key -> Config field. A key outside this map is an error, because a typo
# such as `bacth = 5` that was silently ignored would run the default for a day unnoticed.
_SCHEMA = {
    "feeder": {"batch": "batch", "max_halts": "max_halts", "caffeinate": "caffeinate"},
    "waits": {"quick_death_seconds": "quick_death_seconds",
              "limit_wait_seconds": "limit_wait_seconds", "limit_waits_max": "limit_waits_max",
              "idle_wait_seconds": "idle_wait_seconds", "idle_waits_max": "idle_waits_max",
              "lease_wait_seconds": "lease_wait_seconds"},
    "models": {"default": "default_model", "effort": "default_effort",
               "allowed": "allowed_models", "fallback": "model_fallback",
               "fallback_hours": "fallback_hours"},
    "deny": {"ids": "denied_ids", "labels": "denied_labels"},
    "hooks": {"pre_cycle": "pre_cycle_command", "post_cycle": "post_cycle_command",
              "post_cycle_mode": "post_cycle_mode", "post_cycle_hold": "post_cycle_hold",
              "post_cycle_timeout_seconds": "post_cycle_timeout_seconds"},
    # Read into `TestLoop` by `_load_test_loop`, never into a Config field of its own.
    "test_loop": {spec.name: spec.name for spec in fields(TestLoop)},
}
_READY_KEYS = ("labels", "jql", "command")
# The integer settings where zero means something: no idle waits, leave on the first empty cycle.
_ZERO_ALLOWED = ("idle_waits_max",)


def _is_strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def load_config(path):
    """The sidecar as a Config, or every default when the file does not exist."""
    if not os.path.exists(path):
        return Config()
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("%s is not valid TOML: %s" % (path, exc))
    problems, values, loop_values = [], {}, {}
    for table, body in raw.items():
        if table == "ready":
            continue
        if table not in _SCHEMA or not isinstance(body, dict):
            problems.append("[%s] is not a feeder table" % table)
            continue
        for key, value in body.items():
            if key not in _SCHEMA[table]:
                problems.append("%s.%s is not a feeder setting" % (table, key))
            elif table == "test_loop":
                loop_values[key] = value
            else:
                values[_SCHEMA[table][key]] = value
    ready = raw.get("ready", {})
    if not isinstance(ready, dict):
        problems.append("[ready] must be a table")
        ready = {}
    for key in ready:
        if key not in _READY_KEYS:
            problems.append("ready.%s is not a feeder setting" % key)
    defaults = Config()
    for spec in fields(Config):
        if spec.name not in values:
            continue
        value, default = values[spec.name], getattr(defaults, spec.name)
        if isinstance(default, bool):
            if not isinstance(value, bool):
                problems.append("%s must be true or false" % spec.name)
        elif isinstance(default, int):
            floor = 0 if spec.name in _ZERO_ALLOWED else 1
            if not isinstance(value, int) or isinstance(value, bool) or value < floor:
                problems.append("%s must be %s integer" % (
                    spec.name, "zero or a positive" if floor == 0 else "a positive"))
        elif isinstance(default, str):
            if not isinstance(value, str) or not value.strip():
                problems.append("%s must be a non-empty string" % spec.name)
        elif spec.name == "model_fallback":
            if not isinstance(value, dict) or not all(isinstance(item, str)
                                                      for item in value.values()):
                problems.append("models.fallback must be a table of model = \"model\"")
        elif spec.name in ("denied_ids",):
            # A GitHub number is most naturally written bare, so ids may be integers.
            if not isinstance(value, list) or not all(isinstance(item, (str, int))
                                                      and not isinstance(item, bool)
                                                      for item in value):
                problems.append("denied_ids must be an array of ids")
            else:
                values[spec.name] = tuple(str(item) for item in value)
        elif not _is_strings(value):
            # R9's rule for gate.command, applied to both commands here: an argument list,
            # never a shell string, so nothing in a path or a card is ever interpreted.
            problems.append("%s must be an array of strings%s" % (
                spec.name, " (an argument list, never a shell string)"
                if spec.name.endswith("_command") else ""))
        else:
            values[spec.name] = tuple(value)
    command = ready.get("command", [])
    if not _is_strings(command):
        problems.append("ready.command must be an array of strings (an argument list, never a "
                        "shell string)")
        command = []
    if "labels" in ready and not _is_strings(ready["labels"]):
        problems.append("ready.labels must be an array of strings")
    if "jql" in ready and not isinstance(ready["jql"], str):
        problems.append("ready.jql must be a string")
    values["ready_command"] = tuple(command)
    values["ready_source"] = {key: ready[key] for key in ("labels", "jql") if key in ready}
    # `allowed_models` is a tuple here only when it was valid or left at its default.
    allowed = values.get("allowed_models", Config.allowed_models)
    values["test_loop"] = _load_test_loop(loop_values,
                                          allowed if isinstance(allowed, tuple) else None,
                                          problems)
    if not problems:
        config = Config(**values)
        if config.default_model not in config.allowed_models:
            problems.append("models.default %r is not in models.allowed" % config.default_model)
        for source, target in sorted(config.model_fallback.items()):
            for model in (source, target):
                if model not in config.allowed_models:
                    problems.append("models.fallback names %r, which is not in models.allowed"
                                    % model)
            if source == target:
                problems.append("models.fallback sends %r to itself" % source)
        if config.post_cycle_mode not in HOOK_MODES:
            problems.append("hooks.post_cycle_mode must be %s" % " or ".join(
                '"%s"' % mode for mode in HOOK_MODES))
        elif config.post_cycle_hold and config.post_cycle_mode == HOOK_DETACHED:
            # A detached hook is never waited on, so there is no exit code to hold on.
            problems.append("hooks.post_cycle_hold needs post_cycle_mode = \"%s\"" % HOOK_BLOCKING)
    if problems:
        raise ConfigError("%s: %s" % (path, "; ".join(problems)))
    return config


def _test_loop_problem(name, value, default, allowed):
    """What is wrong with one `[test_loop]` value, or None. An empty string is the unset value
    and passes here; `_load_test_loop` asks for the keys an enabled loop needs."""
    if isinstance(default, bool):
        return None if isinstance(value, bool) else "must be true or false"
    if isinstance(default, int):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            return "must be a positive integer"
        return None
    if isinstance(default, str):
        if not isinstance(value, str):
            return "must be a string"
        if name == "design_note" or not value:
            return None
        if not value.strip():
            return "must not be blank"
        if name in ("model", "design_model") and allowed is not None and value not in allowed:
            # Checked like models.default, so a typo is refused at load, not at the first pass.
            return "%r is not in models.allowed" % value
        if name == "tour":
            parts = os.path.normpath(value).split(os.sep)
            if os.path.isabs(value) or value.startswith("~") or parts[0] == os.pardir:
                return "must be a path relative to the target repository, inside it"
        if name == "url":
            split = urllib.parse.urlsplit(value)
            if split.scheme not in ("http", "https") or not split.netloc:
                return "must be an http or https URL with a host"
        return None
    if not _is_strings(value):
        return "must be an array of strings%s" % (
            " (an argument list, never a shell string)" if name == "prepare" else "")
    if name == "prepare":
        return "must start with the program to run" if value and not value[0].strip() else None
    if any(not item.strip() for item in value):
        return "must not hold a blank string"
    if name == "allowed_tools" and not value:
        # An empty allow list is no allow list at launch, which is no bound on the Test process.
        return "must name at least one tool"
    return None


def _load_test_loop(values, allowed, problems):
    """The `[test_loop]` table's values as a TestLoop, appending every problem to `problems`,
    each one naming its `test_loop.` key. `allowed` is the models a loop model may name, or None
    when `models.allowed` is itself wrong and already named. No values gives the default, so a
    sidecar without the table loads exactly as it did before the loop existed (AE8)."""
    defaults, loaded = TestLoop(), {}
    for spec in fields(TestLoop):
        if spec.name not in values:
            continue
        value = values[spec.name]
        problem = _test_loop_problem(spec.name, value, getattr(defaults, spec.name), allowed)
        if problem:
            problems.append("test_loop.%s %s" % (spec.name, problem))
        else:
            loaded[spec.name] = tuple(value) if isinstance(value, list) else value
    loop = TestLoop(**loaded)
    if loop.enabled:
        for need in _TEST_LOOP_NEEDS:
            if need in values and need not in loaded:
                continue              # given in the wrong shape, and already named for it
            if not getattr(loop, need):
                problems.append("test_loop.enabled needs test_loop.%s" % need)
    return loop


# Pure helpers: text in, data out. Each is tested on its own.

def natural_key(task_id):
    """Sort `9` before `10` and `T-9` before `T-10`: digit runs compare as numbers."""
    return [(0, int(part)) if part.isdigit() else (1, part)
            for part in re.split(r"(\d+)", str(task_id)) if part]


def read_order(text):
    """The order file as {id: rank}. One id per line, highest priority first; anything after
    the id, and any line starting with `#`, is comment. The first mention of an id wins."""
    rank = {}
    for line in (text or "").splitlines():
        match = re.match(r"\s*([^\s#]+)", line)
        if match and match.group(1) not in rank:
            rank[match.group(1)] = len(rank)
    return rank


def read_routing(text, allowed):
    """The routing file as ({id: model}, [notes]). One line per card: id, model, optional
    comment. A model outside `allowed` is dropped and named in the notes, because a typo here
    would halt the card twice and get it excluded."""
    chosen, notes = {}, []
    for line in (text or "").splitlines():
        match = re.match(r"\s*([^\s#]+)\s+([^\s#]+)", line)
        if not match:
            continue
        task_id, model = match.groups()
        if model in allowed:
            chosen[task_id] = model
        else:
            notes.append("the routing file names %r for %s, not an allowed model, ignored"
                         % (model, task_id))
    return chosen, notes


def choose_model(card, routing, config):
    """(model, note). The routing file wins, then a `**Model:** name` line in the card's body,
    then the default. The body line is card text, which the manifest's `editors` sentence
    already says who may write, and the allowed set bounds what it can ask for."""
    if card["id"] in routing:
        return routing[card["id"]], None
    asked = MODEL_LINE_RE.search(card.get("description") or "")
    if asked:
        if asked.group(1) in config.allowed_models:
            return asked.group(1), None
        return config.default_model, ("card %s asks for model %r in its body, not an allowed "
                                      "model, ignored" % (card["id"], asked.group(1)))
    return config.default_model, None


def normalize_cards(payload):
    """The ready command's JSON as cards, or raise ValueError. Accepts the keys `gh` prints
    (`number`, `body`, labels as objects) beside the adapter's own (`id`, `description`), so a
    project's command can be a thin filter over `gh issue list --json`."""
    if not isinstance(payload, list):
        raise ValueError("expected a JSON array of cards")
    cards = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise ValueError("expected every card to be a JSON object")
        task_id = entry.get("id", entry.get("number"))
        if task_id in (None, ""):
            raise ValueError("a card carries neither an id nor a number")
        raw_labels = entry.get("labels") or []
        if not isinstance(raw_labels, list):
            raise ValueError("card %s carries labels that are not a JSON array" % task_id)
        labels = tuple(str(label.get("name") if isinstance(label, dict) else label)
                       for label in raw_labels)
        cards.append({"id": str(task_id), "title": str(entry.get("title") or ""),
                      "description": str(entry.get("description") or entry.get("body") or ""),
                      "labels": labels})
    return cards


def scan_reason(card):
    """None when `card` is clean, else the sentence the R41 scan (`brief.scan`) gives for it.
    Read at append time, this sees only the title and description the ready source returned;
    a card's comments and its rendered brief are read at launch, where the runner's own scan
    (`run.py`'s `_one_task`) stays the backstop for both (issue #41)."""
    hits = brief.scan(card, "")
    return brief.exclusion_reason(hits) if hits else None


def scanned_ids(cards):
    """{id: reason} for every card in `cards` the R41 scan refuses, computed once per cycle so
    `select`, the dry run, the skip report, and the idle check all read the same verdict rather
    than each calling `scan_reason` again over the same text."""
    reasons = {}
    for card in cards:
        reason = scan_reason(card)
        if reason:
            reasons[card["id"]] = reason
    return reasons


def select(cards, listed, config, rank, unsettled_count, scanned, held=()):
    """(fresh, batch). Fresh is every ready card a session may take that the manifest does not
    list yet, in order file order and then by id, including one the R41 scan would refuse.
    `scanned` is `scanned_ids`'s result, and the batch is the head of the ones outside it, as
    long as the room left: the batch size minus the tasks the next run will already launch. A
    card the scan refuses holds no room, so the next clean card in order fills its slot instead.
    Nor does a card in `held`, one routed to a model held back by a usage limit (issue #52)."""
    fresh = [card for card in cards
             if card["id"] not in listed and card["id"] not in config.denied_ids
             and not any(label in config.denied_labels for label in card.get("labels") or ())]
    fresh.sort(key=lambda card: (rank.get(card["id"], UNRANKED), natural_key(card["id"])))
    room = max(0, config.batch - unsettled_count)
    eligible = [card for card in fresh if card["id"] not in scanned and card["id"] not in held]
    return fresh, eligible[:room]


def mark_record(mark):
    """A `limits.Mark` as the state file keeps it under `exhausted`: the death's time, the
    expiry, and where the expiry came from (R4, R12)."""
    return {"since": mark.since.isoformat(timespec="seconds"),
            "until": mark.until.isoformat(timespec="seconds"), "source": mark.source}


def read_mark(value, fallback_hours):
    """The `limits.Mark` a state file entry holds, or None when it cannot be read. A bare time
    is a mark an older feeder wrote at the death, and it expires `fallback_hours` after it."""
    try:
        if isinstance(value, str):
            since = datetime.fromisoformat(value)
            return limits.Mark(since=since, until=since + timedelta(hours=fallback_hours),
                               source=limits.MARK_FALLBACK_HOURS)
        if isinstance(value, dict):
            return limits.Mark(since=datetime.fromisoformat(value["since"]),
                               until=datetime.fromisoformat(value["until"]),
                               source=str(value.get("source") or limits.MARK_FALLBACK_HOURS))
    except (KeyError, TypeError, ValueError):
        pass
    return None


def launched_this_cycle(record, before):
    """True when the run launched a process for this record: it carries a `started_at` the
    record read before the run did not (plan KTD9). A record the run refused before launch, or
    never reached, keeps the old attempt's stamp. The rule `limits.decide_after_run` applies;
    the feeder needs it first, to choose which logs to read and to read a run scoped halt."""
    started = record.get("started_at")
    return started is not None and started != (before or {}).get("started_at")


# The outside world.

@dataclass
class Deps:
    """Every effect the loop has, as a callable. `build_deps` supplies the real ones; a test
    replaces the few it cares about."""
    sleep: object
    now: object
    run_cycle: object          # (manifest_path, retry_ids, defer_ids) -> the runner's exit code
    # (manifest) -> the summary JSON as a dict, {} when none, with `terminal_written_at`, the
    # time the state's terminal record was written, beside the summary's own keys
    read_summary: object
    lease_held: object         # (manifest) -> True while a live runner holds this manifest
    build_adapter: object      # (manifest) -> a tracker adapter
    run_command: object        # (args, cwd, timeout) -> CompletedProcess
    notifier: object = None    # (body) or None
    # The post cycle hook. Both take (args, cwd, extra_env, output_path) and append the hook's
    # output to `output_path`. `run_hook` also takes a timeout and returns the exit code, raising
    # subprocess.TimeoutExpired past it; `start_hook` returns at once with the process started.
    run_hook: object = None
    start_hook: object = None
    # One browser test pass (browser test loop plan, U6, KTD2): (manifest_path, kind, cards,
    # stopped_areas, plan_areas, budget, model) -> the pass record as a dict, always one, with
    # `exit_code` beside the record's own keys. None on a Feeder whose loop is off.
    run_test_pass: object = None


def _state_store(manifest, env):
    """The state store, or None when this manifest has never run. Checked first because
    building a store creates its directory, and neither a dry run nor a read should."""
    home = env.get("HOME")
    directory = os.path.join(state_module.base_dir(home),
                             state_module.sha256_of(os.path.realpath(manifest.path)))
    if not os.path.exists(os.path.join(directory, "state.json")):
        return None
    return state_module.StateStore(manifest.path, manifest.project.repo, home=home)


def runner_entry():
    """`relay_cli.py` in the tree this module was loaded from. The feeder launches the runner
    from its own tree, so a feeder started from a pinned extract drives that extract and a
    checkout somebody is editing is never what runs."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "relay_cli.py")


def runner_tree():
    """The root of the tree `runner_entry` launches from: the directory holding `skills/`."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(runner_entry()))))


def checkout_warning(tree=None):
    """The sentence to show when the feeder's own tree is a git work tree, else None. Every
    cycle launches the runner from that tree, so an edit made there reaches the next task, even
    one in the same batch, since the runner reads brief templates while a batch is in flight."""
    tree = tree or runner_tree()
    top = gitread.work_tree_root(tree)
    if top is None:
        return None
    return ("this feeder launches the runner from %s, inside the git work tree %s, so every "
            "cycle runs whatever that checkout holds at that moment; start it from a pinned "
            "extract with `feed <manifest> --pin`" % (tree, top))


# An extract is `~/.relay/extracts/native-relay-<sha>` with the first 12 characters of the commit
# sha, the same length `git rev-parse --short=12` prints, so one made by hand is reused too.
PIN_SHA_LENGTH = 12


@dataclass(frozen=True)
class Pin:
    """What `feed --pin` extracts, worked out without writing anything (issue #48)."""
    branch: str          # the default branch whose commit is pinned
    sha: str             # that commit, in full
    destination: str     # the extract directory
    exists: bool         # the destination already holds a runner and will be reused untouched
    head_branch: str     # the branch the checkout sits on, `HEAD` when detached
    head_sha: object     # the commit HEAD is at, or None
    uncommitted: object  # the first changed path the extract does not hold, or None
    behind_origin: bool  # origin/<branch> has commits the local branch lacks

    @property
    def short(self):
        return self.sha[:PIN_SHA_LENGTH]


def _extract_entry(destination):
    return os.path.join(destination, "skills", "relay", "scripts", "relay_cli.py")


def _same_repository(one, other):
    try:
        return gitread.repo_identity(one) == gitread.repo_identity(other)
    except (gitread.GitError, OSError, subprocess.SubprocessError):
        return False


def pin_plan(tree, home, manifest):
    """The `Pin` for the work tree at `tree`, reading only. It pins the default branch's commit,
    never HEAD: on a self hosted checkout HEAD is the task branch being built while `--pin` is
    run, and an extract of it would run unmerged, ungated work at every later cycle. The branch
    is the manifest's `project.default_branch` when the manifest's repo is this repository, else
    `origin/HEAD` of this tree. An unresolvable branch raises OSError with a plain sentence."""
    if _same_repository(tree, manifest.project.repo):
        branch = manifest.project.default_branch or gitread.default_branch(tree)
        where = "project.default_branch is unset in the manifest and"
    else:
        branch = gitread.default_branch(tree)
        where = "the manifest's repo is not this checkout, and"
    if not branch:
        raise OSError("%s refs/remotes/origin/HEAD is not set in %s, so there is no default "
                      "branch to pin; name it once with `git -C %s remote set-head origin "
                      "<branch>`" % (where, tree, tree))
    sha = gitread.rev_parse(tree, "refs/heads/" + branch)
    if not sha:
        raise OSError("the default branch %s has no local branch in %s, so there is no commit "
                      "to extract" % (branch, tree))
    remote = gitread.rev_parse(tree, "refs/remotes/origin/" + branch)
    destination = os.path.join(home, ".relay", "extracts",
                               "native-relay-" + sha[:PIN_SHA_LENGTH])
    dirty = gitread.status_porcelain(tree).strip()
    return Pin(branch=branch, sha=sha, destination=destination,
               exists=os.path.isfile(_extract_entry(destination)),
               head_branch=gitread.current_branch(tree),
               head_sha=gitread.rev_parse(tree, "HEAD"),
               uncommitted=dirty.splitlines()[0].strip() if dirty else None,
               behind_origin=bool(remote) and not gitread.is_ancestor(tree, remote, sha))


# A partial folder's own `finally` covers an exception mid extract, never a kill or a power
# loss: neither leaves Python's cleanup code a turn to run. Left alone, one of those sits beside
# the extracts forever. Swept here at a day old (issue #61).
PARTIAL_MAX_AGE_SECONDS = 24 * 60 * 60


def _pid_alive(pid):
    """Whether `pid` names a process this user can see, sending it no signal (issue #61's own
    review of the sweep below): a folder's own mtime only moves when tar adds or removes an
    entry directly inside it, not when a long extraction is still writing into a subdirectory
    that folder already holds, so age alone cannot tell a live extraction from an abandoned one.
    An unreadable answer (no permission) is taken as alive, since the safe failure is to leave a
    folder in place too long, not to sweep a live one out from under it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def sweep_partials(destination, now=None):
    """Remove every `<name>.partial-<pid>` directory beside `destination` that is both older
    than a day and whose pid is no longer running, so a partial another `--pin` is still
    extracting is never mistaken for an abandoned one."""
    now = time.time() if now is None else now
    parent = os.path.dirname(destination)
    try:
        entries = os.listdir(parent)
    except OSError:
        return
    for name in entries:
        if ".partial-" not in name:
            continue
        path = os.path.join(parent, name)
        try:
            age = now - os.stat(path).st_mtime
        except OSError:
            continue
        if age <= PARTIAL_MAX_AGE_SECONDS:
            continue
        pid = name.rsplit(".partial-", 1)[-1]
        if pid.isdigit() and _pid_alive(int(pid)):
            continue
        shutil.rmtree(path, ignore_errors=True)


def pin_extract(tree, pin, run=subprocess.run):
    """Extract `pin.sha` from the repository at `tree` into `pin.destination` and return the
    directory. The directory is named for the sha, so it is the same directory every time the
    default branch is at the same commit, and an existing one that holds the runner is reused
    untouched, since a feeder may be running from it."""
    destination = pin.destination
    sweep_partials(destination)
    if not os.path.isfile(_extract_entry(destination)):
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        partial = "%s.partial-%d" % (destination, os.getpid())
        os.makedirs(partial)
        try:
            archive = subprocess.Popen(["git", "-C", tree, "archive", pin.sha],
                                       stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
            untar = run(["tar", "-x", "-C", partial], stdin=archive.stdout)
            archive.stdout.close()
            if archive.wait() != 0 or untar.returncode != 0:
                raise OSError("git archive or tar failed for %s" % tree)
            if os.path.isdir(destination):
                shutil.rmtree(destination)
            os.rename(partial, destination)
        finally:
            shutil.rmtree(partial, ignore_errors=True)
    return destination


# How long a command's process group gets to leave on SIGTERM before SIGKILL. Long enough for a
# `git fetch` under a wrapper to remove its own lock files, which a live Task process in the same
# repository would otherwise trip over; short beside the minute `status --queue` gives the read.
END_GROUP_GRACE_SECONDS = 5
END_GROUP_POLL_SECONDS = 0.05


def end_group(proc, grace_seconds=END_GROUP_GRACE_SECONDS):
    """End the process group `proc` leads, started with `start_new_session`, and reap `proc`.
    SIGTERM first, then SIGKILL for whatever is left once the whole group has had
    `grace_seconds` to go, not just the leader: a shell leader dies at once, and a `git` under it
    needs its moment too. The group id is `proc.pid`, which stays valid after the leader is
    reaped while any member lives."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        proc.poll()       # reap the leader, so a zombie does not keep the group alive
        try:
            os.killpg(proc.pid, 0)
        except OSError:
            break
        time.sleep(END_GROUP_POLL_SECONDS)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()


def build_deps(config, env, notifier=None, notify_on=False, sleep=None, child_stdout=None):
    """The real effects. `child_stdout` is where each run's output goes; None inherits the
    feeder's own, which under `--detach` is the output file beside the manifest."""

    def run_cycle(manifest_path, retry_ids=(), defer_ids=()):
        # A frozenset, so the ids stay named: never the bare flag, which retries every one.
        command = ([sys.executable, "-u", runner_entry(), "run", manifest_path]
                   + run_module.retry_blocked_argv(frozenset(retry_ids))
                   + run_module.defer_argv(frozenset(defer_ids)))
        if notify_on:
            command.append("--notify")
        if config.caffeinate and shutil.which("caffeinate", path=env.get("PATH")):
            # Keeps a Mac awake for the length of the run. Absent off macOS, and then skipped.
            command = ["caffeinate", "-i"] + command
        return subprocess.run(command, env=env, stdin=subprocess.DEVNULL, check=False,
                              stdout=child_stdout,
                              stderr=subprocess.STDOUT if child_stdout else None).returncode

    def read_summary(manifest):
        store = _state_store(manifest, env)
        if not store:
            return {}
        # The summary reads `limit_passed_over` from whatever terminal record the state holds,
        # which is the last run that wrote one. Its time says whether that run was this one.
        return dict(summary_module.build(manifest, store),
                    terminal_written_at=(store.terminal() or {}).get("written_at"))

    def lease_held(manifest):
        store = _state_store(manifest, env)
        return bool(store) and store.status_word() == "running"

    def run_command(args, cwd, timeout):
        # Its own process group, ended whole at the bound, for the reason `run_hook` gives
        # below (issue #63). A ready command that is a shell wrapper otherwise leaves its
        # children running in the repository, and one that holds the output pipe open keeps
        # the read waiting past the bound.
        with subprocess.Popen(list(args), cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              start_new_session=True) as proc:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except BaseException:
                end_group(proc)
                raise
        return subprocess.CompletedProcess(list(args), proc.returncode, stdout, stderr)

    def run_hook(args, cwd, extra_env, output_path, timeout):
        # Its own process group, and the whole group is ended on a timeout, an interrupt, or
        # the feeder itself being told to terminate (issue #56): a gate under `make` or a shell
        # script is a grandchild, and ending only the direct child would leave it running in the
        # checkout the next cycle merges into.
        with open(output_path, "ab") as output:
            proc = subprocess.Popen(list(args), cwd=cwd, env=dict(env, **extra_env),
                                    stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            # SIGTERM has no Python level exception of its own, unlike SIGINT's
            # KeyboardInterrupt, so without this the hook's group would outlive a feeder ended
            # by signal. The previous handler runs after the group is down, same as launch.py's
            # own SIGINT/SIGTERM handling around a Task process, including its guard against a
            # call off the main thread and against a disposition Python did not itself install
            # (`getsignal` then answers None, which `signal.signal` refuses as a handler).
            previous = signal.getsignal(signal.SIGTERM)
            ended = False

            def end_once():
                # A SIGTERM the handler catches then still unwinds through the `except
                # BaseException` below as the KeyboardInterrupt it raises, so without this the
                # group would be ended twice for one signal.
                nonlocal ended
                if not ended:
                    ended = True
                    end_group(proc)

            def handle(signum, frame):
                end_once()
                if callable(previous):
                    previous(signum, frame)
                else:
                    raise KeyboardInterrupt()

            installed = False
            try:
                signal.signal(signal.SIGTERM, handle)
                installed = True
            except ValueError:
                pass
            try:
                return proc.wait(timeout=timeout)
            except BaseException:
                end_once()
                raise
            finally:
                if installed and previous is not None:
                    try:
                        signal.signal(signal.SIGTERM, previous)
                    except ValueError:
                        pass

    def start_hook(args, cwd, extra_env, output_path):
        # Its own session, like `feed --detach`, so a hook that outlives the feeder is not
        # taken down with it. Never waited on: work that takes a person or a browser is the
        # reason this mode exists.
        with open(output_path, "ab") as output:
            return subprocess.Popen(list(args), cwd=cwd, env=dict(env, **extra_env),
                                    stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.STDOUT, start_new_session=True)

    def run_test_pass(manifest_path, kind, cards=(), stopped_areas=(), plan_areas=(),
                      budget=None, model=None):
        # The `test` verb, launched the way `run_cycle` launches `run` (KTD2), so the processes
        # a pass starts never share the feeder's own process or signal handling. Its output is
        # read for the record's path, the last line it prints, and then passed on to where a
        # run's output goes.
        command = ([sys.executable, "-u", runner_entry(), "test", manifest_path]
                   + pass_argv(kind, cards, stopped_areas, plan_areas, budget, model))
        if config.caffeinate and shutil.which("caffeinate", path=env.get("PATH")):
            command = ["caffeinate", "-i"] + command
        done = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, check=False,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = done.stdout or b""
        try:
            if child_stdout is not None:
                os.write(child_stdout.fileno(), output)
            else:
                sys.stdout.write(output.decode("utf-8", errors="replace"))
                sys.stdout.flush()
        except (OSError, ValueError, AttributeError):
            pass
        return read_pass_output(done.returncode, output.decode("utf-8", errors="replace"))

    return Deps(sleep=sleep or time.sleep, now=datetime.now, run_cycle=run_cycle,
                read_summary=read_summary, lease_held=lease_held,
                build_adapter=lambda manifest: adapters.build(manifest, env=env),
                run_command=run_command, notifier=notifier, run_hook=run_hook,
                start_hook=start_hook, run_test_pass=run_test_pass)


# The `test` verb's exit codes, as `cli.cmd_test` names them.
TEST_EXIT_RAN = 0
TEST_EXIT_REFUSED = 1
TEST_EXIT_HALTED = 2
TEST_EXIT_LEASE = 3


def pass_argv(kind, cards=(), stopped_areas=(), plan_areas=(), budget=None, model=None):
    """The `test` verb's arguments after the manifest for one pass. `--cards` comes last,
    because it takes every argument after it."""
    argv = []
    for name in stopped_areas:
        argv += ["--stopped-area", str(name)]
    for name in plan_areas:
        argv += ["--plan-area", str(name)]
    if budget is not None:
        argv += ["--budget", str(budget)]
    if model:
        argv += ["--model", str(model)]
    if kind == testloop.CHECK:
        argv += ["--cards"] + [str(card) for card in cards]
    else:
        argv.append("--tour")
    return argv


def read_pass_output(code, output):
    """The pass record the `test` verb left, from its exit code and its output, as a dict with
    `exit_code` and `record_path` beside the record's own keys. There is always one: a verb that
    wrote none, refused on configuration or on a held Lease, or one whose record cannot be read,
    reads as a pass that did not run or failed, with its last line of output as the reason, so
    the loop records it like any other and never mistakes it for a pass that ran."""
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    if code in (TEST_EXIT_RAN, TEST_EXIT_HALTED) and last.endswith(".json"):
        try:
            with open(last, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError) as exc:
            return {"status": testloop.FAILED, "exit_code": code, "record_path": last,
                    "reason": "the pass record %s could not be read: %s" % (last, exc)}
        if isinstance(record, dict):
            return dict(record, exit_code=code, record_path=last)
    if code == TEST_EXIT_LEASE:
        status = testloop.NOT_RUN
    else:
        status = testloop.FAILED
    return {"status": status, "exit_code": code, "record_path": None,
            "reason": "relay test exited %s: %s" % (code, last or "no output")}


def new_state():
    # `retry_blocked` is {id: the blocked record's started_at} for each blocked task the next
    # run is to relaunch. The stamp is how a retry the run never reached is told from one that
    # ran: every launch restamps it. `hold` is None, or the record a held post cycle hook left
    # (issue #53), which only `release_hold` clears. `last_held` is {id: model} for the Tasks the
    # last Cycle held, kept for `feed --status` alone: held is derived from the marks and the
    # fallback table each time the machine asks, and nothing reads this back.
    return {"halts": {}, "limit_waits": 0, "idle_waits": 0, "unreadable_waits": 0, "cycles": 0,
            "reported": {}, "refused": {}, "exhausted": {}, "retry_blocked": {}, "hold": None,
            "last_held": {}}


def new_loop_state(now):
    """The state file's `test_loop` key (browser test loop plan, KTD10). `rounds` counts the
    tours that ran; `passes` one summary per pass; `filed` {card id: generation, area, design,
    cause file} for each card the loop confirmed filed; `checks` {landed card id: the areas its
    check filed in}, which `testloop.area_patches` reads; `patches` its counts; `stopped_areas`
    the areas at the patch cap and `planned_areas` those a planning card was asked for; `stop`
    None, or the record of why the loop ended."""
    return {"started_at": now.isoformat(timespec="seconds"), "rounds": 0, "passes": [],
            "filed": {}, "checks": {}, "patches": {}, "stopped_areas": [], "planned_areas": [],
            "stop": None}


def loop_stop_sentence(reason, loop, settings):
    """The sentence for a `testloop` stop word, for the log, the notice, and the stop record."""
    if reason == testloop.STOP_CLEAN:
        return "a full tour found nothing above low"
    if reason == testloop.STOP_OPEN_FINDINGS:
        return ("a full tour's high and medium findings produced no new card: each went to an "
                "open card, a stopped area, or was never confirmed")
    if reason == testloop.STOP_ROUNDS:
        return "%d tours ran, the max_rounds cap of %d" % (loop.get("rounds", 0),
                                                         settings.max_rounds)
    if reason == testloop.STOP_CLOCK:
        return "the loop started at %s and max_hours is %d" % (loop.get("started_at"),
                                                               settings.max_hours)
    if reason == testloop.STOP_BUDGET:
        return "%d cards filed, the max_cards_total cap of %d" % (len(loop.get("filed") or {}),
                                                                  settings.max_cards_total)
    if reason == testloop.STOP_REPORT_ONLY:
        return ("report only mode runs one full tour, and its findings are in the findings "
                "file beside the manifest")
    return reason


def _torn_line(path):
    """True when `path` exists, holds at least one byte, and its last byte is not a newline
    (issue #56): a feeder killed mid write to the events file leaves a fragment, and the next
    line appended straight onto it would glue into one line neither valid JSON nor readable."""
    try:
        with open(path, "rb") as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                return False
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) != b"\n"
    except OSError:
        return False


class Feeder:
    def __init__(self, paths, config, deps, env, out, dry_run=False, once=False,
                 retry_blocked=(), clear_limits=False):
        self.paths, self.config, self.deps = paths, config, deps
        self.env, self.out = env, out
        self.dry_run, self.once = dry_run, once
        self.clear_limits = clear_limits          # `feed --clear-limits --restart`
        self.requested = tuple(retry_blocked)     # `feed --retry-blocked ID`, taken once
        self.name = os.path.basename(os.path.splitext(paths.manifest)[0])
        self.state = self._load_state()
        self.pid = os.getpid()
        # (reason word, sentence) for the `leaving` event, set where the feeder decides to go.
        self.leave_reason = None
        # Extra fields the `leaving` event carries beside `leave_reason`, such as the ids of a
        # scanned out queue a `--once` run met instead of waiting (issue #58).
        self.leave_extra = {}
        # False until `run` has read the state file under the lock. A state file that could
        # not be read then holds the halt counts, so nothing may save over it.
        self.recording = False
        # (Popen, cycle) for each detached post cycle hook not yet seen to finish. Polled at the
        # start of each cycle, so a finished one is reaped on purpose and its exit is logged.
        self.detached = []
        # (id, model) for each Task this process last saw held, so a hold is one `limit` event
        # when it begins and not one more at every Cycle it lasts through.
        self.held_seen = set()

    # Reporting.
    def log(self, message):
        line = log_line(self.deps.now(), message)
        if not self.dry_run:
            append_log(self.paths, line)
        self.out.write(line + "\n")
        if hasattr(self.out, "flush"):
            self.out.flush()

    def notify(self, message):
        if self.deps.notifier is not None and not self.dry_run:
            self.deps.notifier("feeder %s: %s" % (self.name, message))

    def emit(self, event, **fields):
        """Append one JSON line to the events file and keep it in the state file too, as the
        last event and, for a cycle, the last cycle, so `feed --status` reads one file. Every
        line names its manifest and pid, so a stream of two feeders' lines cannot be misread."""
        if self.dry_run:
            return
        record = dict(fields, at=self.deps.now().isoformat(timespec="seconds"), event=event,
                      manifest=self.paths.manifest, pid=self.pid, cycle=self.state["cycles"])
        try:
            prefix = "\n" if _torn_line(self.paths.events) else ""
            with open(self.paths.events, "a", encoding="utf-8") as handle:
                handle.write(prefix + json.dumps(record, sort_keys=True) + "\n")
        except OSError as exc:
            # The events are for watchers. A feeder that stopped feeding over one would be the
            # outcome they exist to catch, so the loop goes on and the log says what was lost.
            self.log("the events file could not be written, %s lost: %s" % (event, exc))
        self.state["last_event"] = record
        if event == EVENT_CYCLE_STARTED:
            self.state["last_cycle"] = {"started": record, "result": None}
        elif event == EVENT_CYCLE_RESULT and self.state.get("last_cycle"):
            self.state["last_cycle"]["result"] = record
        if self.recording:
            self.save_state()

    def report_once(self, key, message):
        """Log and notify something that stays true across cycles, a skipped card above all,
        the first time only. Without this a skip would notify every ninety minutes for a day."""
        if self.state["reported"].get(key) == message:
            return
        self.state["reported"][key] = message
        self.log(message)
        self.notify(message)

    # State.
    def _load_state(self):
        loaded = new_state()
        try:
            loaded.update(read_state(self.paths))
        except ConfigError as exc:
            raise ConfigError("%s. %s" % (exc, STATE_HINT))
        return loaded

    def save_state(self):
        if not self.dry_run:
            write_state(self.paths, self.state)

    # The loop.
    def run(self):
        self.log("feeder start, dry_run=%s, manifest=%s, runner=%s"
                 % (self.dry_run, self.paths.manifest, runner_entry()))
        warning = checkout_warning()
        if warning:
            self.log("warning: " + warning)
        try:
            # Read again now that the lock is held. Under `--restart` the state was first read
            # while the old feeder still ran, and its last cycle's halts, marks, and queued
            # retries would be overwritten by that copy at the first save below.
            self.state = self._load_state()
        except ConfigError as exc:
            return self.leave(self.stop(EXIT_CONFIG, "stopping: %s" % exc, "state_unreadable"))
        self.recording = True
        if not self.once:
            # The queue's "in a row" counts belong to one feeder's life. A stop, a restart, or an
            # interrupt would otherwise hand a partial count to the next feeder. `--once` keeps
            # them, since a feeder driven a cycle at a time by cron has no other life. The usage
            # limit row is not among them: a restart is no evidence the account came back.
            for key in QUEUE_STREAKS:
                self.state[key] = 0
        if self.clear_limits:
            # `feed --clear-limits --restart`: cleared here, under the lock, so the feeder that
            # just left cannot save its marks back over the clearing.
            self.log(cleared_sentence(clear_limit_state(self.state)))
        # A scanned out card is named at every process start, not only the first one for the
        # life of the state file (issue #58): `reported` otherwise carries `scan_skip:<id>`
        # forward for ever, and an operator who missed the first line never sees another. Unlike
        # QUEUE_STREAKS this is not guarded by `not self.once`: a `--once` cron start is as much a
        # start as a long lived process is, and the mechanism paragraph names it explicitly. A
        # tight `--once` cron line against a persistently dirty card therefore renotifies every
        # tick rather than once; that repetition is the tradeoff this issue asks for, naming the
        # card at every start, not a bug to quiet later.
        # Safe to clear blindly because `scanned_ids` is recomputed from the ready cards, unasked,
        # at the top of every cycle (below), so a card still dirty is renamed within one cycle.
        self.state["reported"] = {key: message for key, message in self.state["reported"].items()
                                  if not key.startswith("scan_skip:")}
        # Written while the lock is held, so the pid here is the lock holder's (issue #36).
        self.state["process"] = {
            "pid": self.pid, "hostname": socket.gethostname(),
            "started_at": self.deps.now().isoformat(timespec="seconds"),
            "manifest": self.paths.manifest, "runner": runner_entry(),
            "runner_tree": runner_tree(), "once": self.once, "cycle": self.state["cycles"]}
        self.emit(EVENT_STARTED, runner_tree=runner_tree(), once=self.once)
        try:
            while True:
                code = self.cycle()
                if code is not None:
                    return self.leave(code)
        except KeyboardInterrupt:
            self.log("interrupted")
            self.leave_reason = ("interrupted", "interrupted")
            return self.leave(EXIT_INTERRUPTED)
        except Exception as exc:
            # A feeder that dies silently is the worst outcome: say so, then let it raise.
            message = "feeder crashed: %s: %s" % (type(exc).__name__, exc)
            self.log(message)
            self.notify("crashed: %s" % type(exc).__name__)
            self.leave_reason = ("crashed", message)
            try:
                self.leave(None)
            except Exception:
                # The crash is often a disk that refuses writes, and recording it would then
                # raise over the exception that says so.
                pass
            raise

    def leave(self, code):
        """Stamp the process record and write the `leaving` event. `code` is None only for a
        crash, which leaves by raising. `leave_extra` rides beside `leave_reason`, for a fact a
        bare reason word would otherwise drop, such as a scanned out queue's ids under `--once`
        (issue #58)."""
        reason, message = self.leave_reason or ("once", "left after one cycle")
        process = self.state.get("process")
        if process and process.get("pid") == self.pid:
            process.update(left_at=self.deps.now().isoformat(timespec="seconds"),
                           exit_code=code, left_reason=reason, left_message=message)
        self.emit(EVENT_LEAVING, exit_code=code, reason=reason, message=message,
                  **self.leave_extra)
        return code

    def wait(self, seconds, reason, scan_refused=None):
        """Sleep and go round again, or under --once leave without sleeping. `reason` is the
        word the `waiting` event carries: lease_held, usage_limit, model_held, unreadable_source,
        idle, or idle_scanned when `scan_refused` names the cards holding a scanned out queue
        back from a true empty one (issue #58). Those ids ride on the `waiting` event and, under
        `--once`, on the `leaving` event too, through `leave_extra`, so neither reads as a plain
        idle wait when the board in fact held work the launch scan alone was holding back."""
        extra = {"scan_refused": scan_refused} if scan_refused else {}
        if self.once:
            self.leave_reason = ("once", "left after one cycle instead of waiting (%s)" % reason)
            self.leave_extra = extra
            return EXIT_OK
        until = self.deps.now() + timedelta(seconds=seconds)
        self.emit(EVENT_WAITING, reason=reason, seconds=seconds,
                  until=until.isoformat(timespec="seconds"), **extra)
        self.deps.sleep(seconds)
        return None

    def stop(self, code, message, reason):
        """Leave with `code`. `reason` is the word the `leaving` event and the process record
        carry, so a watcher never has to parse `message`."""
        self.leave_reason = (reason, message)
        self.log(message)
        self.notify(message)
        return code

    def strike(self, key, maximum):
        """Count one more of something that happens in a row, save, and say whether it has now
        happened more than `maximum` times. The three streaks share this so they share one rule
        for the threshold; each is reset to zero where its run is broken."""
        self.state[key] += 1
        self.save_state()
        return self.state[key] > maximum

    def cycle(self):
        """One pass. Returns an exit code to leave with, or None to go round again."""
        config, deps = self.config, self.deps
        self.reap_detached()
        if os.path.exists(self.paths.stop):
            if RESTART_WORD in self._read(self.paths.stop).split():
                return self.stop(EXIT_OK, "stop file present, a restart is taking over, leaving",
                                 RESTART_WORD)
            return self.stop(EXIT_OK, "stop file present, leaving", "stop_file")
        if self.state.get("hold"):
            # `cmd_feed` refuses before it starts a feeder, so this is mostly the one that took
            # over by `--restart` from a feeder that held on its way out. Logged every time and
            # not notified: the hold itself already was, and a cron `--once` would repeat it.
            message = "stopping: " + hold_sentence(self.paths.manifest, self.state["hold"])
            self.leave_reason = (HOLD_WORD, message)
            self.log(message)
            return EXIT_HALTED
        try:
            manifest = manifest_module.load(self.paths.manifest, allow_no_tasks=True)
        except manifest_module.ManifestError as exc:
            return self.stop(EXIT_CONFIG, "stopping: %s" % exc, "manifest_error")
        for reason, problem in (("ready_source", ready_source_problem(manifest, config)),
                                ("checkout", checkout_problem(manifest))):
            if problem:
                return self.stop(EXIT_CONFIG, "stopping: %s. Relay owns the default branch while "
                                              "it runs, so this is a person's to look at."
                                 % problem, reason)
        if not self.dry_run and deps.lease_held(manifest):
            # The manifest is about to be rewritten, and a live runner read it at its start.
            self.log("a runner holds the lease on this manifest, not appending, waiting")
            return self.wait(config.lease_wait_seconds, "lease_held")
        if not self.dry_run:
            if self.loop_on():
                # Before the pre cycle hook, so a board whose ready labels it derives derives
                # them for what the tour filed too (KTD9).
                self.start_tour(manifest)
            self.pre_cycle(manifest)

        text = self._read(self.paths.manifest)
        listed = manifestedit.task_ids(text)
        excluded = manifestedit.excluded_ids(text)
        summary = deps.read_summary(manifest)
        records = {task["id"]: task for task in summary.get("tasks", [])}
        if self.requested:
            code = self.take_requests(listed, excluded, records)
            if code is not None:
                return code
        marks = self.exhausted_models()
        launch = self.start_cycle(listed, excluded, records, marks)
        # The moves above rewrote the manifest, so the append reads it again.
        text = self._read(self.paths.manifest)
        cards, readable = self.ready_cards(manifest)
        scanned = scanned_ids(cards)
        routing, notes = read_routing(self._read(self.paths.routing), config.allowed_models)
        # Routed once per cycle: the held set, the batch, and the dry run all read this.
        routes = {card["id"]: self.route(card, routing, marks) for card in cards
                  if card["id"] not in listed}
        held = {card_id for card_id, (model, _) in routes.items() if model is None}
        fresh, batch = select(cards, set(listed), config, read_order(self._read(self.paths.order)),
                              len(launch.running), scanned, held)
        held_fresh = [card["id"] for card in fresh
                      if card["id"] in held and card["id"] not in scanned]
        if not self.dry_run:
            # A held card is work the mark holds back as much as a held Task is, so `--status`
            # names it beside them, on the model it is routed to.
            self.state.setdefault("last_held", {}).update(
                {card["id"]: choose_model(card, routing, config)[0] for card in fresh
                 if card["id"] in held_fresh})
        entries = []
        for card in batch:
            model, note = routes[card["id"]]
            notes += note
            entries.append({"id": card["id"], "title": card["title"], "model": model,
                            "effort": config.default_effort})
        for note in notes:
            self.log(note)
        if held_fresh or launch.held:
            self.log("held back by a model's mark, with no free model along the fallback chain: "
                     "cards %s and tasks %s, until a mark expires"
                     % (held_fresh, [task_id for task_id, _ in launch.held]))
        entries = [entry for entry in entries
                   if self.state["refused"].get(entry["id"]) != entry["model"]]
        self.log("cycle %d: %d unsettled in the manifest, %d ready and unlisted, appending %s"
                 % (self.state["cycles"], len(launch.running), len(fresh),
                    [(entry["id"], entry["model"]) for entry in entries]))
        if self.dry_run:
            for card in fresh[:DRY_RUN_LINES]:
                reason = scanned.get(card["id"])
                if reason:
                    self.out.write("   would skip %s: %s\n" % (card["id"], reason))
                elif card["id"] in held:
                    self.out.write("   would hold %s on %s until its mark expires: %s\n" % (
                        card["id"], choose_model(card, routing, config)[0], card["title"][:90]))
                else:
                    self.out.write("   would offer %s on %s: %s\n" % (
                        card["id"], routes[card["id"]][0], card["title"][:90]))
            return EXIT_OK

        for card in fresh:
            key = "scan_skip:" + card["id"]
            reason = scanned.get(card["id"])
            if reason:
                self.report_once(key, "%s would be skipped at launch and "
                                 "is left out of the batch: %s" % (card["id"], reason))
            else:
                # A card that scans clean and later trips the scan again is news again
                # (issue #58), the same rule `queue_retry` applies to a retry that blocks again.
                self.state["reported"].pop(key, None)

        appended = self.append(text, entries)
        if appended:
            self.state["idle_waits"] = self.state["unreadable_waits"] = 0
        elif not launch.running and not batch and (held_fresh or launch.held):
            # Work is waiting on a model's mark, not missing (R7). The wait ends no later than
            # the earliest mark that holds that work, so it is bounded without a count of its
            # own, and it is no evidence either way about the whole cycle wait's row. A batch
            # that was taken and then all refused is a routing problem, and `idle` stops on it.
            # `plan_cycle_start`'s own wait covers held Tasks only; a held fresh card is the
            # feeder's, so the wait is taken from the same function over both.
            models = {model for _, model in launch.held}
            models |= {choose_model(card, routing, config)[0] for card in fresh
                       if card["id"] in held_fresh}
            seconds = limits.hold_wait(models, marks, config.model_fallback, deps.now(),
                                       config.limit_wait_seconds) or config.limit_wait_seconds
            self.log("nothing to run until a held model's mark expires, waiting %ds" % seconds)
            return self.wait(seconds, limits.WAIT_MODEL_HELD)
        elif not launch.running:
            # A card the scan refuses never reached model routing, so it is not evidence of a
            # routing problem (issue #41); only a card that got that far belongs in this check.
            # `scan_refused` is issue #58's own case: waits like the rest, but must not read as
            # a true empty queue, so it is threaded separately rather than folded into fresh_ids.
            fresh_ids, scan_refused = [], []
            for fresh_card in fresh:
                card_id = fresh_card["id"]
                if card_id in scanned:
                    scan_refused.append(card_id)
                elif card_id not in held:
                    fresh_ids.append(card_id)
            return self.idle(readable, fresh_ids, scan_refused, manifest=manifest)

        self.state["cycles"] += 1
        self.state.get("process", {})["cycle"] = self.state["cycles"]
        cycle_ids = list(launch.running) + [entry["id"] for entry in appended]
        self.emit(EVENT_CYCLE_STARTED, appended=[entry["id"] for entry in appended],
                  tasks=cycle_ids, retry_blocked=list(launch.retry), deferred=list(launch.defer))
        if launch.retry:
            self.log("relaunching blocked %s with --retry-blocked" % list(launch.retry))
        if launch.defer:
            self.log("leaving %s alone with --defer while its model is held" % list(launch.defer))
        merge = self.default_head(manifest)
        code = deps.run_cycle(self.paths.manifest, launch.retry, launch.defer)
        self.log("relay run exited %s" % code)
        if code == EXIT_LEASE:
            self.emit_result(code)
            self.log("another runner holds the lease, waiting")
            return self.wait(config.lease_wait_seconds, "lease_held")
        if code == EXIT_CONFIG:
            self.emit_result(code)
            return self.stop(EXIT_CONFIG, "relay refused the manifest or the environment. Run "
                                          "validate and read its output.", "run_refused")
        return self.settle(manifest, cycle_ids, code, merge, CycleContext(
            before=records, deferred=frozenset(launch.defer), retries=launch.queue,
            terminal=summary.get("terminal_written_at")))

    def emit_result(self, code, by_status=None):
        """The `cycle_result` event: the run's exit code and this cycle's ids by status."""
        by_status = by_status or {}
        self.emit(EVENT_CYCLE_RESULT, run_exit=code, **{
            status: sorted((task["id"] for task in by_status.get(status, ())), key=natural_key)
            for status in (STATUS_LANDED, STATUS_HALTED, STATUS_BLOCKED, STATUS_SKIPPED)})

    def idle(self, readable, fresh_ids, scan_refused=(), manifest=None):
        """Nothing was appended and no listed task is left to run, while no runner holds the
        lease. Four things look like that and only one is a true empty queue.

        A ready source that could not be read is not an empty queue: the feeder waits and asks
        again, and after `UNREADABLE_WAITS_MAX` waits in a row it stops for a person, because a
        feeder that retried a broken read for ever is a process doing nothing. Ready cards that
        were all refused by validate are not an empty queue either: the board has work, and
        only a person changing the routing can release it, so the feeder stops and names them.

        Ready cards the launch scan refuses (`scan_refused`) wait like an empty queue, since a
        card wanting a reword is not the same urgency as a routing mismatch (issue #41), but the
        `leaving` event must not call that a true empty queue either (issue #58): a watcher
        reading `empty_queue` off the last event would conclude nothing was ever ready, when the
        board in fact held work the scan alone was holding back. `empty_queue_scanned` says so
        and names the cards. The same fact rides on every wait along the way there too, not only
        the terminal one: with `idle_waits_max` above zero the `waiting` event's reason is
        `idle_scanned` rather than the bare `idle` a genuinely empty queue waits under, carrying
        the same ids, and a `--once` run that meets the same cycle carries them on its `leaving`
        event instead of losing them to the generic `once`.

        What is left is a true empty queue. By default the feeder leaves at once rather than
        keep a process alive to poll an empty board. `idle_waits_max` above zero waits that many
        times first, for a board where a person releases cards through the day. With the browser
        test loop on, a full tour runs before that leave, and when it filed a card the ready
        source returns, the feeder goes round to build it instead (KTD9)."""
        config = self.config
        if not readable:
            if self.strike("unreadable_waits", UNREADABLE_WAITS_MAX):
                return self.stop(EXIT_CONFIG, "stopping: the ready source could not be read for "
                                              "%d cycles in a row and nothing is left to run. "
                                              "Read the log." % (UNREADABLE_WAITS_MAX + 1),
                                 "unreadable_source")
            self.log("the ready source could not be read and nothing is left to run, waiting")
            return self.wait(config.idle_wait_seconds, "unreadable_source")
        self.state["unreadable_waits"] = 0
        if fresh_ids:
            return self.stop(EXIT_CONFIG, "stopping: every ready card was refused with the model "
                                          "it is routed to, and nothing is left to run: %s. "
                                          "Change the routing and start the feeder again."
                                          % ", ".join(fresh_ids), "all_refused")
        if self.strike("idle_waits", config.idle_waits_max):
            if scan_refused:
                return self.stop(EXIT_OK, "the queue is empty except for cards the launch scan "
                                          "refuses, leaving: %s. Nothing else is ready, nothing "
                                          "is left to run, and no runner holds the lease. Reword "
                                          "the named cards to release them."
                                          % ", ".join(scan_refused), "empty_queue_scanned")
            if self.drain_tour(manifest):
                self.state["idle_waits"] = 0
                self.save_state()
                return EXIT_OK if self.once else None
            return self.stop(EXIT_OK, "the queue is empty, leaving: nothing ready, nothing left "
                                      "to run, and no runner holds the lease. Everything left "
                                      "on the board is blocked, denied or attended, or there "
                                      "is nothing left.", "empty_queue")
        if scan_refused:
            self.log("nothing ready except cards the launch scan refuses, and nothing unsettled, "
                     "waiting (%d of %d): %s" % (self.state["idle_waits"], config.idle_waits_max,
                                                 ", ".join(scan_refused)))
            return self.wait(config.idle_wait_seconds, "idle_scanned", scan_refused=scan_refused)
        self.log("nothing ready and nothing unsettled, waiting (%d of %d)"
                 % (self.state["idle_waits"], config.idle_waits_max))
        return self.wait(config.idle_wait_seconds, "idle")

    def settle(self, manifest, cycle_ids, code, merge, start):
        """Read what the run did to this cycle's tasks, apply rules 2 and 3, and run the post
        cycle hook. `code` is the run's exit code, for the `cycle_result` event and the hook,
        and `merge` the default branch and its sha before the run, for the hook's merge range.

        The rules come first, so this cycle's halt counts and queued retries are saved before a
        hook that may run for an hour, and a feeder killed during it loses none of them. A wait
        the rules ask for is only returned here, and taken after the hook, so a usage limit
        wait never delays it. A cycle the rules stop still runs it. A hold the hook asks for
        replaces what would have come next, going round or a wait; a stop the rules already
        made stands, and the hold is in the log and in the `post_cycle` event either way. But the
        rules' own stop already logged and notified its own reason, not the hold's, so the hold
        is notified here too whatever the rules decided (issue #53): otherwise a hold that lands
        beside a rules stop is recorded and logged but never reaches the operator, and every later
        refusal stays quiet on the reasoning that the hold itself already did. Either way the
        hold is in the state file too, and blocks every later start until `release_hold`."""
        data = self.deps.read_summary(manifest)
        after = {task["id"]: task for task in data.get("tasks", [])}
        mine = {task_id: after[task_id] for task_id in cycle_ids if task_id in after}
        by_status = {status: [task for task in mine.values() if task.get("status") == status]
                     for status in (STATUS_HALTED, STATUS_LANDED, STATUS_BLOCKED, STATUS_SKIPPED)}
        self.emit_result(code, by_status)
        self.log("cycle result: landed %s, halted %s, blocked %s, skipped %s" % tuple(
            sorted(task["id"] for task in by_status[status])
            for status in (STATUS_LANDED, STATUS_HALTED, STATUS_BLOCKED, STATUS_SKIPPED)))
        outcome = self.apply_rules(data, after, mine, by_status, start,
                                   self.passed_over(data, start))
        held = self.post_cycle(manifest, code, by_status, merge)
        if (self.loop_on() and not held
                and not (isinstance(outcome, int) and outcome != EXIT_OK)):
            # After the hook (KTD9), and never on a default branch a held hook or a stopping
            # rule has put in question.
            self.check_landed(manifest, [task["id"] for task in by_status[STATUS_LANDED]])
        if isinstance(outcome, Pending):
            return self.hold(held) if held else self.wait(outcome.seconds, outcome.reason)
        if held:
            if outcome in (None, EXIT_OK):
                return self.hold(held)
            self.notify(self._hold_message(held))
        return outcome

    @staticmethod
    def passed_over(data, start):
        """The ids this Cycle's run passed over under R11, from the summary's
        `limit_passed_over`. That key is the last terminal record's, so it is trusted only when
        no run is live and the record was written since this Cycle's run started: a run refused
        on the lease, or killed before it wrote one, leaves the record of the run before it."""
        written = data.get("terminal_written_at")
        if (data.get("run_status") == "running" or not written or written == start.terminal):
            return frozenset()
        return frozenset(str(entry.get("task")) for entry in data.get("limit_passed_over") or ()
                         if isinstance(entry, dict) and entry.get("task") is not None)

    def apply_rules(self, data, after, mine, by_status, start, passed_over=frozenset()):
        """Rule 2 and the usage limit machine over this cycle's tasks, as `settle` read them
        from the summary. `after` is every record, `mine` {id: record} this Cycle's. Returns an
        exit code, None to go round, or a `Pending` wait for `settle` to take."""
        config = self.config
        for task in by_status[STATUS_SKIPPED]:
            # The original script counted a skip as settled and told nobody, so a card Relay
            # would never build sat in the manifest looking handled.
            self.report_once("skipped:" + task["id"], "%s was skipped by the runner and will not "
                             "be built until the card is fixed: %s"
                             % (task["id"], task.get("skip_reason") or "no reason recorded"))
        self.prune_retries(after)
        # Only a death this run launched is read, so an old attempt's log is never news (KTD9).
        readings = {task_id: limits.read_death(record, limits.log_tail(record.get("log_path")),
                                               config.quick_death_seconds)
                    for task_id, record in mine.items()
                    if record.get("status") in (STATUS_HALTED, STATUS_BLOCKED)
                    and launched_this_cycle(record, start.before.get(task_id))}
        manifest_text = self._read(self.paths.manifest)
        if data.get("run_status") == contracts.RUN_HALTED:
            halt_task, halt_class = data.get("halt_task"), data.get("halt_class")
            on_limit = readings.get(halt_task, (None, None))[0] == limits.CONFIRMED
            if halt_class in contracts.RUN_SCOPED_HALT_CLASSES and not on_limit:
                # The remote moved, the lease was lost, or the runner itself failed. None of
                # that is the task's doing, so counting it would exclude an innocent card on the
                # next cycle and then the card after it. The original script had this cascade.
                for task in by_status[STATUS_BLOCKED]:
                    if task["id"] not in self.state["retry_blocked"]:
                        self.report_blocked(task)
                self.save_state()
                return self.stop(EXIT_CONFIG, "stopping: the run halted on %s with class %s, "
                                              "which puts something outside the task in "
                                              "question. No halt was counted. Read the summary."
                                              % (halt_task, halt_class), "run_scoped_halt")
            if on_limit:
                # Its own log says the account's limit ended it, whatever the runner made of
                # that, and the run stopped there. A halted record listed after it is the old
                # attempt's, never reached: counting it would exclude a card for the account's
                # limit. One listed ahead of it that the run did not launch was refused before
                # launch and passed, and still counts, so a card the run refuses every Cycle is
                # still excluded and a person told (R3).
                listed = manifestedit.task_ids(manifest_text)
                unreached = frozenset(listed[listed.index(halt_task) + 1:]
                                      if halt_task in listed else ())
                self.log("the run halted on %s with class %s, and its log confirms a usage "
                         "limit: read as the limit, and no halt counted for what it never "
                         "reached" % (halt_task, halt_class))
                passed_over = frozenset(passed_over) | {
                    task_id for task_id, record in mine.items()
                    if task_id in unreached and record.get("status") == STATUS_HALTED
                    and not launched_this_cycle(record, start.before.get(task_id))}
        listed_on = manifestedit.task_models(manifest_text)
        died_on = {task_id: record.get("model") or listed_on.get(task_id)
                   for task_id, record in mine.items()}
        died_at = {task_id: limits.death_time(mine[task_id]) for task_id in readings}
        facts = limits.CycleFacts(
            after=mine, now=self.deps.now(),
            before={task_id: start.before[task_id] for task_id in mine
                    if task_id in start.before},
            readings=readings, died_on=died_on, listed_on=listed_on,
            died_at={task_id: moment for task_id, moment in died_at.items() if moment},
            fallback=config.model_fallback, marks=self.exhausted_models(),
            streak=self.state["limit_waits"], halts=dict(self.state["halts"]),
            deferred=start.deferred, passed_over=passed_over, retries=start.retries,
            settings=config)
        decision = limits.decide_after_run(facts)

        died = {}
        for task_id, (reading, _) in sorted(readings.items(), key=lambda item: natural_key(item[0])):
            if reading == limits.CONFIRMED:
                died.setdefault(died_on.get(task_id), []).append(task_id)
        self.write_marks(decision.marks, decision.notify, died)
        marks = dict(facts.marks, **decision.marks)
        refused = self.write_moves(decision.moves, marks)
        holds = list(decision.holds) + refused
        self.announce_holds(holds, marks)
        self.state.setdefault("last_held", {}).update(holds)
        for task_id in decision.retry:
            self.queue_retry(mine[task_id])
        if decision.retry:
            self.log("%s blocked on a usage limit and will be retried with --retry-blocked"
                     % list(decision.retry))
        for task_id in decision.report:
            self.report_blocked(mine[task_id])
        self.state["halts"].update(decision.halts)
        self.state["limit_waits"] = decision.streak
        for task_id in decision.exclude:
            task, count = mine[task_id], decision.halts[task_id]
            reason = "excluded by the feeder after %d halts, last class %s: %s" % (
                count, task.get("class"), task.get("cause") or "")
            try:
                text = self._read(self.paths.manifest)
                edited = manifestedit.exclude_task(text, task_id, reason)
                if edited is not None:
                    manifestedit.commit(self.paths.manifest, text, edited, env=self.env)
            except manifestedit.EditError as exc:
                # Rule 2 cannot be kept, and without it this task relaunches on every cycle.
                self.save_state()
                return self.stop(EXIT_CONFIG, "stopping: %s halted %d times and could not be "
                                              "excluded: %s" % (task_id, count, exc),
                                 "exclusion_failed")
            self.log("%s %s" % (task_id, reason))
            self.notify("%s excluded after %d halts, %s" % (task_id, count, task.get("class")))
        self.save_state()
        outcome = decision.outcome
        if outcome.kind == limits.LEAVE:
            return self.stop(EXIT_HALTED, "every task has died quickly for %d waits with nothing "
                                          "in its log to confirm a usage limit. Not a usage "
                                          "limit, or one that outlasts the waits. Read the "
                                          "summary." % config.limit_waits_max, outcome.reason)
        if outcome.kind == limits.WAIT:
            self.log("nothing landed and every task that died this cycle died inside %ds, one "
                     "with no result line in its log, reading that as a usage limit, waiting "
                     "%ds; these deaths are not counted"
                     % (config.quick_death_seconds, outcome.seconds))
            return Pending(outcome.seconds, outcome.reason)
        return EXIT_OK if self.once else None

    # Per model usage limits (usage limit plan, R4, R5).
    def exhausted_models(self):
        """{model: limits.Mark} for the models still marked. A mark that has expired, or that
        cannot be read, is dropped here and logged, so the next card routed to that model runs
        on it again. This only removes: `write_marks` is the one writer of a mark."""
        now, hours = self.deps.now(), self.config.fallback_hours
        active, kept = {}, {}
        for model, value in sorted(self.state["exhausted"].items()):
            mark = read_mark(value, hours)
            if mark is not None and mark.until > now:
                active[model], kept[model] = mark, value
                continue
            self.log("%s's mark expired at %s, routing to it again" % (
                model, mark.until.isoformat(timespec="seconds") if mark else "an unreadable time"))
        if kept != self.state["exhausted"]:
            self.state["exhausted"] = kept
            self.save_state()
        return active

    def write_marks(self, marks, notify, died):
        """Write each {model: limits.Mark} into `exhausted`, replacing any mark the model had
        (KTD5), and say so. `notify` is the models that were not marked before, the only ones
        the operator is told about (R4); `died` {model: [id]} the confirmed deaths behind each."""
        for model, mark in sorted(marks.items()):
            self.state["exhausted"][model] = mark_record(mark)
            ids = died.get(model) or []
            until = mark.until.isoformat(timespec="seconds")
            source = ("the reset the CLI printed" if mark.source == limits.MARK_CLI
                      else "%dh after the death" % self.config.fallback_hours)
            message = ("%s died of %s's usage limit, a 429 in the log: %s is marked until %s, %s"
                       % (", ".join(ids), model, model, until, source))
            if model in notify:
                message += ", and nothing is launched on it until then"
                self.log(message)
                self.notify(message)
            else:
                self.log(message + ", replacing its earlier mark")
            self.emit(EVENT_LIMIT, action=LIMIT_MARK, model=model, to=None, tasks=ids,
                      until=until)
        if marks:
            self.save_state()

    def write_moves(self, moves, marks):
        """Move each (id, from, to) in `moves` to its fallback in the manifest, and return
        [(id, from)] for the moves the manifest edit refused. Each of those stays where it is,
        on a marked model, so it is held, not launched there."""
        refused, done = [], {}
        for task_id, source, target in moves:
            if self.dry_run:
                self.out.write("   would move %s from %s to %s\n" % (task_id, source, target))
                continue
            try:
                text = self._read(self.paths.manifest)
                edited = manifestedit.set_model(text, task_id, target)
                if edited is not None:
                    manifestedit.commit(self.paths.manifest, text, edited, env=self.env)
            except manifestedit.EditError as exc:
                self.log("%s could not be moved from %s to %s, so it is held on %s: %s"
                         % (task_id, source, target, source, exc))
                refused.append((task_id, source))
                continue
            done.setdefault((source, target), []).append(task_id)
        for (source, target), ids in sorted(done.items()):
            mark = marks.get(source)
            until = mark.until.isoformat(timespec="seconds") if mark else None
            self.log("%s moved from %s to %s while %s is marked until %s"
                     % (", ".join(ids), source, target, source, until))
            self.emit(EVENT_LIMIT, action=LIMIT_MOVE, model=source, to=target, tasks=ids,
                      until=until)
        return refused

    def announce_holds(self, holds, marks):
        """One `limit` event and one log line per model for each (id, model) in `holds` that
        this process has not seen held already, and remember them. `until` is the earliest
        expiry along the model's fallback chain, the moment the Task can run somewhere."""
        now, by_model = self.deps.now(), {}
        for task_id, model in holds:
            if (task_id, model) not in self.held_seen:
                by_model.setdefault(model, []).append(task_id)
        for model, ids in sorted(by_model.items(), key=lambda item: str(item[0])):
            seconds = limits.hold_wait({model}, marks, self.config.model_fallback, now,
                                       HOLD_SECONDS_MAX)
            until = ((now + timedelta(seconds=seconds)).isoformat(timespec="seconds")
                     if seconds else None)
            self.log("%s held on %s, with no free model along its fallback chain, until %s"
                     % (", ".join(ids), model, until))
            self.emit(EVENT_LIMIT, action=LIMIT_HOLD, model=model, to=None, tasks=ids,
                      until=until)
        self.held_seen |= set(holds)

    def start_cycle(self, listed, excluded, records, marks):
        """The start of a Cycle (R5): every unsettled Task and queued retry the manifest lists on
        a marked model is moved to a free fallback or held, through `limits.plan_cycle_start`.
        A move the manifest edit refuses holds its Task too. Returns a `Launch`."""
        queue = self.queued_retries(listed, excluded, records)
        unsettled = [task_id for task_id in listed if task_id not in excluded
                     and records.get(task_id, {}).get("status") not in SETTLED]
        start = limits.plan_cycle_start(
            unsettled, queue, manifestedit.task_models(self._read(self.paths.manifest)), marks,
            self.config.model_fallback, self.deps.now(), self.config,
            default_model=self.config.default_model)
        refused = self.write_moves(start.moves, marks)
        stuck = {task_id for task_id, _ in refused}
        held = list(start.held) + refused
        retry = tuple(task_id for task_id in start.retry if task_id not in stuck)
        defer = tuple(list(start.defer) + [task_id for task_id in limits.natural_order(stuck)
                                           if task_id not in queue])
        running = tuple([task_id for task_id in unsettled
                         if task_id not in stuck and task_id not in start.defer] + list(retry))
        if not self.dry_run:
            self.announce_holds(held, marks)
            self.held_seen = set(held)
            self.state["last_held"] = {task_id: model for task_id, model in held}
        return Launch(running=running, retry=retry, defer=defer, held=tuple(held), queue=queue)

    def route(self, card, routing, marks):
        """(model, [notes]): `choose_model`, then its fallback while that model is marked. The
        model is None for a card routed to a held model, one `select` leaves out rather than
        append onto a model that would refuse it in seconds."""
        model, note = choose_model(card, routing, self.config)
        notes = [note] if note else []
        # One reading of the marks, so a mark that expires between two checks is not held by
        # one and routed round by the other.
        table, now = self.config.model_fallback, self.deps.now()
        active = limits.active_marks(marks, now)
        if limits.is_held(model, active, table, now):
            return None, notes
        if model in active:
            target = limits.resolve_fallback(model, table, set(active))
            notes.append("card %s is routed to %s, marked until %s, appending it on %s" % (
                card["id"], model, active[model].until.isoformat(timespec="seconds"), target))
            model = target
        return model, notes

    # The browser test loop (browser test loop plan, U6, KTD9, KTD10). Every decision is asked
    # of `testloop`; this only gathers the facts, starts the pass, and records the answer.
    def loop_on(self):
        return self.config.test_loop.enabled and not self.dry_run

    def loop_state(self):
        """The `test_loop` key of the state file, made at the first pass point of a loop that is
        on. A Feeder whose loop is off never calls this, so its state is today's (AE8)."""
        fresh = new_loop_state(self.deps.now())
        loop = self.state.get("test_loop")
        if not isinstance(loop, dict):
            loop = self.state["test_loop"] = fresh
        for key, value in fresh.items():
            loop.setdefault(key, value)
        return loop

    def loop_stopped(self):
        loop = self.state.get("test_loop")
        return isinstance(loop, dict) and bool(loop.get("stop"))

    @staticmethod
    def loop_started_at(loop):
        """The loop's start, or the earliest time there is when it cannot be read, so a damaged
        state file ends the loop on its clock rather than restarting the clock (R16)."""
        try:
            return datetime.fromisoformat(loop["started_at"])
        except (KeyError, TypeError, ValueError):
            return datetime.min

    def loop_model(self):
        """The model a pass runs on: the loop's own, moved along its `[models] fallback` chain
        while it is marked, or None when every model on the chain is held (step 8)."""
        model = self.config.test_model
        table, now = self.config.model_fallback, self.deps.now()
        active = limits.active_marks(self.exhausted_models(), now)
        if limits.is_held(model, active, table, now):
            return None
        if model in active:
            target = limits.resolve_fallback(model, table, set(active))
            self.log("the test pass model %s is marked until %s, running the pass on %s"
                     % (model, active[model].until.isoformat(timespec="seconds"), target))
            return target
        return model

    def start_tour(self, manifest):
        """At the first Cycle of a loop with no tour that ran, a full tour before the ready
        read (R5). A tour that did not run counts for nothing, so the next Cycle tries again."""
        if self.loop_stopped() or self.loop_state()["rounds"]:
            return
        self.test_pass(manifest, testloop.TOUR)

    def check_landed(self, manifest, landed):
        """After a Cycle that landed cards, a check of the ones `testloop` selects (R4, R18)."""
        if not landed or self.loop_stopped():
            return
        cards = testloop.cards_to_check(landed, self.loop_state()["filed"])
        if not cards:
            self.log("this cycle landed only last generation cards %s, so no check runs"
                     % _ids(landed))
            return
        self.test_pass(manifest, testloop.CHECK, cards)

    def drain_tour(self, manifest):
        """In `idle`, before leaving on a true empty queue, a full tour (R5). True when it
        confirmed a filed card the ready source returns, so the feeder goes round to build it
        rather than leave; a filed card the ready source does not return was notified by
        `record_pass`, and touring again would only file past it."""
        if manifest is None or not self.loop_on() or self.loop_stopped():
            return False
        done = self.test_pass(manifest, testloop.TOUR)
        if done is None or not done.ready:
            return False
        self.log("the drain tour filed %s, going round to build them instead of leaving"
                 % _ids(done.ready))
        return True

    def test_pass(self, manifest, kind, cards=()):
        """One pass of `kind`, through `Deps.run_test_pass`, recorded by `record_pass`. Returns
        its `PassDone`, or None when no pass started: the loop is stopped, the clock or the card
        budget stops it now, or every model on the loop model's chain is held."""
        loop, settings = self.loop_state(), self.config.test_loop
        if loop.get("stop"):
            return None
        # Only the clock and the card budget stop a pass that did not run, so asking about one
        # here is asking whether either has run out before this pass starts.
        reason = testloop.should_stop(testloop.PassResult(kind=kind, status=testloop.NOT_RUN),
                                      loop["rounds"], self.loop_started_at(loop),
                                      self.deps.now(), len(loop["filed"]),
                                      report_only=settings.report_only, settings=settings)
        if reason:
            self.stop_loop(reason)
            return None
        model = self.loop_model()
        if model is None:
            self.log("the %s test pass waits for the next pass point: %s is held, with no free "
                     "model along its fallback chain" % (kind, self.config.test_model))
            return None
        stopped = tuple(loop["stopped_areas"])
        plan = tuple(area for area in stopped if area not in loop["planned_areas"])
        budget = max(0, settings.max_cards_total - len(loop["filed"]))
        self.log("starting a %s test pass on %s%s, stopped areas %s, planning %s, budget %d"
                 % (kind, model, " of %s" % _ids(cards) if cards else "", _ids(stopped),
                    _ids(plan), budget))
        record = self.deps.run_test_pass(self.paths.manifest, kind, cards=tuple(cards),
                                         stopped_areas=stopped, plan_areas=plan, budget=budget,
                                         model=model)
        return self.record_pass(manifest, kind, tuple(str(card) for card in cards), plan,
                                record if isinstance(record, dict) else {})

    def record_pass(self, manifest, kind, sent, plan, record):
        """Record one pass under `test_loop` and ask `testloop.should_stop` (KTD10): each new
        confirmed card with its generation, area, design flag, and cause file; the areas each
        checked card's check filed in, and from them the patch counts and the areas newly at
        the cap (R19); the round, for a tour that ran; one `test_pass` event. A pass that did not
        run or failed is notified once per reason and counts as no round."""
        loop, settings, now = self.loop_state(), self.config.test_loop, self.deps.now()
        status = record.get("status")
        reason = str(record.get("reason") or "")
        if status not in (testloop.RAN, testloop.NOT_RUN, testloop.FAILED):
            reason = "the pass record carries no status the loop knows: %r" % (status,)
            status = testloop.FAILED
        new, attended = [], set()
        for entry in record.get("filed") or ():
            if not isinstance(entry, dict) or entry.get("id") in (None, ""):
                continue
            card_id = str(entry["id"])
            if card_id in loop["filed"]:
                continue
            parent = entry.get("card")
            loop["filed"][card_id] = {
                "generation": testloop.generation_for(kind, parent, sent, loop["filed"]),
                "area": entry.get("area"), "design": entry.get("design") is True,
                "cause_file": entry.get("cause_file"), "attended": entry.get("attended") is True,
                "kind": kind, "pass": record.get("pass")}
            new.append(card_id)
            if entry.get("attended") is True:
                attended.add(card_id)
            if kind == testloop.CHECK and parent is not None and str(parent) in sent:
                areas = loop["checks"].setdefault(str(parent), [])
                if entry.get("area") not in areas:
                    areas.append(entry.get("area"))
        commented = [str(entry.get("id")) for entry in record.get("commented") or ()
                     if isinstance(entry, dict) and entry.get("id") is not None]
        if kind == testloop.TOUR and status == testloop.RAN:
            loop["rounds"] += 1
        if status == testloop.RAN:
            loop["planned_areas"] += [area for area in plan if area not in loop["planned_areas"]]
        patches = testloop.area_patches(loop["filed"], loop["checks"],
                                        settings.max_patches_per_area)
        loop["patches"] = dict(patches.counts)
        for area in patches.reached:
            if area in loop["stopped_areas"]:
                continue
            loop["stopped_areas"].append(area)
            self.report_once("test_area:" + area, "the %s area took %d patches and still fails: "
                             "the loop stops testing it and files one attended planning card "
                             "for it at the next pass" % (area, patches.counts[area]))
        loop["passes"].append({
            "pass": record.get("pass"), "kind": kind, "status": status, "reason": reason,
            "at": now.isoformat(timespec="seconds"), "cards": list(sent), "filed": new,
            "commented": commented, "planned": list(plan) if status == testloop.RAN else [],
            "record_path": record.get("record_path")})
        self.log("test pass %s, a %s, %s%s: filed %s, commented %s" % (
            record.get("pass"), kind, status, ": " + reason if reason else "", _ids(new),
            _ids(commented)))
        if status != testloop.RAN:
            # Keyed by the sentence itself, so each reason is notified once for the life of the
            # state file however the passes between it run (step 7), and the log has every one.
            message = "the %s test pass %s: %s" % (
                kind, "was not run" if status == testloop.NOT_RUN else "failed",
                reason or "no reason recorded")
            key = "test_pass:" + message
            if key not in self.state["reported"]:
                self.state["reported"][key] = message
                self.notify(message)
        self.emit(EVENT_TEST_PASS, kind=kind, status=status, reason=reason,
                  pass_number=record.get("pass"), cards=list(sent), filed=new,
                  commented=commented, planned=list(plan) if status == testloop.RAN else [],
                  record_path=record.get("record_path"),
                  transcripts=record.get("transcripts") or {})
        ready = self.ready_filed([card_id for card_id in new if card_id not in attended],
                                 manifest)
        result = testloop.PassResult(
            kind=kind, status=status, new_cards=len(new),
            findings=tuple(finding for finding in record.get("findings") or ()
                           if isinstance(finding, dict)))
        stop = testloop.should_stop(result, loop["rounds"], self.loop_started_at(loop), now,
                                    len(loop["filed"]), report_only=settings.report_only,
                                    settings=settings)
        if stop:
            self.stop_loop(stop, record)
        self.save_state()
        return PassDone(new=tuple(new), ready=tuple(ready))

    def ready_filed(self, filed, manifest):
        """The cards in `filed` the ready source returns. Each one it does not return is
        notified once, naming them (step 10): a configuration problem for the operator, not a
        reason for another tour. An attended planning card is left out by the caller, since it
        is a person's and a ready source is right not to return it. A source that cannot be read
        is no evidence, and every card is taken as ready for the next Cycle's own read."""
        if not filed:
            return []
        cards, readable = self.ready_cards(manifest)
        if not readable:
            return list(filed)
        offered = {str(card["id"]) for card in cards}
        missing = [card_id for card_id in filed if card_id not in offered]
        if missing:
            self.report_once("test_unready", "the test loop filed %s and the tracker confirmed "
                             "them, but the ready source does not return them, so the feeder "
                             "will not build them; check that the ready source admits a card "
                             "carrying the loop's labels" % ", ".join(missing))
        return [card_id for card_id in filed if card_id in offered]

    def stop_loop(self, reason, record=None):
        """End the loop for `reason`, a `testloop` stop word: the stop record, one
        `test_loop_stopped` event, and one notice (R20). The feeder goes on building the cards
        already filed under its ordinary rules, and starts no further pass."""
        loop = self.loop_state()
        sentence = loop_stop_sentence(reason, loop, self.config.test_loop)
        loop["stop"] = {"reason": reason, "message": sentence,
                        "at": self.deps.now().isoformat(timespec="seconds"),
                        "pass": (record or {}).get("pass")}
        message = ("the browser test loop stopped, %s: %s. The feeder goes on building the cards "
                   "already filed" % (reason, sentence))
        self.log(message)
        self.notify(message)
        self.emit(EVENT_TEST_LOOP_STOPPED, reason=reason, message=sentence,
                  pass_number=(record or {}).get("pass"), rounds=loop["rounds"],
                  cards_filed=len(loop["filed"]))
        self.save_state()

    # Blocked tasks and their retries (issue #39).
    def report_blocked(self, task):
        self.report_once("blocked:" + task["id"], "%s blocked; a later run will not retry it "
                         "without --retry-blocked %s" % (task["id"], task["id"]))

    def queue_retry(self, task):
        self.state["retry_blocked"][task["id"]] = task.get("started_at")
        # A retry that blocks again is news, even when its sentence is the one sent last time.
        self.state["reported"].pop("blocked:" + task["id"], None)

    def prune_retries(self, after):
        """Drop each queued retry the run dealt with. A record that was launched again carries a
        new `started_at`; one that moved off blocked with the old stamp was refused first, at
        pre flight or over a stranded branch, and its halt is counted by the machine. One the
        run never reached, because it halted first or its model is held, keeps its place."""
        for task_id, stamp in list(self.state["retry_blocked"].items()):
            record = after.get(task_id)
            if (record is None or record.get("started_at") != stamp
                    or record.get("status") != STATUS_BLOCKED):
                del self.state["retry_blocked"][task_id]

    def queued_retries(self, listed, excluded, records):
        """The ids still queued for a retry. A queued id that is no longer listed, is excluded,
        or no longer reads blocked has nothing to retry, and is dropped here rather than carried
        for ever."""
        queue = self.state["retry_blocked"]
        keep = {task_id: stamp for task_id, stamp in queue.items()
                if task_id in listed and task_id not in excluded
                and records.get(task_id, {}).get("status") == STATUS_BLOCKED}
        if keep != queue:
            self.state["retry_blocked"] = keep
            self.save_state()
        return frozenset(keep)

    def take_requests(self, listed, excluded, records):
        """`feed --retry-blocked ID`: queue those blocked tasks and no others. An id the
        manifest does not list stops the feeder, since a typo would otherwise retry nothing and
        say nothing; one that is excluded or not blocked is logged and passed over."""
        requested, self.requested = self.requested, ()
        unknown = [task_id for task_id in requested if task_id not in listed]
        if unknown:
            return self.stop(EXIT_CONFIG, "stopping: --retry-blocked names %s, not a task in "
                                          "the manifest" % ", ".join(unknown), "retry_unknown")
        for task_id in requested:
            record = records.get(task_id, {})
            if task_id in excluded:
                self.log("%s is excluded in the manifest; nothing to retry until that line goes"
                         % task_id)
                continue
            if record.get("status") != STATUS_BLOCKED:
                self.log("%s reads %s, not blocked; nothing to retry"
                         % (task_id, record.get("status") or "no record"))
                continue
            self.queue_retry(record)
            self.log("%s is queued for a retry at the operator's request" % task_id)
        self.save_state()
        return None

    # The post cycle hook (issue #37).
    def default_head(self, manifest, branch=None):
        """(branch, sha) for the target's default branch, either None when there is no post
        cycle hook to read it for or it cannot be read. Read before `relay run` and again after
        it on the branch the first read found, so the two ends of the merge range are one
        branch's. A git that hangs is a timeout here, logged like any other failure."""
        if not self.config.post_cycle_command:
            return None, None
        try:
            branch = branch or default_branch_of(manifest)
            return branch, gitread.rev_parse(manifest.project.repo, "refs/heads/" + branch)
        except (gitread.GitError, OSError, subprocess.SubprocessError) as exc:
            self.log("the default branch could not be read for the post cycle hook: %s" % exc)
            return branch, None

    def post_cycle(self, manifest, code, by_status, merge):
        """Run the post cycle hook for the cycle `settle` just read, if the sidecar names one.
        Returns the sentence to hold the feeder with, which only a failed blocking hook with
        `post_cycle_hold` on produces, else None.

        The hook learns the cycle from its environment, never from its arguments, so the
        argument list stays exactly what the sidecar says. Its output goes to its own file, and
        the feeder log and the `post_cycle` event carry the result, a blocking hook's exit code
        or the reason it could not run. A detached hook has no result to carry yet at this point:
        its `post_cycle` event is written when it starts, with its pid and no exit code, and the
        feeder log and a later cycle's own start report what it exited once it is reaped."""
        config = self.config
        if not config.post_cycle_command:
            return None
        repo = manifest.project.repo
        branch, base = merge
        branch, head = self.default_head(manifest, branch)
        # None when either end could not be read: "unknown", which a hook must not take for
        # "nothing merged", so it is its own value rather than an empty range.
        moved = base != head if base and head else None
        outcome = {"manifest": self.paths.manifest, "repo": repo, "cycle": self.state["cycles"],
                   "run_exit": code, "default_branch": branch, "merge_base": base,
                   "merge_head": head, "merge_moved": moved,
                   "merge_range": "%s..%s" % (base, head) if moved else ""}
        for status in (STATUS_LANDED, STATUS_HALTED, STATUS_BLOCKED, STATUS_SKIPPED):
            outcome[status] = sorted((task["id"] for task in by_status.get(status, ())),
                                     key=natural_key)
        extra = hook_environment(outcome)
        mode, command = config.post_cycle_mode, list(config.post_cycle_command)
        said = {key: outcome[key] for key in ("merge_base", "merge_head", "merge_moved",
                                               "merge_range")}
        where = merge_words(said)
        try:
            with open(self.paths.hook_out, "a", encoding="utf-8") as handle:
                handle.write("%s cycle %d post_cycle %s: %s\n" % (
                    self.deps.now().isoformat(timespec="seconds"), self.state["cycles"], mode,
                    " ".join(command)))
        except OSError as exc:
            self.log("the post cycle hook's output file could not be written: %s" % exc)
        if mode == HOOK_DETACHED:
            try:
                proc = self.deps.start_hook(command, repo, extra, self.paths.hook_out)
            except (OSError, subprocess.SubprocessError) as exc:
                self.log("the post cycle hook could not start: %s" % exc)
                self.emit(EVENT_POST_CYCLE, mode=mode, hook_pid=None, error=str(exc), **said)
                return None
            pid = proc.pid
            self.detached.append((proc, self.state["cycles"]))
            self.log("the post cycle hook started detached, pid %d, merge range %s, output in %s"
                     % (pid, where, self.paths.hook_out))
            self.emit(EVENT_POST_CYCLE, mode=mode, hook_pid=pid, error=None, **said)
            return None
        # A blocking hook can run for up to `post_cycle_timeout_seconds`, an hour by default,
        # with nothing else to say a feeder is inside it: without this, `feed --status` shows
        # `cycle_result` as the last event throughout (issue #56). `where` carries the same
        # three way reading `post_cycle`'s own log line and hook output use, so `status_lines`
        # need not collapse an unread default branch into the same word as an empty range.
        self.emit(EVENT_POST_CYCLE_STARTED, mode=mode, where=where, **said)
        exit_code, failure = None, None
        try:
            exit_code = self.deps.run_hook(command, repo, extra, self.paths.hook_out,
                                           config.post_cycle_timeout_seconds)
            if exit_code != 0:
                failure = "exited %d" % exit_code
        except subprocess.TimeoutExpired:
            failure = "timed out after %ds" % config.post_cycle_timeout_seconds
        except (OSError, subprocess.SubprocessError) as exc:
            failure = "could not run: %s" % exc
        held = bool(failure) and config.post_cycle_hold
        self.log("the post cycle hook %s%s, merge range %s, output in %s" % (
            failure or "exited 0", ", holding the feeder" if held else "", where,
            self.paths.hook_out))
        if held:
            # Saved before the event and before `settle` decides how to leave, and saved even
            # when the rules already stop the feeder: the gate failed whichever way it goes, and
            # a feeder started again must not run its next cycle on top of it (issue #53).
            self.state["hold"] = dict(said, at=self.deps.now().isoformat(timespec="seconds"),
                                      cycle=self.state["cycles"], failure=failure,
                                      default_branch=branch, hook_out=self.paths.hook_out)
            self.save_state()
        self.emit(EVENT_POST_CYCLE, mode=mode, exit_code=exit_code, error=failure, held=held,
                  **said)
        return failure if held else None

    def _hold_message(self, failure):
        return ("stopping: the post cycle hook %s and post_cycle_hold is on. Read %s and repair "
                "the default branch, then release the hold with `feed %s --release` and start "
                "the feeder again." % (failure, self.paths.hook_out, self.paths.manifest))

    def hold(self, failure):
        return self.stop(EXIT_HALTED, self._hold_message(failure), HOLD_WORD)

    def reap_detached(self):
        """Poll each detached hook this feeder started, and log and forget the ones that have
        finished. A hook still running is left alone: it is in its own session and may outlive
        the feeder, which is what detached is for."""
        running = []
        for proc, cycle in self.detached:
            code = proc.poll()
            if code is None:
                running.append((proc, cycle))
            else:
                self.log("the detached post cycle hook from cycle %d, pid %s, exited %s, output "
                         "in %s" % (cycle, proc.pid, code, self.paths.hook_out))
        self.detached = running

    # The steps.
    def pre_cycle(self, manifest):
        if not self.config.pre_cycle_command:
            return
        try:
            done = self.deps.run_command(self.config.pre_cycle_command, manifest.project.repo,
                                         COMMAND_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log("the pre cycle command could not run: %s" % exc)
            return
        if done.returncode != 0:
            self.log("the pre cycle command exited %d: %s %s" % (
                done.returncode, (done.stdout or "").strip()[-200:],
                (done.stderr or "").strip()[-200:]))

    def ready_cards(self, manifest):
        """(cards, readable). An unreadable tracker is not a crash: it offers no cards, the
        reason is logged, the tasks already listed still run, and the next cycle asks again.
        `readable` keeps that apart from an empty board, which is what ends an idle feeder."""
        cards, reason = read_ready(manifest, self.config, self.deps)
        if reason is not None:
            self.log("the ready source could not be read, offering nothing new: %s" % reason)
            return [], False
        return cards, True

    def append(self, text, entries):
        """Append the batch, and return the entries that made it in. When the batch as a whole
        does not validate, each card is tried alone, so one card routed to a model its backend
        refuses cannot starve the two beside it. A refused card is remembered with the model
        that was refused, and offered again only once its routing changes."""
        if not entries:
            return []
        stamp = self.deps.now().strftime("%Y-%m-%d %H:%M")
        try:
            manifestedit.commit(self.paths.manifest, text,
                                manifestedit.append_tasks(text, entries, stamp), env=self.env)
            return list(entries)
        except manifestedit.EditError as exc:
            if len(entries) == 1:
                self.state["refused"][entries[0]["id"]] = entries[0]["model"]
                message = "%s on %s was not appended: %s" % (entries[0]["id"],
                                                             entries[0]["model"], exc)
                self.log(message)
                self.notify(message)
                return []
            self.log("the batch was not appended, trying each card alone: %s" % exc)
        appended = []
        for entry in entries:
            appended += self.append(self._read(self.paths.manifest), [entry])
        return appended

    @staticmethod
    def _read(path):
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()


def ready_source_problem(manifest, config):
    """None when the feeder has a way to learn which cards are ready, else the sentence to stop
    with. GitHub needs labels and Jira a query; the markdown tracker needs nothing, and a ready
    command replaces all of that. The adapter answers the same gap with a reason at read time,
    but a gap is not a failed read, so it is refused here before a cycle rather than retried."""
    if config.ready_command:
        return None
    adapter, source = manifest.tracker.adapter, config.ready_source
    if adapter == "github" and not source.get("labels"):
        return "no ready labels are configured; set [ready] labels in the feeder sidecar"
    if adapter == "jira" and not str(source.get("jql") or "").strip():
        return "no ready query is configured; set [ready] jql in the feeder sidecar"
    return None


def read_ready(manifest, config, deps, timeout=COMMAND_TIMEOUT_SECONDS):
    """(cards, None) or ([], reason). The one read of the ready source, shared by the loop and
    by `status`, so the two can never disagree about what the board holds."""
    try:
        if config.ready_command:
            done = deps.run_command(config.ready_command, manifest.project.repo, timeout)
            if done.returncode != 0:
                raise ValueError("it exited %d: %s" % (done.returncode,
                                                       (done.stderr or "").strip()[-300:]))
            return normalize_cards(json.loads(done.stdout or "null")), None
        cards, reason = deps.build_adapter(manifest).ready(config.ready_source)
        if reason:
            raise ValueError(reason)
        return cards, None
    except (ValueError, OSError, subprocess.SubprocessError,
            adapters.ConfigurationError) as exc:
        return [], str(exc)


# `status --queue` is a question an operator is waiting on, not a cycle, so a ready command gets
# a minute there rather than the loop's fifteen, and its whole process group ends at that bound.
# An adapter read keeps its own network timeouts, which this does not shorten.
STATUS_READY_TIMEOUT_SECONDS = 60


def ready_queue(manifest, env, deps=None):
    """([(id, model)], None) for the ready cards the next cycles would take, or (None, sentence)
    when that cannot be worked out (issue #50). Relay writes nothing here: no lock, no state
    write, no pre cycle hook. It does run the sidecar's ready command in the target repository,
    or read the tracker, and what that command does beside a live run is the operator's, so
    only `status --queue` calls this and plain `status` never does (issue #63).

    The filter is the loop's `select` and `scanned_ids`: not listed in the manifest (the cycle
    estimate prices those), not denied by id or label, not refused by the R41 scan. Then a card
    already refused with the model it is routed to is dropped, as the loop drops it. The model is
    `choose_model`'s, without the exhausted fallback, because that mark lasts hours and the queue
    it prices lasts longer; the refused check uses the same model, so while a fallback is active
    it can disagree with the loop about a card refused on one side of it."""
    paths = paths_for(manifest.path)
    try:
        config = load_config(paths.config)
    except ConfigError as exc:
        return None, "the feeder sidecar could not be loaded: %s" % exc
    problem = ready_source_problem(manifest, config)
    if problem:
        return None, problem
    deps = deps or build_deps(config, env)
    cards, reason = read_ready(manifest, config, deps, timeout=STATUS_READY_TIMEOUT_SECONDS)
    if reason is not None:
        return None, "the ready source could not be read: %s" % reason
    try:
        routing, _ = read_routing(Feeder._read(paths.routing), config.allowed_models)
    except (OSError, ValueError) as exc:
        return None, "the routing file could not be read: %s" % exc
    try:
        refused = read_state(paths).get("refused") or {}
    except ConfigError as exc:
        # The feeder itself stops on this file, so the queue figure has nothing to stand on.
        return None, str(exc)
    if not isinstance(refused, dict):
        return None, "%s holds a refused set that is not a JSON object" % paths.state
    listed = {task.id for task in manifest.tasks}
    scanned = scanned_ids(cards)
    fresh, _ = select(cards, listed, config, {}, 0, scanned)
    queue = []
    for card in fresh:
        if card["id"] in scanned:
            continue
        model, _ = choose_model(card, routing, config)
        if refused.get(card["id"]) != model:
            queue.append((card["id"], model))
    return queue, None


def default_branch_of(manifest):
    """The branch the runner merges into: the manifest's, else the repo's, else `main`."""
    return (manifest.project.default_branch or gitread.default_branch(manifest.project.repo)
            or "main")


def hook_environment(outcome):
    """The post cycle hook's extra environment: each key of `outcome` as `RELAY_<KEY>`, a list
    as its ids joined by single spaces, a bool as `true` or `false`, None as empty, and the
    whole of it again as JSON in `RELAY_CYCLE_JSON` for a hook that would rather parse one
    value. `RELAY_CYCLE` is the cycle number the events file carries."""
    extra = {}
    for key, value in outcome.items():
        if isinstance(value, list):
            value = " ".join(value)
        elif isinstance(value, bool):
            value = "true" if value else "false"
        extra["RELAY_" + key.upper()] = "" if value is None else str(value)
    extra["RELAY_CYCLE_JSON"] = json.dumps(outcome, sort_keys=True)
    return extra


def checkout_problem(manifest):
    """None when the target checkout is on its default branch with a clean tree, else the
    sentence to stop with. The runner merges into this checkout, so anything else means a
    person or another session is in it."""
    repo = manifest.project.repo
    try:
        default = default_branch_of(manifest)
        branch = gitread.current_branch(repo)
        if branch != default:
            return "the checkout is on %s, not %s" % (branch, default)
        dirty = gitread.status_porcelain(repo).strip()
    except (gitread.GitError, OSError) as exc:
        return "the checkout could not be read: %s" % exc
    if dirty:
        return "the checkout has uncommitted changes: " + dirty.splitlines()[0].strip()
    return None


# The lock and the restart path.

def acquire_lock(paths, sleep=None):
    """An exclusive lock on the feeder's lock file, held for the life of the process, or None
    when another feeder holds it. `flock` is released by the operating system when the holder
    exits however it exits, so there is no stale lock to clean up and no process to look for.

    A refusal is tried again a moment later, `LOCK_ATTEMPTS` times in all, because a watcher's
    `lock_held` probe holds a shared lock for a few system calls, and a feeder that met one would
    otherwise exit 3 as if another feeder held the manifest."""
    handle = open(paths.lock, "a+")
    for attempt in range(LOCK_ATTEMPTS):
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or attempt + 1 == LOCK_ATTEMPTS:
                break
        (sleep or time.sleep)(LOCK_RETRY_SECONDS)
    handle.close()
    return None


def request_stop(paths, word=""):
    """Drop the stop file. `word` is written into it: `restart` when a new feeder is waiting to
    take over, so the leaving feeder says handover rather than stop (issue #36)."""
    with open(paths.stop, "a", encoding="utf-8") as handle:
        if word:
            handle.write(word + "\n")


# Liveness and the watcher's commands (issue #36). A process listing matched on `relay_cli.py
# feed` cannot tell one manifest's feeder from another's; the lock file can, since each manifest
# has its own and only a live feeder holds it.

def lock_held(paths):
    """True when some process holds this manifest's feeder lock. The file is never created here.

    The probe takes a shared lock and drops it at once when nobody holds the exclusive one. A
    feeder starting in that same instant is why `acquire_lock` tries more than once."""
    if not os.path.exists(paths.lock):
        return False
    try:
        handle = open(paths.lock, "r")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            return exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN)
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def pid_alive(pid):
    """True when a process with this pid exists on this host, whoever it belongs to."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_state(paths):
    """The feeder's state file as a dict, {} when there is none. Raises ConfigError when it
    exists and cannot be read. The feeder's own load goes through here too, so a watcher and a
    feeder never disagree about whether the file is readable."""
    if not os.path.exists(paths.state):
        return {}
    try:
        with open(paths.state, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigError("%s could not be read: %s" % (paths.state, exc))
    if not isinstance(loaded, dict):
        raise ConfigError("%s is not a JSON object" % paths.state)
    if loaded.get("hold") is not None and not isinstance(loaded["hold"], dict):
        # Refused rather than read as held or not: a hand edit that left `true` here would
        # otherwise crash every reader of the hold, and `false` would quietly release it.
        raise ConfigError("%s holds a hold that is not a JSON object or null" % paths.state)
    return loaded


def write_state(paths, state):
    """Rename the whole state into place in one step, so a reader never sees half of it."""
    manifestedit.write_atomic(paths.state, json.dumps(state, indent=1, sort_keys=True))


def log_line(when, message):
    """A feeder log line: the time to the second, then the sentence."""
    return "%s %s" % (when.isoformat(timespec="seconds"), message)


def append_log(paths, line):
    with open(paths.log, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def merge_words(record):
    """The three way reading of a merge range the hook's log line uses: unknown, empty, or the
    range itself."""
    if record.get("merge_moved") is None:
        return "unknown"
    return record.get("merge_range") or "empty"


def hold_sentence(manifest_path, hold):
    """The sentence a refused start and `feed --status` say for the hold record `hold`, set on
    the manifest at `manifest_path` (issue #53)."""
    return ("a post cycle hold is set: the hook %s after cycle %s at %s, merge range %s. Read %s "
            "and repair the default branch, then release it with `feed %s --release`."
            % (hold.get("failure"), hold.get("cycle"), hold.get("at"), merge_words(hold),
               hold.get("hook_out") or paths_for(manifest_path).hook_out, manifest_path))


def release_hold(paths, now=datetime.now):
    """Clear the hold from the state file. Returns (the record cleared, or None when none was
    set; None, or the reason the release could not be logged). The caller holds the feeder
    lock, so no feeder saves over this write. Raises ConfigError on a state file that cannot be
    read, which holds the halt counts too, and OSError when the state cannot be written, in
    which case nothing was released.

    The log is written after the state and a failure there is only reported: the release is
    what the operator asked for, and saying it failed once the hold is gone would send them
    after a hold that no longer exists."""
    state = read_state(paths)
    hold = state.get("hold")
    if not hold:
        return None, None
    state["hold"] = None
    write_state(paths, state)
    try:
        append_log(paths, log_line(now(), "the post cycle hold from cycle %s was released by "
                                          "the operator: the hook %s at %s"
                                   % (hold.get("cycle"), hold.get("failure"), hold.get("at"))))
    except OSError as exc:
        return hold, str(exc)
    return hold, None


def clear_limit_state(state):
    """Empty the marks and zero the usage limit row in `state`, in place (R13). Returns (the
    marks cleared, {model: the stored entry}; the row it stood at). The held snapshot goes too,
    since nothing is held once no model is marked. The retry queue stays: a queued retry held on
    a marked model is launched at the next Cycle once the mark is gone. `halts`, `refused`, and
    `reported` are not the limit's, and stay as well."""
    marks, streak = dict(state.get("exhausted") or {}), state.get("limit_waits", 0)
    state["exhausted"], state["limit_waits"], state["last_held"] = {}, 0, {}
    return marks, streak


def cleared_sentence(cleared):
    """The log line and the verb's answer for what `clear_limit_state` returned."""
    marks, streak = cleared
    return ("the usage limits were cleared by the operator: marks on %s, usage limit waits at %s"
            % (", ".join(sorted(marks)) or "no model", streak))


def clear_limits(paths, now=datetime.now):
    """`feed --clear-limits` with no feeder alive: clear the marks and the row in the state
    file. Returns (what `clear_limit_state` returned, or None when there is no state file; None,
    or the reason the clearing could not be logged). The caller holds the feeder lock, as for
    `release_hold`, whose shape this follows: ConfigError on a state file that cannot be read,
    OSError when it cannot be written and nothing was cleared, and a log failure only reported."""
    if not os.path.exists(paths.state):
        return None, None
    state = read_state(paths)
    cleared = clear_limit_state(state)
    write_state(paths, state)
    try:
        append_log(paths, log_line(now(), cleared_sentence(cleared)))
    except OSError as exc:
        return cleared, str(exc)
    return cleared, None


def liveness(paths, state, hostname=None):
    """{"running": True or False, "pid": ..., "detail": sentence} for this manifest's feeder.

    The lock decides. Each manifest has its own lock file and only a live feeder holds it, so
    another board's feeder, a recycled pid, or a laptop whose hostname changed with its network
    can never turn the answer. The record says which process holds it: its pid, checked when it
    was recorded on this host, since a pid means nothing on another."""
    process = state.get("process") or {}
    pid = process.get("pid")
    recorded_here = process.get("hostname") == (hostname or socket.gethostname())
    if lock_held(paths):
        if process and not process.get("left_at") and (pid_alive(pid) or not recorded_here):
            where = "" if recorded_here else " (recorded on host %s)" % process.get("hostname")
            return {"running": True, "pid": pid,
                    "detail": "pid %s%s holds %s" % (pid, where, paths.lock)}
        # Held by a feeder that has not recorded itself: one started from a runner older than
        # the process record, or one in the moment between its lock and its record.
        return {"running": True, "pid": None,
                "detail": "a feeder holds %s but has recorded no pid; a runner older than the "
                          "process record, or one that is starting" % paths.lock}
    if not process:
        return {"running": False, "pid": None,
                "detail": "no feeder has recorded itself for this manifest"}
    if process.get("left_at"):
        return {"running": False, "pid": pid,
                "detail": "pid %s left at %s with exit %s (%s): %s"
                          % (pid, process["left_at"], process.get("exit_code"),
                             process.get("left_reason"), process.get("left_message"))}
    if recorded_here and pid_alive(pid):
        return {"running": False, "pid": pid,
                "detail": "pid %s exists but does not hold %s, so it is another process; the "
                          "feeder left without recording why" % (pid, paths.lock)}
    return {"running": False, "pid": pid,
            "detail": "pid %s holds no lock and recorded no leaving: killed, or the machine "
                      "went down" % pid}


def status_marks(state, paths, now):
    """{model: {since, until, source, expired}} for each mark in the state file (R12). A mark an
    older feeder wrote as a bare time reads as `read_mark` reads it, which is the one case that
    needs the sidecar's `fallback_hours`, so only then is the sidecar read. One that cannot be
    read, like any sidecar problem, only costs that mark its exact expiry: the default stands.
    A mark that cannot be read at all is shown with no times and source `unreadable`, since the
    feeder drops it next Cycle."""
    exhausted = state.get("exhausted") or {}
    hours = Config().fallback_hours
    if any(isinstance(value, str) for value in exhausted.values()):
        try:
            hours = load_config(paths.config).fallback_hours
        except (ConfigError, OSError):
            pass
    marks = {}
    for model, value in exhausted.items():
        mark = read_mark(value, hours)
        if mark is None:
            marks[model] = {"since": None, "until": None, "source": "unreadable", "expired": True}
        else:
            marks[model] = dict(mark_record(mark), expired=mark.until <= now)
    return marks


def status_held(state, marks):
    """{id: model} for the Tasks and cards the last Cycle held, less any whose model has no live
    mark left: the snapshot is only rewritten by a Cycle, so after the feeder leaves it would
    otherwise go on naming work a mark that has since expired no longer holds."""
    return {task_id: model for task_id, model in (state.get("last_held") or {}).items()
            if not (marks.get(model) or {"expired": True})["expired"]}


def status_report(paths, hostname=None, now=datetime.now):
    """What `feed --status --json` prints: the liveness answer beside what the state file holds
    about the feeder, its last cycle, its last event, and the usage limit state: the marks, the
    Tasks and cards the last Cycle held, the queued retries, and the row of usage limit waits.
    Raises ConfigError on an unreadable state file."""
    state = read_state(paths)
    marks = status_marks(state, paths, now())
    report = {"manifest": paths.manifest, "state_path": paths.state, "events_path": paths.events,
              "process": state.get("process"), "cycles": state.get("cycles", 0),
              "last_cycle": state.get("last_cycle"), "last_event": state.get("last_event"),
              "hold": state.get("hold") or None, "marks": marks,
              "held": status_held(state, marks),
              "retry_blocked": sorted(state.get("retry_blocked") or {}, key=natural_key),
              "limit_waits": state.get("limit_waits", 0)}
    report.update(liveness(paths, state, hostname=hostname))
    return report


def limit_lines(report):
    """The usage limit lines of `feed --status`, none when nothing is marked, held, queued, or
    counted: one per mark with its expiry and where that came from, then the held work, the
    row, and the queued retries."""
    lines = []
    for model, mark in sorted((report.get("marks") or {}).items()):
        if mark["source"] == "unreadable":
            lines.append("usage limit mark: %s, unreadable, dropped at the next cycle" % model)
            continue
        source = ("the reset the CLI printed" if mark["source"] == limits.MARK_CLI
                  else "fallback_hours after the death at %s" % mark["since"])
        line = "usage limit mark: %s until %s, from %s" % (model, mark["until"], source)
        if mark.get("expired"):
            line += ", expired, dropped at the next cycle"
        lines.append(line)
    held = report.get("held") or {}
    if held:
        lines.append("held by a usage limit: %s" % ", ".join(
            "%s on %s" % (task_id, held[task_id]) for task_id in sorted(held, key=natural_key)))
    if report.get("limit_waits"):
        lines.append("usage limit waits in a row: %s" % report["limit_waits"])
    if lines:
        # Not for the retry queue alone, which `--clear-limits` leaves where it is. A live
        # feeder refuses the bare flag, so the hint names the form it takes.
        lines.append("clear them with feed %s --clear-limits%s once the limit is over"
                     % (report["manifest"], " --restart" if report.get("running") else ""))
    if report.get("retry_blocked"):
        lines.append("queued retries: %s" % _ids(report["retry_blocked"]))
    return lines


def _ids(values):
    return "[%s]" % ", ".join(str(value) for value in values or ())


def feeder_line(report, with_hold=True):
    """One line: whether this manifest's feeder is running, and why the answer is what it is.
    `with_hold` adds a set hold in brief, for `status`, which has no line of its own for it."""
    line = "feeder: %s, %s" % ("running" if report["running"] else "not running",
                               report["detail"])
    process = report.get("process") or {}
    if report["running"] and process.get("pid") == report.get("pid"):
        line += ", since %s, cycle %s" % (process.get("started_at"), process.get("cycle"))
    hold = report.get("hold")
    if hold and with_hold:
        line += "; held since %s by a failed post cycle hook, release with feed %s --release" % (
            hold.get("at"), report["manifest"])
    return line


def status_lines(report):
    """`feed --status` for a person: the feeder line, a set hold in full, the usage limit state,
    then the last cycle and the last event."""
    lines = [feeder_line(report, with_hold=False), "manifest: %s" % report["manifest"]]
    if report.get("hold"):
        lines.append("hold: " + hold_sentence(report["manifest"], report["hold"]))
    lines += limit_lines(report)
    process = report.get("process") or {}
    if process.get("runner_tree"):
        lines.append("runner tree: %s" % process["runner_tree"])
    cycle = report.get("last_cycle") or {}
    started, result = cycle.get("started"), cycle.get("result")
    if started:
        line = "last cycle: %s started %s, appended %s, tasks %s" % (
            started.get("cycle"), started.get("at"), _ids(started.get("appended")),
            _ids(started.get("tasks")))
        if result:
            line += "; relay run exited %s at %s: landed %s, halted %s, blocked %s, skipped %s" % (
                result.get("run_exit"), result.get("at"), _ids(result.get(STATUS_LANDED)),
                _ids(result.get(STATUS_HALTED)), _ids(result.get(STATUS_BLOCKED)),
                _ids(result.get(STATUS_SKIPPED)))
        else:
            line += "; no result yet"
        lines.append(line)
    else:
        lines.append("last cycle: none recorded")
    event = report.get("last_event")
    if event:
        detail = ""
        if event.get("event") == EVENT_WAITING:
            detail = " %s for %ss, until %s" % (event.get("reason"), event.get("seconds"),
                                                event.get("until"))
        elif event.get("event") == EVENT_LEAVING:
            detail = " exit %s (%s)" % (event.get("exit_code"), event.get("reason"))
        elif event.get("event") == EVENT_POST_CYCLE_STARTED:
            detail = " a blocking hook is running, merge range %s" % (event.get("merge_range")
                                                                       or "empty")
        elif event.get("event") == EVENT_POST_CYCLE:
            detail = " %s, %s" % (event.get("mode"), event.get("error") or (
                "hook pid %s" % event.get("hook_pid") if event.get("mode") == HOOK_DETACHED
                else "exit %s" % event.get("exit_code")))
        lines.append("last event: %s at %s%s" % (event.get("event"), event.get("at"), detail))
    lines.append("events: %s" % report["events_path"])
    lines.append("state: %s" % report["state_path"])
    return lines


def read_events(paths, offset=0):
    """(complete lines from `offset`, the offset after the last complete line). A line still
    being written has no newline yet and is left for the next read. A file shorter than
    `offset` was replaced, and is read again from its start."""
    try:
        with open(paths.events, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() < offset:
                offset = 0
            handle.seek(offset)
            chunk = handle.read()
    except FileNotFoundError:
        return [], 0
    end = chunk.rfind(b"\n") + 1
    text = chunk[:end].decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line.strip()], offset + end


def end_offset(paths):
    """The offset just past the events file's last complete line, found from the end so a
    follower starting over weeks of events reads a block, not the file."""
    try:
        with open(paths.events, "rb") as handle:
            position = handle.seek(0, os.SEEK_END)
            while position > 0:
                start = max(0, position - EVENTS_CHUNK_BYTES)
                handle.seek(start)
                newline = handle.read(position - start).rfind(b"\n")
                if newline >= 0:
                    return start + newline + 1
                position = start
    except FileNotFoundError:
        pass
    return 0


def follow_events(paths, write, sleep, now=datetime.now, hostname=None,
                  poll_seconds=FOLLOW_POLL_SECONDS, grace_polls=FOLLOW_GRACE_POLLS):
    """`feed --follow`: write each event appended from now on, one JSON line each, until the
    feeder leaves. A `leaving` event ends it, except one whose reason is a restart: the new
    feeder's lines follow, so the follow goes on and allows the restart's poll time for them.
    A feeder found not running for `grace_polls` polls in a row with nothing new ends it too,
    which is how a killed feeder, or none at all, ends a follow; that ending is one
    `not_running` line of the follower's own, so a watcher reading JSON lines sees why the
    stream stopped."""
    offset = end_offset(paths)
    missed, allowance = 0, grace_polls
    while True:
        lines, offset = read_events(paths, offset)
        for line in lines:
            write(line)
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("event") != EVENT_LEAVING:
                allowance = grace_polls
            elif event.get("reason") == RESTART_WORD:
                allowance = grace_polls + math.ceil(2 * RESTART_POLL_SECONDS / poll_seconds)
            else:
                return
        if lines:
            missed = 0
        else:
            try:
                answer = liveness(paths, read_state(paths), hostname=hostname)
            except ConfigError as exc:
                answer = {"running": False, "pid": None, "detail": str(exc)}
            missed = 0 if answer["running"] else missed + 1
            if missed >= allowance:
                write(json.dumps({"at": now().isoformat(timespec="seconds"),
                                  "event": EVENT_NOT_RUNNING, "manifest": paths.manifest,
                                  "pid": answer.get("pid"), "detail": answer["detail"]},
                                 sort_keys=True))
                return
        sleep(poll_seconds)


def wait_for_lock(paths, sleep, log, polls_max=RESTART_POLLS_MAX):
    """The restart path: ask the running feeder to stop, wait for it to leave, take its place.
    Nothing is killed, so the task that is running finishes and merges normally. It exists
    because editing a sidecar, or cutting a new runner, does nothing to a process already
    running: that process holds the settings and the code it loaded when it started."""
    request_stop(paths, RESTART_WORD)
    log("restart requested, waiting for the running feeder to leave")
    for _ in range(polls_max):
        handle = acquire_lock(paths)
        if handle is not None:
            os.unlink(paths.stop)
            log("the old feeder is gone, starting")
            return handle
        sleep(RESTART_POLL_SECONDS)
    log("the running feeder never left; the stop file is still in place")
    return None
