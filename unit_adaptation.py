"""Generic per-unit adaptation helpers (V8, context sizes raised in 929.V1).

No product names or domain vocabulary are used. Decisions depend only on requirement-tree
structure: unit size, number of scenarios, declared dependencies, and hierarchy depth.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def walk(node):
    if not isinstance(node, dict):
        return []
    out = [node]
    for child in node.get('children', []) or []:
        out.extend(walk(child))
    return out


def subtree_stats(node):
    nodes = walk(node)
    leaves = [n for n in nodes if not [c for c in n.get('children', []) or [] if isinstance(c, dict)]]
    scenarios = sum(len(n.get('scenarios') or []) for n in nodes)
    dependencies = sum(len(n.get('dependencies') or []) for n in nodes)
    return {
        'nodes': len(nodes),
        'leaves': max(1, len(leaves)),
        'scenarios': scenarios,
        'dependencies': dependencies,
        'scenario_density': scenarios / max(1, len(leaves)),
    }


def unit_profile(unit):
    stats = subtree_stats(unit.get('requirement'))
    chars = len(json.dumps(unit, ensure_ascii=False))
    depth = len(unit.get('ancestors') or [])
    high = (stats['scenarios'] >= 4 and chars >= 6500) or stats['dependencies'] >= 3
    # An additional vote catches dense multi-scenario contracts with little explicit dependency metadata.
    if stats['scenario_density'] >= 4.0 and chars >= 5500:
        high = True
    return {
        'quality': 'high' if high else 'standard',
        # 929.V1: V8's 120k/150k chars were filled by one 121 KB backend file on the larger task, so page
        # files the unit had to change were hidden and their edits rejected. The model accepts ~1M tokens;
        # the stable part of the source is served from the provider's prefix cache.
        'context_limit': 480000,   # 932.V1: whole source, cache-ordered (see generated_application.FULL_SOURCE_CHARS)
        'reasoning_stage': 'source_generation_complex' if high else 'source_generation',
        'continuation_stage': 'source_continuation_complex' if high else 'source_continuation',
        'chars': chars,
        'depth': depth,
        **stats,
    }


def node_index(document):
    return {str(n.get('id')): n for n in walk(document) if n.get('id') is not None}


def ids_in_unit(unit):
    ids = {str(n.get('id')) for n in walk(unit.get('requirement')) if n.get('id') is not None}
    ids.update(str(a.get('id')) for a in unit.get('ancestors') or [] if isinstance(a, dict) and a.get('id') is not None)
    return ids


def dependency_ids(unit):
    deps = []
    seen = set()
    for n in walk(unit.get('requirement')) + [a for a in unit.get('ancestors') or [] if isinstance(a, dict)]:
        for dep in n.get('dependencies') or []:
            dep = str(dep)
            if dep and dep not in seen:
                seen.add(dep)
                deps.append(dep)
    return deps


def dependency_summaries(unit, index, limit=5000):
    rows = []
    size = 0
    for dep in dependency_ids(unit):
        n = index.get(dep)
        if not n:
            continue
        row = f"{dep} {n.get('name', '')}: {' '.join(str(n.get('description', '')).split())[:700]}"
        if size + len(row) > limit:
            break
        rows.append(row)
        size += len(row)
    return '\n'.join(rows)


_ROUTE = re.compile(r'''(?:path\s*=\s*|\bto\s*=\s*)["']([^"']+)["']''')
_NAV = re.compile(r'''navigate\(\s*["']([^"']+)["']''')
_API = re.compile(r'''["'`](/api/[A-Za-z0-9_./:${}-]+)["'`]''')
_STORE = re.compile(r'''(?:loadJson|saveJson)\(\s*["']([^"']+)["']''')


def implementation_surface(root: Path, limit=6000):
    """Compact, deterministic summary of interfaces that already exist in generated source."""
    routes, apis, stores = set(), set(), set()
    for part in ('frontend/src', 'backend/src'):
        base = Path(root) / part
        if not base.exists():
            continue
        for path in base.rglob('*'):
            if not path.is_file() or path.suffix not in {'.js', '.jsx', '.ts', '.tsx'}:
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            routes.update(_ROUTE.findall(text))
            routes.update(_NAV.findall(text))
            apis.update(_API.findall(text))
            stores.update(_STORE.findall(text))
    rows = []
    if routes:
        rows.append('Routes: ' + ', '.join(sorted(routes)[:80]))
    if apis:
        rows.append('API paths: ' + ', '.join(sorted(apis)[:120]))
    if stores:
        rows.append('Persistent data keys: ' + ', '.join(sorted(stores)[:80]))
    return '\n'.join(rows)[:limit]


def large_files_note(root: Path, threshold=30000, limit=12):
    """Files that already exceed the size rule: new code must go into new modules instead."""
    rows = []
    for part in ('frontend/src', 'backend/src'):
        base = Path(root) / part
        if not base.exists():
            continue
        for path in base.rglob('*'):
            if path.is_file() and path.suffix in {'.js', '.jsx', '.ts', '.tsx', '.css'}:
                size = path.stat().st_size
                rel = path.relative_to(root).as_posix()
                if size > threshold and rel not in ('backend/src/index.js', 'frontend/src/main.tsx'):
                    rows.append((size, rel))
    rows.sort(reverse=True)
    if not rows:
        return ''
    return ('FILES ALREADY OVER THE SIZE RULE (do not grow them; put new code in new modules and import it, and when you must '
            'change them use small EDIT blocks): ' + ', '.join(f'{rel} ({size // 1024} KB)' for size, rel in rows[:limit]))


_STUB_TEXT = re.compile(r"coming soon|not (?:yet )?implemented|under construction", re.I)


def known_gaps(root: Path, limit=20):
    """Client routes that render a stub page, and duplicated route conventions for the same page."""
    try:
        from route_map import routes
        table = routes(root)
    except Exception:
        return ''
    stubs, by_file = [], {}
    stub_cache = {}
    for pattern, target in table:
        if target not in stub_cache:
            try:
                text = (Path(root) / target).read_text(encoding='utf-8', errors='replace')
            except OSError:
                text = ''
            stub_cache[target] = 'placeholder' in Path(target).stem.lower() or bool(_STUB_TEXT.search(text))
        if stub_cache[target] and pattern.strip('/') not in ('*', ''):
            stubs.append(pattern)
        by_file.setdefault(target, []).append(pattern)
    rows = []
    if stubs:
        rows.append('Routes that render a stub instead of the feature (implement the real page when a requirement reaches it): '
                    + ', '.join(stubs[:limit]))
    shapes = {}
    for target, patterns in by_file.items():
        if 'placeholder' in Path(target).stem.lower():
            continue
        tails = {}
        for pattern in patterns:
            tail = '/'.join(p for p in pattern.split('/')[-1:] if not p.startswith(':'))
            tails.setdefault(tail, []).append(pattern)
        for tail, group in tails.items():
            if len(group) > 1 and len({g.count('/:') for g in group}) > 1:
                shapes.setdefault(target, group)
    if shapes:
        rows.append('Same page reachable under different URL schemes (links must all use ONE scheme and pass the same ids): '
                    + '; '.join(f"{target}: {', '.join(group[:4])}" for target, group in list(shapes.items())[:8]))
    return '\n'.join(rows)
