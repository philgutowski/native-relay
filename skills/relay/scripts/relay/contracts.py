"""Every string Relay depends on from outside itself, pinned in one place.

A contract here is a fact about another program: a backend CLI, the transcript it writes, or
the built in skill the brief names. Each pin names its source so a version bump is one diff.
Relay's own vocabulary (halt classes, record statuses, the envelope fence tag, the closeout
terminal lines) lives here too so brief, classify, closeout, verify, and summary share one set.
"""
import re

# Backward-compatible Claude pin. New terminal records use the per-backend values in
# BACKEND_PINS, which are the single source of truth for all three CLIs.
CLI_VERSION_TESTED = "2.1.268"

# The return envelope (KTD8): the status field, its three values, and the list keys. The task
# brief asks for it inside a fenced block with ENVELOPE_FENCE_TAG so a quoted `status:`
# elsewhere in the final message cannot be mistaken for it.
ENVELOPE_STATUS_KEY = "status"
ENVELOPE_STATUS_COMPLETE = "complete"
ENVELOPE_STATUS_BLOCKED = "blocked"
ENVELOPE_STATUS_FAILED = "failed"
ENVELOPE_STATUSES = (ENVELOPE_STATUS_COMPLETE, ENVELOPE_STATUS_BLOCKED, ENVELOPE_STATUS_FAILED)
ENVELOPE_BLOCKERS_KEY = "blockers"
ENVELOPE_CHANGED_FILES_KEY = "changed_files"
ENVELOPE_LEARNINGS_KEY = "learnings"
ENVELOPE_FENCE_TAG = "relay-envelope"

# The Closeout process's ending contract: its last non-empty line is exactly one of these. The
# runner reads it from the end of the message, never the head.
CLOSEOUT_COMPLETE_LINE = "Documentation complete"
CLOSEOUT_SKIPPED_LINE = "Documentation skipped"
CLOSEOUT_TERMINAL_LINES = (CLOSEOUT_COMPLETE_LINE, CLOSEOUT_SKIPPED_LINE)


# CLI contracts, observed on CLI_VERSION_TESTED and documented nowhere.
# A denied tool call is a `user` transcript line whose tool_result content begins with this.
# Backends U6 found a real Bash denial reads "Permission to use Bash with command <cmd> has
# been denied.", naming the command between the tool and the verdict; a Jira or other named-tool
# denial has no such clause. `.*` (not anchored immediately after the tool name) covers both,
# proven against tests/fixtures/backends/claude/denial-refusal.jsonl, a real capture this
# anchored-immediately form never matched.
DENIAL_REGEX = re.compile(r"^Permission to use (\w+)\b.*has been denied")
# Issue #57. Grok's own cancellation phrasing for a tool call its permission layer cancelled
# outright rather than refusing: "User cancelled the execution for tool `run_terminal_command`",
# quoted verbatim in the grok BACKEND_PINS comment below. Distinct from DENIAL_REGEX above: no
# "User" is present to have cancelled anything in a headless run, and the shape carries no
# `has been denied` clause, so a shared regex would either miss this or misread a real denial.
# Unanchored, unlike DENIAL_REGEX: DENIAL_REGEX only ever matches text grok.py reconstructs
# itself at a known position, but this one matches Grok's own body text passed through verbatim,
# and code review found no evidence the phrase always leads the body. Matched with `.search()`
# at both call sites (this file's classify.py and backends/grok.py) rather than `.match()`, so a
# future capture that puts other content first still fires this finding instead of silently
# reverting to the pre-fix no_envelope/unclean_exit reading, the same way `_DENIAL_MARKER in
# body` already tolerates surrounding text on the sibling denial path.
CANCELLED_TOOL_REGEX = re.compile(r"User cancelled the execution for tool `?(\w+)`?")
# Under dontAsk an Edit or Write on a path under .claude/ is denied regardless of the allowlist.
CLAUDE_DIR_PATH_REGEX = re.compile(r"(^|/)\.claude/")
# The pre-flight scan form from the solutions doc: catches the path inside prose, quotes,
# and markdown wrappers (a link's `[`, and an asterisk for bold, italic, or a list marker).
CLAUDE_DIR_SCAN_REGEX = re.compile(r"(^|[\s\"'`(/\[*])\.claude/", re.MULTILINE)

# Transcript line types (the session jsonl the CLI writes). Only these three carry evidence.
TRANSCRIPT_TYPE_ASSISTANT = "assistant"
TRANSCRIPT_TYPE_USER = "user"
TRANSCRIPT_TYPE_LAST_PROMPT = "last-prompt"

# Claude's argv flag set (R10, outer loop KTD7). Other backends' flags live on BACKEND_PINS
# and in the backend modules. The stub accepts this set.
CLI_FLAGS = (
    "--session-id",
    "--model",
    "--effort",
    "--permission-mode",
    "--allowedTools",
    "--disallowedTools",
    "--output-format",
    "--verbose",
)
FORBIDDEN_PERMISSION_MODE = "bypassPermissions"
OUTPUT_FORMAT = "stream-json"

# Per-backend launch facts, every one observed by running the installed CLI against a throwaway
# target repository on 2026-08-28, plus `review_skill`, the built in review the native brief
# names on that backend (None where no verified equivalent exists, which is what makes
# manifest.validate refuse the backend in native mode). Nothing here is read from
# documentation. Pins are the producer. backends.Capability is the frozen view the backend
# modules copy. Do not restate these values elsewhere.
#
# The fixtures these were taken from are in tests/fixtures/backends/, one directory per backend,
# and tests/fixtures/backends/README.md names which task produced which file.
BACKEND_PINS = {
    "claude": {
        "binary": "claude",
        # Bumped 2026-09-11 (issue #14) to the version a live run and a gate probe both
        # exercised, replacing a pin seventeen patch versions behind the installed binary. The
        # .claude/ path gate is still in force here for Edit and Write. The probe also found it
        # refusing one Bash redirection form into that directory while allowing another, so the
        # Bash half is leaky and nothing should be built on either outcome;
        # docs/solutions/workflow-issues/headless-dontask-blocks-claude-dir-edits.md has the runs.
        "version_tested": "2.1.268",
        # `claude --version` leads with the number, so the leading-digit parse works here and
        # nowhere else. See the two entries below.
        "version_output_sample": "2.1.268 (Claude Code)",
        "headless_flag": "-p",
        "session_id_choosable": True,
        "permission_mode": "dontAsk",
        "forbidden_permission_modes": ("bypassPermissions",),
        "output_format": ("--output-format", "stream-json", "--verbose"),
        "allow_flag": "--allowedTools",
        "deny_flag": "--disallowedTools",
        # Demonstrated, not assumed (R25): tests/fixtures/backends/claude/denial-refusal.jsonl
        # holds a refused `rm -rf`, and the target file was still present afterwards.
        "enforces_at_launch": True,
        # The built in review skill the native brief's review step names, invoked through the
        # Skill tool in a headless session. The 2026-08-25 proof run showed a headless process
        # reaching it on its own, so the classifier can see the call in the transcript.
        "review_skill": "code-review",
        "evidence": "session jsonl under ~/.claude/projects/<slug>/<session-id>.jsonl",
        "credential_prefixes": ("ANTHROPIC_", "CLAUDE_"),
        "credential_file": "~/.claude/.credentials.json",
        "nesting_markers": ("CLAUDECODE", "CLAUDE_CODE_"),
        "writes_into_worktree": False,
        "extra_writable_dirs": (),
        "config_overrides": (),
        "strict_config": False,
        "grants_network": False,
        "commit_message_constraint": None,
    },
    "codex": {
        "binary": "codex",
        "version_tested": "0.149.0",
        # Leads with a name token, so the leading-digit parse returns None. KTD8 is why parsing
        # is per-backend rather than one regex.
        "version_output_sample": "codex-cli 0.149.0",
        "headless_flag": "exec",
        # Codex assigns its own thread id, so the runner names the evidence instead (KTD4).
        "session_id_choosable": False,
        "permission_mode": "workspace-write",
        "forbidden_permission_modes": ("danger-full-access",
                                       "--dangerously-bypass-approvals-and-sandbox"),
        "output_format": ("--json",),
        "allow_flag": None,
        "deny_flag": None,
        # No per-tool deny flag exists, so no refusal can be demonstrated and R25 records it as
        # not enforcing. R19's acceptance sentence, R21's landing bound, and R24's audit are the
        # compensating controls. Codex does refuse some `rm -f` shapes on its own, but that is
        # its own built-in judgment and not something the manifest's disallow list can reach.
        #
        # Until 2026-09-01 the sandbox's absent network was also holding shut, on this backend
        # alone, every remote reaching pattern in DISALLOWED_TOOLS and all of
        # CLOSEOUT_DISALLOWED_EXTRA: with no deny flag they never reach the argv, so a push
        # simply could not complete. `config_overrides` below removes that, and nothing detects a
        # push from a Task or Closeout here yet. Issue #60.
        "enforces_at_launch": False,
        # No verified built in review reachable from `codex exec`, so native mode refuses this
        # backend at validate until one is observed live.
        "review_skill": None,
        "evidence": "stdout log plus --output-last-message file; session jsonl at "
                    "~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl",
        "credential_prefixes": ("CODEX_", "OPENAI_"),
        "credential_file": "~/.codex/auth.json",
        "nesting_markers": ("CODEX_SANDBOX", "CODEX_HOME"),
        "writes_into_worktree": False,
        # U1 finding, not in the plan. Under `--sandbox workspace-write` every write beneath
        # .git/ is refused ("Unable to create '.../.git/index.lock': Operation not permitted"),
        # so a Task cannot branch or commit at all. Passing the repository's own .git as an
        # extra writable directory is what makes the sandbox usable for Relay's purposes.
        "extra_writable_dirs": ("<repo>/.git",),
        # Issue #51, observed on codex-cli 0.151.0 on 2026-09-01. Under `--sandbox
        # workspace-write` the sandbox blocks network by default, so `gh` cannot reach
        # api.github.com and a Task cannot move or comment its own card. Every codex task cost a
        # hand landing. This override restores the reach; the session header then reads
        # "(network access enabled)".
        #
        # The grant is all or nothing. `sandbox_workspace_write` takes four fields,
        # writable_roots, network_access, exclude_tmpdir_env_var, exclude_slash_tmp, and no host
        # allowlist: `-c 'sandbox_workspace_write.allowed_domains=["api.github.com"]'` is refused
        # with "unknown configuration field". So a codex Task reaches every host, not just the
        # tracker, holding whatever the child env carries, and run._unenforced_scalar says both
        # halves on the record. It is also unconditional across tracker adapters: a markdown
        # adapter run needs no network and gets the reach anyway, because gating the emit on the
        # adapter would put a launch fact outside this table.
        "config_overrides": ("sandbox_workspace_write.network_access=true",),
        # Rides with the override rather than standing on its own. Without it, a key this CLI
        # does not recognize is accepted and ignored: the process runs, the sandbox stays fenced,
        # and the only symptom is the blocked halt the override exists to remove. Neither the
        # suite nor the stub can see that, because both derive the argv from this same pin. With
        # it, the same key fails before launch with "Error loading config.toml: unknown
        # configuration field ... in -c/--config override". The cost is that it also validates the
        # operator's own ~/.codex/config.toml, so a field this codex version rejects there fails
        # every launch, loudly, which is the trade this pin accepts.
        "strict_config": True,
        # What the override above means, stated as a fact rather than left for another module to
        # infer from the token text. `run._unenforced_scalar` needs the answer and must not have
        # to parse a `-c` value to get it: a token pinned as `network_access=false` would read as
        # a grant to any substring test, and the record would then claim a reach the Task does
        # not have.
        "grants_network": True,
        "commit_message_constraint": None,
    },
    "grok": {
        "binary": "grok",
        # Bumped 2026-09-11 (docs/plans/2026-09-11-feat-grok-native-review-step-plan.md).
        # The dontAsk and denial-refusal findings were pinned against 1.0.5. The cancellation
        # finding below was confirmed against 1.0.13. This pin is the version installed and
        # probed that day.
        "version_tested": "1.0.25",
        "version_output_sample": "grok 1.0.25 (f7e67d6988e2) [stable]",
        "headless_flag": "-p",
        "session_id_choosable": True,
        # U1 finding, and a correction to the plan's Assumptions and KTD6. Grok accepts
        # `dontAsk` at launch and then cancels every tool call the task makes, reporting
        # "User cancelled the execution for tool `run_terminal_command`" with no human present
        # to have cancelled anything. Reproduced five times on 1.0.5: two full pipeline runs
        # that died partway through planning, and three single-command probes. Re-observed
        # 2026-09-11 on grok 1.0.25: `--permission-mode dontAsk` cancelled a `write` the same
        # way ("User cancelled the execution for tool `write`"), so the finding is not limited
        # to `run_terminal_command`. `auto` is the mode that runs the task AND still refuses a
        # denied call, so it is the non-bypass posture here.
        #
        # Issue #57, observed round eight 2026-09-01 on tasks 45 and 56, confirmed live against
        # grok 1.0.13 the same day. Under `auto` mode a `run_terminal_command` whose argument
        # uses command substitution or a heredoc, the `git commit -m "$(cat <<'EOF' ...
        # EOF)"` form many agent commit guides teach, is cancelled outright rather
        # than executed or refused: `updates.jsonl` carries the exact same shape as the
        # demonstrated `--deny` refusal above, a `tool_call_update` with `status: "failed"`, but
        # the body reads "User cancelled the execution for tool `run_terminal_command`" instead
        # of a permission-policy denial. This is session-fatal, not a retryable denial. No
        # retry, no return envelope, whatever the task had in flight is stranded.
        # `classify.py` surfaces it as a `CANCELLED_TOOL_CALL` finding, a sibling check beside
        # the denial scan on the same file; `commit_message_constraint` below tells the task to
        # avoid the construct, since instruction is the only enforcement layer this backend has
        # for it. The demonstrated `--deny` refusal above never engages here, because the
        # matcher does not refuse a shape it cannot analyze, it cancels the call instead.
        #
        # Re-observed 2026-09-11 on grok 1.0.25: a trivial single-turn `-p` probe executed that
        # exact heredoc form to completion (commit a69fdfe). The 1.0.13 pin already said a
        # trivial probe does not reproduce the cancel; this is that case. The constraint stays
        # because the original finding was a multi-turn Task, and because grok now loads the
        # operator's skill catalogue, including `ce-commit-push-pr`, which still teaches the
        # heredoc form. Any skill or guide the process reads showing a worked heredoc commit
        # defeats the brief.
        "permission_mode": "auto",
        "forbidden_permission_modes": ("bypassPermissions", "dontAsk"),
        "output_format": ("--output-format", "streaming-json"),
        "allow_flag": "--allow",
        "deny_flag": "--deny",
        # Demonstrated (R25): tests/fixtures/backends/grok/denial-refusal.jsonl holds
        # "Denied by permission policy: deny rule on bash matching \"rm -rf*\"", captured with
        # the target directory still present afterwards. A malformed rule is refused at launch
        # ("malformed rule: missing closing parenthesis") rather than silently accepted, and a
        # bare `Skill` entry, which closeout.BASE_TOOLS carries, is accepted.
        #
        # Re-observed 2026-09-11 on grok 1.0.25: `--deny 'Bash(rm -rf*)'` still refuses, and the
        # marker still contains "Denied by permission policy". `--allow` with Claude tool
        # vocabulary (`Bash`, `Read`, `Edit`, `Write`, `Skill`) is accepted at launch and does
        # not grant grok's real tools. `--deny run_terminal_command` is accepted at launch and
        # does not refuse `run_terminal_command`. The working deny form remains `Bash(glob)`.
        "enforces_at_launch": True,
        # Observed 2026-09-11 on grok 1.0.25 (P1): a headless `grok -p` under `--permission-mode
        # auto` whose prompt named `/code-review` did not reach bundled `code-review` (that
        # skill carries `disable-model-invocation: true` and was not injected). The process
        # read `~/.grok/bundled/skills/review/SKILL.md` and ran `/review` to completion. Skip
        # stays undetectable: there is no Skill tool event, a `read_file` of the skill directory
        # is a read not a run, and `subagent_spawned` with description `[reviewer]` is a
        # convention inside `/review`.
        "review_skill": "review",
        "evidence": "~/.grok/sessions/<url-encoded-realpath-cwd>/<session-id>/updates.jsonl",
        "credential_prefixes": ("GROK_", "XAI_"),
        "credential_file": "~/.grok/auth.json",
        "nesting_markers": ("GROK_SANDBOX",),
        "writes_into_worktree": False,
        "extra_writable_dirs": (),
        "config_overrides": (),
        "strict_config": False,
        "grants_network": False,
        # Issue #57. Instruction is the only enforcement layer this backend has for the
        # cancellation R4 and the BACKEND_PINS caveat below both describe. Strengthened
        # 2026-09-11: grok loads the operator catalogue, so a skill showing the heredoc
        # form is the common case, not an edge.
        "commit_message_constraint": (
            "This CLI can cancel a git commit whose message uses command substitution or a "
            "heredoc, such as `git commit -m \"$(cat <<'EOF' ... EOF)\"`, instead of refusing "
            "it: the tool call is cancelled outright, no envelope is written, and whatever the "
            "task had in flight is stranded. Use plain `git commit` forms only. For a subject "
            "plus body, repeat `-m`: `git commit -m \"Subject\" -m \"Body paragraph.\"`. Any "
            "skill or guide that shows a worked heredoc commit is wrong for this CLI; do not "
            "follow that form even when a skill presents it as the way to commit."
        ),
    },
}

# Round six #40: a task chasing a hung unittest child ran `kill -9 <pids...>` and swept in the
# Runner's own PID and its caffeinate wrapper. kill/pkill/killall had no disallow entry, so
# nothing at the permission layer stopped it on an enforcing backend. Named separately so
# classify.scan_self_kill (KTD2) can check a matched command against exactly these globs without
# re-deriving them.
KILL_LIKE_TOOLS = (
    "Bash(kill*)",
    "Bash(pkill*)",
    "Bash(killall*)",
)

# R10 disallow list with every variant spelling. Defence in depth; landing safety rests on the
# runner owning merge and push.
DISALLOWED_TOOLS = (
    "Bash(git push --force*)",
    "Bash(git push -f*)",
    "Bash(git push --force-with-lease*)",
    "Bash(git push * +*)",
    "Bash(git reset --hard*)",
    "Bash(git checkout -- .*)",
    "Bash(git clean*)",
    "Bash(rm -rf*)",
    "Bash(rm -fr*)",
    "Bash(rm -r *)",
    "Bash(rm -R *)",
) + KILL_LIKE_TOOLS


def disallow_inner(pattern):
    """The glob inside a `Bash(...)` rule, or the pattern unchanged."""
    if pattern.startswith("Bash(") and pattern.endswith(")"):
        return pattern[5:-1]
    return pattern


# Named subset of DISALLOWED_TOOLS. A match on an unenforced backend refuses the landing
# rather than only annotating it. Force push, hard reset, and recursive delete. git clean
# and git checkout -- .* stay in the parent tuple and land with a finding.
DESTRUCTIVE_TOOLS = (
    "Bash(git push --force*)",
    "Bash(git push -f*)",
    "Bash(git push --force-with-lease*)",
    "Bash(git push * +*)",
    "Bash(git reset --hard*)",
    "Bash(rm -rf*)",
    "Bash(rm -fr*)",
    "Bash(rm -r *)",
    "Bash(rm -R *)",
)

# The closeout commits and the runner pushes for it (KTD15). A push from inside the closeout
# would put a commit on the remote before the runner's scope check could bound it, and a local
# reset cannot undo that, so the closeout's disallow list refuses every push spelling. The task
# process keeps the ordinary list, because in pr_terminal mode it has to push its own branch.
# Under shipping.push = false the runner pushes nothing for either process, and
# manifest.resolved_disallowed adds these same patterns to the Task process's list (issue #15).
CLOSEOUT_DISALLOWED_EXTRA = (
    "Bash(git push*)",
    "Bash(git -C * push*)",
)

# Task record statuses (the state machine in the plan's design section).
STATUS_PENDING = "pending"
STATUS_EXCLUDED = "excluded"
STATUS_RUNNING = "running"
STATUS_MERGING = "merging"
STATUS_BLOCKED = "blocked"
STATUS_HALTED = "halted"
STATUS_LANDED = "landed"
RECORD_STATUSES = (
    STATUS_PENDING,
    STATUS_EXCLUDED,
    STATUS_RUNNING,
    STATUS_MERGING,
    STATUS_BLOCKED,
    STATUS_HALTED,
    STATUS_LANDED,
)
# Statuses a reclaimed stale lease turns into halted with class runner_crashed (R55).
IN_FLIGHT_STATUSES = (STATUS_RUNNING, STATUS_MERGING)
# Statuses a task does not leave under its own power. Reaching one is what stamps `ended_at`,
# and a move between two of them is not a new ending: `verify.startup_reverify` promoting a
# halted record to landed must keep the stamp from the run that did the work.
TERMINAL_STATUSES = (STATUS_EXCLUDED, STATUS_BLOCKED, STATUS_HALTED, STATUS_LANDED)

# Halt classes (KTD6). `HALT_LINES` are the summary cause line templates, filled from evidence.
HALT_LANDED = "landed"
HALT_BLOCKED_ENVELOPE = "blocked_envelope"
HALT_NO_ENVELOPE = "no_envelope"
HALT_DENIED_TOOL = "denied_tool"
HALT_PATH_GATE = "path_gate"
HALT_TRACKER_WRITE_DENIED = "tracker_write_denied"
HALT_REMOTE_ADVANCED = "remote_advanced"
HALT_CLOSEOUT_OUT_OF_SCOPE = "closeout_out_of_scope"
HALT_RUNNER_CRASHED = "runner_crashed"
HALT_GATE_REFUSED = "gate_refused"
HALT_PARTIAL_LANDING = "partial_landing"
HALT_TIMEOUT = "timeout"
HALT_UNCLEAN_EXIT = "unclean_exit"
HALT_CI_UNDECIDED = "ci_undecided"
# The case KTD6's table did not cover: a defect or a library exception the runner did not
# anticipate. Without a class for it the run loop had no way to stop the way it promises to,
# so an unexpected error became a traceback and left the record reading running forever.
HALT_UNEXPECTED_ERROR = "unexpected_error"

# Findings the closeout raises (U9). Neither halts a run: the runner's own verify decides
# landing, and a card that went uncommented is a checklist line for the operator, not a stop.
CLOSEOUT_UNFINISHED = "closeout_unfinished"
BLOCKED_UNRECORDED = "blocked_unrecorded"
# A disallowed call that ran on a backend that does not enforce at launch. Finding only:
# landing refusal for the destructive subset is unexpected_error on the record.
UNENFORCED_DISALLOWED = "unenforced_disallowed"
# Round six #40: a stale-lease reclaim whose crashed task's own stdout log named the previous
# Runner's PID in a kill/pkill/killall command (classify.scan_self_kill). Finding only: the
# record's own halt_class stays runner_crashed (KTD6's closed set), this just says why.
RUNNER_SELF_KILL = "runner_self_kill"
# Round six #49: a task whose last message reads as waiting on background work that will not
# resume headless ("standing by", "will resume", "once the run finishes"). Finding only: the
# record's own halt_class stays whatever classify or the git-tree check already assigned (often
# no_envelope or unclean_exit), this just says the mechanism instead of leaving the Cause line
# to read only the downstream symptom.
WAITING_LAST_MESSAGE = "waiting_last_message"
# Issue #57. Grok's own permission layer cancelled a tool call outright (no user present in a
# headless run) rather than refusing it in a way the task could react to, most often a commit
# whose argument used command substitution or a heredoc. Finding only: the record's own
# halt_class stays whatever classify or the git-tree check already assigned (usually no_envelope
# or unclean_exit), this names the mechanism instead of leaving the Cause line to read only the
# downstream symptom, the same shape WAITING_LAST_MESSAGE above already established.
CANCELLED_TOOL_CALL = "cancelled_tool_call"
# Native mode. A Task on a backend with a `review_skill` whose skip is detectable, whose
# envelope read complete, and whose transcript holds no Skill call naming that skill.
# Finding only: the runner's own verify decides landing, and a review that never ran is a
# check by hand for the operator, not a stop. A backend that cannot observe a Skill call
# lists this class in `undetectable` even when it names a review skill, so classify does
# not attach a false skip. The 2026-08-25 proof run is the precedent: a headless process
# substituted or skipped review steps twice.
REVIEW_SKIPPED = "review_skipped"
# Issue #58. The Manifest's resolution decided this relaunch's backend or model, and it differed
# from what the record carried, so the Task went somewhere other than where it last ran. Finding
# only, and unlike the three above it names an operator's own choice rather than a failure: the
# record's `halt_class` is untouched, and summary deliberately keeps it off the pending checks
# list, because a move the operator asked for is not a chore they owe.
BACKEND_REASSIGNED = "backend_reassigned"
# Stale cards, 2026-09-08. The Closeout for a blocked or halted Task was told to return the card
# to the status it read before the run, and the card still reads the in review status when the
# Runner reads it back. Finding only, the same shape as BLOCKED_UNRECORDED: the Runner never
# writes to a tracker, so a card it could not get moved is a check by hand, not a stop.
CARD_LEFT_IN_REVIEW = "card_left_in_review"

# The audit's own classes (stale cards, 2026-09-08). These belong to a run rather than to a
# record, so they are not in LINE_CLASSES and have no HALT_LINES template: `audit.build` writes
# each finding's sentence itself, and `summary` copies it into the pending checks as it is.
AUDIT_STALE_IN_REVIEW = "card_stale_in_review"
AUDIT_REOPENED = "card_reopened"
AUDIT_CLOSED_UNLANDED = "card_closed_unlanded"
AUDIT_UNREADABLE = "card_unreadable"
AUDIT_FAILED = "audit_failed"
AUDIT_CLASSES = (AUDIT_STALE_IN_REVIEW, AUDIT_REOPENED, AUDIT_CLOSED_UNLANDED,
                 AUDIT_UNREADABLE, AUDIT_FAILED)

# The two .claude/ operator sentences, one per raiser of HALT_PATH_GATE from
# CLAUDE_DIR_PATH_REGEX (issue #8). HALT_LINES[path_gate] is {detail}, so the raiser writes the
# whole sentence and the Cause line says which wall was hit. They are separate because the
# repairs are opposites: one task's work is unfinished, the other's is finished and unmerged.
# docs/solutions/workflow-issues/headless-dontask-blocks-claude-dir-edits.md carries the
# evidence, under the Examples heading for the run this split came from.
#
# Both are written in the third person, describing a state rather than instructing a reader.
# They are Cause lines, and a Cause line reaches the Closeout process's brief by either of two
# routes. A finding is rendered through classify.finding_line, which closeout.py:109 calls in a
# loop over the record's findings. A tail refusal carries its own message instead, built by
# summary.cause_line at run.py:812 and placed in the brief's Cause field at closeout.py:162
# through brief.defang. The backstop sentence below only ever takes the second route, because a
# tail refusal attaches no finding at all: the 2026-09-11 live proof's record read findings: [].
# An earlier version of this comment named finding_line alone, which sent a reader tracing this
# defect past the raiser that caused it. Either route reaches an agent whose whole brief is to
# record the outcome and touch nothing, so a second person imperative would be an instruction to
# it. The operator's imperative lives in summary's checks by hand, which no process but the
# operator ever reads.
#
# classify's transcript promotion. The task asked the harness for a write and was refused, so
# the work never happened.
PATH_GATE_CLAUDE_DIR = (
    "edit under .claude/ denied by the task's permission posture; the work is unfinished and "
    "needs an attended session to do it, see solutions doc"
)
# gitwrite.local_merge_tail's backstop. The task did the work and the runner declined to land
# it, so the branch holds finished commits. Formatted at the raise site, not here: a {field}
# inside a filled {detail} is never expanded a second time.
PATH_GATE_CLAUDE_DIR_BACKSTOP = (
    "the merge tail refused {branch} because its diff touches {paths}; the work is finished and "
    "needs an attended gate and merge, not a rerun, see solutions doc"
)
# The one merge tail stage name read outside gitwrite: `summary` groups the backstop refusal's
# check line by it, because that check names the opposite repair to the transcript raiser's.
# Every other stage stays a literal at its own TailResult, since nothing else reads them.
TAIL_STAGE_BACKSTOP = "backstop"

HALT_CLASSES = (
    HALT_LANDED,
    HALT_BLOCKED_ENVELOPE,
    HALT_NO_ENVELOPE,
    HALT_DENIED_TOOL,
    HALT_PATH_GATE,
    HALT_TRACKER_WRITE_DENIED,
    HALT_REMOTE_ADVANCED,
    HALT_CLOSEOUT_OUT_OF_SCOPE,
    HALT_RUNNER_CRASHED,
    HALT_GATE_REFUSED,
    HALT_PARTIAL_LANDING,
    HALT_TIMEOUT,
    HALT_UNCLEAN_EXIT,
    HALT_CI_UNDECIDED,
    HALT_UNEXPECTED_ERROR,
)

# Classes that always stop the whole run (issue #15). Each puts something outside the failing
# task in question, so no later task's assumptions hold. Every other class is a candidate for
# continuing past when the manifest opts in, decided from the repo's state after the halt by
# gitwrite.resume_disposition rather than from the class name: the same class can leave the
# repo usable (a gate command that failed on the task branch) or not (a push that failed after
# the merge, leaving the default ahead of origin), and only the repo can tell them apart.
RUN_SCOPED_HALT_CLASSES = (
    HALT_REMOTE_ADVANCED,   # origin moved under the runner; every later baseline is suspect
    HALT_RUNNER_CRASHED,    # the lease was lost; another runner may be live in this repo
    HALT_UNEXPECTED_ERROR,  # a defect or library error with unknown blast radius
)

# Classes that mean the Closeout process itself just misbehaved. Distinct from
# RUN_SCOPED_HALT_CLASSES (neither stops the whole run, per _continue_past): the run.py halt
# comment (KTD3, R5) skips relaunching Closeout on these, since relaunching the exact mechanism
# that just went out of scope would trust it again on the strength of the trust that just failed.
# HALT_TRACKER_WRITE_DENIED is deliberately absent: it is a FINDING_CLASSES member, attached to a
# record rather than ever raised as a _Halt's own class, so it can never reach this check.
CLOSEOUT_MISBEHAVED_HALT_CLASSES = (
    HALT_CLOSEOUT_OUT_OF_SCOPE,
)

# Classes that are findings attached to a record rather than the record's own class.
FINDING_CLASSES = (
    HALT_DENIED_TOOL,
    HALT_PATH_GATE,
    HALT_TRACKER_WRITE_DENIED,
    HALT_NO_ENVELOPE,
    CLOSEOUT_UNFINISHED,
    BLOCKED_UNRECORDED,
    UNENFORCED_DISALLOWED,
    RUNNER_SELF_KILL,
    WAITING_LAST_MESSAGE,
    CANCELLED_TOOL_CALL,
    REVIEW_SKIPPED,
    BACKEND_REASSIGNED,
    CARD_LEFT_IN_REVIEW,
)

# Every class that can reach a summary line: the closed halt class set of KTD6, plus the
# findings that are never a record's own class but still have to print.
LINE_CLASSES = HALT_CLASSES + (
    CLOSEOUT_UNFINISHED, BLOCKED_UNRECORDED, UNENFORCED_DISALLOWED, RUNNER_SELF_KILL,
    WAITING_LAST_MESSAGE, CANCELLED_TOOL_CALL, REVIEW_SKIPPED, BACKEND_REASSIGNED,
    CARD_LEFT_IN_REVIEW,
)

HALT_LINES = {
    HALT_LANDED: "landed at {ref}",
    HALT_BLOCKED_ENVELOPE: "blocked: {blocker}",
    HALT_NO_ENVELOPE: "exited without a return envelope; last message: {last_message}",
    HALT_DENIED_TOOL: "{tool} denied by the task's permission posture on {target}",
    HALT_PATH_GATE: "{detail}",
    HALT_TRACKER_WRITE_DENIED: "code landed, card unmoved: {tool} denied",
    # Names both movers. Under shipping.push = false the one that moved can be the local default
    # branch, and run._merge_route raises with this line as its message, so a line naming only
    # the remote would be the operator's sole and wrong account. Evidence `reason` says which.
    HALT_REMOTE_ADVANCED: ("the default branch moved during the task, locally or at the remote; "
                           "merge aborted at {sha}"),
    HALT_CLOSEOUT_OUT_OF_SCOPE: "closeout changed {path} outside {allowed}",
    # `status_before`, not `status`: the record is a rendering source too and carries its
    # own post crash `status`, which used to shadow the evidence and print "during halted"
    # for every crash. The tree is deliberately absent: the reclaim path that raises this
    # most often runs in a later process that never saw the repository.
    HALT_RUNNER_CRASHED: "runner died during {status_before} on {branch}",
    HALT_GATE_REFUSED: "gate refused {branch} at {sha}; output in {log}",
    HALT_PARTIAL_LANDING: "landed at {sha} but card reads {card_status}",
    HALT_TIMEOUT: "timed out after {active_minutes} active minutes ({wall_minutes} wall); tree {tree} on {branch}",
    HALT_UNCLEAN_EXIT: "left the tree dirty on {branch}",
    HALT_CI_UNDECIDED: "PR {url} open, CI undecided after {minutes} minutes",
    HALT_UNEXPECTED_ERROR: "the runner hit an unexpected {error_type} on {task}: {error}",
    CLOSEOUT_UNFINISHED: "the closeout ended without a terminal line; last message: {last_message}",
    BLOCKED_UNRECORDED: "blocked and the card carries no new comment; check {task} by hand",
    UNENFORCED_DISALLOWED: "{tool} ran {argument} at line {line} matching {pattern}",
    RUNNER_SELF_KILL: "self-kill: {command} named the runner's own pid {victim_pid} among {pids}",
    WAITING_LAST_MESSAGE: "ended the turn waiting on background work that does not resume headless: {last_message}",
    CANCELLED_TOOL_CALL: "the CLI cancelled its own tool call, no user present: {tool} on {target}",
    REVIEW_SKIPPED: "completed without running {review}",
    # Tense neutral on purpose: the runner streams this sentence before the launch, where the
    # move is still intent, and writes it onto the record afterwards, where it is history.
    BACKEND_REASSIGNED: ("{to_backend} {to_model}, reassigned from "
                         "{from_backend} {from_model}"),
    CARD_LEFT_IN_REVIEW: ("the card still reads {card_status} after the closeout; move {task} "
                          "to {return_to} by hand"),
}

# The digest classify.classify() (U7) guarantees, read by run.py and closeout.py via
# digest.get(...). tests/test_contracts.py asserts both readers stay inside this set and that
# classify keeps setting every key either reader uses.
DIGEST_KEYS = frozenset((
    "transcript_path",
    "transcript_present",
    "exit_code",
    "timed_out",
    "line_count",
    "malformed_lines",
    "tool_calls",
    "findings",
    # R13, KTD5: True when the evidence source could not be read, so `findings` is None rather
    # than empty. A reader that cannot tell "we looked and found none" from "we could not look"
    # reports a runner fault as the task's silence.
    "findings_unavailable",
    "envelope",
    "last_message",
    "last_message_tail",
    "halt_class",
    "routable",
    # Backends U6, R5: halt-class constants this backend's evidence cannot show, so a reader can
    # tell "not checked" from "checked, none found" per finding class.
    "undetectable",
))

# Terminal record run statuses (R30, U3 step 6).
RUN_COMPLETED = "completed"
RUN_HALTED = "halted"
RUN_CRASHED = "crashed"

# Defaults from KTD11, applied by name in validate so nothing is silent.
DEFAULT_TASK_TIMEOUT_MINUTES = 120
DEFAULT_CI_POLL_MINUTES = 30
DEFAULT_CLOSEOUT_TIMEOUT_MINUTES = 20
DEFAULT_CLOSEOUT_MODEL = "sonnet"
DEFAULT_CLOSEOUT_EFFORT = "medium"
DEFAULT_GATE_TIMEOUT_MINUTES = 30
DEFAULT_TASK_BRANCH_PREFIX = "relay/"

# A push can run the project's gate inside a pre-push hook, so it is bounded by the gate's own
# timeout plus the network transfer, never by gitread's read timeout. See
# docs/solutions/ for why: a 120 second read bound killed a push whose hook was running a 216
# second suite, and the runner reported it as an unexpected error.
PUSH_NETWORK_MARGIN_SECONDS = 120
DEFAULT_DOCS_ROOT = "docs"
CONCEPTS_FILE = "CONCEPTS.md"

# Lease timing (KTD10). The TTL must stay shorter than any task timeout.
LEASE_HEARTBEAT_SECONDS = 60
LEASE_TTL_SECONDS = 600

STATE_SCHEMA_VERSION = 2


def slug_for(path):
    """The CLI's project slug (KTD7): the absolute path with every character outside
    [A-Za-z0-9] replaced by a hyphen. Verified against the directories under ~/.claude/projects.
    Callers pass a realpath, because macOS temp dirs are symlinks and both sides must agree."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def transcript_path(home, cwd_realpath, session_id):
    """Where the CLI writes the session transcript for a process started in cwd."""
    import os

    return os.path.join(home, ".claude", "projects", slug_for(cwd_realpath), session_id + ".jsonl")
