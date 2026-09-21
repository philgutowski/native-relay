"""Dual manifest pairs: split a mixed Task list into claude and grok members.

A Pair is two Manifests that share the project, tracker, qualifying sentences, shipping, and
gate, and that partition one Task list so each Task appears in exactly one member. The original
order is the merge order. Dispatch runs both backends at once in worktrees and still merges in
that order.

Backend assignment is not done here. The rubric in the skill is judgment, not a table. This
module preserves the order the operator already chose and splits on the backend each Task
already names.
"""
import json
import os
from dataclasses import dataclass, replace

from . import gitread, manifest as manifest_module

NATIVE_PAIR_BACKENDS = ("claude", "grok")


class PairError(ValueError):
    """The file is not a pair, or the two members do not make one."""


@dataclass(frozen=True)
class Pair:
    path: str
    claude: object
    grok: object
    order: tuple
    # "pair" when loaded from a pair file, "mixed" when derived from one Manifest.
    source: str = "pair"


def queues(manifest):
    """Task ids grouped by backend, plus the original order. The merge sequence is `order`."""
    claude, grok, other = [], [], []
    for task in manifest.tasks:
        if task.backend == "claude":
            claude.append(task.id)
        elif task.backend == "grok":
            grok.append(task.id)
        else:
            other.append(task.id)
    return {
        "claude": tuple(claude),
        "grok": tuple(grok),
        "other": tuple(other),
        "order": tuple(task.id for task in manifest.tasks),
    }


def from_manifest(manifest):
    """Build an in memory Pair from a mixed Manifest. Raises PairError when both native
    backends are not present, or when a Task names a third backend."""
    grouped = queues(manifest)
    if grouped["other"]:
        raise PairError(
            "a pair cannot include backend %s; native dispatch runs claude and grok"
            % ", ".join(sorted(set(
                task.backend for task in manifest.tasks if task.backend not in NATIVE_PAIR_BACKENDS
            )))
        )
    if not grouped["claude"] or not grouped["grok"]:
        raise PairError(
            "a pair needs at least one claude task and one grok task; use run for a single backend"
        )
    claude_tasks = tuple(task for task in manifest.tasks if task.backend == "claude")
    grok_tasks = tuple(task for task in manifest.tasks if task.backend == "grok")
    return Pair(
        path=manifest.path,
        claude=replace(manifest, tasks=claude_tasks),
        grok=replace(manifest, tasks=grok_tasks),
        order=grouped["order"],
        source="mixed",
    )


def load(path):
    """Load a pair file and both member Manifests. Paths in [pair] are relative to the file."""
    import tomllib

    path = os.path.abspath(path)
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        raise PairError("pair file not found: %s" % path)
    except tomllib.TOMLDecodeError as exc:
        raise PairError("pair file is not valid TOML: %s" % exc)
    table = raw.get("pair")
    if not isinstance(table, dict):
        raise PairError("not a pair file: missing [pair] table")
    for key in ("claude", "grok", "order"):
        if key not in table:
            raise PairError("[pair] is missing %s" % key)
    if not isinstance(table["order"], list) or not table["order"]:
        raise PairError("[pair] order must be a non empty list of task ids")
    base = os.path.dirname(path)
    claude_path = table["claude"] if os.path.isabs(table["claude"]) else os.path.join(base, table["claude"])
    grok_path = table["grok"] if os.path.isabs(table["grok"]) else os.path.join(base, table["grok"])
    try:
        claude = manifest_module.load(claude_path)
        grok = manifest_module.load(grok_path)
    except manifest_module.ManifestError as exc:
        raise PairError(str(exc)) from exc
    return Pair(path=path, claude=claude, grok=grok, order=tuple(str(item) for item in table["order"]),
                source="pair")


def is_pair_file(path):
    """True when the file has a [pair] table. Used by the CLI to pick load vs split."""
    import tomllib

    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return isinstance(raw.get("pair"), dict)


def combine(pair):
    """One Manifest whose tasks follow pair.order, keyed on the pair file path for state."""
    by_id = {}
    for task in tuple(pair.claude.tasks) + tuple(pair.grok.tasks):
        if task.id in by_id:
            raise PairError("task %s appears in both pair members" % task.id)
        by_id[task.id] = task
    missing = [task_id for task_id in pair.order if task_id not in by_id]
    extra = [task_id for task_id in by_id if task_id not in pair.order]
    if missing or extra:
        raise PairError("pair order does not match the union of member tasks: missing %s extra %s"
                        % (missing, extra))
    tasks = tuple(by_id[task_id] for task_id in pair.order)
    return replace(pair.claude, path=pair.path, tasks=tasks)


def member_errors(pair):
    """The ways two members fail to be one pair. Empty means they compose."""
    errors = []
    claude, grok = pair.claude, pair.grok
    try:
        claude_id = gitread.repo_identity(claude.project.repo)
        grok_id = gitread.repo_identity(grok.project.repo)
        if claude_id != grok_id:
            errors.append("pair members must name the same repository (claude %s, grok %s)"
                          % (claude_id, grok_id))
    except gitread.GitError as exc:
        errors.append("could not read a pair member repository: %s" % exc)
    if claude.tracker.adapter != grok.tracker.adapter:
        errors.append("pair members must use the same tracker adapter")
    if claude.shipping_mode != grok.shipping_mode or claude.shipping_push != grok.shipping_push:
        errors.append("pair members must use the same shipping mode and push setting")
    if claude.project.branch_prefix != grok.project.branch_prefix:
        errors.append("pair members must use the same branch prefix")
    if tuple(claude.gate.command) != tuple(grok.gate.command):
        errors.append("pair members must use the same gate command")
    claude_ids = [task.id for task in claude.tasks]
    grok_ids = [task.id for task in grok.tasks]
    overlap = sorted(set(claude_ids) & set(grok_ids))
    if overlap:
        errors.append("task ids appear in both members: %s" % ", ".join(overlap))
    wrong_claude = [task.id for task in claude.tasks if task.backend != "claude"]
    wrong_grok = [task.id for task in grok.tasks if task.backend != "grok"]
    if wrong_claude:
        errors.append("claude member has non claude tasks: %s" % ", ".join(wrong_claude))
    if wrong_grok:
        errors.append("grok member has non grok tasks: %s" % ", ".join(wrong_grok))
    if not claude.tasks or not grok.tasks:
        errors.append("each pair member must list at least one task")
    union = claude_ids + grok_ids
    if sorted(union) != sorted(pair.order):
        errors.append("pair order must be the union of member task ids, each once")
    if len(set(pair.order)) != len(pair.order):
        errors.append("pair order repeats a task id")
    return errors


def validate(pair, env=None):
    """Validate both members as Manifests, then the pair shape. Returns a list of errors."""
    errors = []
    for label, member in (("claude", pair.claude), ("grok", pair.grok)):
        result = manifest_module.validate(member, check_environment=True, env=env,
                                          check_branches=False)
        for item in result.errors:
            errors.append("%s member: %s" % (label, item))
    errors.extend(member_errors(pair))
    try:
        combine(pair)
    except PairError as exc:
        errors.append(str(exc))
    return errors


def split(manifest, out_dir=None):
    """Write sibling member files and a pair file next to the source, or in out_dir.

    Returns the loaded Pair. Comments in the source are not preserved: the members are
    emitted from the loaded data.
    """
    pair = from_manifest(manifest)
    out_dir = out_dir or os.path.dirname(os.path.abspath(manifest.path))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(manifest.path))[0]
    claude_name = stem + ".claude.toml"
    grok_name = stem + ".grok.toml"
    pair_name = stem + ".pair.toml"
    claude_path = os.path.join(out_dir, claude_name)
    grok_path = os.path.join(out_dir, grok_name)
    pair_path = os.path.join(out_dir, pair_name)
    _write_member(claude_path, manifest, "claude")
    _write_member(grok_path, manifest, "grok")
    with open(pair_path, "w", encoding="utf-8") as handle:
        handle.write("# Relay pair: claude and grok members of one task list.\n")
        handle.write("# Dispatch this file to run both backends at once. Merges follow order.\n\n")
        handle.write("[pair]\n")
        handle.write("claude = %s\n" % json.dumps(claude_name))
        handle.write("grok = %s\n" % json.dumps(grok_name))
        handle.write("order = [%s]\n" % ", ".join(json.dumps(task_id) for task_id in pair.order))
    return load(pair_path)


def _write_member(path, source, backend):
    raw = dict(source.raw)
    source_default = str((raw.get("defaults") or {}).get("backend") or manifest_module.DEFAULT_BACKEND)
    defaults = dict(raw.get("defaults") or {})
    defaults["backend"] = backend
    raw["defaults"] = defaults
    # A task that omitted backend inherits the source default. After the split, membership is
    # the resolved backend, so rewrite omitted keys to keep the member self contained.
    kept = []
    for entry in raw.get("tasks") or []:
        resolved = str(entry.get("backend") or source_default)
        if resolved != backend:
            continue
        kept.append(dict(entry, backend=backend))
    raw["tasks"] = kept
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# Relay pair member. Backend is %s. Do not add the other backend's tasks.\n"
                     % backend)
        handle.write("# Author the pair file and dispatch that, rather than running this alone,\n")
        handle.write("# when both backends should build at once.\n\n")
        handle.write(_emit_manifest_toml(raw))


def _emit_manifest_toml(raw):
    """A TOML dump of the tables a Manifest uses. Good enough to round trip split output."""
    lines = []
    table_order = (
        "project", "tracker", "shipping", "permissions", "timeouts", "closeout", "gate",
        "qualifying", "on_blocked", "on_halt", "defaults",
    )
    for name in table_order:
        table = raw.get(name)
        if not isinstance(table, dict):
            continue
        lines.append("[%s]" % name)
        for key, value in table.items():
            if value is None:
                continue
            lines.append("%s = %s" % (key, _toml_value(value)))
        lines.append("")
    for entry in raw.get("tasks") or []:
        lines.append("[[tasks]]")
        for key, value in entry.items():
            if value is None:
                continue
            lines.append("%s = %s" % (key, _toml_value(value)))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError("cannot emit %r as TOML" % (value,))
