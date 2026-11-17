"""Tools des agents dev/tester : shell et fichiers bornés au worktree."""

from __future__ import annotations

import asyncio
import os
import re
import shlex

async def _sh(cmd: str, cwd: str) -> dict:
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    return {"code": proc.returncode, "output": out.decode(errors="replace")[:8000]}


_FORBIDDEN = (
    re.compile(r"\bsudo\b"),
    re.compile(r"\bgit\s+push\b"),
    re.compile(r"\bworktree\s+(?:remove|prune)\b"),
)
_DESTRUCTIVE = re.compile(r"\b(?:rm|rmdir|unlink|xargs)\b")
_MOVES = {"mv", "cp", "ln"}
_REDIRECTS = {">", ">>", "2>", "2>>", "&>", "&>>"}


def _arg_outside_worktree(arg: str, root: str) -> bool:
    """True si `arg` vise le worktree lui-même ou hors du worktree."""
    a = arg.strip().strip("'\"")
    if not a or a.startswith("-"):
        return False
    if "$(" in a or "`" in a:
        return True
    a = a.replace("${PWD}", root).replace("$PWD", root)
    if a == "~" or a.startswith("~/"):
        a = os.path.expanduser(a)
    if not os.path.isabs(a):
        a = os.path.join(root, a)
    return not os.path.realpath(a).startswith(root + os.sep)


def _shell_violation(command: str, root: str) -> str | None:
    """Refuse les commandes destructrices hors worktree (la liste `rm -rf /` ne
    suffisait pas : `rm -rf ..`, `git worktree remove --force .`, `find .. -delete`
    supprimaient le worktree et faisaient échouer la gate run_tests)."""
    for rx in _FORBIDDEN:
        if rx.search(command):
            return "command not allowed"
    destructive = _DESTRUCTIVE.search(command) is not None
    for segment in re.split(r"[|;&]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return "command not allowed"
        if not tokens:
            continue
        prog = os.path.basename(tokens[0])
        check_targets = destructive or prog in _MOVES or (
            prog == "find" and re.search(r"-(?:delete|exec)\b", segment) is not None)
        for i, tok in enumerate(tokens):
            if i == 0:
                continue
            if tok in _REDIRECTS or (i >= 2 and tokens[i - 1] in _REDIRECTS):
                if _arg_outside_worktree(tok, root):
                    return "command not allowed (redirection hors worktree)"
                continue
            if check_targets and _arg_outside_worktree(tok, root):
                return f"command not allowed (cible hors worktree: {tok})"
    return None


def build_dev_tools(worktree: str) -> dict:
    """(schéma, implémentation) pour run_agent — tout est confiné au worktree."""

    def safe_path(rel: str) -> str:
        full = os.path.realpath(os.path.join(worktree, rel))
        root = os.path.realpath(worktree)
        if full != root and not full.startswith(root + os.sep):
            raise PermissionError(f"path escapes worktree: {rel}")
        return full

    async def shell(command: str) -> dict:
        violation = _shell_violation(command, os.path.realpath(worktree))
        if violation:
            return {"error": violation}
        return await _sh(command, worktree)

    async def read(path: str) -> dict:
        with open(safe_path(path), encoding="utf-8") as fh:
            return {"content": fh.read()[:20000]}

    async def write(path: str, content: str) -> dict:
        full = safe_path(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        return {"written": path}

    return {
        "shell": ({"description": "Run a shell command inside your worktree.",
                   "parameters": {"type": "object",
                                  "properties": {"command": {"type": "string"}},
                                  "required": ["command"]}},
                  shell),
        "read_file": ({"description": "Read a file from your worktree.",
                       "parameters": {"type": "object",
                                      "properties": {"path": {"type": "string"}},
                                      "required": ["path"]}},
                      read),
        "write_file": ({"description": "Create or overwrite a file in your worktree.",
                        "parameters": {"type": "object",
                                       "properties": {"path": {"type": "string"},
                                                      "content": {"type": "string"}},
                                       "required": ["path", "content"]}},
                       write),
    }
