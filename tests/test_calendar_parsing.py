"""Regression tests for the embassy calendar request + grid parsing.

These lock the contract that broke silently in production: the booking site
(a CakePHP app) renders the calendar grid with status icons and ships a per-form
security token. The scraper must (a) pick the calendar form with its _Token, and
(b) read day availability from the icon FILENAME (icon_circle.svg = open), NOT
from the <img alt> text, which this site serves INVERTED (an open day's circle
icon carries alt "Not available / Qabul tugadi"; a disabled day carries alt
"Available / Qabul qilinmoqda"). Verified against a live open slot 2026-06-30.
"""

import json

import check_slots as c


# A trimmed but faithful copy of the booking page: a non-calendar form plus the
# real calendar form carrying the CakePHP _Token[fields]/_Token[unlocked].
BOOKING_PAGE = """
<html><body>
  <form action="/search"><input type="hidden" name="q" value="x"></form>
  <form action="/reservations/calendar?event=20&category=12">
    <input type="hidden" name="_method" value="POST"/>
    <input type="hidden" name="_csrfToken" value="csrf-abc"/>
    <input type="hidden" name="category" value="12"/>
    <input type="hidden" name="event" value="20"/>
    <input type="hidden" name="plan" value="19"/>
    <input type="hidden" name="date" value="2026/06/28"/>
    <input type="hidden" name="disp_type" value="month"/>
    <input type="hidden" name="_Token[fields]" value="tok-fields-123"/>
    <input type="hidden" name="_Token[unlocked]" value=""/>
  </form>
</body></html>
"""


def _grid_cell(day, alt, icon="icon_circle.svg"):
    return (
        '<td><div class="sc_cal_month_itemlist">'
        f'<div class="sc_cal_date"><a href="#" class="js_change_date" data-date="2026/07/0{day}">{day}</a></div>'
        f'<a href="#" class="c_cal_time_cell"><img src="/assets/images/user/{icon}" '
        f'alt="{alt}" data-date="2026/07/0{day}"/></a>'
        '</div></td>'
    )


def test_extract_calendar_form_picks_the_token_form():
    form = c.extract_calendar_form(BOOKING_PAGE, "20")
    assert form is not None
    assert form["event"] == "20"
    assert form["_Token[fields]"] == "tok-fields-123"
    # Every field the CakePHP token covers must be captured for the replay.
    for field in ("_method", "_csrfToken", "category", "plan", "date", "disp_type", "_Token[unlocked]"):
        assert field in form


def test_extract_calendar_form_missing_returns_none():
    assert c.extract_calendar_form("<html><body>no forms</body></html>", "20") is None


def test_slot_is_open_trusts_icon_over_inverted_alt_text():
    base = "/assets/images/user/"
    # Circle icon = OPEN, even though its alt text lies and says "Not available".
    assert c.slot_is_open(base + "icon_circle.svg?1", " Not available / Qabul tugadi") is True
    # Disabled icon = CLOSED, even though its alt text lies and says "Available".
    assert c.slot_is_open(base + "icon_disabled.svg?1", " Available / Qabul qilinmoqda") is False
    # Triangle = few remaining = OPEN.
    assert c.slot_is_open(base + "icon_triangle.svg", "anything") is True
    # An unrecognized icon fails closed: the alt text is never trusted on its own.
    assert c.slot_is_open(base + "icon_mystery.svg", "Qabul qilinmoqda") is False
    # icon_is_open reports the raw verdict, or None when unrecognized.
    assert c.icon_is_open("icon_circle.svg") is True
    assert c.icon_is_open("icon_disabled.svg") is False
    assert c.icon_is_open("icon_mystery.svg") is None


def test_scan_reads_grid_from_json_response_and_flags_open():
    # The AJAX endpoint returns {"html": "<grid>"}; extract_html must unwrap it.
    grid = (
        "<table><tr>"
        # Day 1 is CLOSED: disabled icon (its alt lies and says "Qabul qilinmoqda").
        + _grid_cell(1, " Available / Qabul qilinmoqda / Приём ведётся", icon="icon_disabled.svg")
        # Day 2 is OPEN: circle icon (its alt lies and says "Not available").
        + _grid_cell(2, " Not available / Qabul tugadi / Приём окончен", icon="icon_circle.svg")
        + "</tr></table>"
    )
    response_text = json.dumps({"html": grid})

    html_doc = c.extract_html(response_text)
    scan = c.scan_calendar_html(html_doc)
    assert len(scan["items"]) == 2  # both in-window days have icons

    current_open: dict[str, str] = {}
    c.log_month("2026-07", scan, current_open)
    # Only the day whose ICON says open is recorded, regardless of alt text.
    assert "2026-07-02" in current_open
    assert "2026-07-01" not in current_open


def test_icon_filename_is_authoritative_not_alt_text():
    # GROUND TRUTH captured live on 2026-06-30: the embassy's <img alt> text is
    # INVERTED relative to availability. An OPEN day renders icon_circle.svg but
    # carries alt "Not available / Qabul tugadi / Приём окончен"; a CLOSED day
    # renders icon_disabled.svg but carries alt "Available / Qabul qilinmoqda".
    # Detection MUST key off the icon filename, never the alt text.
    open_cell = (
        '<td><div class="sc_cal_month_itemlist">'
        '<div class="sc_cal_date">3</div>'
        '<a class="c_cal_time_cell"><img src="/assets/images/user/icon_circle.svg?1604042662" '
        'alt=" Not available / Qabul tugadi / Приём окончен" '
        'data-date="2026/07/03" data-value="day" data-target="sel_disp_type"/></a>'
        '</div></td>'
    )
    closed_cell = (
        '<td><div class="sc_cal_month_itemlist">'
        '<div class="sc_cal_date">6</div>'
        '<p class="c_cal_time_cell"><img src="/assets/images/user/icon_disabled.svg?1604042663" '
        'alt=" Available / Qabul qilinmoqda / Приём ведётся" width="24" height="24"/></p>'
        '</div></td>'
    )
    grid = "<table><tr>" + open_cell + closed_cell + "</tr></table>"
    scan = c.scan_calendar_html(grid)
    current_open: dict[str, str] = {}
    c.log_month("2026-07", scan, current_open)
    # The circle day is OPEN despite its "Not available" alt; the disabled day is
    # CLOSED despite its "Available" alt.
    assert "2026-07-03" in current_open
    assert "2026-07-06" not in current_open
