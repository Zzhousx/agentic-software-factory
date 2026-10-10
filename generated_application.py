"""Requirement-driven source generation (929.V1; acceptance loop since 927.V1).

Every unit is accepted only after `vite build`, `node --check` and a runtime smoke test that
starts the app the way ARC-Bench does. With 927.V1 each unit is additionally run against
Playwright specs written from its requirement scenarios, and failures are repaired
(see tdd_loop.py). Build success alone never counts.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import context_select
from requirement_context import clean, global_contracts, seed_index
from unit_adaptation import (dependency_ids, dependency_summaries, ids_in_unit, implementation_surface, known_gaps,
                             large_files_note, node_index, subtree_stats, unit_profile)
from source_blocks import FORMAT_HELP, PROTECTED, block_parser, write_staged
from app_runtime import smoke


class NoGeneratedSource(RuntimeError):
    """No requirement unit produced a verified application."""


class StarterBuildError(RuntimeError):
    """Template dependencies could not be installed/built at all."""


def work_units(document, limit=16000):
    """Split requirement tree with generic density-aware granularity.

    Dense multi-scenario subtrees use a smaller 12k threshold; ordinary workflow subtrees
    keep the 16k threshold. This is based only on requirement structure.
    """
    units = []

    def visit(node, ancestors):
        if not isinstance(node, dict):
            return
        encoded = json.dumps(node, ensure_ascii=False)
        children = [c for c in node.get('children', []) or [] if isinstance(c, dict)]
        stats = subtree_stats(node)
        local_limit = 12000 if stats['scenario_density'] >= 3.5 else limit
        if len(encoded) > local_limit and children:
            context = {k: v for k, v in node.items() if k != 'children'}
            for child in children:
                visit(child, ancestors + [context])
        else:
            units.append({'id': str(node.get('id', len(units))), 'ancestors': ancestors, 'requirement': node})

    if isinstance(document, list):
        document = {'id': 'ROOT', 'name': 'Application', 'children': document}
    visit(document, [])
    return units


# (path, content hash) -> modification time first seen. 935.V1: a reverted change restores the files' old mtimes, so the
# cache-friendly source order (oldest first, see source_context) and with it the provider's cached prefix survive a revert.
_SEEN_MTIME: dict[tuple[str, str], int] = {}


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest()


def source_snapshot(root):
    snapshot = {}
    for part in ('frontend/src', 'backend/src'):
        for p in (root / part).rglob('*'):
            if p.is_file():
                name = p.relative_to(root).as_posix()
                content = p.read_text(encoding='utf-8', errors='replace')
                snapshot[name] = content
                try:
                    _SEEN_MTIME.setdefault((name, _digest(content)), p.stat().st_mtime_ns)
                except OSError:
                    pass
    return snapshot


def restore_sources(root, snapshot):
    for name in source_snapshot(root):
        if name not in snapshot:
            (root / name).unlink()
    for name, content in snapshot.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists() or p.read_text(encoding='utf-8', errors='replace') != content:
            p.write_text(content, encoding='utf-8')
            seen = _SEEN_MTIME.get((name, _digest(content)))
            if seen:
                try:
                    os.utime(p, ns=(seen, seen))
                except OSError:
                    pass


def changed_paths(before, after):
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def _npm():
    return shutil.which('npm.cmd') or shutil.which('npm') or 'npm'


def build(root, deadline=None, install=False):
    """node --check every backend module + vite build. Fixed timeouts, independent of the run deadline."""
    npm = _npm()
    node = shutil.which('node') or 'node'
    commands = []
    for part in ('frontend', 'backend'):
        if install or not (root / part / 'node_modules').exists():
            commands.append(([npm, 'install', '--no-audit', '--no-fund', '--fetch-retries=2', '--fetch-timeout=60000'], root / part, 420))
    commands += [([node, '--check', str(p.resolve())], root, 30) for p in sorted((root / 'backend/src').rglob('*.js'))]
    commands.append(([npm, 'run', 'build'], root / 'frontend', 240))
    try:
        for command, cwd, limit in commands:
            result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=limit)
            if result.returncode:
                label = 'node --check ' + Path(command[-1]).name if command[1] == '--check' else ' '.join(Path(command[0]).name.split() + command[1:3])
                return {'ok': False, 'stage': 'build', 'error': f'{label} failed:\n' + (result.stdout + result.stderr)[-5000:]}
        return {'ok': True}
    except Exception as error:
        return {'ok': False, 'stage': 'build', 'error': type(error).__name__ + ': ' + str(error)[:500]}


def verify(root, deadline, routes=None, browser=True):
    """Build + platform-like runtime smoke. Returns {'ok', 'stage', 'error'}."""
    result = build(root, deadline)
    if not result['ok']:
        return result
    try:
        report = smoke(root, deadline, routes=routes or ['/'], browser=browser)
    except Exception as error:
        return {'ok': False, 'stage': 'smoke', 'error': type(error).__name__ + ': ' + str(error)[:500]}
    if not report.get('ok'):
        return {'ok': False, 'stage': report.get('stage', 'smoke'), 'error': report.get('error', 'runtime smoke failed'), 'smoke': report}
    return {'ok': True, 'smoke': report}


# Host mode: our fixed host (normal) or an inherited application's own server entry (evolution tasks).
MODE = {'fixed_host': True}


def protected_paths():
    return set(PROTECTED) if MODE['fixed_host'] else {'frontend/src/main.tsx'}


def detect_host_mode(root):
    entry = root / 'backend/src/index.js'
    MODE['fixed_host'] = (not entry.exists()) or 'EVOGIT_FIXED_HOST' in entry.read_text(encoding='utf-8', errors='replace')
    return MODE['fixed_host']


# 932.V1: whole source in every code-writing prompt (up to this size), in a stable order, so consecutive calls share
# one long cached prefix and every call sees every file. Above the limit, files are selected by relevance.
FULL_SOURCE_CHARS = 480000
REPAIR_CONTEXT_CHARS = FULL_SOURCE_CHARS


def source_context(root, limit=REPAIR_CONTEXT_CHARS, focus=(), unit_text='', recent=()):
    snapshot = {k: v for k, v in source_snapshot(root).items() if k not in protected_paths()}
    context = context_select.select(snapshot, limit=limit, focus=focus, unit_text=unit_text, recent=recent)
    # Prompt-cache friendly order: files that have not changed for longest first, recently written last.
    # (928/V8 sorted hub files first; hub files change in almost every step, which ended the cached
    # prefix right after the static preamble.)
    def mtime(path):
        try:
            return (root / path).stat().st_mtime_ns
        except OSError:
            return 0
    context['order'] = sorted(context['files'], key=lambda p: (mtime(p), p))
    return context


def render_context(context):
    return context_select.render(context, MODE['fixed_host'])


_PATH_IN_TEXT = re.compile(r'((?:frontend|backend)/src/[A-Za-z0-9_./-]+\.(?:tsx|ts|jsx|js|mjs|cjs|css|json))')


def paths_in(text, snapshot):
    found = []
    for match in _PATH_IN_TEXT.findall(text or ''):
        if match in snapshot and match not in found:
            found.append(match)
    return found


CONTRACT = """You implement a production web application from its requirements, one requirement unit at a time, in an existing project.

PROJECT LAYOUT (fixed, already installed):
- frontend/: Vite + React 19 + react-router-dom 7 + Tailwind CSS v4 (frontend/src/index.css already has @import "tailwindcss").
  TypeScript/TSX is transpiled without type checking. frontend/src/main.tsx (read-only) renders the default export of
  frontend/src/App.tsx inside <BrowserRouter>. Put pages/components under frontend/src/.
  ONLY react, react-dom and react-router-dom are installed. Never import any other package (no axios, UI kits, icon packs, date libs).
  Use fetch or frontend/src/api/index.ts (apiGet/apiPost/apiPut/apiPatch/apiDelete return parsed JSON and throw ApiError on non-2xx).
- backend/: Node.js standard library only, CommonJS. Requiring any npm package (express, sqlite3, bcrypt, uuid, ...) crashes the API.
  backend/src/index.js is a read-only host: it listens on PORT, answers GET /api/health, serves the built frontend with SPA
  fallback, and forwards every other /api/* request to backend/src/app.js. Implement backend/src/app.js (and helper modules
  under backend/src/ that it requires):
    module.exports = {
      init(ctx) { /* create/seed state; runs once before the server accepts requests */ },
      async handle(req, res, ctx) { /* return true when handled; unhandled /api/* requests get 404 */ },
    };
  ctx: method, path, query (URLSearchParams), cookies (object), match(method, '/api/items/:id') -> params object or null,
  await readBody(req) -> parsed JSON/form object, sendJson(res, status, value) -> true, setCookie(res, name, value, {maxAge, httpOnly}),
  clearCookie(res, name), dataDir (absolute path), loadJson(name, fallback), saveJson(name, value) (atomic file write in dataDir).
  Persist server state with loadJson/saveJson. In init(ctx) seed every account and record the requirements name (only when the
  data file does not exist yet). Never use process.cwd() or relative paths for files.

FUNCTIONAL VERIFICATION EXPECTATIONS (derived only from the supplied requirements):
- Every test starts in a fresh browser at "/", signs in through the UI with the seeded credentials when the scenario needs
  a user, and reaches features only through visible links/buttons/tabs/menus named in the requirements (or URLs the
  requirements state). Tests run sequentially against one server started from the seed data.
- Elements are found with getByRole(role, { name }), getByLabel, getByPlaceholder and getByText using the exact quoted names
  (case-insensitive). Therefore: give every control the exact accessible name from the requirements (visible text or
  aria-label); label every input with <label htmlFor> or aria-label equal to the quoted label; use the ARIA roles the text
  names (dialog with aria-label, region, article, listitem, row/cell, grid/gridcell, tab/tabpanel, status/alert, navigation,
  heading); keep each name unique on its page (duplicates break strict locators); show feedback messages as visible text
  (role="status" or role="alert") with the exact wording; keep controls visible without hover.
- Seed data named in the requirements (accounts, passwords, records) must exist on first start and appear where described.
  Data created through the UI must persist on the server and still be there after navigation or reload.
- The GLOBAL CONTRACTS (root and module descriptions) bind EVERY unit, including units of other modules: use exactly the
  roles, accessible names, state attributes, entry points, page titles and sign-in flow they define.
- Where the requirements are silent, behave like the real product the application imitates (PRODUCT CONVENTIONS in the
  plan): its URL patterns, page structure, labels and keyboard/mouse behaviour. The requirement text always wins.
- Discoverability: tests can only click. The home page (visitors: public records) and the page shown right after sign-in
  (records the signed-in user can access) list the main records as links named exactly by the record's name, and every
  record page links to its sections/sub-pages with the names the requirements use. Lists, search and direct reloads of a
  record page follow the same visibility rules and show the same persisted data.

RULES:
- Implement real behaviour: state changes, server persistence, validation errors, sessions and permissions when specified.
  No placeholders, TODOs, mock success messages or hardcoded test answers.
- The UI must render without runtime errors even when an API call fails; guard against undefined data.
- Keep modules cohesive. Only import files that already exist or that you create in this same response.
- Keep files small: no source file above ~600 lines (~30 KB). backend/src/app.js only wires modules together; put each
  feature area in its own backend module (all sharing ONE store/data-model module and ONE seed module) and each page in
  its own frontend file. When a file is already large, put new code in a new module and import it.
- One data model: every feature reads and writes the same persisted collections. Seed each shared record once (same
  id/name everywhere) and extend it with the fields later features need; never create a second copy of an account or record
  or a parallel store for the same kind of object.
- Keep what already works: never delete or break earlier units' features, routes or seed data. No placeholder pages or
  "not implemented" stubs: a route the requirements reach must render the real feature.
- Never use window.alert/confirm/prompt (automated browsers dismiss them): confirmations are in-page dialogs
  (role="dialog") with named buttons. Text inputs commit on Enter as well as through their button."""


INHERITED_NOTE = """
INHERITED PROJECT: this workspace already contains an application from a previous stage. backend/src/index.js is ITS
server entry (not the fixed host described above): extend it in place, keep its PORT handling, GET /api/health, static
frontend/dist serving with SPA fallback and all existing routes. Keep every existing feature working."""


def contract_text():
    return CONTRACT if MODE['fixed_host'] else CONTRACT + INHERITED_NOTE


DEFAULT_ARCHITECTURE = ("Single-page React app: App.tsx defines the layout, navigation and <Routes>; one module per feature area under "
                        "frontend/src/pages and shared UI under frontend/src/components; shared types in frontend/src/types.ts. "
                        "Backend: backend/src/app.js dispatches /api routes with ctx.match to handler modules under backend/src/routes, "
                        "state persisted with ctx.loadJson/saveJson in backend/src/store.js, seed data created in init(ctx). "
                        "Sessions: random token in an HttpOnly cookie mapped to a user id in sessions.json.")


def truncation_hint():
    return ("Output at most 3 blocks this time, keep each FILE block under ~400 lines, and prefer EDIT blocks for existing files. "
            "Anything you leave out will be requested in a follow-up step.")


def contract_check(root, staged):
    """A reply that only rewrote the host-owned server has no backend at all: send it back."""
    ignored_host = any('backend/src/index.js' in w for w in staged.get('warnings', []))
    if ignored_host and not (root / 'backend/src/app.js').exists():
        return {'ok': False, 'stage': 'contract',
                'error': 'Your backend/src/index.js was IGNORED: it is the read-only host. Implement the API in backend/src/app.js '
                         'exporting { init(ctx), async handle(req, res, ctx) } exactly as described in the contract (use ctx.match, '
                         'ctx.readBody, ctx.sendJson, ctx.loadJson/saveJson). Do not start a server or call listen().'}
    return None


def repair_until_verified(root, call, deadline, before, check, label, rounds=3):
    """Feed build/runtime failures back to the model until verification passes (or give up)."""
    attempts = []
    for _ in range(rounds):
        if check['ok']:
            break
        attempts.append({'stage': check.get('stage'), 'error': check.get('error', '')[:600]})
        current = source_snapshot(root)
        focus = paths_in(check.get('error', ''), current) + changed_paths(before, current)
        repair_context = source_context(root, focus=focus, unit_text=check.get('error', ''))
        fixed = call('build_repair', shared_head() + source_section(root, repair_context) + task_header('build_repair') +
                     'The application FAILED verification after the last change (' + label + ').\nFAILED CHECK: ' +
                     str(check.get('stage')) + '\nERROR:\n' + check.get('error', '')[-6000:] +
                     '\n\nFix the root cause with minimal changes. Keep every implemented feature. '
                     'Remember: only react/react-dom/react-router-dom on the frontend, only Node built-ins on the backend.',
                     lambda d: d, 1200, max_tokens=24000, parser=block_parser(root, set(repair_context['files']), protected_paths()),
                     attempt_timeout=900, truncation_hint=truncation_hint())
        if fixed is None:
            break
        write_staged(root, fixed['staged'])
        check = (contract_check(root, fixed) if MODE['fixed_host'] else None) or verify(root, deadline)
    return check, attempts


def prepare_workspace(root, template, inherited):
    from core_utils import copy_template as _copy_template
    if inherited:
        # Evolution task: keep the inherited application, add only missing template files.
        for source in template.rglob('*'):
            rel = source.relative_to(template)
            if source.is_file() and rel.name != 'template.yaml' and not (root / rel).exists():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, root / rel)
    else:
        _copy_template(template, root)
    detect_host_mode(root)


def outline_of(document):
    rows = []

    def walk(node):
        if not isinstance(node, dict):
            return
        rows.append({k: node[k] for k in ('id', 'name', 'dependencies') if k in node})
        for child in node.get('children', []) or []:
            walk(child)
    walk(document if isinstance(document, dict) else {'children': document})
    return rows


def plan_architecture(root, document, call, generation, contracts='', seeds='', memory=None):
    outline = outline_of(document)
    global_info = {k: v for k, v in document.items() if k != 'children'} if isinstance(document, dict) else {}
    memory = memory or {}
    earlier = ''
    if memory.get('inherited'):
        # 935.V1: an evolution stage plans ON the inherited application (932.V1 planned stage 2 as if from scratch).
        from stage_memory import existing_application_text
        earlier = ('\n\nTHE APPLICATION ALREADY EXISTS (built by an earlier stage of this project). Your plan EXTENDS it: keep '
                   'its routes, page modules, data model, API paths, sign-in flow, seed accounts/records and UI names, reuse '
                   'them for the new requirements, and describe only additions and the changes they need.\n' +
                   existing_application_text(root, memory, implementation_surface(root, 8000)))
    if memory.get('prerequisites'):
        from stage_memory import prerequisites_text
        earlier += '\n\nEARLIER-STAGE PREREQUISITES:\n' + prerequisites_text(memory['prerequisites'], memory.get('inherited'))
    plan = call('architecture',
                'TASK STAGE: architecture\nDesign a compact implementation contract for ALL outlined requirements of this web application. '
                'Return JSON {"architecture": string, "conventions": string, "seed_conflicts": string}. "conventions": if the '
                'requirements say the application imitates a real product, name it and list that product\'s conventions to follow '
                'wherever the requirements are silent: the URL pattern of every kind of page, page titles and headings, where '
                'navigation and account menus sit, default control and menu labels, and keyboard/mouse behaviour (e.g. what Enter, '
                'Escape, Tab, double-click and right-click do); requirement text always wins over a convention. '
                '"architecture" (at most ~2500 words) covers: (1) the navigation map: frontend routes and page '
                'modules, the navigation shell, what the home page and the signed-in landing page list, and the exact link/tab names '
                'from each page to the next; (2) ONE data model: persisted collections with their fields and relationships; '
                '(3) the seed plan: every seed record the requirements quote (see QUOTED VALUES: accounts with passwords, containers, '
                'records, memberships/permissions, relationships) with the owner/parent of each, consistent across all requirements. '
                'Tests can only sign in with credentials the scenarios state, so an account whose password is given is used for '
                'scenarios of many features: give it every role and membership those scenarios need (the highest one when they '
                'differ) so each such scenario can succeed; accounts named without a password get the same password as a stated '
                'account; '
                '(4) every /api endpoint (method, path, request, response) grouped by backend module; (5) sessions/permissions; '
                '(6) which module owns which requirement ids. Keep modules small (see RULES). "seed_conflicts": when different '
                'requirements describe DIFFERENT initial contents for the SAME seeded record (e.g. different values for the same '
                'fields of one record), the seed can hold only one of them. For each such record state: the record, the content '
                'the seed holds (the content most requirements share), and the requirement ids whose scenarios need other '
                'contents (their tests first enter those contents through the UI). Use an empty string when there is no such '
                'conflict.\n' + contract_text() +
                ('\n\nGLOBAL CONTRACTS:\n' + contracts if contracts else '\nGLOBAL:' + json.dumps(global_info, ensure_ascii=False)) +
                ('\n\nVALUES AND NAMES QUOTED IN THE REQUIREMENTS (value used by N: requirement ids; a value shared by several requirements is ONE seed record or ONE UI name everywhere):\n' + seeds if seeds else '') +
                '\nOUTLINE:' + json.dumps(outline, ensure_ascii=False) + earlier,
                # 935.V1: 600 s per attempt (was the 300 s default; accepted plans took 163-214 s, 9 of 12 attempts in the
                # 932.V1 runs were cut at 300 s) and a lower effort after a cut (model_transport).
                validate_architecture, 1500, max_tokens=24000, attempt_timeout=600)
    if plan is None:
        plan = {'architecture': DEFAULT_ARCHITECTURE, 'fallback': True}
    (root / 'IMPLEMENTATION_PLAN.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    from stage_memory import save_plan
    save_plan(root, plan)
    generation['architecture'] = plan
    return plan, outline


def unit_view(unit, contracts=''):
    """The unit as shown to the model. Ancestor descriptions already given in GLOBAL CONTRACTS are
    referenced instead of repeated."""
    ancestors = []
    for ancestor in unit.get('ancestors') or []:
        text = clean(ancestor.get('description', ''))
        if contracts and text and text[:200] in contracts:
            ancestors.append({'id': ancestor.get('id'), 'name': ancestor.get('name'), 'description': '(see GLOBAL CONTRACTS)'})
        else:
            ancestors.append(ancestor)
    return json.dumps({'id': unit.get('id'), 'ancestors': ancestors, 'requirement': unit.get('requirement')}, ensure_ascii=False)


def rejected_text(rejected, limit=24000):
    rows, size = [], 0
    for item in rejected:
        row = (f"- {item.get('kind', '').upper()} {item.get('path')}: {item.get('error')}\n"
               + (f"  intended OLD (did not match):\n{item['old']}\n" if item.get('old') else '')
               + f"  intended NEW content:\n{item.get('new', '')}")
        if size + len(row) > limit:
            break
        rows.append(row)
        size += len(row)
    return '\n'.join(rows)


def static_prompt_head(stage, contracts, seeds, plan_text, outline_text, shared_text):
    """The shared prefix of EVERY code-writing call (generation, continuation, build fix, repair, review).

    932.V1: the stage name used to be the first line, so each stage had its own cache line and the
    source that followed was re-read at full price by every stage (30% cache hits in the 930.V1 runs).
    Now the prefix is identical for all stages; the stage and its instructions come after the source.
    `stage` is accepted for compatibility and ignored."""
    return (contract_text() + '\n\n' + FORMAT_HELP +
            ('\n\nGLOBAL CONTRACTS (root and module rules; binding for every unit, also outside their module):\n' + contracts
             if contracts else '') +
            ('\n\nVALUES AND NAMES QUOTED IN THE REQUIREMENTS (value used by N: requirement ids; a value shared by several requirements is ONE seed record or ONE UI name everywhere):\n' + seeds
             if seeds else '') +
            ('\n\nARCHITECTURE PLAN:\n' + plan_text if plan_text else '') +
            ('\n\nREQUIREMENT OUTLINE (orientation only):\n' + outline_text if outline_text else '') +
            ('\n\nSHARED FLOWS (sign-in, seed data, navigation) that every feature must stay compatible with:\n' + shared_text
             if shared_text else ''))


def shared_head():
    """static_prompt_head() from the values recorded by generate(); plain contract before planning."""
    if not SHARED.get('plan'):
        return contract_text() + '\n\n' + FORMAT_HELP
    return static_prompt_head('', SHARED['contracts'], SHARED['seeds'], SHARED['plan'], SHARED['outline'], SHARED['shared'])


def task_header(stage, title=''):
    return '\n\n=== YOUR TASK' + (' ' + title if title else '') + ' ===\nTASK STAGE: ' + stage + '\n'


def source_section(root, context):
    """Source (stable files first) followed by observations that change after every step."""
    surface = implementation_surface(root)
    notes = [n for n in (large_files_note(root), known_gaps(root)) if n]
    return ('\n\nCURRENT SOURCE:\n' + render_context(context) +
            ('\n\nCURRENT IMPLEMENTATION SURFACE (observed from source; reuse these interfaces rather than inventing parallel ones):\n'
             + surface if surface else '') +
            ('\n\nKNOWN GAPS IN THE CURRENT SOURCE:\n' + '\n'.join(notes) if notes else ''))


# Filled by generate(); reused by the acceptance repair prompts.
SHARED = {'contracts': '', 'seeds': '', 'plan': '', 'outline': '', 'shared': ''}


def generate(root, template, document, call, status, persist, deadline, inherited=False, on_unit=None, reserve=1500,
             loop=None, shared_text='', memory=None):
    """Implement every unit. `loop` (tdd_loop.AcceptanceLoop) runs the unit's specs and repairs failures."""
    prepare_workspace(root, template, inherited)
    units = work_units(document)
    req_index = node_index(document)
    req_to_units = {}
    for candidate in units:
        for req_id in ids_in_unit(candidate):
            req_to_units.setdefault(req_id, set()).add(candidate['id'])
    files_by_unit = {}
    generation = status['generation'] = {'units_total': len(units), 'units': [], 'mode': 'requirement_source_generation_v929'}
    persist()
    contracts = global_contracts(document)
    seeds = seed_index(document)
    plan, outline = plan_architecture(root, document, call, generation, contracts, seeds, memory)
    setup = build(root, deadline, install=True)
    if not setup['ok']:
        setup = build(root, deadline, install=True)
    if not setup['ok']:
        generation['starter_build'] = setup
        persist()
        raise StarterBuildError('Starter dependency/build failure: ' + setup['error'][-1500:])
    if plan.get('conventions'):
        print('[evogit] product conventions: ' + ' '.join(plan['conventions'].split())[:600], flush=True)
    if plan.get('seed_conflicts'):
        print('[evogit] seed conflicts: ' + ' '.join(plan['seed_conflicts'].split())[:600], flush=True)
    if loop is not None:
        loop.seed_notes = plan.get('seed_conflicts') or ''
        loop.start(units)
    accepted, recent = [], []
    started_units = time.monotonic()
    outline_text = '\n'.join(f"- {row.get('id')}: {row.get('name', '')}" for row in outline)[:12000]
    if memory and memory.get('prerequisites'):
        from stage_memory import prerequisites_text
        outline_text += '\n\nEARLIER-STAGE PREREQUISITES:\n' + prerequisites_text(memory['prerequisites'], memory.get('inherited'))
    if memory and memory.get('inherited') and memory.get('previous_outline'):
        outline_text += ('\n\nALREADY IMPLEMENTED BY EARLIER STAGES (keep these features working; do not rebuild them):\n' +
                         memory['previous_outline'][:6000])
    SHARED.update(contracts=contracts, seeds=seeds, plan=plan_text(plan), outline=outline_text, shared=shared_text)
    head = static_prompt_head('source_generation', contracts, seeds, plan_text(plan), outline_text, shared_text)
    state = {'recent': []}

    def implement(position, unit, row, retry=False):
        """Generate, verify and acceptance-test one unit. Returns True when the unit was kept."""
        before = source_snapshot(root)
        unit_text = unit_view(unit, contracts)
        profile = unit_profile(unit)
        dep_ids = dependency_ids(unit)
        dep_units = sorted({u for dep in dep_ids for u in req_to_units.get(dep, set()) if u != unit['id']})
        dep_focus = []
        for dep_unit in dep_units:
            dep_focus.extend(files_by_unit.get(dep_unit, []))
        context = source_context(root, limit=profile['context_limit'], focus=dep_focus[:18], unit_text=unit_text,
                                 recent=state['recent'])
        dep_text = dependency_summaries(unit, req_index)
        row['profile'] = profile
        row['dependency_units'] = dep_units
        foundation = ('This is the FIRST unit: create the application foundation now together with this unit: App.tsx layout, '
                      'navigation shell and routes (incl. sign-in if the requirements have accounts); the home page and the '
                      'signed-in landing page listing the main records as links; backend/src/app.js wiring small modules; ONE '
                      'store/data-model module; ONE seed module that creates every shared seed record from the QUOTED VALUES and the '
                      'ARCHITECTURE PLAN (accounts with passwords, containers, main records, memberships/permissions and '
                      'relationships) so later units only extend it.\n') if not accepted else ''
        specs_text = loop.prompt_specs(unit['id']) if loop is not None else ''
        # Stable prefix first (contract, global contracts, seeds, plan, outline, shared flows, then source ordered
        # stable->recent) so the provider's prefix cache serves most input tokens; everything unit-specific comes last.
        task = (task_header('source_generation', '(unit %d of %d)' % (position, len(units))) +
                'ALREADY IMPLEMENTED UNITS: ' + json.dumps(accepted) +
                '\n\n' + foundation +
                ('DECLARED DEPENDENCY CONTRACTS (preserve their behavior and data shapes):\n' + dep_text + '\n\n' if dep_text else '') +
                'CURRENT UNIT (implement it completely now):\n' + unit_text +
                ('\n\nACCEPTANCE TESTS FOR THIS UNIT (written independently from the requirements; they will be run against your '
                 'implementation right after this step, so every control, label, message and navigation path they use must exist):\n'
                 + specs_text if specs_text else ''))
        prompt = head + source_section(root, context) + task
        readable = set(context['files'])
        staged = call('source_generation', prompt, lambda d: d, 1800, max_tokens=32000,
                      policy_stage=('source_generation_retry' if retry else
                                    'source_generation_first' if not accepted else profile['reasoning_stage']),
                      parser=block_parser(root, readable, protected_paths()), attempt_timeout=1200, truncation_hint=truncation_hint())
        if staged is None:
            row['status'] = 'no_valid_source'
            print(f"[evogit-unit] {unit['id']} ({position}/{len(units)}) no usable model answer{' on retry' if retry else ''}", flush=True)
            persist()
            return False
        row['warnings'] = staged['warnings'][:10] + staged['errors'][:10]
        if not staged['staged'] and staged.get('no_changes'):
            # An inherited application (evolution stage) may already satisfy even the first unit (935.V1).
            satisfied = bool(accepted) or inherited
            row['status'] = 'already_satisfied' if satisfied else 'no_valid_source'
            if satisfied:
                accepted.append(unit['id'])
                if loop is not None:
                    row['acceptance'] = loop.after_unit(unit, [], deadline, allow_repair=False)
            persist()
            return False
        write_staged(root, staged['staged'])
        rejected = list(staged.get('rejected') or [])
        if staged.get('incomplete') or staged.get('truncated'):
            row['continued'] = True
            more_context = source_context(root, limit=profile['context_limit'], focus=dep_focus[:18], unit_text=unit_text,
                                          recent=list(staged['staged']))
            more = call('source_continuation', head + source_section(root, more_context) + task +
                        '\n\nPREVIOUS PARTIAL ANSWER: your last answer for this unit was cut off. These blocks were already applied '
                        'and appear in CURRENT SOURCE above: ' + json.dumps(sorted(staged['staged'])) +
                        '. These were cut off and discarded: ' + json.dumps(staged.get('incomplete', [])) +
                        '. Output ONLY the remaining blocks needed to finish this unit.',
                        lambda d: d, 1500, max_tokens=32000, parser=block_parser(root, set(more_context['files']), protected_paths()),
                        attempt_timeout=1200, truncation_hint=truncation_hint(), policy_stage=profile['continuation_stage'])
            if more:
                write_staged(root, more['staged'])
                rejected += list(more.get('rejected') or [])
        if rejected:
            # V8 silently dropped these (8 units of the larger task lost part of their change).
            row['rejected_blocks'] = [f"{r.get('path')}: {r.get('error')}" for r in rejected][:10]
            fix_context = source_context(root, limit=profile['context_limit'], focus=[r['path'] for r in rejected] + dep_focus[:10],
                                         unit_text=unit_text, recent=changed_paths(before, source_snapshot(root)))
            fixed = call('source_continuation', head + source_section(root, fix_context) + task +
                         '\n\nYOUR ANSWER FOR THIS UNIT WAS APPLIED EXCEPT THESE BLOCKS, WHICH WERE REJECTED (the target files '
                         'are shown in full in CURRENT SOURCE above, including your applied changes):\n' + rejected_text(rejected) +
                         '\n\nOutput ONLY blocks that complete the rejected changes against the CURRENT SOURCE (EDIT OLD text copied '
                         'exactly with enough context to be unique, or complete FILE blocks). If they are no longer needed, output '
                         '<<<NO CHANGES>>>.',
                         lambda d: d, 1200, max_tokens=24000, parser=block_parser(root, set(fix_context['files']), protected_paths()),
                         attempt_timeout=900, truncation_hint=truncation_hint(), policy_stage=profile['continuation_stage'])
            if fixed and fixed.get('staged'):
                write_staged(root, fixed['staged'])
                row['rejected_blocks_fixed'] = sorted(fixed['staged'])
        check = (contract_check(root, staged) if MODE['fixed_host'] else None) or verify(root, deadline)
        check, attempts = repair_until_verified(root, call, deadline, before, check, 'unit ' + unit['id'])
        row['repairs'] = attempts
        if not check['ok']:
            restore_sources(root, before)
            build(root, deadline)
            row.update(status='rolled_back_verification_failure', verification={'stage': check.get('stage'), 'error': check.get('error', '')[:1500]})
            print(f"[evogit-unit] {unit['id']} ({position}/{len(units)}) ROLLED BACK after {len(attempts)} repairs: "
                  f"{check.get('stage')}: {check.get('error', '').splitlines()[0][:200] if check.get('error') else ''}", flush=True)
            persist()
            return False
        changed = changed_paths(before, source_snapshot(root))
        state['recent'] = changed[-12:]
        files_by_unit[unit['id']] = changed
        accepted.append(unit['id'])
        row.update(status='verified_build_and_runtime', render=(check.get('smoke') or {}).get('render', {}).get('status'),
                   changed_files=changed)
        print(f"[evogit-unit] {unit['id']} ({position}/{len(units)}) verified{' on retry' if retry else ''}; {len(changed)} files changed"
              + (f"; {len(attempts)} build repairs" if attempts else '')
              + (f"; {len(rejected)} rejected blocks re-requested" if rejected else '')
              + (f"; warnings: {row['warnings'][:2]}" if row.get('warnings') else ''), flush=True)
        persist()
        if loop is not None:
            # Repair inline only if the remaining units still fit: a missing feature costs more than a fixable one.
            done = max(1, position)
            per_unit = (time.monotonic() - started_units) / done
            needed = per_unit * (len(units) - position) * 1.1 + reserve
            row['acceptance'] = loop.after_unit(unit, changed, deadline, allow_repair=(deadline - time.monotonic()) > needed + 300)
        if on_unit:
            try:
                on_unit(unit['id'])
            except Exception:
                pass
        persist()
        return True

    for position, unit in enumerate(units, 1):
        row = {'id': unit['id'], 'status': 'pending'}
        generation['units'].append(row)
        persist()
        if deadline - time.monotonic() < reserve:
            row['status'] = 'skipped_time_reserve'
            continue
        implement(position, unit, row)
    # A unit whose answers were all lost (dropped connections) or did not verify gets one more try once every
    # other unit exists, at low effort so the call is short. 929.V1 lost 4 of 37 and 4 of 22 units this way.
    for position, unit in enumerate(units, 1):
        row = generation['units'][position - 1]
        if row.get('status') not in ('no_valid_source', 'rolled_back_verification_failure'):
            continue
        if deadline - time.monotonic() < max(1800, reserve * 0.7):
            break
        row['first_attempt'] = row['status']
        print(f"[evogit-unit] {unit['id']} ({position}/{len(units)}) retrying after all units (first attempt: {row['status']})", flush=True)
        implement(position, unit, row, retry=True)
    generation['units_verified'] = len(accepted)
    persist()
    if not accepted:
        raise NoGeneratedSource('No requirement unit produced a verified application')
    return units


def validate_architecture(data):
    if not isinstance(data, dict) or not isinstance(data.get('architecture'), str) or not 40 < len(data['architecture']) < 40000:
        raise ValueError('Return JSON {"architecture": "...", "seed_conflicts": "..."} with a compact plan (40-40000 chars)')
    for key, limit in (('seed_conflicts', 6000), ('conventions', 8000)):
        notes = data.get(key)
        if isinstance(notes, (list, dict)):
            notes = json.dumps(notes, ensure_ascii=False)
        notes = str(notes or '').strip()
        data[key] = '' if notes.lower() in ('', 'none', 'n/a', 'no conflicts', 'none.') else notes[:limit]
    return data


def plan_text(plan):
    notes = plan.get('seed_conflicts') or ''
    conventions = plan.get('conventions') or ''
    return (plan['architecture'] +
            ('\n\nPRODUCT CONVENTIONS (follow them wherever the requirements are silent; requirement text always wins):\n' +
             conventions if conventions else '') +
            ('\n\nSEED CONFLICTS (the seed holds one content per record; tests of the listed '
             'requirements enter their own contents through the UI):\n' + notes if notes else ''))
