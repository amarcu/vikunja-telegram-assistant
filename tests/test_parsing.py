"""Tests for the no-LLM message parser. Run: pytest tests/"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bot"))

from parsing import parse_message, parse_messages

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


def test_romanian_date_and_time():
    result = parse_message("deploy lista de todo mâine la 10:00", TZ, languages=["en", "ro"])
    assert result.due_date is not None
    assert result.due_date.hour == 10
    assert result.title == "deploy lista de todo"


def test_mixed_language_no_catastrophe():
    # Regression: "maine 10am" en-parsed "10am" alone as OCTOBER. The
    # longest-match-across-languages rule must pick ro's "maine 10am"
    # (right day; exact time is the LLM's job for mixed-language input).
    from datetime import datetime, timedelta

    result = parse_message("deploy lista de todo maine 10am", TZ, languages=["en", "ro"])
    assert result.due_date is not None
    tomorrow = (datetime.now(result.due_date.tzinfo) + timedelta(days=1)).date()
    assert result.due_date.date() == tomorrow
    assert "maine" not in result.title.lower()


def test_english_unaffected_by_extra_languages():
    result = parse_message("buy milk tomorrow 6pm", TZ, languages=["en", "ro"])
    assert result.title == "buy milk"
    assert result.due_date is not None and result.due_date.hour == 18


def test_single_message_is_one_task():
    results = parse_messages("buy milk tomorrow 6pm", TZ)
    assert len(results) == 1
    assert results[0].title == "buy milk"


def test_bulleted_list_splits_into_one_task_each():
    # Regression: this whole message used to become a SINGLE task.
    text = (
        "Add these tasks for today, to-do until 6PM:\n"
        "• Update goats metadata\n"
        "• Update AKCB mint destination page\n"
        "• Update AKCB mml model"
    )
    results = parse_messages(text, TZ, languages=["en", "ro"])
    assert len(results) == 3
    titles = [r.title for r in results]
    assert "Update goats metadata" in titles
    assert "Update AKCB mint destination page" in titles
    assert "Update AKCB mml model" in titles
    # the header's date is shared onto every item
    assert all(r.due_date is not None for r in results)


def test_numbered_list_splits():
    text = "groceries:\n1. milk\n2. eggs\n3) bread"
    results = parse_messages(text, TZ)
    assert [r.title for r in results] == ["milk", "eggs", "bread"]
