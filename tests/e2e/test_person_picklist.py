"""Production /todo person-picklist behavior and persistence gates."""

import json
import os

from playwright.sync_api import Page, expect


SCREENSHOTS_DIR = os.path.join("temp", "person-picklist")


def _seed_task(page: Page, base_url: str) -> int:
    people = [
        {
            "name": "Srini Raghavan",
            "email": "srini.raghavan@microsoft.com",
            "role": "CVP, Copilot & Agent Ecosystem",
            "alternatives": [
                {
                    "name": "Srinivas O'Rao",
                    "email": "srinivas.rao@microsoft.com",
                    "role": "VP, Director's \"Strategy\" Office",
                }
            ],
        },
        {
            "name": "Phil Topness",
            "email": "phil.topness@microsoft.com",
            "role": "Copilot Acceleration Team",
            "alternatives": [],
        },
    ]
    response = page.request.post(
        f"{base_url}/api/tasks",
        data={
            "title": "Person picklist visual gate",
            "description": "Verify resolved people persist.",
            "parse_status": "parsed",
            "key_people": json.dumps(people),
        },
    )
    assert response.ok
    return response.json()["task"]["id"]


def _delete_task(page: Page, base_url: str, task_id: int) -> None:
    page.request.delete(f"{base_url}/api/tasks/{task_id}")


def _open_task(page: Page, base_url: str, task_id: int) -> None:
    page.goto(base_url + "/todo")
    page.wait_for_function(f"Boolean(tasks.find(task => task.id === {task_id}))")
    page.evaluate(f"selectTask({task_id})")


def _stored_people(page: Page, base_url: str, task_id: int) -> list[dict]:
    task = page.request.get(f"{base_url}/api/tasks/{task_id}").json()["task"]
    return json.loads(task["key_people"])


class TestPersonPicklist:
    def test_long_person_chip_stays_inside_key_people_card(
        self, page: Page, base_url
    ):
        response = page.request.post(
            f"{base_url}/api/tasks",
            data={
                "title": "Long Key People pill visual gate",
                "description": "Verify long people stay inside the card.",
                "parse_status": "parsed",
                "key_people": json.dumps([{
                    "name": "Phil Topness",
                    "email": "phil@topness.com",
                    "role": "Principal Consultant, Ascentium Federal",
                    "alternatives": [{
                        "name": "Phil Topness",
                        "email": "phil.topness@microsoft.com",
                        "role": "Copilot Acceleration Team",
                    }],
                }]),
            },
        )
        assert response.ok
        task_id = response.json()["task"]["id"]
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        try:
            page.goto(base_url)
            page.wait_for_function(f"Boolean(tasks.find(task => task.id === {task_id}))")
            page.evaluate(f"selectTask({task_id})")
            wrapper = page.locator(".person-pill-wrapper").filter(
                has_text="Phil Topness"
            )
            chip = wrapper.locator(".person-pill")
            expect(wrapper).to_have_count(1)
            expect(chip).to_be_visible()
            card = wrapper.locator(
                "xpath=ancestor::*[contains(@class,'detail-card')][1]"
            )
            # The chip is asserted visible above but the card never was, and
            # bounding_box on an unsettled element returns None -- this failed
            # as "assert None is not None" under full-suite load.
            expect(card).to_be_visible()
            chip_box = chip.bounding_box()
            card_box = card.bounding_box()

            assert chip_box is not None
            assert card_box is not None
            assert chip_box["x"] + chip_box["width"] <= (
                card_box["x"] + card_box["width"] - 12
            )
            assert chip.locator(".person-name").evaluate(
                "el => getComputedStyle(el).textOverflow"
            ) == "ellipsis"
            assert chip.locator(".person-role").evaluate(
                "el => getComputedStyle(el).textOverflow"
            ) == "ellipsis"

            expect(chip.locator(".person-pill-avatar")).to_be_visible()
            chip.click()
            expect(wrapper.locator(".alternatives-dropdown")).to_be_visible()
            expect(wrapper.locator(".remove-person")).to_be_visible()

            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "long-chip-light.png"),
                full_page=True,
            )
            page.evaluate(
                "document.documentElement.setAttribute('data-theme', 'dark')"
            )
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "long-chip-dark.png"),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_picklist_renders_and_persists_mutations(self, page: Page, base_url):
        task_id = _seed_task(page, base_url)
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        try:
            _open_task(page, base_url, task_id)

            picklists = page.get_by_test_id("person-picklist")
            expect(picklists).to_have_count(2)
            first = picklists.first
            expect(first).to_contain_text("SR")
            expect(first).to_contain_text("Srini Raghavan")
            expect(first).to_contain_text("CVP, Copilot & Agent Ecosystem")
            expect(
                picklists.nth(1).get_by_role(
                    "button", name="Resolve Phil Topness"
                )
            ).to_have_count(0)
            resolver = first.get_by_role("button", name="Resolve Srini Raghavan")
            expect(resolver).to_have_attribute("aria-haspopup", "listbox")
            resolver.click()
            alternative = page.get_by_role(
                "option", name="Srinivas O'Rao VP, Director's \"Strategy\" Office"
            )
            expect(alternative).to_be_visible()

            with page.expect_request(
                lambda request: request.method == "PUT"
                and request.url.endswith(f"/api/tasks/{task_id}")
            ) as request_info:
                alternative.click()
            payload = request_info.value.post_data_json
            assert isinstance(payload["key_people"], str)
            page.wait_for_function(
                """async ([taskId]) => {
                    const response = await fetch(`/api/tasks/${taskId}`);
                    const body = await response.json();
                    return JSON.parse(body.task.key_people)[0].name === "Srinivas O'Rao";
                }""",
                arg=[task_id],
            )
            people = _stored_people(page, base_url, task_id)
            assert people[0]["name"] == "Srinivas O'Rao"
            assert people[0]["role"] == "VP, Director's \"Strategy\" Office"
            assert people[0]["alternatives"][0]["name"] == "Srini Raghavan"

            page.reload()
            page.wait_for_function(f"Boolean(tasks.find(task => task.id === {task_id}))")
            page.evaluate(f"selectTask({task_id})")
            expect(page.get_by_test_id("person-picklist").first).to_contain_text(
                "Srinivas O'Rao"
            )

            page.evaluate(
               """([taskId]) => {
                   addPerson(taskId, 'Adele Vance');
                   addPerson(taskId, 'Megan Bowen');
               }""",
               [task_id],
            )
            page.wait_for_function(
               """async ([taskId]) => {
                    const response = await fetch(`/api/tasks/${taskId}`);
                    const body = await response.json();
                    const names = JSON.parse(body.task.key_people).map(
                        person => person.name
                    );
                    return names.includes('Adele Vance')
                        && names.includes('Megan Bowen');
                }""",
               arg=[task_id],
            )

            phil_picklist = page.get_by_test_id("person-picklist").filter(
                has_text="Phil Topness"
            )
            remove = phil_picklist.get_by_role("button", name="Remove Phil Topness")
            remove.focus()
            expect(remove).to_be_visible()
            with page.expect_request(
                lambda request: request.method == "PUT"
                and request.url.endswith(f"/api/tasks/{task_id}")
            ):
                remove.click(force=True)
            page.wait_for_function(
                """async ([taskId]) => {
                    const response = await fetch(`/api/tasks/${taskId}`);
                    const body = await response.json();
                    return !JSON.parse(body.task.key_people).some(
                        person => person.name === 'Phil Topness'
                    );
                }""",
                arg=[task_id],
            )

            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "picklist-light.png"),
                full_page=True,
            )
            page.locator("body").evaluate("body => body.classList.add('dark')")
            page.screenshot(
                path=os.path.join(SCREENSHOTS_DIR, "picklist-dark.png"),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)

    def test_attendance_uncertain_people_without_alternatives_render(
        self, page: Page, base_url
    ):
        people = [
            {
                "name": "Sally Shi",
                "email": "sally.shi@microsoft.com",
                "role": "Principal Program Manager",
                "aad_object_id": "aad-sally-2495",
                "attendance_uncertain": True,
            },
            {
                "name": "Azharullah Meer",
                "email": "ameer@microsoft.com",
                "role": "Senior Product Manager",
                "aad_object_id": "aad-azharullah-2495",
                "attendance_uncertain": True,
            },
        ]
        response = page.request.post(
            f"{base_url}/api/tasks",
            data={
                "title": "Attendance confirmation renderer regression",
                "description": "Confirmed identities still need attendance confirmation.",
                "parse_status": "parsed",
                "action_type": "schedule-meeting",
                "key_people": json.dumps(people),
            },
        )
        assert response.ok, response.text()
        task_id = response.json()["task"]["id"]
        render_errors = []
        page.on(
            "console",
            lambda message: render_errors.append(message.text)
            if message.type == "error" and "alternatives" in message.text
            else None,
        )
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

        try:
            page.goto(base_url)
            page.wait_for_function(
                f"Boolean(tasks.find(task => task.id === {task_id}))"
            )
            page.evaluate(f"selectTask({task_id})")

            wrappers = page.locator(".person-pill-wrapper")
            expect(wrappers).to_have_count(2)
            expect(wrappers.nth(0)).to_contain_text("Sally Shi")
            expect(wrappers.nth(1)).to_contain_text("Azharullah Meer")

            first_pill = wrappers.nth(0).locator(".person-pill")
            expect(first_pill).to_have_class(
                __import__("re").compile(r"\bis-unresolved\b")
            )
            first_pill.click()
            expect(
                wrappers.nth(0).locator(
                    ".alternatives-dropdown .alternatives-header"
                )
            ).to_have_text("Confirm attendee")
            assert render_errors == []

            page.screenshot(
                path=os.path.join(
                    SCREENSHOTS_DIR,
                    "attendance-uncertain-without-alternatives.png",
                ),
                full_page=True,
            )
        finally:
            _delete_task(page, base_url, task_id)
