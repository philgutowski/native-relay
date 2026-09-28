"""The browser test loop's rules (browser test loop plan, U1, KTD3).

Every decision the loop makes is a pure function here, from facts to a decision: whether one
finding has the shape the Test report contract names (KTD4), which findings a pass files under
its caps (R11, R17), which landed cards a check pass looks at (R18), what generation a filed card
takes (R18), which areas have taken too many patches (R19), and whether the loop stops (R16, R17).
None reads a file, a clock, or a state object, and none writes anything; the caller applies the
answer.

A card's generation: a tour filing is generation 1, and so is a filing from checking a card the
loop did not file. A filing from checking a generation 1 card is generation 2, the last, and a
landed card of the last generation is never checked (R18).

This module imports nothing from the feeder or the runner. `testpass.py` and the Feeder both
read the loop through it.
"""
from dataclasses import dataclass, field
from datetime import timedelta

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
SEVERITIES = (HIGH, MEDIUM, LOW)
# The severities that become cards. A low never reaches the Tracker (R11).
FILED_SEVERITIES = (HIGH, MEDIUM)

KINDS = ("defect", "improvement")
VERDICTS = ("defect", "intended")

# The two kinds of pass: a full tour of the tour document, or a check of landed cards.
TOUR = "tour"
CHECK = "check"

# A pass record's own status words (KTD12). Only a pass that ran can end the loop on what it
# found; the clock and the budget end it at any pass.
RAN = "ran"
NOT_RUN = "not_run"
FAILED = "failed"

LAST_GENERATION = 2

# A finding carrying this key as true is the planning card of R19: the loop's own request that a
# person plan an area it has stopped testing. It is not a finding about the app, so it is not
# serious to `should_stop`, and the card it becomes is a person's, counted toward neither the
# new cards of a pass nor the loop's card budget (R17). `filing.py` reads the key from here.
ATTENDED_KEY = "attended"

# What `select_findings` did with each finding, in the order the report gave them.
FILE = "file"
OUTCOME_LOW = LOW
OVER_CAP = "over_cap"
OVER_BUDGET = "over_budget"
DROPPED = "dropped"

# Why the loop stopped (R16, R17, R23). Each stop names its own.
STOP_CLEAN = "clean"
STOP_OPEN_FINDINGS = "open_findings"
STOP_ROUNDS = "round_cap"
STOP_CLOCK = "clock_cap"
STOP_BUDGET = "budget"
STOP_REPORT_ONLY = "report_only"

_RANK = {severity: rank for rank, severity in enumerate(SEVERITIES)}
_KINDS = (TOUR, CHECK)
_STATUSES = (RAN, NOT_RUN, FAILED)


@dataclass(frozen=True)
class Settings:
    """The loop's caps, with the plan's defaults. The sidecar's `[test_loop]` table carries
    every one of these names, so it can be passed in place of this. `should_stop` reads the
    round, clock, and card budget caps; the caller hands `max_cards_per_pass` to
    `select_findings` and `max_patches_per_area` to `area_patches`."""
    max_rounds: int = 6
    max_hours: int = 24
    max_cards_per_pass: int = 10
    max_patches_per_area: int = 3
    max_cards_total: int = 30


def _text(value):
    return isinstance(value, str) and value.strip() != ""


def _texts(value):
    return isinstance(value, list) and bool(value) and all(_text(item) for item in value)


def validate_finding(finding):
    """The problems with one finding from a Test report, as sentences, or an empty list when it
    has the shape KTD4 names:

      title       a non empty string
      severity    high, medium, or low
      kind        defect or improvement
      area        the tour document heading, a non empty string; whether it is a heading of
                  this tour document is the pass's check, not this one
      design      true or false: the finding changes what a user sees (R14)
      cause       an object: `file` a non empty string, `line` a positive integer, and
                  `verdict` defect or intended (R10)
      steps       a non empty array of non empty strings
      expected    a string
      observed    a string, the one field that carries text copied from the app
      done_when   a non empty array of non empty strings
      card        on a check pass, the id of the checked card it came from, a non empty
                  string or an integer, since a model copies a numeric id as a number
                  (issue #118); a boolean is refused; absent or null otherwise

    Keys beyond these are left alone."""
    if not isinstance(finding, dict):
        return ["a finding must be a JSON object"]
    problems = []
    if not _text(finding.get("title")):
        problems.append("title must be a non empty string")
    severity = finding.get("severity")
    if severity is None:
        problems.append("severity is missing")
    elif severity not in SEVERITIES:
        problems.append("severity %r is not one of %s" % (severity, ", ".join(SEVERITIES)))
    if finding.get("kind") not in KINDS:
        problems.append("kind must be one of %s" % ", ".join(KINDS))
    if not _text(finding.get("area")):
        problems.append("area must be a non empty string")
    if not isinstance(finding.get("design"), bool):
        problems.append("design must be true or false")
    cause = finding.get("cause")
    if not isinstance(cause, dict):
        problems.append("cause must be an object with file, line, and verdict")
    else:
        if not _text(cause.get("file")):
            problems.append("cause.file must be a non empty string")
        line = cause.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            problems.append("cause.line must be a positive integer")
        if cause.get("verdict") not in VERDICTS:
            problems.append("cause.verdict must be one of %s" % ", ".join(VERDICTS))
    if not _texts(finding.get("steps")):
        problems.append("steps must be a non empty array of strings")
    for key in ("expected", "observed"):
        if not isinstance(finding.get(key), str):
            problems.append("%s must be a string" % key)
    if not _texts(finding.get("done_when")):
        problems.append("done_when must be a non empty array of strings")
    card = finding.get("card")
    card_is_int = isinstance(card, int) and not isinstance(card, bool)
    if card is not None and not card_is_int and not _text(card):
        problems.append("card must be a non empty string or an integer when present")
    return problems


def is_serious(finding):
    """True for a finding of a severity that becomes a card. Anything that is not a finding
    object, an invalid entry a report carried among them, is not serious."""
    return isinstance(finding, dict) and finding.get("severity") in FILED_SEVERITIES


def is_attended(finding):
    """True for the planning finding of R19, marked with `ATTENDED_KEY` true. It is filed high so
    a person sees it, and it is still not a defect the tour found."""
    return isinstance(finding, dict) and finding.get(ATTENDED_KEY) is True


def _positive(name, value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("%s must be a positive integer, not %r" % (name, value))


@dataclass(frozen=True)
class Selection:
    """What one pass does with its findings. `to_file` the findings to hand the Filing process,
    highest severity first and in report order within a severity; `lows` the low findings, for
    the lows file; `over_cap` the serious findings past the per pass cap and `over_budget` those
    past the loop's card budget, both for the pass's findings record; `dropped` the findings in
    a stopped area. `outcomes` is one outcome word per finding, in report order."""
    to_file: tuple
    lows: tuple
    over_cap: tuple
    over_budget: tuple
    dropped: tuple
    outcomes: tuple


def select_findings(findings, cap, budget_left, stopped_areas=()):
    """Split a pass's validated findings (R11, R17, R19). A finding in a stopped area is dropped
    whatever its severity. A low goes to the lows whatever the room. The serious findings are
    ordered highest severity first, keeping report order within a severity, and the first
    min(`cap`, `budget_left`) are filed. The rest are past the loop's budget when the budget is
    as tight as the cap or tighter, since the loop stops on its budget after this pass, and
    over the per pass cap otherwise. A finding with a severity outside
    the known three raises ValueError, since the caller passes only validated findings."""
    _positive("the per pass cap", cap)
    if isinstance(budget_left, bool) or not isinstance(budget_left, int):
        raise ValueError("the loop budget left must be an integer, not %r" % (budget_left,))
    budget_left = max(0, budget_left)
    stopped = set(stopped_areas)
    outcomes = [None] * len(findings)
    serious = []
    for index, finding in enumerate(findings):
        severity = finding.get("severity")
        if severity not in _RANK:
            raise ValueError("finding %d has severity %r, not one of %s"
                             % (index + 1, severity, ", ".join(SEVERITIES)))
        if finding.get("area") in stopped:
            outcomes[index] = DROPPED
        elif not is_serious(finding):
            outcomes[index] = OUTCOME_LOW
        else:
            serious.append(index)
    serious.sort(key=lambda index: _RANK[findings[index]["severity"]])
    room = min(cap, budget_left)
    overflow = OVER_BUDGET if budget_left <= cap else OVER_CAP
    for position, index in enumerate(serious):
        outcomes[index] = FILE if position < room else overflow

    def having(outcome, order=range(len(findings))):
        return tuple(findings[index] for index in order if outcomes[index] == outcome)

    return Selection(to_file=having(FILE, serious), lows=having(OUTCOME_LOW),
                     over_cap=having(OVER_CAP, serious),
                     over_budget=having(OVER_BUDGET, serious),
                     dropped=having(DROPPED), outcomes=tuple(outcomes))


def _generation(card, filed):
    """The generation the loop recorded for `card`, or None for a card the loop did not file.
    `filed` is {card id: record}, each record carrying `generation`. A record whose generation
    is missing or unreadable reads as the last, so a damaged state file can only stop checks,
    never let the loop fan out (R18)."""
    record = filed.get(str(card))
    if record is None:
        return None
    generation = record.get("generation") if isinstance(record, dict) else None
    if (isinstance(generation, bool) or not isinstance(generation, int)
            or not 1 <= generation <= LAST_GENERATION):
        return LAST_GENERATION
    return generation


def cards_to_check(landed, filed):
    """The landed card ids a check pass looks at, in landed order: every one but a card of the
    last generation (R18). A card the loop never filed is checked like a generation 1 card."""
    return tuple(card for card in landed
                 if _generation(card, filed) != LAST_GENERATION)


def generation_for(kind, card, sent, filed):
    """The generation of a card filed from one finding (R18). `kind` is TOUR or CHECK; `card`
    the checked card the finding names, or None; `sent` the card ids the check pass was sent;
    `filed` the loop's {card id: record}.

    A tour filing is generation 1. On a check, a finding that names no card, or a card the pass
    was not sent, takes the last generation, since nothing can show it is further from a fix
    (KTD4). A finding from a card the loop did not file is generation 1, and from a card the
    loop filed, one past that card's, never past the last."""
    if kind == TOUR:
        return 1
    if kind != CHECK:
        raise ValueError("pass kind must be one of %s, not %r" % (", ".join(_KINDS), kind))
    if card is None or str(card) not in {str(item) for item in sent}:
        return LAST_GENERATION
    parent = _generation(card, filed)
    if parent is None:
        return 1
    return min(parent + 1, LAST_GENERATION)


@dataclass(frozen=True)
class Patches:
    """Patch counts per area and the areas that have reached the cap, sorted (R19)."""
    counts: dict = field(default_factory=dict)
    reached: tuple = ()


def area_patches(filed, checks, cap):
    """Count, per area, the landed loop cards whose check filed a finding in the same area
    again, and name the areas at `cap` or past it (R19). `filed` is the loop's {card id:
    record}, each record carrying `area`; `checks` is {card id: areas}, the areas each landed
    card's check filed in. A card the loop did not file patches nothing, and a card counts
    once however many findings its check filed in its area."""
    _positive("the patch cap", cap)
    counts = {}
    for card, areas in checks.items():
        record = filed.get(str(card))
        if record is None:
            continue
        area = record.get("area")
        if area is not None and area in set(areas):
            counts[area] = counts.get(area, 0) + 1
    reached = tuple(sorted(area for area, count in counts.items() if count >= cap))
    return Patches(counts=counts, reached=reached)


@dataclass(frozen=True)
class PassResult:
    """One pass, as `should_stop` reads it. `kind` TOUR or CHECK; `status` RAN, NOT_RUN, or
    FAILED; `findings` every finding the report gave, of any outcome, and the planning finding
    the pass synthesized, which `should_stop` sets aside by its mark; `new_cards` the count of
    new cards the pass confirmed filed, a comment on an open card and an attended planning card
    both not among them."""
    kind: str
    status: str
    findings: tuple = ()
    new_cards: int = 0


def should_stop(result, rounds, started_at, now, cards_filed, report_only=False,
                settings=Settings()):
    """The reason the loop stops after `result`, a PassResult, or None to go on (R16, R17,
    R23). `rounds` is the count of tours that ran, this one included; `started_at` and `now`
    the loop's start and the present, in one frame; `cards_filed` the loop's new cards so far,
    this pass's included.

    In order: a report only tour that ran stops; a tour that ran with no high or medium finding
    of any outcome stops clean, the planning finding of R19 not counted, since it is the loop's
    request for a person and not a defect the tour found; the card budget stops any pass, one
    that did not run among them, once `max_cards_total` is reached, and so names the budget
    rather than open findings for a tour whose findings it kept from filing; a tour that ran
    whose high and medium findings produced no new card, all commented onto open cards or
    dropped for stopped areas, stops on open findings rather than clean, and a planning card
    filed beside them is no new card; the round cap stops a tour that ran at `max_rounds`; and
    the clock stops any pass. A check pass never stops on what it found. A pass kind or status
    outside the known words raises ValueError rather than reading as a pass that did not run."""
    if result.kind not in _KINDS:
        raise ValueError("pass kind must be one of %s, not %r"
                         % (", ".join(_KINDS), result.kind))
    if result.status not in _STATUSES:
        raise ValueError("pass status must be one of %s, not %r"
                         % (", ".join(_STATUSES), result.status))
    toured = result.status == RAN and result.kind == TOUR
    if toured and report_only:
        return STOP_REPORT_ONLY
    if toured and not any(is_serious(finding) and not is_attended(finding)
                          for finding in result.findings):
        return STOP_CLEAN
    if cards_filed >= settings.max_cards_total:
        return STOP_BUDGET
    if toured and result.new_cards == 0:
        return STOP_OPEN_FINDINGS
    if toured and rounds >= settings.max_rounds:
        return STOP_ROUNDS
    if now - started_at >= timedelta(hours=settings.max_hours):
        return STOP_CLOCK
    return None
