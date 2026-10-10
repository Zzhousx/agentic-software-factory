"""Map a page URL seen by a failing test to the source files that render it (929.V1).

Repairs in the V8 run were often aimed at the wrong page: the failure showed the URL where the
test got stuck, but the repair context was chosen by keywords, so the component actually rendered
at that URL was frequently missing from the prompt. This reads the client routes from source
(<Route path element> and route objects) and resolves the element's import to a file.
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

_ROUTE_TAG = re.compile(r"<Route\b(.{0,400}?)/?>", re.S)
_PATH_ATTR = re.compile(r"\bpath\s*=\s*\{?\s*['\"`]([^'\"`]+)['\"`]")
_ELEMENT_ATTR = re.compile(r"\b(?:element|Component|component)\s*=\s*\{\s*<?\s*([A-Z][A-Za-z0-9_]*)")
_ROUTE_OBJECT = re.compile(r"path\s*:\s*['\"`]([^'\"`]+)['\"`]\s*,\s*(?:element|Component)\s*:\s*<?\s*([A-Z][A-Za-z0-9_]*)")
_IMPORT_DEFAULT = re.compile(r"import\s+([A-Z][A-Za-z0-9_]*)\s*(?:,\s*\{[^}]*\})?\s*from\s*['\"](\.[^'\"]+)['\"]")
_IMPORT_NAMED = re.compile(r"import\s*(?:[A-Za-z0-9_]+\s*,\s*)?\{([^}]*)\}\s*from\s*['\"](\.[^'\"]+)['\"]")
_LAZY = re.compile(r"(?:const|let)\s+([A-Z][A-Za-z0-9_]*)\s*=\s*(?:React\.)?lazy\(\s*\(\)\s*=>\s*import\(\s*['\"](\.[^'\"]+)['\"]")
_SUFFIXES = ("", ".tsx", ".ts", ".jsx", ".js", "/index.tsx", "/index.ts", "/index.jsx", "/index.js")


def _resolve(root: Path, importer: str, spec: str) -> str | None:
    base = PurePosixPath(importer).parent
    for suffix in _SUFFIXES:
        candidate = (base / (spec + suffix)).as_posix()
        parts = []
        for part in candidate.split("/"):
            if part == "..":
                if parts:
                    parts.pop()
            elif part and part != ".":
                parts.append(part)
        rel = "/".join(parts)
        if (root / rel).is_file():
            return rel
    return None


def _imports(root: Path, rel: str, text: str) -> dict[str, str]:
    found = {}
    for name, spec in _IMPORT_DEFAULT.findall(text) + _LAZY.findall(text):
        target = _resolve(root, rel, spec)
        if target:
            found[name] = target
    for names, spec in _IMPORT_NAMED.findall(text):
        target = _resolve(root, rel, spec)
        if not target:
            continue
        for item in names.split(","):
            item = item.strip()
            if not item:
                continue
            alias = item.split(" as ")[-1].strip()
            if alias[:1].isupper():
                found[alias] = target
    return found


def routes(root: Path) -> list[tuple[str, str]]:
    """(path pattern, file) for every client route found in frontend/src."""
    root = Path(root)
    base = root / "frontend/src"
    if not base.exists():
        return []
    result = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix not in (".tsx", ".jsx", ".ts", ".js"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "path" not in text or ("<Route" not in text and "element:" not in text and "Component:" not in text):
            continue
        rel = path.relative_to(root).as_posix()
        imports = _imports(root, rel, text)
        pairs = []
        for match in _ROUTE_TAG.finditer(text):
            attrs = match.group(1)
            p, e = _PATH_ATTR.search(attrs), _ELEMENT_ATTR.search(attrs)
            if p and e:
                pairs.append((p.group(1), e.group(1)))
        pairs += _ROUTE_OBJECT.findall(text)
        for pattern, component in pairs:
            target = imports.get(component) or (rel if re.search(r"(?:function|const|class)\s+" + re.escape(component) + r"\b", text) else None)
            if target:
                result.append((pattern, target))
    return result


def _matches(pattern: str, segments: list[str]) -> int:
    """Specificity score (>0) if the pattern matches the URL segments, else 0."""
    parts = [p for p in pattern.split("?")[0].split("/") if p]
    absolute = pattern.startswith("/")
    if not absolute:
        segments = segments[-len(parts):] if parts and len(segments) >= len(parts) else segments
    score = 1
    for index, part in enumerate(parts):
        if part == "*":
            return score
        if index >= len(segments):
            return 0
        if part.startswith(":"):
            continue
        if part.lower() != segments[index].lower():
            return 0
        score += 2
    return score if len(parts) == len(segments) else 0


def files_for_urls(root: Path, urls, limit: int = 4) -> list[str]:
    """Source files rendering the given page URLs, most specific route first."""
    table = routes(root)
    found: list[str] = []
    for url in urls:
        if not url:
            continue
        path = urlparse(url).path if "://" in url else url
        segments = [s for s in path.split("/") if s]
        scored = sorted(((score, target) for pattern, target in table if (score := _matches(pattern, segments))), reverse=True)
        for _, target in scored[:2]:
            if target not in found:
                found.append(target)
        if len(found) >= limit:
            break
    return found[:limit]
