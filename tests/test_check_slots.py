"""Offline unit tests for the pure, network-free helpers in check_slots.py.

These tests must stay deterministic and offline: they never make a real network
call and they never read the wall clock. Any time-dependent assertion uses an
explicitly-constructed, fixed datetime.

Several assertions encode *correctness fixes* (the false-positive slot guard in
``is_open`` in particular). If the implementation under test still has a bug,
the corresponding test is expected to fail until the fix lands -- that is the
point of the test, not a flaw in it.
"""

from datetime import datetime, timezone

import pytest

import check_slots


# --------------------------------------------------------------------------- #
# is_open -- the high-value "don't send a FALSE slot alert" guard.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "alt",
    [
        "unavailable",
        "currently unavailable",
        "Unavailable",
        "not available",
    ],
)
def test_is_open_false_for_closed_states(alt):
    # A closed/unavailable day must never read as open. In particular the bare
    # substring "available" inside "unavailable" must NOT trigger a positive.
    assert check_slots.is_open(alt) is False


@pytest.mark.parametrize(
    "alt",
    [
        "受付中",            # Japanese: "accepting"
        "few left",          # English: limited availability
        "qabul qilinmoqda",  # Uzbek: "being accepted"
    ],
)
def test_is_open_true_for_open_states(alt):
    assert check_slots.is_open(alt) is True


@pytest.mark.parametrize("alt", [None, ""])
def test_is_open_false_for_empty(alt):
    assert check_slots.is_open(alt) is False


# --------------------------------------------------------------------------- #
# parse_year_month
# --------------------------------------------------------------------------- #


def test_parse_year_month_valid():
    assert check_slots.parse_year_month("2026年06月") == "2026-06"


def test_parse_year_month_invalid_raises():
    with pytest.raises(ValueError):
        check_slots.parse_year_month("no month here")


# --------------------------------------------------------------------------- #
# add_months -- month arithmetic, including year-boundary rollover.
# --------------------------------------------------------------------------- #


def test_add_months_rolls_over_year_boundary():
    assert check_slots.add_months("2026-11", 3) == "2027-02"


def test_add_months_zero_offset_is_identity():
    assert check_slots.add_months("2026-01", 0) == "2026-01"


# --------------------------------------------------------------------------- #
# month_param_value
# --------------------------------------------------------------------------- #


def test_month_param_value():
    assert check_slots.month_param_value("2026-06") == "2026/06/01"


# --------------------------------------------------------------------------- #
# command_from_text -- normalize Telegram command strings.
# --------------------------------------------------------------------------- #


def test_command_from_text_strips_bot_mention_and_args():
    assert check_slots.command_from_text("/status@MyBot extra") == "/status"


def test_command_from_text_trims_and_lowercases():
    assert check_slots.command_from_text("  /Subscribe ") == "/subscribe"


# --------------------------------------------------------------------------- #
# normalize_open_days -- set -> dict-with-empty-values; dict -> preserved.
# --------------------------------------------------------------------------- #


def test_normalize_open_days_from_set():
    result = check_slots.normalize_open_days({"2026-06-01", "2026-06-02"})
    assert result == {"2026-06-01": "", "2026-06-02": ""}


def test_normalize_open_days_from_dict_preserves_values():
    result = check_slots.normalize_open_days({"2026-06-01": "few left"})
    assert result == {"2026-06-01": "few left"}


# --------------------------------------------------------------------------- #
# should_send_status -- interval gating with a fixed, explicit "now".
# Never calls datetime.now(); every timestamp is constructed by hand.
# --------------------------------------------------------------------------- #


def test_should_send_status_true_when_never_sent():
    now = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)
    assert check_slots.should_send_status(None, 3600, now) is True


def test_should_send_status_false_before_interval():
    last = "2026-06-25T11:30:00+00:00"
    now = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)  # 1800s later
    assert check_slots.should_send_status(last, 3600, now) is False


def test_should_send_status_true_at_interval_boundary():
    last = "2026-06-25T11:00:00+00:00"
    now = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)  # exactly 3600s
    assert check_slots.should_send_status(last, 3600, now) is True


def test_should_send_status_true_past_interval():
    last = "2026-06-25T10:00:00+00:00"
    now = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)  # 7200s later
    assert check_slots.should_send_status(last, 3600, now) is True
