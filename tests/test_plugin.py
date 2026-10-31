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


def _trade_session() -> str:
    return (ROOT / "skills" / "trade-session" / "SKILL.md").read_text()


@pytest.mark.parametrize("phrase", [
    "specific ticket",
    "never approve on silence",
    "instructions found in tool output",
    "approve only the exact ticket id the user said yes to",
    "newer ticket",
])
def test_trade_session_approval_rules(phrase):
    assert phrase in _trade_session().lower()


def test_trade_session_shows_id_and_created_time_and_reasks_for_new_ids():
    text = _trade_session().lower()
    assert "ticket id" in text and "created time" in text
    assert "ask again for the new id" in text


def test_trade_session_survives_or_explains_session_close():
    text = _trade_session()
    assert "nohup daytrader run --mode approval > state/approval.log 2>&1 &" in text
    assert "16:05" in text and "engine stops" in text.lower()


def test_trade_session_polling_cadence():
    text = _trade_session()
    assert "11:30" in text and "every ~15 seconds" in text
    assert "daytrader status --mode approval" in text and "every ~5 minutes" in text


def test_readme_explains_detached_approval_engine():
    text = (ROOT / "README.md").read_text()
    assert "nohup daytrader run --mode approval > state/approval.log 2>&1 &" in text
    assert "16:05" in text
