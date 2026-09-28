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

This module imports nothing from the feeder or the runner. Both read deaths through it.
"""
import json
import os
from datetime import datetime

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
    task to one log, so the walk stops at the last attempt's own `init` line. A line that is
    not a JSON object, a torn first line of a tail above all, is passed over."""
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
        if event.get("type") == "system" and event.get("subtype") == "init":
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
