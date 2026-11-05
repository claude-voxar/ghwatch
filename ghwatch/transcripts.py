"""Find ghwatch problems in other Claude Code sessions' transcripts.

Claude Code writes each session as JSONL under ~/.claude/projects. The
scanner follows those files incrementally. It pairs each Bash/Monitor
`tool_use` whose command mentions ghwatch with its `tool_result`, and
reports results that look like failures. It also reports GHWATCH-STALE
lines and ghwatch tracebacks that reach a session as Monitor notifications.

State (byte offsets, unmatched tool calls, what was already reported) lives
in $GHWATCH_HOME/transcript-scan.json, so restarts neither miss nor repeat.
"""
import glob
import json
import os
import re

DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")
TOOLS = {"Bash", "Monitor"}
MAX_PENDING = 2000
MAX_REPORTED = 5000

# Output of a ghwatch command that indicates something went wrong.
FAILURE = re.compile(
    r"Traceback \(most recent call last\)|GHWATCH-STALE|^\S*ghwatch[^:\n]*: error:|"
    r"\b\w*Error\b|No such file|Permission denied|database is locked|command not found|"
    r"not an owner/name|unknown filter|did not stop|could not |Exit code [1-9]",
    re.MULTILINE)
# Text arriving in a session outside a tool result (e.g. Monitor events).
NOTICE = re.compile(r"GHWATCH-STALE[^\n\"]*")


class Problem:
    def __init__(self, session, cwd, timestamp, command, detail, where):
        self.session = session
        self.cwd = cwd
        self.timestamp = timestamp
        self.command = command
        self.detail = detail
        self.where = where

    def line(self):
        cmd = " ".join((self.command or "").split())[:160]
        detail = " ".join(self.detail.split())[:240]
        return (f"GHWATCH-ISSUE session={self.session[:8]} cwd={self.cwd} at={self.timestamp} "
                f"cmd=\"{cmd}\" problem=\"{detail}\" transcript={self.where}")


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text(c.get("text") if isinstance(c, dict) and "text" in c else
                               c.get("content") if isinstance(c, dict) else c)
                         for c in content if c is not None)
    return "" if content is None else str(content)


def failure_detail(output, is_error):
    """The first line explaining a failure, or None if the output looks fine."""
    m = FAILURE.search(output)
    if m:
        start = output.rfind("\n", 0, m.start()) + 1
        end = output.find("\n", m.end())
        line = output[start:end if end != -1 else None].strip()
        if line.startswith("Traceback"):
            # The exception message is on the last non-empty line.
            tail = [ln for ln in output[m.start():].splitlines() if ln.strip()]
            line = tail[-1].strip() if tail else line
        return line
    if is_error:
        return (output.strip().splitlines() or ["tool reported an error"])[0]
    return None


class Scanner:
    def __init__(self, root=DEFAULT_ROOT, state_path=None, exclude_sessions=()):
        self.root = root
        self.state_path = state_path
        self.exclude = {s for s in exclude_sessions if s}
        self.state = {"offsets": {}, "lines": {}, "pending": {}, "reported": [], "initialized": False}
        if state_path and os.path.exists(state_path):
            with open(state_path) as fh:
                self.state.update(json.load(fh))
        self._reported = set(self.state["reported"])

    def save(self):
        if not self.state_path:
            return
        self.state["reported"] = self.state["reported"][-MAX_REPORTED:]
        pending = self.state["pending"]
        if len(pending) > MAX_PENDING:
            for key in list(pending)[:len(pending) - MAX_PENDING]:
                del pending[key]
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.state, fh)
        os.replace(tmp, self.state_path)

    def files(self):
        return sorted(glob.glob(os.path.join(self.root, "**", "*.jsonl"), recursive=True))

    def skip_history(self):
        """Start every existing transcript at its current end (first run only)."""
        for path in self.files():
            self.state["offsets"][path] = os.path.getsize(path)
            self.state["lines"][path] = 0
        self.state["initialized"] = True

    def scan(self):
        """Read new transcript lines everywhere. Returns new Problems."""
        if not self.state["initialized"]:
            self.state["initialized"] = True  # files seen from now on are read from the start
        problems = []
        for path in self.files():
            problems.extend(self._scan_file(path))
        return problems

    def _scan_file(self, path):
        offset = self.state["offsets"].get(path, 0)
        try:
            size = os.path.getsize(path)
        except OSError:
            return []
        if size < offset:  # truncated or replaced: start over
            offset, self.state["lines"][path] = 0, 0
        if size == offset:
            return []
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
        end = chunk.rfind(b"\n")
        if end == -1:
            return []  # no complete line yet
        complete = chunk[:end + 1]
        self.state["offsets"][path] = offset + len(complete)
        lineno = self.state["lines"].get(path, 0)
        problems = []
        for raw in complete.splitlines():
            lineno += 1
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            problems.extend(self._entry(entry, f"{path}:{lineno}"))
        self.state["lines"][path] = lineno
        return problems

    def _entry(self, entry, where):
        kind = entry.get("type")
        session = entry.get("sessionId") or ""
        if kind not in ("assistant", "user") or session in self.exclude:
            return []
        content = (entry.get("message") or {}).get("content")
        meta = (session, entry.get("cwd") or "?", entry.get("timestamp") or "?")
        out = []
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if kind == "assistant" and item.get("type") == "tool_use":
                    self._remember(item)
                elif kind == "user" and item.get("type") == "tool_result":
                    out.extend(self._result(item, meta, where))
        if kind == "user":
            out.extend(self._notices(content, meta, where, entry.get("uuid") or where))
        return out

    def _remember(self, item):
        if item.get("name") not in TOOLS:
            return
        command = (item.get("input") or {}).get("command") or ""
        if "ghwatch" in command:
            self.state["pending"][item.get("id")] = command

    def _result(self, item, meta, where):
        command = self.state["pending"].pop(item.get("tool_use_id"), None)
        if command is None:
            return []
        detail = failure_detail(_text(item.get("content")), bool(item.get("is_error")))
        if detail is None:
            return []
        return self._report(f"{meta[0]}:{item.get('tool_use_id')}", Problem(*meta, command, detail, where))

    def _notices(self, content, meta, where, entry_id):
        if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content or []):
            return []  # tool results are handled with their command above
        text = _text(content)
        out = []
        for m in NOTICE.finditer(text):
            out.extend(self._report(f"{meta[0]}:{entry_id}:{m.group(0)}",
                                    Problem(*meta, "(monitor event)", m.group(0), where)))
        if "ghwatch" in text and "Traceback (most recent call last)" in text:
            out.extend(self._report(f"{meta[0]}:{entry_id}:traceback",
                                    Problem(*meta, "(monitor event)", failure_detail(text, True), where)))
        return out

    def _report(self, key, problem):
        if key in self._reported:
            return []
        self._reported.add(key)
        self.state["reported"].append(key)
        return [problem]
