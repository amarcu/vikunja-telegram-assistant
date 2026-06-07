"""Tests for the no-LLM message parser. Run: pytest tests/"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bot"))

from parsing import parse_message

TZ = "Europe/Bucharest"


def test_plain_title():
    result = parse_message("buy milk", TZ)
    assert result.title == "buy milk"
    assert result.due_date is None
    assert result.priority is None
    assert result.project_hint is None


def test_date_extraction():
    result = parse_message("buy milk tomorrow 6pm", TZ)
    assert result.title == "buy milk"
    assert result.due_date is not None
    assert result.due_date.hour == 18


def test_future_preference():
    result = parse_message("dentist friday", TZ)
    assert result.due_date is not None
    assert result.due_date >= datetime.now(result.due_date.tzinfo)


def test_project_and_priority():
    result = parse_message("ship the report friday #work !4", TZ)
    assert result.title == "ship the report"
    assert result.project_hint == "work"
    assert result.priority == 4
    assert result.due_date is not None


def test_numbers_not_eaten_as_dates():
    result = parse_message("buy 2 milk", TZ)
    assert "2" in result.title


def test_relative_time():
    result = parse_message("check the oven in 20 minutes", TZ)
    assert result.due_date is not None
    assert result.title == "check the oven"


def test_no_date_keeps_full_title():
    result = parse_message("refactor the parser module", TZ)
    assert result.title == "refactor the parser module"
    assert result.due_date is None
