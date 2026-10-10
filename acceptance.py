"""Requirement-derived Playwright acceptance tests (927.V1).

The agent derives local Playwright checks only from the supplied requirement scenarios.
These checks provide an internal black-box feedback loop while units are implemented;
failures are returned with Playwright diagnostics, accessibility snapshots, browser errors
and server errors for repair.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from app_runtime import child_env, ensure_playwright, running_app
from requirement_context import clean, global_contracts, seed_index
from source_blocks import parse_blocks

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "acceptance_assets"
SPEC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.spec\.ts$")
LEAF_ID = re.compile(r"^\s*([A-Za-z]+-[0-9][0-9A-Za-z._-]*?)(?=[:\s]|$)")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
CROSS_CUTTING = re.compile(r"sign[ -]?in|log[ -]?in\b|login|log[ -]?out|sign[ -]?up|register|account|password|credential|"
                           r"seed|navigation|header|sidebar|home ?page|landing|dashboard|profile", re.I)


def leaves(node) -> list[dict]:
    if not isinstance(node, dict):
        return []
    children = [c for c in node.get("children", []) or [] if isinstance(c, dict)]
    if not children:
        return [node] if node.get("id") else []
    found = [node] if node.get("scenarios") else []
    for child in children:
        found += leaves(child)
    return found


_SENTENCE = re.compile(r"(?<=[.;!?])\s+(?=[A-Z`\u201c\"(])")


def describe_leaf(node: dict, limit: int = 12000) -> str:
    """Requirement text for prompts. Scenario steps repeat long boilerplate verbatim; sentences already shown for
    this requirement are left out so that EVERY scenario fits. (Before 931.V1 a 2500-4000 char cut hid 40 of 100
    scenarios of one task and 20 of 100 of the other from the test writer, and from repair prompts.)"""
    lines = [f"{node.get('id')} {node.get('name', '')}".strip()]
    if node.get("description"):
        lines.append(clean(node["description"]))
    scenarios = [s for s in node.get("scenarios") or [] if isinstance(s, dict)]
    seen: set[str] = set()
    for index, scenario in enumerate(scenarios, 1):
        lines.append(f"Scenario {index}/{len(scenarios)}: " + str(scenario.get("name", "")))
        for step in scenario.get("steps") or []:
            if not isinstance(step, dict):
                continue
            parts = [part for part in _SENTENCE.split(" ".join(str(step.get("content", "")).split())) if part]
            fresh = []
            for part in parts:
                if part not in seen:
                    fresh.append(part)
                    seen.add(part)
            body = " ".join(fresh) if fresh else "(same as above)"
            lines.append(f"  {step.get('keyword', '')} {body}")
    text = "\n".join(lines)
    return text[:limit]


def scenario_count(node: dict) -> int:
    return len([s for s in node.get("scenarios") or [] if isinstance(s, dict)])


def cross_cutting_text(document, limit: int = 7000) -> str:
    """Auth/seed/navigation requirements every unit and every test depends on."""
    parts, size = [], 0
    for node in leaves(document):
        head = f"{node.get('name', '')} {node.get('description', '')}"
        if CROSS_CUTTING.search(head):
            text = describe_leaf(node, 1800)
            if size + len(text) > limit:
                break
            parts.append(text)
            size += len(text)
    return "\n\n".join(parts)


TEST_CONVENTIONS = """You are an independent QA engineer. Write Playwright acceptance tests (TypeScript) for the requirements below,
as black-box checks derived only from the supplied requirement text and scenarios.

RULES
- Exactly ONE test per listed scenario (Scenario k/N -> the k-th test), in order. Title MUST start with the requirement id
  and the scenario number: test('REQ-2.3 #2: Reject empty name', async ({ page }) => { ... }).
  Put all tests of one requirement id in the file named <id>.spec.ts (e.g. REQ-2.3.spec.ts).
- Scenario names and steps may be partly generic placeholders (e.g. "the requested workflow"). Then use the concrete words
  that remain in the scenario (a key name, a file type, a record name) and the requirement DESCRIPTION to decide what the
  scenario checks, giving each scenario a DIFFERENT behavior of the description: the main success path, alternative
  inputs or entry paths, each validation/error rule with its exact message, permission denials, state attributes, and
  persistence after reload. Across all tests of a requirement, every verifiable statement of its description (names,
  roles, states, messages, counts, ordering, persistence) must be asserted by at least one test.
- Import exactly: import { test, expect } from '@playwright/test'; import * as h from './evogit-helpers';
- Every test starts in a fresh browser at await page.goto('/') (baseURL is the app root). Navigate through visible UI
  (links, buttons, tabs, menus named in the requirements). Use page.goto('/path') only for URLs the requirements state.
- If a scenario needs a signed-in user, sign in through the UI with the seeded credentials given in the requirements,
  using the sign-in controls the requirements name. Never call APIs directly, never read localStorage/cookies.
- Every test starts from the freshly seeded application state (the server data is reset before each test); never rely
  on data created by another test. Use the literal example values from the scenario text.
- Seeded records named in GIVEN (accounts, named containers or items) exist at the start. If SEED CONFLICTS below say
  that the seed does not hold the contents your scenario describes (field or cell values of a seeded record), first put
  those contents in place through the UI exactly as a user would, then perform the WHEN steps.
- Locate elements the way the requirements describe them: when a role is stated use
  page.getByRole('<role>', { name: h.exact('<Name>') }); otherwise use the helpers:
    await h.click(scope, 'Name')          // unique visible button/link/tab/menuitem/option/checkbox or exact text
    await h.fill(scope, 'Label', 'value') // unique field by exact label, placeholder or accessible name
    await h.select(scope, 'Label', 'Option')
    await h.expectText(scope, 'text')     // visible text containing this (case-insensitive)
    await h.expectNoText(scope, 'text')
    h.region(page, 'dialog', 'Name')      // container locator by role (+ exact name)
    h.itemWith(page, 'row', 'text')       // row/article/listitem/option/gridcell containing text
    h.cell(page, 'A1')                    // grid/table cell by its accessible name (when the requirements name cells)
    h.exact('Name')                       // exact, case-insensitive name matcher for getByRole/getByLabel
  scope is page or a container locator. Assert with expect(...).toBeVisible(), toHaveText, toHaveValue, toHaveCount, toHaveURL.
- The GLOBAL CONTRACTS are binding for every test: where they define how elements are exposed (roles, accessible names,
  selection/state attributes, which control opens which menu or page, how accounts sign in), locate and assert exactly that
  way, even when the scenario text below is less specific. Never invent a different role or name for such elements.
- Reach records only through navigation the requirements describe. Where they give none, open the record by clicking its
  exact name in a list reachable from the home page (visitors) or from the page shown after sign-in.
- Assert on the specific element that shows a value (a named cell, row, heading, field or status message), not with a
  page-wide getByText for short values that may also appear elsewhere (lists, editors, formula bars, menus).
- Assert the THEN outcome precisely (messages, visible records, counts, values, disabled states) using the exact quoted strings.
- No test.skip, no conditional assertions, no page.waitForTimeout longer than 500 ms, no screenshots.

OUTPUT: only FILE blocks, one per requirement id, no prose:
<<<FILE REQ-2.3.spec.ts>>>
...spec source...
<<<END FILE>>>"""


def spec_prompt(unit: dict, app_info: str, shared: str, feedback: str = "", contracts: str = "", seeds: str = "",
                seed_notes: str = "") -> str:
    """Stable prefix first (conventions, global contracts, seed index, shared flows) for the provider's prompt cache;
    the unit's parent context and requirements last. Parent descriptions are no longer cut to 600 chars."""
    unit_leaves = leaves(unit["requirement"])
    context = "\n".join(f"{a.get('id', '')} {a.get('name', '')}: {clean(a.get('description', ''))[:3000]}"
                        for a in unit.get("ancestors", []) if clean(a.get("description", ""))[:200] not in contracts)
    return ("TASK STAGE: test_generation\n" + TEST_CONVENTIONS +
            ("\n\nGLOBAL CONTRACTS (root and module rules that apply to every feature):\n" + contracts if contracts
             else "\n\nAPPLICATION: " + app_info[:3000]) +
            ("\n\nVALUES AND NAMES QUOTED IN THE REQUIREMENTS (value used by N: requirement ids; a value shared by several requirements is ONE seed record or ONE UI name everywhere):\n" + seeds if seeds else "") +
            ("\n\nSEED CONFLICTS (from planning; binding for tests):\n" + seed_notes if seed_notes else "") +
            ("\n\nSHARED FLOWS (sign-in, seed data, navigation) defined elsewhere in the requirements:\n" + shared if shared else "") +
            ("\n\nPARENT CONTEXT:\n" + context if context else "") +
            "\n\nREQUIREMENTS TO TEST (write tests for exactly these ids: " + ", ".join(str(l.get("id")) for l in unit_leaves) + "):\n" +
            "TESTS REQUIRED: " + ", ".join(f"{l.get('id')}: {max(1, scenario_count(l))}" for l in unit_leaves) + "\n\n" +
            "\n\n".join(describe_leaf(l, 12000) for l in unit_leaves) + feedback)


_TEST_CALL = re.compile(r"\btest(?:\.only)?\(\s*['\"`]")


def spec_parser(leaf_ids: list[str], expected: dict[str, int] | None = None):
    """FILE blocks -> {name: source}. The first answer with clearly fewer tests than scenarios is sent back once."""
    ids = set(leaf_ids)
    state = {"short_rejected": False}

    def parse(text, finish_reason):
        parsed = parse_blocks(text, finish_reason)
        files = {}
        for op in parsed["ops"]:
            name = op.get("path", "").strip().split("/")[-1]
            content = op.get("content") or op.get("new") or ""
            if op.get("kind") != "file" or not SPEC_NAME.match(name) or "test(" not in content:
                continue
            files[name] = content
        if not files:
            raise ValueError("no <<<FILE <id>.spec.ts>>> blocks with Playwright tests found")
        unknown = [n for n in files if n[:-len(".spec.ts")] not in ids]
        if len(unknown) == len(files) and ids:
            raise ValueError("spec files must be named after the requirement ids: " + ", ".join(sorted(ids))[:300])
        if expected and not state["short_rejected"]:
            short = []
            for leaf_id, wanted in expected.items():
                written = len(_TEST_CALL.findall(files.get(f"{leaf_id}.spec.ts", "")))
                if written < wanted:
                    short.append(f"{leaf_id}: {written} of {wanted}")
            if short and finish_reason != "length":
                state["short_rejected"] = True
                raise ValueError("write exactly one test per scenario; too few tests for " + "; ".join(short)[:400] +
                                 ". Answer again with ALL files and all tests.")
        return files
    return parse


def prepare_spec(content: str) -> str:
    """Route generated specs through the capture fixture."""
    content = re.sub(r"from\s+['\"]@playwright/test['\"]", "from './evogit-fixtures'", content)
    return content


def leaf_of(title: str, file: str) -> str:
    match = LEAF_ID.match(title or "")
    if match:
        return match.group(1).rstrip(".")
    return (file or "").rsplit("/", 1)[-1].replace(".spec.ts", "")


class SuiteRunner:
    """Runs a set of spec files against a freshly started app (isolated data dir)."""

    def __init__(self, deadline=None):
        self.tool = ensure_playwright(deadline)
        self.runs = 0

    @property
    def available(self) -> bool:
        return bool(self.tool.get("ok") and self.tool.get("cli"))

    def _workdir(self, specs: dict[str, str]) -> Path:
        base = Path(self.tool.get("folder") or tempfile.gettempdir()) / "suites"
        base.mkdir(parents=True, exist_ok=True)
        self.runs += 1
        work = Path(tempfile.mkdtemp(prefix=f"run{self.runs}-", dir=base))
        for asset in ("evogit-helpers.ts", "evogit-fixtures.ts"):
            shutil.copy2(ASSETS / asset, work / asset)
        for name, content in specs.items():
            (work / name).write_text(prepare_spec(content), encoding="utf-8")
        (work / "playwright.config.js").write_text("""module.exports = {
  testDir: '.', testMatch: '*.spec.ts', timeout: 25000, expect: { timeout: 4000 }, workers: 1, retries: 0, fullyParallel: false,
  reporter: [['json', { outputFile: 'report.json' }]],
  use: { baseURL: process.env.EVOGIT_BASE_URL, actionTimeout: 5000, navigationTimeout: 12000, headless: true,
         permissions: ['clipboard-read', 'clipboard-write'],
         viewport: { width: 1280, height: 720 }, trace: 'off', screenshot: 'off', video: 'off',
         launchOptions: Object.assign({ args: ['--no-sandbox', '--disable-dev-shm-usage'] },
                                      process.env.EVOGIT_CHROMIUM_PATH ? { executablePath: process.env.EVOGIT_CHROMIUM_PATH } : {}) },
};
""", encoding="utf-8")
        return work

    def _list(self, specs: dict[str, str]):
        work = self._workdir(specs)
        node = shutil.which("node") or "node"
        try:
            subprocess.run([node, self.tool["cli"], "test", "--list", "--config", str(work / "playwright.config.js")],
                           cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                           env=child_env({"EVOGIT_BASE_URL": "http://127.0.0.1:9"}))
            return json.loads((work / "report.json").read_text(encoding="utf-8"))
        except Exception:
            return None
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def load_errors(self, specs: dict[str, str]) -> dict[str, str]:
        """Specs that do not load (syntax/import errors) or define no tests, via `playwright test --list`.
        One broken file makes Playwright list nothing, so broken files are peeled off and the rest re-listed."""
        if not self.available or not specs:
            return {}
        errors: dict[str, str] = {}
        remaining = dict(specs)
        for _ in range(4):
            if not remaining:
                break
            report = self._list(remaining)
            if report is None:
                errors.update({name: "could not list tests" for name in remaining})
                break
            found = {}
            for item in report.get("errors", []) or []:
                location = (item.get("location") or {}).get("file", "")
                name = Path(location).name if location else ""
                message = ANSI.sub("", item.get("message", ""))[:1200]
                for spec in remaining:
                    if spec == name or (not name and spec in message):
                        found[spec] = message
            if found:
                errors.update(found)
                remaining = {k: v for k, v in remaining.items() if k not in found}
                continue
            if report.get("errors"):
                errors.update({name: ANSI.sub("", report["errors"][0].get("message", ""))[:800] for name in remaining})
                break
            listed = {Path(s.get("file", "")).name for s in _walk_specs(report)}
            errors.update({spec: "file defines no runnable tests" for spec in remaining if spec not in listed})
            break
        return errors

    def run(self, root: Path, specs: dict[str, str], timeout: int = 1800, _retry: bool = True) -> dict:
        if not self.available:
            return {"status": "unavailable", "reason": self.tool.get("reason"), "tests": {}}
        if not specs:
            return {"status": "executed", "tests": {}, "server_errors": []}
        work = self._workdir(specs)
        node = shutil.which("node") or "node"
        started = time.monotonic()
        # Every local test starts from the seeded state (host reset endpoint, see backend/src/index.js).
        with running_app(Path(root), extra_env={"EVOGIT_TEST_RESET": "1"}) as app:
            if app.error:
                shutil.rmtree(work, ignore_errors=True)
                return {"status": "app_failed", "reason": app.error + "\n" + app.log_tail(1500), "tests": {}}
            try:
                subprocess.run([node, self.tool["cli"], "test", "--config", str(work / "playwright.config.js")],
                               cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, env=child_env({"EVOGIT_BASE_URL": app.base_url, "EVOGIT_RESET": "1"}))
            except subprocess.TimeoutExpired:
                pass
            state = app.host_state() or {}
            server_log = app.log_tail(3000)
        try:
            report = json.loads((work / "report.json").read_text(encoding="utf-8"))
        except Exception as error:
            shutil.rmtree(work, ignore_errors=True)
            return {"status": "runner_error", "reason": type(error).__name__, "tests": {}}
        load_failed = {Path((item.get("location") or {}).get("file", "")).name for item in report.get("errors", []) or []
                       if (item.get("location") or {}).get("file")}
        if load_failed and not list(_walk_specs(report)) and set(specs) - load_failed and _retry:
            retry = self.run(root, {k: v for k, v in specs.items() if k not in load_failed}, timeout, _retry=False)
            for item in report.get("errors", []) or []:
                location = (item.get("location") or {}).get("file", "")
                file = Path(location).name if location else "unknown"
                retry.setdefault("tests", {})[f"{file}::<load>"] = {"file": file, "title": "<load error>", "leaf": file.replace(".spec.ts", ""),
                                                                    "status": "load_error", "error": ANSI.sub("", item.get("message", ""))[:1500], "capture": {}}
            shutil.rmtree(work, ignore_errors=True)
            return retry
        tests = {}
        for spec in _walk_specs(report):
            file = Path(spec.get("file", "")).name
            for test in spec.get("tests", []) or []:
                results = test.get("results") or [{}]
                last = results[-1]
                status = last.get("status") or test.get("status") or "unknown"
                key = f"{file}::{spec.get('title', '')}"
                error = ""
                if last.get("error"):
                    error = ANSI.sub("", last["error"].get("message", "") or "")
                elif last.get("errors"):
                    error = ANSI.sub("", (last["errors"][0] or {}).get("message", "") or "")
                capture = {}
                for attachment in last.get("attachments", []) or []:
                    if attachment.get("name") == "evogit-failure" and attachment.get("body"):
                        try:
                            capture = json.loads(base64.b64decode(attachment["body"]).decode("utf-8", "replace"))
                        except Exception:
                            capture = {}
                if status not in ("passed", "expected") and TEST_DEFECT.match(error.strip()):
                    status = "test_error"   # the spec's own code is wrong (e.g. an assertion called without expect)
                tests[key] = {"file": file, "title": spec.get("title", ""), "leaf": leaf_of(spec.get("title", ""), file),
                              "status": "passed" if status in ("passed", "expected") else status,
                              "error": error[:2500], "capture": capture,
                              "line": ((last.get("error") or {}).get("location") or {}).get("line")}
        for item in report.get("errors", []) or []:
            location = (item.get("location") or {}).get("file", "")
            file = Path(location).name if location else "unknown"
            tests[f"{file}::<load>"] = {"file": file, "title": "<load error>", "leaf": file.replace(".spec.ts", ""),
                                        "status": "load_error", "error": ANSI.sub("", item.get("message", ""))[:1500], "capture": {}}
        shutil.rmtree(work, ignore_errors=True)
        return {"status": "executed", "tests": tests, "seconds": round(time.monotonic() - started, 1),
                "server_errors": (state.get("runtime_errors") or [])[-8:], "server_log": server_log}


def _walk_specs(report: dict):
    stack = list(report.get("suites", []) or [])
    while stack:
        suite = stack.pop()
        for spec in suite.get("specs", []) or []:
            spec.setdefault("file", suite.get("file", ""))
            yield spec
        stack.extend(suite.get("suites", []) or [])


def passed_keys(result: dict) -> set[str]:
    return {k for k, t in (result.get("tests") or {}).items() if t["status"] == "passed"}


TEST_DEFECT = re.compile(r"^(?:TypeError|ReferenceError|SyntaxError): (?!.*\b(?:fetch|Failed to fetch)\b)")


def failed_tests(result: dict) -> dict:
    """Behaviour failures only: specs that do not load or whose own code throws are test defects, not application defects."""
    return {k: t for k, t in (result.get("tests") or {}).items()
            if t["status"] not in ("passed", "load_error", "skipped", "test_error")}


_SIGNATURE_PATTERNS = [
    re.compile(r"(Not found: (?:control|field) \S+)"),
    re.compile(r"(Ambiguous (?:control|field) \S+)"),
    re.compile(r"waiting for (getBy[A-Za-z]+\([^\n]{0,120}?\))"),
    re.compile(r"(Locator: [^\n]{0,140})"),
    re.compile(r"(Expected (?:string|pattern|substring)?:? [^\n]{0,100})"),
]


def signature(test: dict) -> str:
    error = test.get("error", "")
    for pattern in _SIGNATURE_PATTERNS:
        match = pattern.search(error)
        if match:
            return re.sub(r"\d+", "#", match.group(1))[:160]
    first = error.strip().splitlines()[0] if error.strip() else test.get("status", "failed")
    return re.sub(r"\d+", "#", first)[:120]


_EXPECTED = re.compile(r"^\s*Expected(?: [a-z]+)?:\s*(.+)$", re.M)
_RECEIVED = re.compile(r"^\s*Received(?: [a-z]+)?:\s*(.+)$", re.M)


def error_excerpt(test: dict, limit: int = 180) -> str:
    """One line per failure for the platform log: the error's first line plus expected/received values."""
    error = (test.get("error") or "").strip()
    if not error:
        return str(test.get("status", "failed"))
    first = error.splitlines()[0]
    parts = [first[:110]]
    expected, received = _EXPECTED.search(error), _RECEIVED.search(error)
    if expected:
        parts.append("expected " + expected.group(1).strip()[:50])
    if received:
        parts.append("received " + received.group(1).strip()[:50])
    return " | ".join(parts)[:limit]


def group_failures(result: dict) -> list[list[str]]:
    """Failures sharing a cause (same missing control, same assertion) first: one fix, many tests."""
    groups: dict[str, list[str]] = {}
    for key, test in failed_tests(result).items():
        groups.setdefault(signature(test), []).append(key)
    return sorted(groups.values(), key=lambda keys: -len(keys))


def digest(result: dict, keys: list[str], specs: dict[str, str], limit: int = 26000) -> str:
    """Failure report for a repair prompt: spec source, error, what the page showed, runtime errors."""
    parts, size = [], 0
    shown_files = set()
    for key in keys:
        test = result["tests"].get(key)
        if not test:
            continue
        block = [f"### FAILED: {test['title']}  ({test['file']}{', line ' + str(test['line']) if test.get('line') else ''})",
                 "ERROR:\n" + test.get("error", "")[:1800]]
        capture = test.get("capture") or {}
        if capture.get("url"):
            block.append("PAGE URL AT FAILURE: " + capture["url"])
        if capture.get("errors"):
            block.append("BROWSER ERRORS / API RESPONSES >= 400 (a 4xx is expected when the scenario is a rejected action): "
                         + " | ".join(capture["errors"][:10]))
        if capture.get("aria"):
            block.append("ACCESSIBILITY SNAPSHOT AT FAILURE (what the test could see):\n" + capture["aria"][:3500])
        if test["file"] not in shown_files and test["file"] in specs:
            block.append(f"SPEC SOURCE {test['file']}:\n" + specs[test["file"]][:5000])
            shown_files.add(test["file"])
        text = "\n".join(block)
        if size + len(text) > limit:
            break
        parts.append(text)
        size += len(text)
    if result.get("server_errors"):
        parts.append("SERVER ERRORS DURING THE RUN:\n" + "\n".join(result["server_errors"][-6:])[:4000])
    return "\n\n".join(parts)


class SpecGenerator(threading.Thread):
    """Writes specs for every unit in the background (black box: requirements only)."""

    def __init__(self, units, document, call, store_dir: Path, runner: SuiteRunner, app_info: str, persist, deadline):
        super().__init__(daemon=True)
        self.units, self.document, self.call = units, document, call
        self.store_dir, self.runner, self.app_info = Path(store_dir), runner, app_info
        self.persist, self.deadline = persist, deadline
        self.specs: dict[str, dict[str, str]] = {}
        self.ready: dict[str, threading.Event] = {u["id"]: threading.Event() for u in units}
        self.report: dict[str, dict] = {}
        self.stop = threading.Event()
        self.shared = cross_cutting_text(document)
        self.contracts = global_contracts(document)
        self.seeds = seed_index(document, 3000)
        self.seed_notes = ""        # seed conflicts resolved by the planning step (set before start())

    def all_specs(self) -> dict[str, str]:
        merged = {}
        for unit in self.units:
            merged.update(self.specs.get(unit["id"], {}))
        return merged

    def run(self):
        self.store_dir.mkdir(parents=True, exist_ok=True)
        for unit in self.units:
            if self.stop.is_set() or time.monotonic() > self.deadline:
                break
            try:
                self._generate(unit)
            except Exception as error:  # never let the background thread kill the run
                self.report[unit["id"]] = {"status": "error", "error": f"{type(error).__name__}: {error}"[:300]}
            finally:
                self.ready[unit["id"]].set()
        for event in self.ready.values():
            event.set()

    def _generate(self, unit):
        ids = [str(l.get("id")) for l in leaves(unit["requirement"]) if l.get("id")]
        if not ids:
            self.report[unit["id"]] = {"status": "no_leaves"}
            return
        feedback = ""
        expected = {str(l.get("id")): max(1, scenario_count(l)) for l in leaves(unit["requirement"]) if l.get("id")}
        for attempt in range(2):
            files = self.call("test_generation", spec_prompt(unit, self.app_info, self.shared, feedback, self.contracts, self.seeds,
                                                          self.seed_notes),
                              lambda d: d, 1200,
                              max_tokens=32000, parser=spec_parser(ids, expected), attempt_timeout=900,
                              truncation_hint="Write shorter tests: fewer comments, only the scenario steps and assertions.")
            if not files:
                self.report[unit["id"]] = {"status": "no_valid_specs"}
                return
            broken = self.runner.load_errors(files) if self.runner.available else {}
            good = {n: c for n, c in files.items() if n not in broken}
            if good:
                self.specs[unit["id"]] = {**self.specs.get(unit["id"], {}), **good}
                for name, content in good.items():
                    (self.store_dir / name).write_text(content, encoding="utf-8")
            if not broken:
                break
            feedback = ("\n\nTHESE FILES FAILED TO LOAD; rewrite them:\n" +
                        "\n".join(f"{n}: {e[:600]}" for n, e in broken.items()))
        self.report[unit["id"]] = {"status": "ready" if self.specs.get(unit["id"]) else "failed",
                                   "files": sorted(self.specs.get(unit["id"], {}))}

    def rewrite(self, unit, defects: dict[str, str]) -> dict[str, str]:
        """Rewrite spec files whose own code threw (TypeError etc.). Returns the replaced files."""
        ids = [str(l.get("id")) for l in leaves(unit["requirement"]) if l.get("id")]
        current = self.specs.get(unit["id"], {})
        feedback = ("\n\nTHESE TEST FILES WERE ALREADY WRITTEN BUT THEIR OWN CODE THREW ERRORS (defects in the tests, not in the "
                    "application). Rewrite exactly these files completely, fixing the test code:\n" +
                    "\n".join(f"=== {name} (error: {error[:400]}) ===\n{current.get(name, '')[:5000]}" for name, error in defects.items()))
        files = self.call("test_generation", spec_prompt(unit, self.app_info, self.shared, feedback, self.contracts, self.seeds,
                                                         self.seed_notes),
                          lambda d: d, 900, max_tokens=24000, parser=spec_parser(ids), attempt_timeout=900,
                          truncation_hint="Write shorter tests: fewer comments, only the scenario steps and assertions.")
        if not files:
            return {}
        files = {n: c for n, c in files.items() if n in defects}
        broken = self.runner.load_errors(files) if (files and self.runner.available) else {}
        good = {n: c for n, c in files.items() if n not in broken}
        if good:
            self.specs[unit["id"]] = {**current, **good}
            for name, content in good.items():
                (self.store_dir / name).write_text(content, encoding="utf-8")
        return good
