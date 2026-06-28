"""Regression tests for the embassy calendar request + grid parsing.

These lock the contract that broke silently in production: the booking site
(a CakePHP app) renders the calendar grid with <img alt="..."> status icons and
ships a per-form security token. The scraper must (a) pick the calendar form
with its _Token, and (b) read day availability from the img alt text.
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


def _grid_cell(day, alt):
    return (
        '<td><div class="sc_cal_month_itemlist">'
        f'<div class="sc_cal_date"><a href="#" class="js_change_date" data-date="2026/07/0{day}">{day}</a></div>'
        f'<a href="#" class="c_cal_time_cell"><img src="/assets/images/user/icon_circle.svg" '
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


def test_is_open_recognizes_embassy_statuses():
    # The real "closed" wording the site serves for full/expired days.
    assert c.is_open(" Not available / Qabul tugadi / Приём окончен") is False
    # Positive wordings that must trigger an alert.
    assert c.is_open("Qabul qilinmoqda") is True
    assert c.is_open("受付中") is True
    assert c.is_open("残りわずか") is True


def test_scan_reads_grid_from_json_response_and_flags_open():
    # The AJAX endpoint returns {"html": "<grid>"}; extract_html must unwrap it.
    grid = (
        "<table><tr>"
        + _grid_cell(1, " Not available / Qabul tugadi / Приём окончен")
        + _grid_cell(2, "Qabul qilinmoqda")
        + "</tr></table>"
    )
    response_text = json.dumps({"html": grid})

    html_doc = c.extract_html(response_text)
    scan = c.scan_calendar_html(html_doc)
    assert len(scan["items"]) == 2  # both in-window days have icons

    current_open: dict[str, str] = {}
    c.log_month("2026-07", scan, current_open)
    # Only the day with a positive status is recorded as open.
    assert "2026-07-02" in current_open
    assert "2026-07-01" not in current_open
