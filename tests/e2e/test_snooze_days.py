"""E2E gates for the snooze dropdown's day row and date picker.

Two requests from the dogfood, 2026-08-07:

1. The weekday row should offer the next four days horizontally. It used to
   shrink as the week wore on - `offset <= (5 - day)` gave four buttons on
   Monday, one by Thursday, and a bare "Mon" on Fri/Sat/Sun. Phil hit it on a
   Friday and saw a single button where a row belonged.

2. The date field required typing. `<input type="date">` only opens its calendar
   when the small icon is hit; clicking the text area just puts a caret in
   `mm/dd/yyyy`.

Weekends stay skipped. The previous code deliberately routed Fri/Sat/Sun to
Monday, and the row is labelled "9 AM:", so a 9 AM Saturday reminder is not
what this control is for. The change is that the row is now always four days
long, not that it starts including weekends.
"""

import re

from playwright.sync_api import Page, expect


def _seed(page: Page, base_url: str) -> int:
    response = page.request.post(
        f"{base_url}/api/tasks", data={"title": "Snooze row probe"}
    )
    assert response.ok, response.text()
    return response.json()["task"]["id"]


def _delete(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _open(page: Page, base_url: str, task_id: int) -> None:
    page.goto(base_url + "/")
    page.wait_for_function(f"Boolean(tasks.find(t => t.id === {task_id}))")
    page.evaluate(f"selectTask({task_id})")
    page.wait_for_selector(f"#snooze-dropdown-{task_id}", state="attached")


def _row_html(page: Page, iso_date: str) -> str:
    """Render the weekday row as it would appear on a given date."""
    return page.evaluate(
        f"renderWeekdaySnoozeRow(1, new Date('{iso_date}T10:00:00'))"
    )


def _labels(html: str) -> list:
    return re.findall(r">([A-Z][a-z]{2})</button>", html)


class TestWeekdayRow:
    # 2026-08-03 is a Monday, so this walks Mon..Sun.
    DAYS = {
        "2026-08-03": "Mon",
        "2026-08-04": "Tue",
        "2026-08-05": "Wed",
        "2026-08-06": "Thu",
        "2026-08-07": "Fri",
        "2026-08-08": "Sat",
        "2026-08-09": "Sun",
    }

    def test_always_offers_four_days(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            for iso, name in self.DAYS.items():
                labels = _labels(_row_html(page, iso))
                assert len(labels) == 4, (name, iso, labels)
        finally:
            _delete(page, base_url, task_id)

    def test_skips_weekends(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            for iso, name in self.DAYS.items():
                labels = _labels(_row_html(page, iso))
                assert "Sat" not in labels and "Sun" not in labels, (name, labels)
        finally:
            _delete(page, base_url, task_id)

    def test_friday_starts_at_monday(self, page: Page, base_url):
        """The reported case: a Friday used to show one button."""
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            assert _labels(_row_html(page, "2026-08-07")) == [
                "Mon", "Tue", "Wed", "Thu"
            ]
        finally:
            _delete(page, base_url, task_id)

    def test_midweek_rolls_into_next_week(self, page: Page, base_url):
        """Thursday used to offer only Friday."""
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            assert _labels(_row_html(page, "2026-08-06")) == [
                "Fri", "Mon", "Tue", "Wed"
            ]
        finally:
            _delete(page, base_url, task_id)

    def test_each_button_names_its_date(self, page: Page, base_url):
        """Day names alone cannot say which week a button means."""
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            html = _row_html(page, "2026-08-07")
            titles = re.findall(r'title="([^"]+)"', html)
            assert len(titles) == 4, titles
            assert any("Aug" in t or "August" in t for t in titles), titles
        finally:
            _delete(page, base_url, task_id)

    def test_row_is_horizontal(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleSnoozeDropdown({task_id})")
            layout = page.evaluate(
                f"""() => {{
                    const row = document.querySelector(
                        '#snooze-dropdown-{task_id} .snooze-weekday-row');
                    const btns = [...row.querySelectorAll('.snooze-weekday-btn')];
                    return {{
                        display: getComputedStyle(row).flexDirection,
                        count: btns.length,
                        tops: [...new Set(btns.map(
                            b => Math.round(b.getBoundingClientRect().top)))].length
                    }};
                }}"""
            )
            assert layout["count"] == 4, layout
            assert layout["tops"] == 1, layout   # all on one line
            assert layout["display"] == "row", layout
        finally:
            _delete(page, base_url, task_id)

    def test_buttons_fit_inside_the_dropdown(self, page: Page, base_url):
        """Four buttons must not overflow the menu they live in."""
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleSnoozeDropdown({task_id})")
            fits = page.evaluate(
                f"""() => {{
                    const dd = document.getElementById('snooze-dropdown-{task_id}');
                    const row = dd.querySelector('.snooze-weekday-row');
                    const btns = [...row.querySelectorAll('.snooze-weekday-btn')];
                    const d = dd.getBoundingClientRect();
                    return btns.every(b => {{
                        const r = b.getBoundingClientRect();
                        return r.left >= d.left - 1 && r.right <= d.right + 1;
                    }});
                }}"""
            )
            assert fits
        finally:
            _delete(page, base_url, task_id)


class TestDatePicker:
    def test_clicking_the_field_opens_the_picker(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleSnoozeDropdown({task_id})")
            called = page.evaluate(
                f"""() => {{
                    const el = document.getElementById('snooze-date-{task_id}');
                    let hit = 0;
                    el.showPicker = () => {{ hit++; }};
                    el.click();
                    return hit;
                }}"""
            )
            assert called == 1
        finally:
            _delete(page, base_url, task_id)

    def test_time_field_opens_its_picker_too(self, page: Page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleSnoozeDropdown({task_id})")
            called = page.evaluate(
                f"""() => {{
                    const el = document.getElementById('snooze-time-{task_id}');
                    let hit = 0;
                    el.showPicker = () => {{ hit++; }};
                    el.click();
                    return hit;
                }}"""
            )
            assert called == 1
        finally:
            _delete(page, base_url, task_id)

    def test_click_does_not_close_the_dropdown(self, page: Page, base_url):
        """Opening the picker must not dismiss the menu underneath it."""
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleSnoozeDropdown({task_id})")
            page.evaluate(
                f"""() => {{
                    const el = document.getElementById('snooze-date-{task_id}');
                    el.showPicker = () => {{}};
                    el.click();
                }}"""
            )
            expect(
                page.locator(f"#snooze-dropdown-{task_id}")
            ).to_have_class(re.compile(r"\bopen\b"))
        finally:
            _delete(page, base_url, task_id)

    def test_missing_showpicker_is_survivable(self, page: Page, base_url):
        """Older browsers lack showPicker; a click must not throw."""
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleSnoozeDropdown({task_id})")
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.evaluate(
                f"""() => {{
                    const el = document.getElementById('snooze-date-{task_id}');
                    el.showPicker = undefined;
                    el.click();
                }}"""
            )
            page.wait_for_timeout(200)
            assert not errors, errors
        finally:
            _delete(page, base_url, task_id)


# ---------------------------------------------------------------- /todo -----
# The same shrinking-week bug lives in `static/mock-todo.html`, and bites
# harder: its loop is `for i in 1..5 if i > today.getDay()`, so a Friday
# produces the "No more weekdays" empty state - zero buttons, not one.


def _todo_days(page: Page, iso_date: str) -> list:
    return page.evaluate(
        f"nextWeekdaySnoozeDays(new Date('{iso_date}T10:00:00'), 4)"
    )


# Chromium renders roughly a 16px calendar/clock affordance inside a native
# date or time input, on top of the text. Measured, not guessed.
_PICKER_AFFORDANCE = 16


class TestSnoozeFieldsAreLegible:
    """A field too narrow to show its own value is the `textarea.cw-draft` bug
    again. `.snooze-time-input` was a fixed 70px: a 56px content box holding
    51px of "09:00 AM" plus a ~16px clock, so it rendered "09:0C".

    scrollWidth does not catch this - native inputs clip internally rather than
    overflow - so the assertion compares the content box against the text
    measured in the field's own computed font.
    """

    @staticmethod
    def _probe(page: Page, element_id: str, text: str) -> dict:
        return page.evaluate(
            """([id, text]) => {
                const el = document.getElementById(id);
                const cs = getComputedStyle(el);
                const ctx = document.createElement('canvas').getContext('2d');
                ctx.font = cs.fontSize + ' ' + cs.fontFamily;
                const pad = parseFloat(cs.paddingLeft)
                          + parseFloat(cs.paddingRight);
                return {
                    contentBox: el.clientWidth - pad,
                    textNeeds: Math.ceil(ctx.measureText(text).width)
                };
            }""",
            [element_id, text],
        )

    def test_time_field_can_show_its_value_and_its_picker(self, page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleSnoozeDropdown({task_id})")
            m = self._probe(page, f"snooze-time-{task_id}", "09:00 AM")
            assert m["contentBox"] >= m["textNeeds"] + _PICKER_AFFORDANCE, m
        finally:
            _delete(page, base_url, task_id)

    def test_date_field_can_show_its_value_and_its_picker(self, page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleSnoozeDropdown({task_id})")
            m = self._probe(page, f"snooze-date-{task_id}", "mm/dd/yyyy")
            assert m["contentBox"] >= m["textNeeds"] + _PICKER_AFFORDANCE, m
        finally:
            _delete(page, base_url, task_id)

    def test_custom_row_stays_inside_the_dropdown(self, page: Page, base_url):
        """Widening a field must not push the Go button out of the menu."""
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate(f"toggleSnoozeDropdown({task_id})")
            assert page.evaluate(
                f"""() => {{
                    const dd = document.getElementById('snooze-dropdown-{task_id}');
                    const d = dd.getBoundingClientRect();
                    return [...dd.querySelectorAll('.snooze-custom-row > *')]
                        .every(el => {{
                            const r = el.getBoundingClientRect();
                            return r.left >= d.left - 1 && r.right <= d.right + 1;
                        }});
                }}"""
            )
        finally:
            _delete(page, base_url, task_id)


class TestNativeControlsFollowTheTheme:
    """Without a `color-scheme` declaration Chromium paints native form
    controls in light mode regardless of the page theme, so on a dark input the
    calendar/clock affordance is a dark glyph on a dark field - effectively
    invisible - and the picker panel opens white.

    That is a direct cause of the reported symptom: an affordance you cannot
    see reads as "there is no picker, I have to type the date".
    """

    def test_dashboard_dark_theme_renders_dark_controls(self, page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate("document.documentElement.setAttribute('data-theme','dark')")
            page.evaluate(f"toggleSnoozeDropdown({task_id})")
            scheme = page.evaluate(
                f"""() => getComputedStyle(
                    document.getElementById('snooze-date-{task_id}')).colorScheme"""
            )
            assert "dark" in scheme, scheme
        finally:
            _delete(page, base_url, task_id)

    def test_dashboard_light_theme_renders_light_controls(self, page, base_url):
        task_id = _seed(page, base_url)
        try:
            _open(page, base_url, task_id)
            page.evaluate("document.documentElement.setAttribute('data-theme','light')")
            page.evaluate(f"toggleSnoozeDropdown({task_id})")
            scheme = page.evaluate(
                f"""() => getComputedStyle(
                    document.getElementById('snooze-date-{task_id}')).colorScheme"""
            )
            assert "dark" not in scheme, scheme
        finally:
            _delete(page, base_url, task_id)

    def test_todo_dark_theme_renders_dark_controls(self, page, base_url):
        task_id = _seed(page, base_url)
        try:
            page.goto(base_url + "/todo")
            page.wait_for_function(
                f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
            )
            page.evaluate("document.body.classList.add('dark')")
            page.evaluate(
                f"""() => showSnoozePicker(
                    {{ stopPropagation(){{}}, currentTarget: document.body }},
                    {task_id})"""
            )
            page.wait_for_selector("#snooze-picker", state="attached")
            scheme = page.evaluate(
                "() => getComputedStyle(document.getElementById('snooze-date')).colorScheme"
            )
            assert "dark" in scheme, scheme
        finally:
            _delete(page, base_url, task_id)


class TestTodoWeekdayRow:
    DAYS = TestWeekdayRow.DAYS

    def _load(self, page: Page, base_url):
        page.goto(base_url + "/todo")
        page.wait_for_function("typeof nextWeekdaySnoozeDays === 'function'")

    def test_always_offers_four_days(self, page: Page, base_url):
        self._load(page, base_url)
        for iso, name in self.DAYS.items():
            assert len(_todo_days(page, iso)) == 4, (name, iso)

    def test_skips_weekends(self, page: Page, base_url):
        self._load(page, base_url)
        for iso, name in self.DAYS.items():
            for d in _todo_days(page, iso):
                assert d["label"] not in ("Sat", "Sun"), (name, d)

    def test_friday_is_not_empty(self, page: Page, base_url):
        """Friday used to render the 'No more weekdays' empty state."""
        self._load(page, base_url)
        labels = [d["label"] for d in _todo_days(page, "2026-08-07")]
        assert labels == ["Mon", "Tue", "Wed", "Thu"], labels

    def test_thursday_rolls_into_next_week(self, page: Page, base_url):
        self._load(page, base_url)
        labels = [d["label"] for d in _todo_days(page, "2026-08-06")]
        assert labels == ["Fri", "Mon", "Tue", "Wed"], labels

    def test_each_day_carries_an_iso_date(self, page: Page, base_url):
        """doSnooze() is called with a date string, so it must be well formed."""
        self._load(page, base_url)
        for d in _todo_days(page, "2026-08-07"):
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", d["value"]), d

    def test_dates_are_local_not_utc_shifted(self, page: Page, base_url):
        """toISOString() on a local evening rolls the date forward west of UTC.

        The old code did `d.toISOString().split('T')[0]` on a Date carrying the
        current time, so after ~5pm Pacific every button snoozed to the wrong
        day. 23:00 local reproduces it.
        """
        self._load(page, base_url)
        days = page.evaluate(
            "nextWeekdaySnoozeDays(new Date('2026-08-07T23:00:00'), 4)"
        )
        assert [d["value"] for d in days] == [
            "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"
        ], days


class TestTodoDatePicker:
    def _open_picker(self, page: Page, base_url):
        self.task_id = _seed(page, base_url)
        page.goto(base_url + "/todo")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {self.task_id})"
        )
        page.evaluate(
            f"""() => showSnoozePicker(
                {{ stopPropagation(){{}}, currentTarget: document.body }},
                {self.task_id})"""
        )
        page.wait_for_selector("#snooze-picker", state="attached")

    def test_clicking_the_date_field_opens_the_picker(self, page: Page, base_url):
        self._open_picker(page, base_url)
        try:
            assert page.evaluate(
                """() => {
                    const el = document.getElementById('snooze-date');
                    let hit = 0; el.showPicker = () => { hit++; };
                    el.click(); return hit;
                }"""
            ) == 1
        finally:
            _delete(page, base_url, self.task_id)

    def test_clicking_the_time_field_opens_the_picker(self, page: Page, base_url):
        self._open_picker(page, base_url)
        try:
            assert page.evaluate(
                """() => {
                    const el = document.getElementById('snooze-time');
                    let hit = 0; el.showPicker = () => { hit++; };
                    el.click(); return hit;
                }"""
            ) == 1
        finally:
            _delete(page, base_url, self.task_id)

    def test_click_does_not_dismiss_the_picker(self, page: Page, base_url):
        self._open_picker(page, base_url)
        try:
            page.evaluate(
                """() => {
                    const el = document.getElementById('snooze-date');
                    el.showPicker = () => {};
                    el.click();
                }"""
            )
            page.wait_for_timeout(150)
            assert page.locator("#snooze-picker").count() == 1
        finally:
            _delete(page, base_url, self.task_id)

    def test_four_buttons_stay_on_one_line(self, page: Page, base_url):
        """.sp-weekdays wraps, so a too-narrow picker would stack them."""
        self._open_picker(page, base_url)
        try:
            rows = page.evaluate(
                """() => {
                    const b = [...document.querySelectorAll(
                        '#snooze-picker .sp-weekday')];
                    return {
                        count: b.length,
                        lines: [...new Set(b.map(
                            x => Math.round(x.getBoundingClientRect().top)))].length
                    };
                }"""
            )
            assert rows["count"] == 4, rows
            assert rows["lines"] == 1, rows
        finally:
            _delete(page, base_url, self.task_id)
