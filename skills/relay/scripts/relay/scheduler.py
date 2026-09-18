"""Fail-closed pre-launch scheduling for same-backend Relay runs.

This module decides only *when* otherwise valid tasks may build.  It does not launch a
process, mutate git, or write a tracker.  A missing or unclear signal becomes a conflict edge;
the caller can therefore render the whole schedule before the first worker exists.

`parallel` is deliberately not an assertion that all tasks may run together.  It permits a
task pair into the same wave only when both tasks declare non-empty, valid, disjoint paths and
none of the conservative repository or card-text signals says that they share a concern.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


POLICIES = ("serial", "parallel")


@dataclass(frozen=True)
class DeclaredPath:
    """One canonical repository-relative path bound.

    ``directory`` deliberately comes from the manifest spelling (a trailing slash).  A path for
    a not-yet-created file cannot be stat'ed safely to infer whether it was intended as a broad
    directory declaration.
    """

    value: str
    directory: bool


@dataclass(frozen=True)
class ConflictEdge:
    """A pair Relay must serialize, named in manifest order."""

    first: str
    second: str
    reason: str


@dataclass(frozen=True)
class Schedule:
    """A complete pre-launch result.  Waves and task ids preserve manifest order."""

    policy: str
    task_ids: tuple[str, ...]
    waves: tuple[tuple[str, ...], ...]
    edges: tuple[ConflictEdge, ...]


# These are deliberately a small allow-nothing list, rather than a claim to recognize every
# build system.  An unrecognized path can become parallel only when it has an explicit narrow
# declaration; recognised repository-wide surfaces always serialize.
_EXACT_GLOBAL = frozenset({
    "readme", "readme.md", "readme.rst", "concepts.md", "changelog", "changelog.md",
    "license", "license.md", "makefile", "dockerfile", "compose.yml", "compose.yaml",
    "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "noxfile.py", "manage.py",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "go.mod", "go.sum", "cargo.toml", "cargo.lock", "gemfile", "gemfile.lock",
    "composer.json", "composer.lock", "podfile", "podfile.lock", "requirements.txt",
    "requirements-dev.txt", "poetry.lock", "pdm.lock", "uv.lock", "pipfile", "pipfile.lock",
    "terraform.lock.hcl", "flake.lock",
})
_GLOBAL_PREFIXES = (
    ".github/", ".gitlab/", ".circleci/", ".buildkite/", ".azure/", ".ci/", "ci/",
    "migrations/", "migration/", "db/migrations/", "schema/", "schemas/", "database/",
    "generated/", "gen/", "vendor/", "dist/", "build/", "coverage/", "docs/", "tests/",
)
_GLOBAL_SUFFIXES = (".lock", ".lockb", ".min.js", ".map", ".generated.py", ".gen.go")
_TEXT_GLOBAL = re.compile(
    r"\b(?:shared\s+(?:api|interface|contract|config)|public\s+api|"
    r"(?:database|schema)\s+migration|dependency\s+(?:upgrade|update)|"
    r"(?:release|version)\s+(?:bump|change|update)|rename\s+(?:the\s+)?(?:module|package|api)|"
    r"generated\s+(?:code|output)|ci\s+(?:pipeline|workflow)|build\s+(?:system|config))\b",
    re.IGNORECASE,
)
# A path reference outside the declared scope is an uncertainty signal, not a request to expand
# scope based on natural language.  Keep this deliberately file/path-shaped to avoid treating
# ordinary prose such as "API/v2" as a repository path.
_TEXT_PATH = re.compile(r"(?<![\w.-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+(?:\.[A-Za-z0-9_.-]+)?)(?![\w.-])")


def _task_id(task):
    return str(getattr(task, "id", ""))


def normalize_declared_paths(paths, repo_root):
    """Return ``(paths, reason)`` without accepting an ambiguous scope.

    ``repo_root`` is checked so an existing symlink cannot make an apparently narrow path point
    outside the frozen checkout.  Nonexistent paths are valid: a task may add a new exact file.
    """
    if not isinstance(paths, (list, tuple)) or not paths:
        return (), "declared_paths_missing"
    root = os.path.realpath(os.path.abspath(repo_root))
    found = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            return (), "declared_path_invalid"
        original = raw.strip().replace("\\", "/")
        directory = original.endswith("/")
        if os.path.isabs(original):
            return (), "declared_path_invalid"
        normalized = os.path.normpath(original).replace("\\", "/")
        if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
            return (), "declared_path_invalid"
        absolute = os.path.join(root, normalized)
        if os.path.lexists(absolute) and os.path.realpath(absolute) != absolute:
            resolved = os.path.realpath(absolute)
            if os.path.commonpath((root, resolved)) != root:
                return (), "declared_path_symlink_ambiguous"
        entry = DeclaredPath(normalized.rstrip("/"), directory)
        if entry not in found:
            found.append(entry)
    return tuple(found), None


def _is_global(path):
    value = path.value.lower()
    if value in _EXACT_GLOBAL or value.endswith(_GLOBAL_SUFFIXES):
        return True
    if value.startswith(_GLOBAL_PREFIXES):
        return True
    # A test file outside a conventional tests directory still changes a shared verification
    # surface.  This intentionally sacrifices concurrency for an auditable safety rule.
    base = value.rsplit("/", 1)[-1]
    return base.startswith("test_") or base.endswith("_test.py")


def _contains(container, child):
    if container.value == child.value:
        return True
    return container.directory and child.value.startswith(container.value + "/")


def _overlap(left, right):
    return any(_contains(a, b) or _contains(b, a) for a in left for b in right)


def _text_reason(text, task_id, other_ids, declared):
    """Return one conservative task-local reason, or a direct referenced task id reason."""
    if not isinstance(text, str):
        return "task_text_unavailable"
    if _TEXT_GLOBAL.search(text):
        return "task_text_global_signal"
    lower = text.lower()
    for other in other_ids:
        if other != task_id and re.search(r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])" % re.escape(other), text):
            if re.search(r"\b(?:depend(?:s|ency)?|after|before|prerequisite|blocked\s+by|follow[- ]?up)\b.{0,80}%s|%s.{0,80}\b(?:depend(?:s|ency)?|after|before|prerequisite|blocked\s+by|follow[- ]?up)\b" % (re.escape(other), re.escape(other)), text, re.IGNORECASE | re.DOTALL):
                return "task_text_dependency:%s" % other
    for token in _TEXT_PATH.findall(text):
        normalized, reason = normalize_declared_paths((token,), ".")
        if reason:
            continue
        candidate = normalized[0]
        if not any(_contains(scope, candidate) for scope in declared):
            return "task_text_undeclared_path"
    return None


def _repository_reference_reason(repo_root, left, right):
    """Read only exact declared source files and add an edge for an explicit path reference.

    This is intentionally not an import resolver.  A language-specific resolver that fails or
    guesses would be a false proof of independence; literal references are safe evidence only
    for serialization.
    """
    root = os.path.abspath(repo_root)
    left_files = [entry for entry in left if not entry.directory]
    right_files = [entry for entry in right if not entry.directory]
    for source_set, target_set in ((left_files, right_files), (right_files, left_files)):
        for source in source_set:
            filename = os.path.join(root, source.value)
            if not os.path.isfile(filename):
                continue
            try:
                if os.path.getsize(filename) > 256 * 1024:
                    return "repository_file_too_large"
                with open(filename, "r", encoding="utf-8") as handle:
                    content = handle.read()
            except (OSError, UnicodeError):
                return "repository_dependency_unknown"
            for target in target_set:
                if target.value in content:
                    return "repository_path_reference"
    return None


def _add_edge(edges, first, second, reason, index):
    if first == second:
        return
    if index[first] > index[second]:
        first, second = second, first
    key = (first, second)
    if key not in edges:
        edges[key] = reason


def _pair_text_reason(reason, other_id):
    """A named card dependency is a pair fact; other text uncertainty is task-wide."""
    if reason and reason.startswith("task_text_dependency:"):
        return reason if reason.split(":", 1)[1] == other_id else None
    return reason


def build_schedule(tasks, repo_root, task_text, policy="serial", semantic_edges=()):
    """Compute a deterministic schedule without side effects.

    ``task_text`` maps task id to the pre-launch title/description/comments text.  Semantic
    analysis is optional and deliberately asymmetric: ``semantic_edges`` may add ``(id, id,
    reason)`` edges but can never remove one chosen by deterministic checks.
    """
    if policy not in POLICIES:
        raise ValueError("policy must be one of %s" % ", ".join(POLICIES))
    task_ids = tuple(_task_id(task) for task in tasks)
    if not all(task_ids) or len(set(task_ids)) != len(task_ids):
        raise ValueError("tasks must have unique non-empty ids")
    index = {task_id: position for position, task_id in enumerate(task_ids)}
    normalized, local_reasons = {}, {}
    text_reasons = {}
    for task in tasks:
        task_id = _task_id(task)
        paths, reason = normalize_declared_paths(getattr(task, "declared_paths", ()), repo_root)
        normalized[task_id] = paths
        if reason:
            local_reasons[task_id] = reason
        elif any(_is_global(path) for path in paths):
            local_reasons[task_id] = "declared_path_global"
        text_reason = _text_reason((task_text or {}).get(task_id) if isinstance(task_text, dict) else None,
                                   task_id, task_ids, paths)
        if text_reason:
            text_reasons[task_id] = text_reason

    edges = {}
    for position, first in enumerate(task_ids):
        for second in task_ids[position + 1:]:
            if policy == "serial":
                _add_edge(edges, first, second, "policy_serial", index)
                continue
            first_reason = local_reasons.get(first) or _pair_text_reason(
                text_reasons.get(first), second)
            second_reason = local_reasons.get(second) or _pair_text_reason(
                text_reasons.get(second), first)
            if first_reason:
                _add_edge(edges, first, second, first_reason, index)
            elif second_reason:
                _add_edge(edges, first, second, second_reason, index)
            elif _overlap(normalized[first], normalized[second]):
                _add_edge(edges, first, second, "declared_paths_overlap", index)
            else:
                repository_reason = _repository_reference_reason(
                    repo_root, normalized[first], normalized[second])
                if repository_reason:
                    _add_edge(edges, first, second, repository_reason, index)

    for raw in semantic_edges or ():
        try:
            first, second, reason = raw
        except (TypeError, ValueError):
            continue
        if first in index and second in index and first != second:
            _add_edge(edges, first, second, "semantic:%s" % (str(reason) or "conflict"), index)

    ordered_edges = tuple(ConflictEdge(first, second, reason)
                          for (first, second), reason in edges.items())
    waves = []
    for task_id in task_ids:
        for wave in waves:
            if all((min(task_id, member, key=index.get), max(task_id, member, key=index.get)) not in edges
                   for member in wave):
                wave.append(task_id)
                break
        else:
            waves.append([task_id])
    return Schedule(policy, task_ids, tuple(tuple(wave) for wave in waves), ordered_edges)


def render(schedule):
    """Render a compact, stable pre-launch explanation for logs and operator review."""
    lines = ["run policy: %s" % schedule.policy]
    for number, wave in enumerate(schedule.waves, 1):
        lines.append("wave %d: %s" % (number, ", ".join(wave)))
    if schedule.edges:
        lines.append("serialized edges:")
        lines.extend("- %s -> %s: %s" % (edge.first, edge.second, edge.reason)
                     for edge in schedule.edges)
    else:
        lines.append("serialized edges: none")
    return "\n".join(lines)
