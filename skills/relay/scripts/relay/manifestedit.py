"""Manifest edits made by code (feeder plan, KTD2).

Until the feeder, a manifest was written by a person or by the `/relay` skill and only ever read
by the runner. The feeder changes that: between runs it appends `[[tasks]]` blocks and marks a
task excluded. This module is the whole of that write path, kept apart from the loop so every
edit can be tested as text in, text out.

The standard library reads TOML (`tomllib`) and cannot write it, so an edit is a line edit that
leaves every comment and every hand written line exactly where the operator put it. A line edit
is only as good as its idea of where a block ends, so nothing here trusts one. Each edit is
proven three ways before it reaches the real file:

1. `check_append` or `check_exclusion` parses the text before and after with `tomllib` and
   refuses any edit whose parsed result differs from the one intended. The line editor may be
   wrong about an unusual file; the parser is not.
2. `commit` writes the candidate to a temporary file beside the manifest and runs the manifest
   module's own `load` and `validate` on it. That is what gives model routing the backend
   coherence check for free.
3. Only then is the temporary file renamed over the manifest. A rename within one directory is
   atomic (it either fully happens or does not happen), so a runner or a reader never sees a
   half written manifest, and an edit that fails validation is rolled back by never having
   been made.
"""
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass

from . import manifest as manifest_module

# A table header on its own line: `[tracker]` or `[[tasks]]`, bare keys only, which is every
# table a Relay manifest has. A line of a multi line array such as `"src/",` never matches.
HEADER_RE = re.compile(r"^\s*\[\[?\s*[A-Za-z0-9_.\-]+\s*\]\]?\s*(#.*)?$")
TASKS_HEADER_RE = re.compile(r"^\s*\[\[\s*tasks\s*\]\]\s*(#.*)?$")
TITLE_CHARS = 110
REASON_CHARS = 200


class EditError(ValueError):
    """The edit was not made. The manifest on disk is unchanged."""


@dataclass(frozen=True)
class Block:
    """One `[[tasks]]` block: its parsed keys, the index of its header line, and the index one
    past its last key line, which is where a new key is inserted."""
    keys: dict
    start: int
    insert_at: int

    @property
    def id(self):
        return str(self.keys.get("id", ""))


def _one_line(value):
    """Tabs and line breaks become spaces, and every other control character is dropped."""
    return "".join(" " if char in "\n\r\t" else char for char in str(value)
                   if char in "\n\r\t" or (ord(char) >= 0x20 and ord(char) != 0x7F))


def toml_string(value):
    """One TOML basic string literal. Card ids and model names are plain, but a halt cause line
    can carry anything, so it is flattened to one line and its backslashes and quotes escaped."""
    return '"%s"' % _one_line(value).replace("\\", "\\\\").replace('"', '\\"')


def comment_text(value, limit=TITLE_CHARS):
    """A card title made safe for a `#` comment line: one line, no control characters."""
    return _one_line(value).strip()[:limit]


def task_blocks(text):
    """Every `[[tasks]]` block in file order. A block runs from its header to the next table
    header. Its keys come from parsing the block's own lines, so an id is found whatever quoting
    or key order the operator used; a block that does not parse alone gets empty keys and is
    never chosen for an edit."""
    lines = text.split("\n")
    blocks = []
    index = 0
    while index < len(lines):
        if not TASKS_HEADER_RE.match(lines[index]):
            index += 1
            continue
        end = index + 1
        while end < len(lines) and not HEADER_RE.match(lines[end]):
            end += 1
        insert_at = index + 1
        for position in range(index + 1, end):
            stripped = lines[position].strip()
            if stripped and not stripped.startswith("#"):
                insert_at = position + 1
        try:
            keys = tomllib.loads("\n".join(lines[index + 1:end]))
        except tomllib.TOMLDecodeError:
            keys = {}
        blocks.append(Block(keys=keys, start=index, insert_at=insert_at))
        index = end
    return blocks


def task_ids(text):
    """Task ids in manifest order, read with the real parser rather than from the blocks."""
    return [str(entry.get("id", "")) for entry in _parse(text).get("tasks", [])
            if isinstance(entry, dict)]


def excluded_ids(text):
    return {str(entry.get("id", "")) for entry in _parse(text).get("tasks", [])
            if isinstance(entry, dict) and entry.get("excluded") is True}


def _parse(text):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise EditError("the manifest is not valid TOML: %s" % exc)


def append_tasks(text, entries, stamp):
    """The text with one `[[tasks]]` block per entry added at the end. An entry is a dict with
    `id`, `model`, `effort`, and an optional `title` that becomes the comment above the block.
    `stamp` is the time written into that comment, passed in so the edit is deterministic."""
    known = set(task_ids(text))
    out = text if text.endswith("\n") or not text else text + "\n"
    for entry in entries:
        if str(entry["id"]) in known:
            raise EditError("task %s is already in the manifest" % entry["id"])
        known.add(str(entry["id"]))
        out += "\n# appended by the feeder %s: %s\n[[tasks]]\nid = %s\nmodel = %s\neffort = %s\n" % (
            stamp, comment_text(entry.get("title", "")), toml_string(entry["id"]),
            toml_string(entry["model"]), toml_string(entry["effort"]))
    check_append(text, out, entries)
    return out


def check_append(before, after, entries):
    """Refuse an append whose parsed result is anything but the old manifest plus these tasks."""
    old, new = _parse(before), _parse(after)
    expected = list(old.get("tasks", [])) + [
        {"id": str(entry["id"]), "model": str(entry["model"]), "effort": str(entry["effort"])}
        for entry in entries]
    if new.get("tasks") != expected or _without_tasks(old) != _without_tasks(new):
        raise EditError("the append did not parse as the old manifest plus the new tasks")


def exclude_task(text, task_id, reason):
    """The text with `excluded = true` and a `reason` written into one task's block, or None
    when that task is already excluded. `reason` is a field the manifest also uses for a task
    whose backend differs from the default, so an existing one is kept inside the new sentence
    rather than overwritten or duplicated, which TOML would refuse."""
    task_id = str(task_id)
    if task_id in excluded_ids(text):
        return None
    block = next((found for found in task_blocks(text) if found.id == task_id), None)
    if block is None:
        raise EditError("no [[tasks]] block with id %s could be found to exclude" % task_id)
    lines = text.split("\n")
    body = range(block.start + 1, block.insert_at)
    sentence = _flat(reason)
    if block.keys.get("reason"):
        sentence = "%s; the reason it carried before: %s" % (sentence, _flat(block.keys["reason"]))
    sentence = sentence[:REASON_CHARS]
    drop = [position for position in body
            if re.match(r"^\s*(excluded|reason)\s*=", lines[position])]
    for position in drop:
        try:
            tomllib.loads(lines[position])
        except tomllib.TOMLDecodeError:
            raise EditError("task %s has a multi line %s the feeder will not rewrite"
                            % (task_id, lines[position].split("=")[0].strip()))
    kept = [line for position, line in enumerate(lines) if position not in drop]
    insert_at = block.insert_at - len(drop)
    kept[insert_at:insert_at] = ["excluded = true", "reason = %s" % toml_string(sentence)]
    out = "\n".join(kept)
    check_exclusion(text, out, task_id, sentence)
    return out


def check_exclusion(before, after, task_id, sentence):
    """Refuse an exclusion that changed anything but that one task's two keys."""
    old, new = _parse(before), _parse(after)
    expected = []
    for entry in old.get("tasks", []):
        if str(entry.get("id", "")) == task_id:
            entry = dict(entry, excluded=True, reason=sentence)
        expected.append(entry)
    if new.get("tasks") != expected or _without_tasks(old) != _without_tasks(new):
        raise EditError("the exclusion of %s did not parse as that one change" % task_id)


def _flat(value):
    return " ".join(_one_line(value).split())


def _without_tasks(raw):
    return {key: value for key, value in raw.items() if key != "tasks"}


def write_atomic(path, text):
    """Replace `path` with `text` in one step: write a temporary file in the same directory,
    flush it to disk, then rename it over the target. The same directory matters, because a
    rename is only atomic within one filesystem."""
    tmp = _write_candidate(path, text)
    os.replace(tmp, path)


def _write_candidate(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".%s." % os.path.basename(path), suffix=".tmp",
                               dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
    except BaseException:
        _discard(tmp)
        raise
    return tmp


def _discard(tmp):
    try:
        os.unlink(tmp)
    except OSError:
        pass


def commit(path, before, after, env=None, validate=None):
    """Make `after` the manifest, or raise EditError and leave the file as it was.

    `before` is the text the edit was computed from. If the file no longer reads that way,
    somebody edited it in the moment between, and their edit wins: the feeder computes again
    next cycle rather than overwrite it.

    `validate` is injectable for tests; the default is the manifest module's own rules with the
    repository checks on and the environment checks off, since the run that follows makes
    those itself.
    """
    if validate is None:
        def validate(candidate):
            return manifest_module.validate(candidate, check_repo=True, env=env,
                                            check_branches=False).errors
    tmp = _write_candidate(path, after)
    try:
        try:
            errors = validate(manifest_module.load(tmp))
        except manifest_module.ManifestError as exc:
            errors = [str(exc)]
        if errors:
            raise EditError("the edited manifest does not validate: %s" % "; ".join(errors))
        with open(path, encoding="utf-8") as handle:
            if handle.read() != before:
                raise EditError("the manifest changed on disk while the edit was being made")
        os.replace(tmp, path)
    except BaseException:
        _discard(tmp)
        raise
