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
3.  A cycle whose launched tasks all died quickly is read as a usage limit and waited out.
    Relay has no usage limit handling: the headless process exits, the task halts, and with
    `continue_past_task_halt` on every remaining task does the same in seconds. This is a
    heuristic (a rule of thumb that is usually right, not a detection), and
    `looks_like_usage_limit` is named for what it is.

    One model's limit is read the same way, per model, when the sidecar names a fallback for
    it (`[models] fallback`, off by default). A task on such a model that died quickly is read
    as that model's usage limit, even while tasks on other models landed beside it: the model
    is marked exhausted in the state file for `fallback_hours`, the task is moved to the
    fallback in the manifest so the next run relaunches it there, its halt is not counted, and
    new cards routed to the model go to the fallback until the mark expires.
    `model_limit_moves` is the heuristic, named like the other. A fallback is only taken when
    it leads to a model that is not exhausted and did not itself die quickly this cycle, so a
    chain of fallbacks never loops. A cycle where every task died quickly, nothing landed, and
    some halt has no such fallback is still waited out as a whole.

    A limit death is not always a halt (issue #39). A process that printed only the CLI's limit
    message and exited is recorded `blocked` with class `no_envelope`, and a blocked record is
    one the runner never relaunches unasked. `blocked_by_usage_limit` reads such a record, on a
    model that has a fallback, as that model's limit too, confirmed by the log's `result` line
    when it has one. It is moved like a halt, and the feeder then passes `--retry-blocked ID`
    for it alone to the next run, so no other blocked record is revived with it.

The feeder never merges, pushes, moves a card, or edits the target repository. It writes three
things, all beside the manifest: the manifest itself, through `manifestedit`; its own state
file; and its log. The tracker is read only here too, so the invariant that the runner never
writes to a tracker on a normal manifest holds for the feeder as well.

Everything project specific is data in the sidecar file, `<manifest stem>.feeder.toml`. It is a
sidecar and not a manifest table because the feeder rewrites the manifest while older pinned
runners must still load it, and a runner that met an unknown table would be within its rights
to refuse it.

Every outside effect is reached through `Deps`, a record of callables, so the suite drives the
whole loop with a fake runner and a sleep that does not sleep.
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field, fields
from datetime import datetime

from . import (adapters, contracts, gitread, manifest as manifest_module, manifestedit,
               state as state_module, summary as summary_module)

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_HALTED = 2
EXIT_LEASE = 3
EXIT_INTERRUPTED = 130

# The state counts that mean "this many times in a row", reset when a feeder starts.
STREAKS = ("limit_waits", "idle_waits", "unreadable_waits")

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
# The HTTP status the CLI's `result` line carries as `api_error_status` when the account's
# limit for the model is spent. Not `terminal_reason: api_error` alone: a model the account
# cannot reach at all ends with that too, beside a 404.
USAGE_LIMIT_STATUS = 429
LOG_TAIL_BYTES = 64 * 1024        # the terminal `result` line is the log's last


class ConfigError(ValueError):
    """The sidecar file is wrong. Every problem found is in the message."""


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


def paths_for(manifest_path):
    manifest_path = os.path.abspath(manifest_path)
    stem = os.path.splitext(manifest_path)[0]
    return Paths(manifest=manifest_path, config=stem + ".feeder.toml", stop=stem + ".feeder.stop",
                 state=stem + ".feeder.state.json", order=stem + ".order",
                 routing=stem + ".models", log=stem + ".feeder.log", lock=stem + ".feeder.lock",
                 out=stem + ".feeder.out")


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
    model_fallback: dict = field(default_factory=dict)   # empty: no per model fallback
    fallback_hours: int = 5


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
    "hooks": {"pre_cycle": "pre_cycle_command"},
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
    problems, values = [], {}
    for table, body in raw.items():
        if table == "ready":
            continue
        if table not in _SCHEMA or not isinstance(body, dict):
            problems.append("[%s] is not a feeder table" % table)
            continue
        for key, value in body.items():
            if key not in _SCHEMA[table]:
                problems.append("%s.%s is not a feeder setting" % (table, key))
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
    if problems:
        raise ConfigError("%s: %s" % (path, "; ".join(problems)))
    return config


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
        labels = tuple(str(label.get("name") if isinstance(label, dict) else label)
                       for label in entry.get("labels") or [])
        cards.append({"id": str(task_id), "title": str(entry.get("title") or ""),
                      "description": str(entry.get("description") or entry.get("body") or ""),
                      "labels": labels})
    return cards


def select(cards, listed, config, rank, unsettled_count):
    """(fresh, batch). Fresh is every ready card a session may take that the manifest does not
    list yet, in order file order and then by id. The batch is the head of it, as long as the
    room left: the batch size minus the tasks the next run will already launch."""
    fresh = [card for card in cards
             if card["id"] not in listed and card["id"] not in config.denied_ids
             and not any(label in config.denied_labels for label in card.get("labels") or ())]
    fresh.sort(key=lambda card: (rank.get(card["id"], UNRANKED), natural_key(card["id"])))
    room = max(0, config.batch - unsettled_count)
    return fresh, fresh[:room]


def died_quickly(task, config):
    """A process that launched and died inside `quick_death_seconds`.

    A halt with no wall time never launched a process, a pre flight refusal for example, and a
    usage limit cannot be what stopped a process that never started. So it is not a quick
    death. That is one deliberate change from the original script, which read a missing wall
    time as zero seconds and would have waited eight hours on a stale branch."""
    return (task.get("wall_seconds") is not None
            and task["wall_seconds"] < config.quick_death_seconds)


def looks_like_usage_limit(halted, landed, config):
    """The heuristic of rule 3, and only a heuristic. True when something halted, nothing
    landed, and every halt died quickly in the sense of `died_quickly`."""
    if not halted or landed:
        return False
    return all(died_quickly(task, config) for task in halted)


def result_event(log_text):
    """The last `result` event in a task's stream-json stdout, or None when there is none: the
    process was killed first, the text is a tail that cut it off, or the backend prints another
    format. A line that is not JSON, a torn first line of a tail above all, is passed over."""
    for line in reversed((log_text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


def blocked_by_usage_limit(task, config, log_text):
    """Issue #39. A blocked record that reads as a usage limit death: no envelope, and a process
    that died inside `quick_death_seconds`, which also means it had no time to write anything a
    retry would have to step over.

    The log decides when it can. A `result` event with `api_error_status` 429 is the CLI saying
    the limit is spent; a `result` event saying anything else is a process that finished a turn
    or failed on something else, a model it cannot reach for one, and moving it would not help.
    With no `result` event to read, the time rule stands alone, as it does for a halt."""
    if task.get("class") != contracts.HALT_NO_ENVELOPE or not died_quickly(task, config):
        return False
    event = result_event(log_text)
    return event is None or event.get("api_error_status") == USAGE_LIMIT_STATUS


def resolve_fallback(model, table, unavailable):
    """The first model along `model`'s fallback chain that is not in `unavailable`, or None
    when the chain ends or comes back on itself first. `model` itself is taken to be
    unavailable. Every model is visited once at most, so no table can make this loop."""
    seen, current = {model}, model
    while True:
        current = table.get(current)
        if current is None or current in seen:
            return None
        if current not in unavailable:
            return current
        seen.add(current)


def model_limit_moves(halted, models, config, exhausted):
    """The per model half of rule 3, and a heuristic like the whole cycle half. [(task, from,
    to)] for every task whose model has a fallback and that died quickly: each is read as its
    model's usage limit and moved. The tasks are the halted ones and the blocked ones
    `blocked_by_usage_limit` already chose.

    `models` is {id: model} for those tasks and `exhausted` the models already marked. A
    model that died quickly this cycle is treated as exhausted too when it is looked at as a
    fallback, so two models that fall back to each other and both died never send their
    tasks back and forth; neither is moved, and the whole cycle rule decides."""
    quick = [task for task in halted if died_quickly(task, config)]
    dying = {models.get(task["id"]) for task in quick} & set(config.model_fallback)
    unavailable = set(exhausted) | dying
    moves = []
    for task in quick:
        source = models.get(task["id"])
        if source not in dying:
            continue
        target = resolve_fallback(source, config.model_fallback, unavailable)
        if target is not None:
            moves.append((task, source, target))
    return moves


# The outside world.

@dataclass
class Deps:
    """Every effect the loop has, as a callable. `build_deps` supplies the real ones; a test
    replaces the few it cares about."""
    sleep: object
    now: object
    run_cycle: object          # (manifest_path, retry_ids) -> the runner's exit code
    read_summary: object       # (manifest) -> the summary JSON as a dict, {} when none
    lease_held: object         # (manifest) -> True while a live runner holds this manifest
    build_adapter: object      # (manifest) -> a tracker adapter
    run_command: object        # (args, cwd, timeout) -> CompletedProcess
    notifier: object = None    # (body) or None


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


def build_deps(config, env, notifier=None, notify_on=False, sleep=None, child_stdout=None):
    """The real effects. `child_stdout` is where each run's output goes; None inherits the
    feeder's own, which under `--detach` is the output file beside the manifest."""
    import time

    def run_cycle(manifest_path, retry_ids=()):
        command = [sys.executable, "-u", runner_entry(), "run", manifest_path]
        for task_id in retry_ids:
            # One id per flag, never the bare flag, which would retry every blocked record.
            command += ["--retry-blocked", task_id]
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
        return summary_module.build(manifest, store) if store else {}

    def lease_held(manifest):
        store = _state_store(manifest, env)
        return bool(store) and store.status_word() == "running"

    def run_command(args, cwd, timeout):
        return subprocess.run(list(args), cwd=cwd, env=env, capture_output=True, text=True,
                              check=False, timeout=timeout, stdin=subprocess.DEVNULL)

    return Deps(sleep=sleep or time.sleep, now=datetime.now, run_cycle=run_cycle,
                read_summary=read_summary, lease_held=lease_held,
                build_adapter=lambda manifest: adapters.build(manifest, env=env),
                run_command=run_command, notifier=notifier)


def new_state():
    # `retry_blocked` is {id: the blocked record's started_at} for each blocked task the next
    # run is to relaunch. The stamp is how a retry the run never reached is told from one that
    # ran: every launch restamps it.
    return {"halts": {}, "limit_waits": 0, "idle_waits": 0, "unreadable_waits": 0, "cycles": 0,
            "reported": {}, "refused": {}, "exhausted": {}, "retry_blocked": {}}


class Feeder:
    def __init__(self, paths, config, deps, env, out, dry_run=False, once=False,
                 retry_blocked=()):
        self.paths, self.config, self.deps = paths, config, deps
        self.env, self.out = env, out
        self.dry_run, self.once = dry_run, once
        self.requested = tuple(retry_blocked)     # `feed --retry-blocked ID`, taken once
        self.name = os.path.basename(os.path.splitext(paths.manifest)[0])
        self.state = self._load_state()

    # Reporting.
    def log(self, message):
        line = "%s %s" % (self.deps.now().isoformat(timespec="seconds"), message)
        if not self.dry_run:
            with open(self.paths.log, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        self.out.write(line + "\n")
        if hasattr(self.out, "flush"):
            self.out.flush()

    def notify(self, message):
        if self.deps.notifier is not None and not self.dry_run:
            self.deps.notifier("feeder %s: %s" % (self.name, message))

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
        if os.path.exists(self.paths.state):
            try:
                with open(self.paths.state, encoding="utf-8") as handle:
                    loaded.update(json.load(handle))
            except (OSError, ValueError) as exc:
                raise ConfigError("%s could not be read: %s. It holds the halt counts, so fix "
                                  "or remove it by hand." % (self.paths.state, exc))
        return loaded

    def save_state(self):
        if not self.dry_run:
            manifestedit.write_atomic(self.paths.state, json.dumps(self.state, indent=1,
                                                                   sort_keys=True))

    # The loop.
    def run(self):
        self.log("feeder start, dry_run=%s, manifest=%s, runner=%s"
                 % (self.dry_run, self.paths.manifest, runner_entry()))
        if not self.once:
            # The "in a row" counts belong to one feeder's life. A stop, a restart, or an
            # interrupt would otherwise hand a partial count to the next feeder. `--once` keeps
            # them, since a feeder driven a cycle at a time by cron has no other life.
            for key in STREAKS:
                self.state[key] = 0
            self.save_state()
        try:
            while True:
                code = self.cycle()
                if code is not None:
                    return code
        except KeyboardInterrupt:
            self.log("interrupted")
            return EXIT_INTERRUPTED
        except Exception as exc:
            # A feeder that dies silently is the worst outcome: say so, then let it raise.
            self.log("feeder crashed: %s: %s" % (type(exc).__name__, exc))
            self.notify("crashed: %s" % type(exc).__name__)
            raise

    def wait(self, seconds):
        """Sleep and go round again, or under --once leave without sleeping."""
        if self.once:
            return EXIT_OK
        self.deps.sleep(seconds)
        return None

    def stop(self, code, message):
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
        if os.path.exists(self.paths.stop):
            return self.stop(EXIT_OK, "stop file present, leaving")
        try:
            manifest = manifest_module.load(self.paths.manifest, allow_no_tasks=True)
        except manifest_module.ManifestError as exc:
            return self.stop(EXIT_CONFIG, "stopping: %s" % exc)
        problem = ready_source_problem(manifest, config) or checkout_problem(manifest)
        if problem:
            return self.stop(EXIT_CONFIG, "stopping: %s. Relay owns the default branch while it "
                                          "runs, so this is a person's to look at." % problem)
        if not self.dry_run and deps.lease_held(manifest):
            # The manifest is about to be rewritten, and a live runner read it at its start.
            self.log("a runner holds the lease on this manifest, not appending, waiting")
            return self.wait(config.lease_wait_seconds)
        if not self.dry_run:
            self.pre_cycle(manifest)

        text = self._read(self.paths.manifest)
        listed = manifestedit.task_ids(text)
        excluded = manifestedit.excluded_ids(text)
        records = self._records(manifest)
        if self.requested:
            code = self.take_requests(listed, records)
            if code is not None:
                return code
        retry_ids = self.pending_retries(listed, excluded, records)
        # A blocked task queued for a retry holds room like any task the next run launches.
        unsettled = [task_id for task_id in listed if task_id not in excluded
                     and (records.get(task_id, {}).get("status") not in SETTLED
                          or task_id in retry_ids)]
        cards, readable = self.ready_cards(manifest)
        fresh, batch = select(cards, set(listed), config, read_order(self._read(self.paths.order)),
                              len(unsettled))
        routing, notes = read_routing(self._read(self.paths.routing), config.allowed_models)
        exhausted = self.exhausted_models()
        entries = []
        for card in batch:
            model, note = self.route(card, routing, exhausted)
            notes += note
            entries.append({"id": card["id"], "title": card["title"], "model": model,
                            "effort": config.default_effort})
        for note in notes:
            self.log(note)
        entries = [entry for entry in entries
                   if self.state["refused"].get(entry["id"]) != entry["model"]]
        self.log("cycle %d: %d unsettled in the manifest, %d ready and unlisted, appending %s"
                 % (self.state["cycles"], len(unsettled), len(fresh),
                    [(entry["id"], entry["model"]) for entry in entries]))
        if self.dry_run:
            for card in fresh[:DRY_RUN_LINES]:
                self.out.write("   would offer %s on %s: %s\n" % (
                    card["id"], self.route(card, routing, exhausted)[0], card["title"][:90]))
            return EXIT_OK

        appended = self.append(text, entries)
        if appended:
            self.state["idle_waits"] = self.state["unreadable_waits"] = 0
        elif not unsettled:
            return self.idle(readable, [card["id"] for card in fresh])

        self.state["cycles"] += 1
        self.save_state()
        cycle_ids = list(unsettled) + [entry["id"] for entry in appended]
        if retry_ids:
            self.log("relaunching blocked %s with --retry-blocked" % retry_ids)
        code = deps.run_cycle(self.paths.manifest, retry_ids)
        self.log("relay run exited %s" % code)
        if code == EXIT_LEASE:
            self.log("another runner holds the lease, waiting")
            return self.wait(config.lease_wait_seconds)
        if code == EXIT_CONFIG:
            return self.stop(EXIT_CONFIG, "relay refused the manifest or the environment. Run "
                                          "validate and read its output.")
        return self.settle(manifest, cycle_ids)

    def idle(self, readable, fresh_ids):
        """Nothing was appended and no listed task is left to run, while no runner holds the
        lease. Three things look like that and only one is an empty queue.

        A ready source that could not be read is not an empty queue: the feeder waits and asks
        again, and after `UNREADABLE_WAITS_MAX` waits in a row it stops for a person, because a
        feeder that retried a broken read for ever is a process doing nothing. Ready cards that
        were all refused by validate are not an empty queue either: the board has work, and
        only a person changing the routing can release it, so the feeder stops and names them.

        What is left is an empty queue. By default the feeder leaves at once rather than keep a
        process alive to poll an empty board. `idle_waits_max` above zero waits that many times
        first, for a board where a person releases cards through the day."""
        config = self.config
        if not readable:
            if self.strike("unreadable_waits", UNREADABLE_WAITS_MAX):
                return self.stop(EXIT_CONFIG, "stopping: the ready source could not be read for "
                                              "%d cycles in a row and nothing is left to run. "
                                              "Read the log." % (UNREADABLE_WAITS_MAX + 1))
            self.log("the ready source could not be read and nothing is left to run, waiting")
            return self.wait(config.idle_wait_seconds)
        self.state["unreadable_waits"] = 0
        if fresh_ids:
            return self.stop(EXIT_CONFIG, "stopping: every ready card was refused with the model "
                                          "it is routed to, and nothing is left to run: %s. "
                                          "Change the routing and start the feeder again."
                                          % ", ".join(fresh_ids))
        if self.strike("idle_waits", config.idle_waits_max):
            return self.stop(EXIT_OK, "the queue is empty, leaving: nothing ready, nothing left "
                                      "to run, and no runner holds the lease. Everything left "
                                      "on the board is blocked, denied or attended, or there "
                                      "is nothing left.")
        self.log("nothing ready and nothing unsettled, waiting (%d of %d)"
                 % (self.state["idle_waits"], config.idle_waits_max))
        return self.wait(config.idle_wait_seconds)

    def settle(self, manifest, cycle_ids):
        """Read what the run did to this cycle's tasks and apply rules 2 and 3."""
        config = self.config
        data = self.deps.read_summary(manifest)
        after = {task["id"]: task for task in data.get("tasks", [])}
        mine = [after[task_id] for task_id in cycle_ids if task_id in after]
        by_status = {status: [task for task in mine if task.get("status") == status]
                     for status in (STATUS_HALTED, STATUS_LANDED, STATUS_BLOCKED, STATUS_SKIPPED)}
        halted, landed = by_status[STATUS_HALTED], by_status[STATUS_LANDED]
        self.log("cycle result: landed %s, halted %s, blocked %s, skipped %s" % tuple(
            sorted(task["id"] for task in by_status[status])
            for status in (STATUS_LANDED, STATUS_HALTED, STATUS_BLOCKED, STATUS_SKIPPED)))
        for task in by_status[STATUS_SKIPPED]:
            # The original script counted a skip as settled and told nobody, so a card Relay
            # would never build sat in the manifest looking handled.
            self.report_once("skipped:" + task["id"], "%s was skipped by the runner and will not "
                             "be built until the card is fixed: %s"
                             % (task["id"], task.get("skip_reason") or "no reason recorded"))
        self.prune_retries(after)
        blocked = by_status[STATUS_BLOCKED]
        limited = self.limit_blocked(blocked)
        limited_ids = {task["id"] for task in limited}
        for task in blocked:
            # A limit death is reported below only if no fallback takes it; a task still queued
            # is one this run never reached, and its report was made when it first blocked.
            if task["id"] not in limited_ids and task["id"] not in self.state["retry_blocked"]:
                self.report_blocked(task)
        if (data.get("run_status") == contracts.RUN_HALTED
                and data.get("halt_class") in contracts.RUN_SCOPED_HALT_CLASSES):
            # The remote moved, the lease was lost, or the runner itself failed. None of that
            # is the task's doing, so counting it would exclude an innocent card on the next
            # cycle and then the card after it. The original script had this cascade.
            for task in limited:
                self.report_blocked(task)
            self.save_state()
            return self.stop(EXIT_CONFIG, "stopping: the run halted on %s with class %s, which "
                                          "puts something outside the task in question. No halt "
                                          "was counted. Read the summary."
                                          % (data.get("halt_task"), data.get("halt_class")))
        dead = halted + limited
        moves = model_limit_moves(dead, self._models(dead), config, self.exhausted_models())
        halted_ids = {task["id"] for task in halted}
        halted_moves = [move for move in moves if move[0]["id"] in halted_ids]
        if looks_like_usage_limit(halted, landed, config) and len(halted_moves) < len(halted):
            # Some quick death has no fallback to take, so the whole cycle rule decides. When
            # every one has, the moves below replace the wait. A blocked limit death waits with
            # the halts and then relaunches where it was, as they do.
            for task in limited:
                self.queue_retry(task)
            if limited:
                self.log("%s blocked on a usage limit and will be retried after the wait with "
                         "--retry-blocked" % sorted(limited_ids))
            if self.strike("limit_waits", config.limit_waits_max):
                return self.stop(EXIT_HALTED, "every task has died quickly for %d waits. Not a "
                                              "usage limit. Read the summary."
                                              % config.limit_waits_max)
            self.log("every halted task died inside %ds, reading that as a usage limit, waiting "
                     "%ds; these halts are not counted" % (config.quick_death_seconds,
                                                           config.limit_wait_seconds))
            return self.wait(config.limit_wait_seconds)
        self.state["limit_waits"] = 0
        moved = self.fall_back(moves, limited_ids)
        for task in limited:
            if task["id"] in moved:
                self.queue_retry(task)
            else:
                # No fallback is free, so this is an ordinary blocked task again.
                self.report_blocked(task)
        for task in halted:
            if task["id"] in moved:
                continue
            count = self.state["halts"].get(task["id"], 0) + 1
            self.state["halts"][task["id"]] = count
            if count < config.max_halts:
                continue
            reason = "excluded by the feeder after %d halts, last class %s: %s" % (
                count, task.get("class"), task.get("cause") or "")
            try:
                text = self._read(self.paths.manifest)
                edited = manifestedit.exclude_task(text, task["id"], reason)
                if edited is not None:
                    manifestedit.commit(self.paths.manifest, text, edited, env=self.env)
            except manifestedit.EditError as exc:
                # Rule 2 cannot be kept, and without it this task relaunches on every cycle.
                self.save_state()
                return self.stop(EXIT_CONFIG, "stopping: %s halted %d times and could not be "
                                              "excluded: %s" % (task["id"], count, exc))
            self.log("%s %s" % (task["id"], reason))
            self.notify("%s excluded after %d halts, %s" % (task["id"], count, task.get("class")))
        self.save_state()
        return EXIT_OK if self.once else None

    # Per model usage limits.
    def exhausted_models(self):
        """{model: marked at} for the models still marked exhausted. A mark older than
        `fallback_hours` is dropped here and logged, so the next card routed to that model runs
        on it again, and a quick death there marks it again."""
        now, hours = self.deps.now(), self.config.fallback_hours
        active = {}
        for model, stamp in sorted(self.state["exhausted"].items()):
            try:
                marked = datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                marked = None
            if marked is not None and (now - marked).total_seconds() < hours * 3600:
                active[model] = stamp
                continue
            self.log("%s was marked exhausted at %s, over %dh ago, routing to it again"
                     % (model, stamp, hours))
        if active != self.state["exhausted"]:
            self.state["exhausted"] = active
            self.save_state()
        return active

    def route(self, card, routing, exhausted):
        """(model, [notes]): `choose_model`, then its fallback while that model is exhausted."""
        model, note = choose_model(card, routing, self.config)
        notes = [note] if note else []
        if model in exhausted:
            target = resolve_fallback(model, self.config.model_fallback, set(exhausted))
            if target is None:
                notes.append("card %s is routed to %s, marked exhausted at %s, and no fallback "
                             "of it is free, keeping %s" % (card["id"], model, exhausted[model],
                                                            model))
            else:
                notes.append("card %s is routed to %s, marked exhausted at %s, appending it on "
                             "%s" % (card["id"], model, exhausted[model], target))
                model = target
        return model, notes

    def _models(self, tasks):
        """{id: model} each task ran on: the summary's own field, else the manifest's."""
        listed = manifestedit.task_models(self._read(self.paths.manifest))
        return {task["id"]: task.get("model") or listed.get(task["id"]) for task in tasks}

    def fall_back(self, moves, blocked_ids=()):
        """Mark each model in `moves` exhausted and move its tasks to the fallback in the
        manifest. Returns the ids moved, whose halts are not counted. A task the manifest edit
        refused stays where it is and its halt counts as any other. `blocked_ids` are the moved
        tasks that read blocked rather than halted, named apart because only a retry relaunches
        them."""
        moved, by_source = set(), {}
        for task, source, target in moves:
            try:
                text = self._read(self.paths.manifest)
                edited = manifestedit.set_model(text, task["id"], target)
                if edited is not None:
                    manifestedit.commit(self.paths.manifest, text, edited, env=self.env)
            except manifestedit.EditError as exc:
                self.log("%s could not be moved from %s to %s, its halt is counted: %s"
                         % (task["id"], source, target, exc))
                continue
            moved.add(task["id"])
            by_source.setdefault((source, target), []).append(task["id"])
        stamp = self.deps.now().isoformat(timespec="seconds")
        for source in sorted({source for _, source, _ in moves}):
            self.state["exhausted"][source] = stamp
        for (source, target), ids in sorted(by_source.items()):
            message = ("%s died inside %ds on %s, reading that as %s's usage limit: %s marked "
                       "exhausted for %dh and moved to %s; these halts are not counted"
                       % (", ".join(ids), self.config.quick_death_seconds, source, source,
                          source, self.config.fallback_hours, target))
            retried = [task_id for task_id in ids if task_id in blocked_ids]
            if retried:
                message += ("; %s read blocked and relaunch with --retry-blocked"
                            % ", ".join(retried))
            self.log(message)
            self.notify(message)
        self.save_state()
        return moved

    # Blocked tasks and their retries (issue #39).
    def limit_blocked(self, blocked):
        """The blocked tasks this cycle that read as their model's usage limit, on a model with
        a fallback, since without one the feeder has nowhere better to send them. A task still
        queued for a retry is left out: the run never reached it, so its record is the old one
        and was read when it was queued."""
        if not self.config.model_fallback:
            return []
        fresh = [task for task in blocked if task["id"] not in self.state["retry_blocked"]]
        models = self._models(fresh)
        return [task for task in fresh
                if models.get(task["id"]) in self.config.model_fallback
                and blocked_by_usage_limit(task, self.config,
                                           self._log_tail(task.get("log_path")))]

    def report_blocked(self, task):
        self.report_once("blocked:" + task["id"], "%s blocked; a later run will not retry it "
                         "without --retry-blocked %s" % (task["id"], task["id"]))

    def queue_retry(self, task):
        self.state["retry_blocked"][task["id"]] = task.get("started_at")

    def prune_retries(self, after):
        """Drop each queued retry the run carried out: the record moved off blocked, or it was
        launched again and so carries a new `started_at`. One the run never reached, because it
        halted first, keeps its place for the next run."""
        for task_id, stamp in list(self.state["retry_blocked"].items()):
            record = after.get(task_id)
            if (record is None or record.get("status") != STATUS_BLOCKED
                    or record.get("started_at") != stamp):
                del self.state["retry_blocked"][task_id]

    def pending_retries(self, listed, excluded, records):
        """The ids the next run is to pass as `--retry-blocked`, sorted. A queued id that is no
        longer listed, is excluded, or no longer reads blocked has nothing to retry, and is
        dropped here rather than carried for ever."""
        queue = self.state["retry_blocked"]
        keep = {task_id: stamp for task_id, stamp in queue.items()
                if task_id in listed and task_id not in excluded
                and records.get(task_id, {}).get("status") == STATUS_BLOCKED}
        if keep != queue:
            self.state["retry_blocked"] = keep
            self.save_state()
        return sorted(keep, key=natural_key)

    def take_requests(self, listed, records):
        """`feed --retry-blocked ID`: queue those blocked tasks and no others. An id the
        manifest does not list stops the feeder, since a typo would otherwise retry nothing and
        say nothing; one that is listed but not blocked is logged and passed over."""
        requested, self.requested = self.requested, ()
        unknown = [task_id for task_id in requested if task_id not in listed]
        if unknown:
            return self.stop(EXIT_CONFIG, "stopping: --retry-blocked names %s, not a task in "
                                          "the manifest" % ", ".join(unknown))
        for task_id in requested:
            record = records.get(task_id, {})
            if record.get("status") != STATUS_BLOCKED:
                self.log("%s reads %s, not blocked; nothing to retry"
                         % (task_id, record.get("status") or "no record"))
                continue
            self.queue_retry(record)
            self.log("%s is queued for a retry at the operator's request" % task_id)
        self.save_state()
        return None

    @staticmethod
    def _log_tail(path):
        """The end of a task's stdout log, or "" when there is none to read. The tail is
        enough: the `result` event is the last line a finished process prints."""
        if not path:
            return ""
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

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
        try:
            if self.config.ready_command:
                done = self.deps.run_command(self.config.ready_command, manifest.project.repo,
                                             COMMAND_TIMEOUT_SECONDS)
                if done.returncode != 0:
                    raise ValueError("it exited %d: %s" % (done.returncode,
                                                           (done.stderr or "").strip()[-300:]))
                return normalize_cards(json.loads(done.stdout or "null")), True
            cards, reason = self.deps.build_adapter(manifest).ready(self.config.ready_source)
            if reason:
                raise ValueError(reason)
            return cards, True
        except (ValueError, OSError, subprocess.SubprocessError,
                adapters.ConfigurationError) as exc:
            self.log("the ready source could not be read, offering nothing new: %s" % exc)
            return [], False

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

    def _records(self, manifest):
        return {task["id"]: task for task in self.deps.read_summary(manifest).get("tasks", [])}

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


def checkout_problem(manifest):
    """None when the target checkout is on its default branch with a clean tree, else the
    sentence to stop with. The runner merges into this checkout, so anything else means a
    person or another session is in it."""
    repo = manifest.project.repo
    try:
        default = manifest.project.default_branch or gitread.default_branch(repo) or "main"
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

def acquire_lock(paths):
    """An exclusive lock on the feeder's lock file, held for the life of the process, or None
    when another feeder holds it. `flock` is released by the operating system when the holder
    exits however it exits, so there is no stale lock to clean up and no process to look for."""
    handle = open(paths.lock, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def request_stop(paths):
    with open(paths.stop, "a", encoding="utf-8"):
        pass


def wait_for_lock(paths, sleep, log, polls_max=RESTART_POLLS_MAX):
    """The restart path: ask the running feeder to stop, wait for it to leave, take its place.
    Nothing is killed, so the task that is running finishes and merges normally. It exists
    because editing a sidecar, or cutting a new runner, does nothing to a process already
    running: that process holds the settings and the code it loaded when it started."""
    request_stop(paths)
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
