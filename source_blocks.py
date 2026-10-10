"""Source-change protocol for model output: FILE / EDIT / DELETE blocks.

JSON-escaped whole files (925.V1) roughly double the completion tokens of code and a
single cut-off made the whole response unusable. Blocks are cheaper, and when a reply
is truncated every block that was closed before the cut is still usable.
"""
from __future__ import annotations
import json
import re
from pathlib import Path, PurePosixPath

from model_transport import clean_text

SOURCE_ROOTS = ("frontend/src/", "backend/src/")
SUFFIXES = {".tsx", ".ts", ".js", ".jsx", ".mjs", ".cjs", ".css", ".json"}
# Host-owned files: the fixed backend host and the React entry that mounts BrowserRouter.
PROTECTED = {"backend/src/index.js", "frontend/src/main.tsx"}
MAX_RESPONSE_CHARS = 240000

FORMAT_HELP = """OUTPUT FORMAT (strict). Output only change blocks, no explanations, no markdown fences around blocks.
Create a new file or replace a whole file:
<<<FILE frontend/src/pages/Example.tsx>>>
...complete file content...
<<<END FILE>>>
Change part of an existing file (OLD must be copied exactly from CURRENT SOURCE and occur exactly once):
<<<EDIT backend/src/app.js>>>
<<<OLD>>>
...exact existing lines...
<<<NEW>>>
...replacement lines...
<<<END EDIT>>>
Delete a file you created earlier:
<<<DELETE frontend/src/pages/Unused.tsx>>>
Prefer EDIT blocks for small changes to large files. Never abbreviate a FILE block ("rest unchanged" is forbidden).
If CURRENT SOURCE already implements everything asked, output exactly: <<<NO CHANGES>>>"""

_HEADER = re.compile(r"^[ \t]*<{2,3}\s*(FILE|EDIT|DELETE)\s*:?\s*[`'\"]?([^\s`'\"<>]+)[`'\"]?\s*>{1,3}[ \t]*$", re.M | re.I)
_END_FILE = re.compile(r"^[ \t]*<{2,3}\s*END[ _-]?FILE\b[^>\n]*>{1,3}[ \t]*$|^[ \t]*<{2,3}\s*END\s*>{1,3}[ \t]*$", re.M | re.I)
_END_EDIT = re.compile(r"^[ \t]*<{2,3}\s*END[ _-]?EDIT\b[^>\n]*>{1,3}[ \t]*$|^[ \t]*<{2,3}\s*END\s*>{1,3}[ \t]*$", re.M | re.I)
_OLD = re.compile(r"^[ \t]*<{2,3}\s*OLD\s*>{1,3}[ \t]*\n?", re.M | re.I)
_NO_CHANGES = re.compile(r"<{2,3}\s*NO[ _-]?CHANGES?\s*>{1,3}", re.I)
_NEW = re.compile(r"^[ \t]*<{2,3}\s*NEW\s*>{1,3}[ \t]*\n?", re.M | re.I)


def _strip_fence(content: str) -> str:
    lines = content.split("\n")
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
    return "\n".join(lines)


def _body(text: str) -> str:
    # Drop exactly one newline after the header and before the end marker.
    if text.startswith("\r\n"):
        text = text[2:]
    elif text.startswith("\n"):
        text = text[1:]
    if text.endswith("\n"):
        text = text[:-1]
    return _strip_fence(text)


def _trim_tail(text: str) -> str:
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    return _strip_fence(text)


def parse_blocks(text: str, finish_reason: str | None = None) -> dict:
    """Return {'ops': [...], 'truncated': bool, 'incomplete': [paths]} ."""
    text = clean_text(text)
    headers = list(_HEADER.finditer(text))
    ops, incomplete = [], []
    if not headers:
        data = None
        try:
            from model_transport import extract_json
            data = extract_json(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            for item in data.get("files") or []:
                if isinstance(item, dict) and isinstance(item.get("path"), str) and isinstance(item.get("content"), str):
                    ops.append({"kind": "file", "path": item["path"], "content": item["content"]})
            for item in data.get("edits") or []:
                if isinstance(item, dict) and all(isinstance(item.get(k), str) for k in ("path", "old", "new")):
                    kind = "edit" if item["old"] else "file"
                    ops.append({"kind": kind, "path": item["path"], "old": item["old"], "new": item["new"], "content": item["new"]})
        return {"ops": ops, "truncated": finish_reason == "length", "incomplete": incomplete}
    for index, header in enumerate(headers):
        kind, path = header.group(1).lower(), header.group(2).strip()
        segment_end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        segment = text[header.end():segment_end]
        is_last = index + 1 == len(headers)
        if kind == "delete":
            ops.append({"kind": "delete", "path": path})
            continue
        end = (_END_FILE if kind == "file" else _END_EDIT).search(segment)
        if end is None:
            # A block without its end marker is only trusted when the reply ended normally.
            if not (is_last and finish_reason not in ("length",)):
                incomplete.append(path)
                continue
            inner = segment
        else:
            inner = segment[:end.start()]
        if kind == "file":
            ops.append({"kind": "file", "path": path, "content": _body(inner)})
            continue
        old_match, new_match = _OLD.search(inner), _NEW.search(inner)
        if not old_match or not new_match or new_match.start() < old_match.end():
            incomplete.append(path)
            continue
        old = _trim_tail(inner[old_match.end():new_match.start()])
        new = _trim_tail(inner[new_match.end():])
        ops.append({"kind": "edit", "path": path, "old": old, "new": new})
    return {"ops": ops, "truncated": finish_reason == "length" or bool(incomplete), "incomplete": incomplete}


def normalize_path(name: str) -> str | None:
    name = (name or "").strip().replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    p = PurePosixPath(name)
    if not name or p.is_absolute() or ":" in name or ".." in p.parts or any(part.startswith(".") for part in p.parts):
        return None
    if not name.startswith(SOURCE_ROOTS) or p.suffix not in SUFFIXES:
        return None
    return name


def _find_once(content: str, old: str):
    if old and content.count(old) == 1:
        return content.index(old), len(old)
    # Tolerate trailing-whitespace and CRLF drift, still requiring a unique match.
    def norm(value):
        return "\n".join(line.rstrip() for line in value.replace("\r\n", "\n").split("\n"))
    n_content, n_old = norm(content), norm(old).strip("\n")
    if n_old and n_content.count(n_old) == 1 and content.replace("\r\n", "\n") == content:
        lines = content.split("\n")
        target = n_old.split("\n")
        for start in range(len(lines) - len(target) + 1):
            if [line.rstrip() for line in lines[start:start + len(target)]] == target:
                offset = sum(len(line) + 1 for line in lines[:start])
                length = sum(len(line) + 1 for line in lines[start:start + len(target)]) - 1
                return offset, length
    return None


def stage_ops(root: Path, parsed: dict, readable: set[str] | None = None, protected=PROTECTED, allow_empty=False) -> dict:
    """Dry-run the ops against the workspace. Raises ValueError only when nothing is usable."""
    ops = parsed.get("ops") or []
    staged: dict[str, str | None] = {}
    warnings, errors = [], []
    rejected: list[dict] = []   # blocks that could not be applied (for a targeted follow-up)
    total = 0

    def reject(op, path, message):
        errors.append(message)
        rejected.append({"path": path, "kind": op.get("kind"), "error": message.split(": ", 1)[-1][:160],
                         "old": (op.get("old") or "")[:1500], "new": (op.get("new") or op.get("content") or "")[:6000]})
    for op in ops:
        path = normalize_path(op.get("path", ""))
        if path is None:
            warnings.append(f"ignored path outside frontend/src or backend/src: {op.get('path')!r}")
            continue
        if path in protected:
            warnings.append(f"ignored {path}: host-owned file (put backend code in backend/src/app.js, frontend code in App.tsx)")
            continue
        target = root / path
        exists = path in staged and staged[path] is not None or (path not in staged and target.exists())
        current = staged[path] if path in staged else (target.read_text(encoding="utf-8") if target.exists() else None)
        if op["kind"] == "delete":
            if current is not None:
                staged[path] = None
            continue
        if op["kind"] == "file":
            content = op.get("content", "")
            if not content.strip():
                warnings.append(f"ignored empty FILE block for {path}")
                continue
            if exists and readable is not None and path not in readable and path not in staged:
                reject(op, path, f"{path}: whole-file replacement of a file not shown in CURRENT SOURCE; use EDIT blocks or request it")
                continue
            total += len(content)
            staged[path] = content if content.endswith("\n") else content + "\n"
            continue
        if current is None:
            reject(op, path, f"{path}: EDIT on a file that does not exist; use a FILE block")
            continue
        found = _find_once(current, op.get("old", ""))
        if found is None:
            count = current.count(op.get("old", "")) if op.get("old") else 0
            reject(op, path, f"{path}: EDIT OLD text {'matches ' + str(count) + ' times' if count else 'not found'}; copy it exactly and make it unique")
            continue
        offset, length = found
        new = op.get("new", "")
        total += len(new)
        staged[path] = current[:offset] + new + current[offset + length:]
    if total > MAX_RESPONSE_CHARS:
        raise ValueError(f"response too large ({total} chars); implement a smaller slice")
    changed = {p: c for p, c in staged.items() if (c is None) or not (root / p).exists() or (root / p).read_text(encoding="utf-8") != c}
    if not changed and not errors and (allow_empty or parsed.get("no_changes") or ops):
        return {"staged": {}, "warnings": warnings, "errors": [], "truncated": False, "incomplete": [], "no_changes": True}
    if not changed:
        detail = "; ".join(errors + warnings)[:900] or "no change blocks found"
        raise RejectedBlocks("no applicable change blocks: " + detail, rejected)
    return {"staged": changed, "warnings": warnings, "errors": errors, "rejected": rejected,
            "truncated": bool(parsed.get("truncated")), "incomplete": parsed.get("incomplete", [])}


class RejectedBlocks(ValueError):
    """No block of a reply could be applied. `correction` (set by block_parser) shows the files the
    blocks targeted, so the retry can copy OLD text exactly instead of guessing again (V8: the same
    unit failed three times on a file that was never shown to the model)."""

    def __init__(self, message, rejected=()):
        super().__init__(message)
        self.rejected = list(rejected)
        self.correction = ""


def files_for_correction(root: Path, paths, limit: int = 90000) -> str:
    parts, size = [], 0
    for path in dict.fromkeys(paths):
        target = root / path
        if not target.is_file():
            continue
        content = target.read_text(encoding="utf-8", errors="replace")
        if size + len(content) > limit:
            continue
        parts.append(f"=== FILE {path} ===\n{content}\n=== END FILE {path} ===")
        size += len(content)
    return "\n\n".join(parts)


def write_staged(root: Path, staged: dict) -> list[str]:
    for path, content in staged.items():
        target = root / path
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("path escaped workspace: " + path)
    for path, content in staged.items():
        target = root / path
        if content is None:
            if target.exists():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return sorted(staged)


def block_parser(root: Path, readable=None, protected=PROTECTED):
    """Parser+validator pair for request_json: returns the staged change set.
    When nothing applies, the files the rejected blocks targeted become readable and are attached to
    the retry's correction (request_json appends `error.correction`)."""
    readable = set(readable) if readable is not None else None

    def parse(text, finish_reason):
        parsed = parse_blocks(text, finish_reason)
        if not parsed["ops"] and _NO_CHANGES.search(clean_text(text)):
            return {"staged": {}, "warnings": [], "errors": [], "truncated": False, "incomplete": [], "no_changes": True}
        if not parsed["ops"]:
            if finish_reason == "length":
                raise ValueError("truncated_output before the first complete block")
            raise ValueError("no change blocks found; use <<<FILE path>>> ... <<<END FILE>>> blocks")
        try:
            return stage_ops(root, parsed, readable, protected)
        except RejectedBlocks as error:
            paths = [r["path"] for r in error.rejected if r.get("path")]
            if paths:
                if readable is not None:
                    readable.update(paths)
                shown = files_for_correction(root, paths)
                if shown:
                    error.correction = ("\nCURRENT CONTENT OF THE FILES YOUR REJECTED BLOCKS TARGETED (copy EDIT OLD text exactly "
                                        "from here, with enough surrounding lines to be unique; a FILE block must keep "
                                        "everything that still applies):\n" + shown)
            raise
    return parse
