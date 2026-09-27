"""Host conditions (issue #32): a snapshot of how loaded the machine was, taken at Task start and end.

A record's `wall_seconds` and `active_seconds` say how long a task took and nothing about what it
took that long under. The two clocks differ only when the host sleeps, and under `caffeinate` they
never do, so when pace changes between runs neither can tell a slow card from a slow host. Five
numbers twice per task can.

`load_1m` is the one minute load average. `free_bytes` is memory nothing holds, and
`inactive_bytes` is memory the kernel can reclaim without swapping; together they are the headroom.
`swapins` and `swapouts` are the kernel's page counters since boot, so a single value means nothing:
the end snapshot minus the start one is the swap activity during the task.

`snapshot()` never raises. It runs inside `launch()` ahead of the process start, and a field it
cannot read on this platform is None rather than a reason to skip a launch.
"""
import os
import re
import subprocess
import sys

VM_STAT = "/usr/bin/vm_stat"
VM_STAT_TIMEOUT_SECONDS = 5
FIELDS = ("load_1m", "free_bytes", "inactive_bytes", "swapins", "swapouts")


def empty():
    return {field: None for field in FIELDS}


def parse_vm_stat(text):
    """macOS `vm_stat` output to the memory and swap fields. Pages are scaled by the page size
    the header names; the counters stay in pages, since only their difference is read."""
    out = {}
    page = re.search(r"page size of (\d+) bytes", text or "")
    size = int(page.group(1)) if page else None
    rows = {}
    for line in (text or "").splitlines():
        match = re.match(r'^"?([^":]+)"?:\s+(\d+)\.?\s*$', line.strip())
        if match:
            rows[match.group(1).strip()] = int(match.group(2))
    if size is not None:
        if "Pages free" in rows:
            out["free_bytes"] = rows["Pages free"] * size
        if "Pages inactive" in rows:
            out["inactive_bytes"] = rows["Pages inactive"] * size
    if "Swapins" in rows:
        out["swapins"] = rows["Swapins"]
    if "Swapouts" in rows:
        out["swapouts"] = rows["Swapouts"]
    return out


def parse_linux(meminfo, vmstat):
    """Linux `/proc/meminfo` (kB) and `/proc/vmstat` (pages) to the same fields."""
    out = {}
    for line in (meminfo or "").splitlines():
        match = re.match(r"^(MemFree|Inactive):\s+(\d+)\s*kB", line)
        if match:
            key = "free_bytes" if match.group(1) == "MemFree" else "inactive_bytes"
            out[key] = int(match.group(2)) * 1024
    for line in (vmstat or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("pswpin", "pswpout") and parts[1].isdigit():
            out["swapins" if parts[0] == "pswpin" else "swapouts"] = int(parts[1])
    return out


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def snapshot(platform=None, run=subprocess.run, read=_read, loadavg=os.getloadavg):
    """The five fields for this host now. Any one that cannot be read is None."""
    result = empty()
    try:
        result["load_1m"] = round(loadavg()[0], 2)
    except (OSError, AttributeError, IndexError, TypeError):
        pass
    platform = platform or sys.platform
    try:
        if platform == "darwin":
            proc = run([VM_STAT], capture_output=True, text=True,
                       timeout=VM_STAT_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
            if proc.returncode == 0:
                result.update(parse_vm_stat(proc.stdout))
        elif platform.startswith("linux"):
            result.update(parse_linux(read("/proc/meminfo"), read("/proc/vmstat")))
    except Exception:
        # A missing binary, a timeout, an unreadable /proc, or output this parser has never seen.
        # The load average already read stays; the rest is None.
        pass
    return result


def _gib(value):
    return "?" if value is None else "%.1f GiB" % (value / float(1 << 30))


def _load(value):
    return "?" if value is None else "%.2f" % value


def line(start, end):
    """One sentence for the summary's text form, or None when the record carries neither
    snapshot (a task that never launched, or a record written before the fields existed)."""
    if not start and not end:
        return None
    start = start or empty()
    end = end or empty()
    text = "host: load %s to %s, free %s to %s, inactive %s to %s" % (
        _load(start.get("load_1m")), _load(end.get("load_1m")),
        _gib(start.get("free_bytes")), _gib(end.get("free_bytes")),
        _gib(start.get("inactive_bytes")), _gib(end.get("inactive_bytes")))
    deltas = []
    for field in ("swapins", "swapouts"):
        before, after = start.get(field), end.get(field)
        if before is None or after is None:
            deltas.append("? %s" % field)
        else:
            deltas.append("%d %s" % (after - before, field))
    return text + ", " + " and ".join(deltas) + " during the task"
