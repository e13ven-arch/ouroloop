"""The four tools of the minimal agent: read, write, edit and bash. File tools stay inside the working directory."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..chat import ToolSpec


@dataclass
class ToolResult:
    output: str
    is_error: bool = False
    meta: dict = field(default_factory=dict)   # e.g. exit_code / stdout / stderr for bash, path for file tools


class ToolError(Exception):
    pass


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n[... {len(text) - limit} characters omitted ...]\n{text[-half:]}"


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


SPECS = [
    ToolSpec("read", "Read a text file from the working directory. Returns numbered lines.",
             _schema({"path": {"type": "string"},
                      "offset": {"type": "integer", "description": "first line to return, 1-based"},
                      "limit": {"type": "integer", "description": "maximum number of lines"}}, ["path"])),
    ToolSpec("write", "Create or overwrite a file in the working directory.",
             _schema({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"])),
    ToolSpec("edit", "Replace one exact occurrence of old_text with new_text in a file. old_text must occur once.",
             _schema({"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
                     ["path", "old_text", "new_text"])),
    ToolSpec("bash", "Run a shell command in the working directory. Returns its output and exit code.",
             _schema({"command": {"type": "string"},
                      "timeout": {"type": "integer", "description": "seconds, default 120"}}, ["command"])),
]


def _text(data) -> str:
    return data.decode("utf-8", "replace") if isinstance(data, bytes) else (data or "")


class Tools:
    def __init__(self, workdir: str | Path, max_output_chars: int = 20000, bash_timeout: int = 120,
                 descriptions: dict[str, str] | None = None):
        self.workdir = Path(workdir).resolve()
        self.max_output_chars = max_output_chars
        self.bash_timeout = bash_timeout
        self.descriptions = descriptions or {}      # tool name -> description, from the harness prompt

    def specs(self) -> list[ToolSpec]:
        return [replace(s, description=self.descriptions[s.name]) if s.name in self.descriptions else s
                for s in SPECS]

    def resolve(self, path: str) -> Path:
        p = (self.workdir / path).resolve()
        if p != self.workdir and self.workdir not in p.parents:
            raise ToolError(f"{path} is outside the working directory")
        return p

    def run(self, name: str, args: dict) -> ToolResult:
        if name not in {s.name for s in SPECS}:
            return ToolResult(f"unknown tool {name!r}", True)
        try:
            return getattr(self, f"_{name}")(**args)
        except ToolError as e:
            return ToolResult(str(e), True)
        except TypeError as e:
            return ToolResult(f"bad arguments for {name}: {e}", True)
        except (OSError, UnicodeError) as e:
            return ToolResult(f"{type(e).__name__}: {e}", True)

    def _read(self, path: str, offset: int = 1, limit: int = 2000) -> ToolResult:
        p = self.resolve(path)
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(offset))
        chunk = lines[start - 1:start - 1 + int(limit)]
        text = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(chunk, start)) or "(empty file)"
        return ToolResult(clip(text, self.max_output_chars), meta={"path": str(p)})

    def _write(self, path: str, content: str) -> ToolResult:
        p = self.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResult(f"wrote {len(content)} characters to {path}", meta={"path": str(p)})

    def _edit(self, path: str, old_text: str, new_text: str) -> ToolResult:
        p = self.resolve(path)
        text = p.read_text(encoding="utf-8")
        n = text.count(old_text)
        if n != 1:
            raise ToolError(f"old_text occurs {n} times in {path}; it must occur exactly once")
        p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return ToolResult(f"edited {path}", meta={"path": str(p)})

    def _bash(self, command: str, timeout: int | None = None) -> ToolResult:
        limit = timeout or self.bash_timeout
        try:
            proc = subprocess.run(["bash", "-c", command], cwd=self.workdir, capture_output=True, text=True,
                                  timeout=limit)
        except subprocess.TimeoutExpired as e:
            out = _text(e.stdout) + _text(e.stderr)
            return ToolResult(clip(f"{out}\n[timed out after {limit}s]", self.max_output_chars), True,
                              {"command": command, "exit_code": 124, "stdout": _text(e.stdout), "stderr": "timed out"})
        parts = [s.rstrip() for s in (proc.stdout, proc.stderr) if s] + [f"[exit code {proc.returncode}]"]
        return ToolResult(clip("\n".join(parts), self.max_output_chars), proc.returncode != 0,
                          {"command": command, "exit_code": proc.returncode, "stdout": proc.stdout,
                           "stderr": proc.stderr})
