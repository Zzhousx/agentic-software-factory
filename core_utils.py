"""Small shared helpers: requirement loading, model client, template copy, git.

928.V1 replaces the 434 KB legacy engine (evogit_agent.py), which bundled prebuilt apps for
other benchmark tasks. Those were never used for the hackathon tasks and the rules prohibit
prebuilt task pages, so everything the agent ships is now generated from the requirements.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml


def load_requirements(raw: str) -> tuple[Any, list[dict[str, Any]]]:
    """Parse published YAML, repairing only known adjacent-key indentation slips."""
    repairs: list[dict[str, Any]] = []
    try:
        return yaml.safe_load(raw), repairs
    except yaml.YAMLError:
        lines = raw.splitlines()
        for index in range(1, len(lines)):
            prior = re.match(r"^(\s*)- keyword:", lines[index - 1])
            if prior and re.match(r"^\s*content:", lines[index]):
                fixed = prior.group(1) + "  " + lines[index].lstrip()
                if fixed != lines[index]:
                    lines[index] = fixed
                    repairs.append({"line": index + 1, "kind": "scenario_content_indentation"})
            prior_type = re.match(r"^(\s*)type:\s*(?:ATOMIC|FOLDER)\s*$", lines[index - 1])
            if prior_type and re.match(r"^\s*description:", lines[index]):
                fixed = prior_type.group(1) + lines[index].lstrip()
                if fixed != lines[index]:
                    lines[index] = fixed
                    repairs.append({"line": index + 1, "kind": "description_indentation"})
        return yaml.safe_load("\n".join(lines)), repairs


def make_client():
    from openai import OpenAI
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    base = os.environ.get("OPENAI_BASE_URL", "").strip()
    model = os.environ.get("MODEL", "").strip()
    if not key or not base or not model:
        raise RuntimeError("OPENAI_API_KEY, OPENAI_BASE_URL and MODEL are required")
    return OpenAI(api_key=key, base_url=base), model


def copy_template(template_dir: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted(template_dir.iterdir()):
        if source.name == "template.yaml":
            continue
        destination = output_dir / source.name
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)


def git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=120)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def requirement_leaves(node) -> list[dict]:
    if not isinstance(node, dict):
        return []
    children = [c for c in node.get("children", []) or [] if isinstance(c, dict)]
    if not children:
        return [node] if node.get("id") else []
    found = []
    for child in children:
        found += requirement_leaves(child)
    return found
