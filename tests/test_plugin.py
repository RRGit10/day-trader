import argparse
import json
import re
from pathlib import Path

import pytest

from daytrader.cli import _parser

ROOT = Path(__file__).resolve().parent.parent
SKILLS = sorted((ROOT / "skills").glob("*/SKILL.md"))


def _subcommands() -> set[str]:
    for action in _parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("CLI parser has no subcommands")


@pytest.mark.parametrize("name", ["plugin.json", "marketplace.json"])
def test_manifests_are_valid_json_named_daytrader(name):
    data = json.loads((ROOT / ".claude-plugin" / name).read_text())
    assert data["name"] == "daytrader"


def test_marketplace_lists_the_plugin():
    data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert [p["name"] for p in data["plugins"]] == ["daytrader"]


def test_expected_skills_exist():
    assert {p.parent.name for p in SKILLS} == {
        "trade-session", "scan", "bot-status", "report", "backtest"}


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_frontmatter_has_name_and_description(path):
    text = path.read_text()
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "missing frontmatter"
    fields = dict(
        line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    assert fields["name"].strip() == path.parent.name
    assert fields["description"].strip()


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_skill_commands_exist_in_cli(path):
    valid = _subcommands()
    mentioned = re.findall(
        r"`daytrader(?: --config \S+)? ([a-z][a-z-]*)", path.read_text())
    assert mentioned, "skill mentions no daytrader command"
    for sub in mentioned:
        assert sub in valid, f"`daytrader {sub}` is not a CLI subcommand"


def test_trade_session_requires_explicit_approval():
    text = (ROOT / "skills" / "trade-session" / "SKILL.md").read_text()
    assert "daytrader run --mode approval" in text
    assert "explicit" in text.lower()
