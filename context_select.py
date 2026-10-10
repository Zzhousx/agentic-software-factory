"""Deterministic source-context selection for model prompts.

926.V1 asked the model which files to read (source_focus). Those calls drew on a small
token bucket; once it ran out (larger task, 19:06) every later unit was generated with only
files < 3000 chars visible, i.e. without App.tsx / app.js / the store, so new features could
not be wired into routes or the API. Selection is now deterministic and never skipped.
"""
from __future__ import annotations
import re

HUB_NAME = re.compile(r"^(app|index|store|db|database|data|seed|seeds|types?|api|auth|session|sessions|routes?|router|"
                      r"layout|nav|navigation|context|state|permissions?|utils?|helpers?)\.(t|j)sx?$", re.I)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{3,}")
_STOP = set("""this that with from when then user users page pages shows show should must will have into only each also them
they their there where which what while about after before other than able data item items name named text button field
value values open opens view list display displayed visible exactly unique given scenario steps content keyword description
requirement requirements type folder atomic dependencies children true false none null import export default return const
function react string number boolean props state async await""".split())

_EXPORT = re.compile(r"^export\s+(?:default\s+)?(?:async\s+)?(?:function|const|class|type|interface|enum)\s+([A-Za-z0-9_]+)", re.M)
_CJS = re.compile(r"module\.exports\s*=\s*\{([^}]{0,300})\}")
_API = re.compile(r"ctx\.match\(\s*['\"]([A-Za-z*]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]")
_ROUTE = re.compile(r"<Route\s+[^>]*path=[\"'{]+([^\"'}]+)")


def keywords(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "") if w.lower() not in _STOP}


def outline(content: str) -> list[str]:
    """Exports, CommonJS exports, API patterns and client routes of a file not shown in full."""
    items = ["export " + m.group(1) for m in _EXPORT.finditer(content)]
    items += ["module.exports {" + " ".join(m.group(1).split())[:200] + "}" for m in _CJS.finditer(content)]
    items += [f"api {m.group(1).upper()} {m.group(2)}" for m in _API.finditer(content)]
    items += ["route " + m.group(1) for m in _ROUTE.finditer(content)]
    return list(dict.fromkeys(items))[:40]


def select(snapshot: dict[str, str], limit: int = 140000, focus=(), unit_text: str = "", recent=()) -> dict:
    total = sum(len(v) for v in snapshot.values())
    if total <= limit:
        return {"files": dict(snapshot), "index": [], "outlines": {}, "large_files_omitted": False}
    wanted = keywords(unit_text)
    scores: dict[str, float] = {}
    for path, content in snapshot.items():
        name = path.rsplit("/", 1)[-1]
        score = 0.0
        if path in focus:
            score += 300
        if path in ("frontend/src/App.tsx", "backend/src/app.js"):
            score += 250
        elif HUB_NAME.match(name):
            score += 120
        if path in recent:
            score += 60
        if wanted:
            spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", path).replace("/", " ").replace("_", " ").replace("-", " ")
            score += 12 * len(wanted & keywords(spaced))
            score += 40 * len(wanted & keywords(content[:20000])) / max(8, len(wanted))
        scores[path] = score
    files, budget = {}, limit
    for path in sorted(snapshot, key=lambda p: (-scores[p], len(snapshot[p]))):
        size = len(snapshot[path])
        if size <= budget and (scores[path] >= 100 or size <= limit * 0.35):
            files[path] = snapshot[path]
            budget -= size
    outlines = {p: outline(c) for p, c in snapshot.items() if p not in files}
    return {"index": [{"path": k, "chars": len(v)} for k, v in snapshot.items()], "files": files,
            "outlines": outlines, "large_files_omitted": True, "scores": {p: round(scores[p], 1) for p in files}}


def render(context: dict, fixed_host: bool = True) -> str:
    parts = []
    if context.get("large_files_omitted"):
        parts.append("Some files are not shown in full (project too large); their outlines are listed at the end. "
                     "Change them only with EDIT blocks whose OLD text you are certain of, or leave them alone.")
    order = [p for p in context.get("order") or [] if p in context["files"]]
    order += sorted(p for p in context["files"] if p not in order)
    for path in order:
        parts.append(f"=== FILE {path} ===\n{context['files'][path]}\n=== END FILE {path} ===")
    if context.get("outlines"):
        sizes = {row["path"]: row["chars"] for row in context.get("index", [])}
        rows = [f"- {path} ({sizes.get(path, 0)} chars): " + ("; ".join(items) if items else "no exports detected")
                for path, items in sorted(context["outlines"].items())]
        parts.append("OUTLINE OF FILES NOT SHOWN:\n" + "\n".join(rows))
    if fixed_host:
        parts.append("READ-ONLY HOST FILES (exist, do not modify, not shown): backend/src/index.js, frontend/src/main.tsx")
    else:
        parts.append("READ-ONLY FILE (exists, not shown): frontend/src/main.tsx")
    return "\n\n".join(parts)
