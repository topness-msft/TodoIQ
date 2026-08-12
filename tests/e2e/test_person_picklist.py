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
