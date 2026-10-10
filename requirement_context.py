"""Requirement-wide context shared by every prompt (929.V1).

The V8 run showed that cross-cutting contracts written in FOLDER descriptions (the root and the
top-level modules) did not reach the prompts that needed them: a unit only saw its own ancestors,
and test generation cut every ancestor description to 600 characters. Contracts such as "grid
cells use the gridcell role named by their coordinate" (stated once in one module, relied on by
every other module) or "seed accounts sign in through ..." were therefore invisible to most
units and to the test writer.

Everything here is derived mechanically from the supplied requirement tree: no product names,
no domain vocabulary.
"""
from __future__ import annotations

import re

_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_SCREENSHOT = re.compile(r"(?:Screenshot|Page) reference:?\s*", re.I)
_TICK = re.compile(r"`([^`\n]{1,80})`")


def clean(text) -> str:
    text = _IMAGE.sub("", str(text or ""))
    text = _SCREENSHOT.sub("", text)
    return " ".join(text.split())


def _children(node):
    return [c for c in (node.get("children") or []) if isinstance(c, dict)] if isinstance(node, dict) else []


def _walk(node):
    if not isinstance(node, dict):
        return []
    out = [node]
    for child in _children(node):
        out.extend(_walk(child))
    return out


def _leaves(node):
    return [n for n in _walk(node) if not _children(n) and n.get("id")]


def contract_nodes(document) -> list[tuple[int, dict]]:
    """(level, node) for the root and the non-leaf nodes of the first two levels: module-wide contracts."""
    if isinstance(document, list):
        document = {"id": "ROOT", "children": document}
    nodes = [(0, document)]
    for child in _children(document):
        if _children(child):
            nodes.append((1, child))
            nodes.extend((2, c) for c in _children(child) if _children(c))
    return nodes


def contract_ids(document, text: str) -> set[str]:
    """Ids of contract nodes whose description is contained in `text` (already shown elsewhere)."""
    return {str(n.get("id")) for _, n in contract_nodes(document) if n.get("id") is not None and clean(n.get("description")) and
            clean(n.get("description"))[:200] in text}


def global_contracts(document, limit: int = 16000) -> str:
    """Root + module descriptions in full, in requirement order. They define shared UI/data rules
    (roles, names, seed data, sign-in flow) that apply to features outside the module that states them.
    Levels 0-1 always (up to `limit`); level-2 module descriptions only while everything still fits."""
    nodes = contract_nodes(document)
    rows = {}
    size = 0
    for level in (0, 1, 2):
        for position, (lvl, node) in enumerate(nodes):
            if lvl != level:
                continue
            text = clean(node.get("description"))
            if not text:
                continue
            row = f"{node.get('id', '')} {node.get('name', '')}: {text}".strip()
            if size + len(row) > limit:
                if level == 2:
                    continue
                row = row[:max(0, limit - size)]
                if len(row) < 200:
                    continue
            rows[position] = row
            size += len(row) + 2
    return "\n\n".join(rows[k] for k in sorted(rows))


def seed_index(document, limit: int = 5000) -> str:
    """Every literal value quoted with backticks in requirement leaves, with the requirements that use it.
    Values used by many requirements are shared seed records: they must exist once, consistently."""
    index: dict[str, list[str]] = {}
    for leaf in _leaves(document):
        texts = [str(leaf.get("description", ""))]
        for scenario in leaf.get("scenarios") or []:
            if isinstance(scenario, dict):
                texts.extend(str(step.get("content", "")) for step in scenario.get("steps") or [] if isinstance(step, dict))
        for text in texts:
            for value in _TICK.findall(text):
                value = value.strip()
                if len(value) < 2 or not re.search(r"[A-Za-z0-9]", value):
                    continue
                ids = index.setdefault(value, [])
                if str(leaf.get("id")) not in ids:
                    ids.append(str(leaf.get("id")))
    if not index:
        return ""
    rows, size = [], 0
    for value, ids in sorted(index.items(), key=lambda kv: (-len(kv[1]), kv[0].lower())):
        shown = ", ".join(ids[:5]) + (f" (+{len(ids) - 5} more)" if len(ids) > 5 else "")
        row = f"`{value}` used by {len(ids)}: {shown}"
        if size + len(row) > limit:
            break
        rows.append(row)
        size += len(row) + 1
    return "\n".join(rows)
