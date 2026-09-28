"""The usage limit reader (usage limit plan, U1, KTD1, KTD3).

One question, asked of one death: did the account's limit for the model end this process? The
answer is read from the task's own log, from the last attempt's `result` line, and comes back as
one of three words:

  confirmed     the `result` line carries `api_error_status` 429 (R1)
  refuted       any other `result` line, or no process ran, or a slow death with no `result` line
  unconfirmed   a quick death whose last attempt has no `result` line (R2)

Timing decides only the last split, a death with nothing in its log to read. The record's status
and class are never consulted, so a halted death is read exactly like a blocked one.

Beside a confirmed reading comes the reset time the CLI printed, when the last attempt carries a
`rate_limit_event` whose status is `rejected` (KTD10), so a mark can end when the limit does.

An attempt starts at whichever comes last of two lines: the CLI's own `init`, and the line the
launcher appends before every launch (`attempt_line`). The second exists because an attempt that
prints nothing, a binary that would not start or a CLI that died on a bad flag with plain text,
has no `init` of its own, and without a line of the runner's the walk back would read the
attempt before it as this death. A log written before that line existed reads as it always did.

The second half of the module is the state machine (U2, KTD4), as two pure functions:
`decide_after_run` turns one Cycle's facts into a `Decision`, and `plan_cycle_start` turns the
work waiting at the start of a Cycle into a `CycleStart`. Neither reads a file, a clock, or a
state object, and neither writes anything; the caller applies the record.

This module imports nothing from the feeder or the runner. Both read deaths through it.
"""
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# The HTTP status the CLI's `result` line carries as `api_error_status` when the account's
# limit for the model is spent. Not `terminal_reason: api_error` alone: a model the account
# cannot reach at all ends with that too, beside a 404.
USAGE_LIMIT_STATUS = 429
LOG_TAIL_BYTES = 64 * 1024        # the terminal `result` line is the log's last

CONFIRMED = "confirmed"
REFUTED = "refuted"
UNCONFIRMED = "unconfirmed"

# The `rate_limit_info.status` of the event the CLI prints when it refuses the turn. Not
# `overageStatus`, which reads rejected on an account with overage off while the turn runs.
RATE_LIMIT_REJECTED = "rejected"

# The `subtype` of the `system` line the launcher writes to a process's log before starting it.
# The runner's own name, so no CLI's event can be mistaken for it. Every reader of the log that
# counts or prints lines passes it over; see `is_attempt_boundary`.
ATTEMPT_SUBTYPE = "relay_attempt"


def attempt_line(now=None):
    """The boundary line for one launch, newline terminated: a `system` object with the runner's
    own subtype and the launch time in UTC."""
    moment = now or datetime.now(timezone.utc)
    return json.dumps({"type": "system", "subtype": ATTEMPT_SUBTYPE,
                       "at": moment.isoformat()}) + "\n"


def is_attempt_boundary(event):
    """True for the line `attempt_line` writes, and for nothing a CLI prints."""
    return (isinstance(event, dict) and event.get("type") == "system"
            and event.get("subtype") == ATTEMPT_SUBTYPE)


def _is_attempt_start(event):
    return is_attempt_boundary(event) or (
        event.get("type") == "system" and event.get("subtype") == "init")


def log_tail(path):
    """The end of a task's stdout log, or "" when there is none to read. The tail is enough:
    the `result` event is the last line a finished process prints."""
    if not path:
        return ""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _last_attempt(log_text):
    """The JSON events of the last attempt, newest first. The runner appends every attempt of a
    task to one log, so the walk stops at the last attempt's own `init` line or the launcher's
    boundary line above it, whichever it meets first. A line that is not a JSON object, a torn
    first line of a tail above all, is passed over."""
    for line in reversed((log_text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if _is_attempt_start(event):
            return
        yield event


def _rejected_reset(event):
    """The reset time a rejected `rate_limit_event` carries, as a local time, or None for any
    other event or a `resetsAt` that is not a readable epoch."""
    if event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info")
    if not isinstance(info, dict) or info.get("status") != RATE_LIMIT_REJECTED:
        return None
    seconds = info.get("resetsAt")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(seconds)
    except (OverflowError, OSError, ValueError):
        return None


def _read_attempt(log_text):
    """(result, resets_at) for the last attempt, in one pass: its `result` event or None, and
    the latest reset among its rejected `rate_limit_event` lines or None. The latest, because
    two limits can be spent at once, a session's and a week's, and the model is back only when
    both have lifted. A warning above the attempt's `init` line belongs to no attempt."""
    result, resets = None, []
    for event in _last_attempt(log_text):
        if result is None and event.get("type") == "result":
            result = event
            continue
        reset = _rejected_reset(event)
        if reset is not None:
            resets.append(reset)
    return result, max(resets, default=None)


def result_event(log_text):
    """The last attempt's `result` event in a task's stream-json stdout, or None when it has
    none: the process was killed first, or the backend prints another format."""
    return _read_attempt(log_text)[0]


def read_death(record, log_text, quick_death_seconds):
    """(reading, resets_at) for one death. `reading` is CONFIRMED, REFUTED, or UNCONFIRMED;
    `resets_at` is the CLI's reset time beside a confirmed reading when the log gives one, and
    None otherwise.

    The caller passes only a record whose process was launched this time. A record the run
    refused before launch keeps the previous attempt's `wall_seconds` and log, so reading it
    here would read that attempt's death again; the caller tells the two apart by `started_at`
    (plan KTD9). A record with no wall time at all never launched a process, and a limit cannot
    have stopped a process that never started, so it reads refuted whatever its log says."""
    wall = record.get("wall_seconds")
    if wall is None:
        return REFUTED, None
    event, resets_at = _read_attempt(log_text)
    if event is not None:
        if event.get("api_error_status") == USAGE_LIMIT_STATUS:
            return CONFIRMED, resets_at
        return REFUTED, None
    if wall < quick_death_seconds:
        return UNCONFIRMED, None
    return REFUTED, None


# The state machine (U2, KTD4). Everything below is a pure function of its arguments.

# Where a mark's expiry came from: the reset the CLI printed (KTD10), or `fallback_hours`.
MARK_CLI = "cli"
MARK_FALLBACK_HOURS = "fallback_hours"

# The three outcomes of a Cycle, and the words and code the Feeder already uses for two of them.
GO = "go"
WAIT = "wait"
LEAVE = "leave"
WAIT_USAGE_LIMIT = "usage_limit"
WAIT_MODEL_HELD = "model_held"
LEAVE_LIMIT_WAITS = "limit_waits_exhausted"
LEAVE_EXIT = 2


@dataclass(frozen=True)
class Settings:
    """The configuration values the machine reads. The Feeder's own config has every one of
    these attributes, so it can be passed in place of this."""
    quick_death_seconds: int = 600
    limit_wait_seconds: int = 1800
    limit_waits_max: int = 16
    max_halts: int = 2
    fallback_hours: int = 5
    batch: int = 3


@dataclass(frozen=True)
class Mark:
    """A model at its limit: marked `since` the death that said so, `until` the time it is
    expected back, and `source`, MARK_CLI or MARK_FALLBACK_HOURS, for where that time came
    from (R4)."""
    since: datetime
    until: datetime
    source: str


@dataclass(frozen=True)
class Outcome:
    """What the Cycle asks for next: GO round, WAIT `seconds` with `reason`, or LEAVE with exit
    `code` and `reason`."""
    kind: str
    reason: str = None
    seconds: int = None
    code: int = None


GO_ROUND = Outcome(GO)


@dataclass(frozen=True)
class CycleFacts:
    """One Cycle, as the Feeder read it. `before` and `after` are {id: record} for this Cycle's
    Tasks before and after the run; `readings` {id: (reading, resets_at)} from `read_death` for
    each death; `died_on` {id: model} the model each ran on, and `listed_on` {id: model} the
    Manifest's. `died_at` {id: time} the time each death's process ended, from `death_time`; a
    death with none is taken to have ended at `now`. `marks` is {model: Mark}, `streak` the
    `limit_waits` count, `halts` {id: count}. `deferred` and `passed_over` are the ids this Cycle
    deferred and the ids the run passed over under R11. `retries` is the ids queued for a retry
    when the run started."""
    after: dict
    now: datetime
    before: dict = field(default_factory=dict)
    readings: dict = field(default_factory=dict)
    died_at: dict = field(default_factory=dict)
    died_on: dict = field(default_factory=dict)
    listed_on: dict = field(default_factory=dict)
    fallback: dict = field(default_factory=dict)
    marks: dict = field(default_factory=dict)
    streak: int = 0
    halts: dict = field(default_factory=dict)
    deferred: frozenset = frozenset()
    passed_over: frozenset = frozenset()
    retries: frozenset = frozenset()
    settings: object = Settings()


@dataclass(frozen=True)
class Decision:
    """What one Cycle's facts decide, for the Feeder to apply.

    `marks` {model: Mark} to write, each replacing any mark the model had (KTD5); `notify` the
    models among them that were not marked before (R4). `moves` (id, from, to) to write to the
    Manifest; `holds` (id, model) left where they are. `retry` the ids to queue for a retry.
    `halts` {id: new count} for each halt to count, and `exclude` the ids among them that have
    reached `max_halts`. `report` the ids to report blocked. `streak` the new `limit_waits`."""
    marks: dict
    notify: tuple
    moves: tuple
    holds: tuple
    retry: tuple
    halts: dict
    exclude: tuple
    report: tuple
    streak: int
    outcome: Outcome


@dataclass(frozen=True)
class CycleStart:
    """What the start of a Cycle decides. `moves` (id, from, to) to write before the run;
    `retry` the ids to pass as `--retry-blocked` and `defer` the ids to pass as `--defer`;
    `held` (id, model) for every held Task, a held queued retry among them keeping its place in
    the queue; `room` the batch's room left by the work that will run; `wait` the R7 wait, an
    Outcome with reason WAIT_MODEL_HELD, when held work is all there is among these Tasks, else
    None. Held fresh cards are the caller's to add, through `hold_wait`."""
    moves: tuple
    retry: tuple
    defer: tuple
    held: tuple
    room: int
    wait: int = None


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


def _chain(model, table):
    """`model` and every model along its fallback chain, each once."""
    seen = []
    while model is not None and model not in seen:
        seen.append(model)
        model = table.get(model)
    return seen


def active_marks(marks, now):
    """The marks in `marks` that have not expired by `now`."""
    return {model: mark for model, mark in marks.items() if mark.until > now}


def is_held(model, marks, table, now):
    """A model marked at `now` whose fallback chain has no free model: nothing listed or routed
    on it can run anywhere until a mark along that chain expires. Derived, never stored."""
    active = active_marks(marks, now)
    return model in active and resolve_fallback(model, table, set(active)) is None


def death_time(record):
    """The time a record's process ended, `started_at` plus `wall_seconds`, as a local time
    with no zone, the frame `now` and a reset time are read in. None when either is missing or
    unreadable. The runner stamps `started_at` in UTC with its offset; a stamp with no offset
    is taken to be local already."""
    started, wall = record.get("started_at"), record.get("wall_seconds")
    if (not isinstance(started, str) or isinstance(wall, bool)
            or not isinstance(wall, (int, float))):
        return None
    try:
        moment = datetime.fromisoformat(started)
        if moment.tzinfo is not None:
            moment = moment.astimezone().replace(tzinfo=None)
        return moment + timedelta(seconds=wall)
    except (OverflowError, OSError, ValueError):
        return None


def mark_for(now, resets_at, fallback_hours, died_at=None):
    """The mark one confirmed death that ended at `died_at` writes, decided at `now`, or None
    when the limit is already over (R4, KTD10). Until the CLI's reset when it lies ahead of
    `now`. None when the reset fell between the death and `now`. Otherwise `fallback_hours`
    after the death, or None when that too has passed. A death with no time is taken to have
    ended at `now`."""
    died = now if died_at is None else died_at
    if resets_at is not None and resets_at > now:
        return Mark(since=died, until=resets_at, source=MARK_CLI)
    if resets_at is not None and died < resets_at:
        return None
    until = died + timedelta(hours=fallback_hours)
    if until <= now:
        return None
    return Mark(since=died, until=until, source=MARK_FALLBACK_HOURS)


def natural_order(ids):
    """`ids` sorted with the digits in each read as numbers, so 9 comes before 10."""
    def key(task_id):
        parts = re.split(r"(\d+)", str(task_id))
        return [int(part) if index % 2 else part for index, part in enumerate(parts)]
    return sorted(ids, key=key)


def hold_wait(held_models, marks, table, now, cap):
    """The R7 wait for work held on `held_models`: `cap` seconds, or less when a mark along one
    of those models' chains expires sooner, since that expiry frees the work. None when nothing
    is held. At least one second, so a mark due to expire now is not a wait of nothing."""
    active = active_marks(marks, now)
    expiries = [active[model].until for held in held_models
                for model in _chain(held, table) if model in active]
    if not expiries:
        return None
    seconds = math.ceil((min(expiries) - now).total_seconds())
    return max(1, min(cap, seconds))


def _launched(task_id, facts):
    """True when this Cycle's run launched a process for the Task: its record carries a
    `started_at` the record before the run did not (KTD9)."""
    started = facts.after[task_id].get("started_at")
    return started is not None and started != facts.before.get(task_id, {}).get("started_at")


def decide_after_run(facts):
    """The decision for one Cycle, from `facts`, a `CycleFacts`. In order: set the records the
    run did not launch aside, so none is read as a limit death (R3, KTD9); read each launched
    death; mark the model of every confirmed death whose limit is not over yet, timed from the
    death (R4); move or hold each confirmed death on a marked model against the marks as they
    stand after this Cycle's (R5, R6); decide the whole Cycle wait
    from the unconfirmed deaths (R8, R9); then sort what is left, the unlaunched halted records
    among it, into counted halts and reported blocked Tasks. A Task deferred or passed over
    this Cycle is in neither."""
    settings, now = facts.settings, facts.now
    ids = list(facts.after)
    status = {task_id: facts.after[task_id].get("status") for task_id in ids}
    # Landed in this Cycle: a record already landed before the run is no news of the account.
    landed = any(status[task_id] == "landed"
                 and facts.before.get(task_id, {}).get("status") != "landed" for task_id in ids)
    dead = [task_id for task_id in ids if status[task_id] in ("halted", "blocked")]
    launched = [task_id for task_id in dead if _launched(task_id, facts)]
    left_alone = frozenset(facts.deferred) | frozenset(facts.passed_over)
    unlaunched = [task_id for task_id in dead
                  if task_id not in launched and task_id not in left_alone]

    def model_of(task_id):
        return (facts.died_on.get(task_id) or facts.after[task_id].get("model")
                or facts.listed_on.get(task_id))

    reading = {task_id: facts.readings.get(task_id, (REFUTED, None))[0] for task_id in launched}
    # A confirmed death is never counted (R6), even one whose model nobody knows; that one
    # marks nothing, moves nothing, and holds nothing, and if blocked it is reported rather
    # than queued, since no mark bounds its retry.
    confirmed = [task_id for task_id in launched if reading[task_id] == CONFIRMED]
    unconfirmed = [task_id for task_id in launched if reading[task_id] == UNCONFIRMED]
    ordinary = [task_id for task_id in launched
                if task_id not in confirmed and task_id not in unconfirmed]

    # Mark. Two deaths on one model in one Cycle keep the later expiry: it is back only when
    # both limits have lifted. A death whose limit is already over marks nothing.
    before = active_marks(facts.marks, now)
    marks = {}
    for task_id in confirmed:
        model = model_of(task_id)
        mark = mark_for(now, facts.readings[task_id][1], settings.fallback_hours,
                        facts.died_at.get(task_id))
        if model is None or mark is None:
            continue
        if model not in marks or mark.until > marks[model].until:
            marks[model] = mark
    notify = tuple(sorted(model for model in marks if model not in before))

    # Move or hold, against every mark standing once this Cycle's are written. A death on a
    # model that stands open is left where it is: its limit is over.
    unavailable = set(before) | set(marks)
    moves, holds, retry, report = [], [], [], []
    for task_id in confirmed:
        source = model_of(task_id)
        if source in unavailable:
            target = resolve_fallback(source, facts.fallback, unavailable)
            if target is None:
                holds.append((task_id, source))
            else:
                moves.append((task_id, source, target))
        if status[task_id] == "blocked":
            (retry if source is not None else report).append(task_id)

    # The whole Cycle wait: nothing landed, every launched death quick, one unconfirmed.
    quick = all(facts.after[task_id].get("wall_seconds") is not None
                and facts.after[task_id]["wall_seconds"] < settings.quick_death_seconds
                for task_id in launched)
    streak, outcome = facts.streak, GO_ROUND
    waited = bool(unconfirmed) and not landed and quick
    if waited:
        streak += 1
        if streak > settings.limit_waits_max:
            # Leaving: every blocked limit death of the Cycle is reported, and none is queued.
            streak = 0
            outcome = Outcome(LEAVE, reason=LEAVE_LIMIT_WAITS, code=LEAVE_EXIT)
            report = [task_id for task_id in ids
                      if (task_id in confirmed or task_id in unconfirmed)
                      and status[task_id] == "blocked"]
            retry = []
        else:
            outcome = Outcome(WAIT, reason=WAIT_USAGE_LIMIT, seconds=settings.limit_wait_seconds)
            retry += [task_id for task_id in unconfirmed if status[task_id] == "blocked"]
    elif landed or not quick or not launched:
        streak = 0
    if not waited:
        ordinary += unconfirmed

    # What is left: ordinary deaths, and the records the run did not launch.
    halts, exclude = {}, []
    for task_id in [task_id for task_id in ids if task_id in ordinary or task_id in unlaunched]:
        if status[task_id] == "halted":
            count = facts.halts.get(task_id, 0) + 1
            halts[task_id] = count
            if count >= settings.max_halts:
                exclude.append(task_id)
        elif task_id in ordinary or task_id not in facts.retries:
            # A queued retry the run never reached keeps its place and was reported already.
            report.append(task_id)

    return Decision(marks=marks, notify=notify, moves=tuple(moves), holds=tuple(holds),
                    retry=tuple(natural_order(retry)), halts=halts, exclude=tuple(exclude),
                    report=tuple(report), streak=streak, outcome=outcome)


def plan_cycle_start(unsettled, retries, listed_on, marks, table, now, settings,
                     default_model=None):
    """The start of a Cycle (R5, R7). `unsettled` is the listed Task ids left to run, in order;
    `retries` the ids queued for a retry, in order, which run with them; `listed_on` {id: model}
    the Manifest's models, and `default_model` the model a Task the Manifest gives none runs on;
    `marks` {model: Mark}; `table` the fallback table. The Manifest decides, not the record: a
    moved retry's record still names the model it died on.

    A Task listed on a marked model moves to the first free model along its chain. With none
    free it is held: a queued retry keeps its place and is not retried, and any other Task is
    deferred. A held Task takes no room in the batch. A Task with no model from either source
    raises ValueError naming it, since R5 cannot be applied to it. `retry` comes back in
    natural order whatever order or container `retries` came in."""
    active = active_marks(marks, now)
    retries = natural_order(set(retries))
    ids = list(unsettled) + [task_id for task_id in retries if task_id not in unsettled]
    moves, retry, defer, held, running = [], [], [], [], 0
    for task_id in ids:
        model = listed_on.get(task_id) or default_model
        if model is None:
            raise ValueError("Task %s has no model: the Manifest lists none and no default "
                             "model was given, so a marked model cannot be kept from it"
                             % task_id)
        if model in active:
            target = resolve_fallback(model, table, set(active))
            if target is None:
                held.append((task_id, model))
                if task_id not in retries:
                    defer.append(task_id)
                continue
            moves.append((task_id, model, target))
        running += 1
        if task_id in retries:
            retry.append(task_id)
    wait = None
    if held and not running:
        wait = Outcome(WAIT, reason=WAIT_MODEL_HELD, seconds=hold_wait(
            {model for _, model in held}, marks, table, now, settings.limit_wait_seconds))
    return CycleStart(moves=tuple(moves), retry=tuple(natural_order(retry)), defer=tuple(defer),
                      held=tuple(held), room=max(0, settings.batch - running), wait=wait)
