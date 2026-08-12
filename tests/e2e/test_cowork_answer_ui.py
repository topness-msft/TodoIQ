import json
import os
import re

from playwright.sync_api import Page, expect


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEMP_DIR = os.path.join(PROJECT_ROOT, "temp")
os.makedirs(TEMP_DIR, exist_ok=True)


def test_blocked_question_can_be_answered_in_place(page: Page, base_url):
    page.set_viewport_size({"width": 1280, "height": 900})
    page.route(
        "https://images.example.test/**",
        lambda route: route.fulfill(
            status=200,
            content_type="image/svg+xml",
            body=(
                '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
                '<rect width="40" height="40" rx="8" fill="#0f6cbd"/>'
                '<path d="M12 20h16M20 12v16" stroke="white" stroke-width="3"/>'
                "</svg>"
            ),
        ),
    )
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Resolve the Cowork question"},
    )
    task_id = created.json()["task"]["id"]
    posted = {}

    def answer_route(route):
        posted.update(json.loads(route.request.post_data))
        route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps({
                "action": {
                    "task_id": task_id,
                    "state": "previewing",
                    "waiting_on_user": False,
                    "conversation_id": "t:u:blocked",
                },
            }),
        )

    page.route(f"**/api/tasks/{task_id}/cowork/answer", answer_route)
    page.goto(base_url + "/")
    page.wait_for_function(
        f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
    )
    page.evaluate(
        """taskId => {
            const task = tasks.find(t => t.id === taskId);
            selectedTaskId = taskId;
            _cwActions[taskId] = {
                task_id: taskId,
                state: 'previewing',
                waiting_on_user: true,
                interaction_request: {
                    invocation_id: 'invoke-1',
                    questions: [{
                        id: '0',
                        producer_id: 'account',
                        header: '',
                        question: 'Which account should Cowork use?',
                        options: [
                            {
                                value: 'A',
                                label: 'Account A',
                                description: 'Primary tenant',
                                image_url: 'https://images.example.test/a.svg'
                            },
                            {value: 'B', label: 'Account B', description: 'Sandbox'}
                        ]
                    }, {
                        id: '1',
                        producer_id: 'scope',
                        header: '',
                        question: 'Which scopes?',
                        multi_select: true,
                        options: [
                            {value: 'A', label: 'Scope A', description: ''},
                            {value: 'B', label: 'Scope B', description: ''}
                        ]
                    }]
                },
                blocked_question: '{"invocation_id":"invoke-1"}',
                conversation_id: 't:u:blocked',
                island_url: 'https://example.invalid'
            };
            renderDetailPane(task);
        }""",
        task_id,
    )

    blocked = page.locator('[data-testid="cw-blocked"]')
    expect(blocked).to_be_visible()
    expect(blocked).to_contain_text("Which account should Cowork use?")
    expect(blocked.locator("img")).to_have_count(1)
    expect(page.locator('[data-testid="cw-answer"]')).to_have_count(2)
    submit = page.locator('[data-testid="cw-answer-submit"]')
    expect(submit).to_have_text("Answer and continue")
    redirect = page.locator('[data-testid="cw-open-cowork"]')
    expect(redirect).to_have_text("Edit or redirect")
    expect(redirect).not_to_have_attribute("title", re.compile("draft", re.IGNORECASE))
    choices = page.locator('[data-testid="cw-choice"]')
    expect(choices).to_have_count(4)
    expect(choices.nth(0)).to_have_accessible_name("Account A Primary tenant")
    expect(choices.nth(1)).to_have_accessible_name("Account B Sandbox")
    first_choice_box = choices.first.bounding_box()
    assert first_choice_box and first_choice_box["height"] < 40
    choices.nth(0).click()
    expect(choices.nth(0)).to_have_attribute("aria-pressed", "true")
    choices.nth(1).click()
    expect(choices.nth(0)).to_have_attribute("aria-pressed", "false")
    expect(choices.nth(1)).to_have_attribute("aria-pressed", "true")
    choices.nth(0).click()
    choices.nth(2).click()
    choices.nth(3).click()
    page.wait_for_timeout(700)
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-interaction-actions-light.png"),
        full_page=True,
    )
    page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
    selected_bg = choices.nth(0).evaluate(
        "element => getComputedStyle(element).backgroundColor"
    )
    assert selected_bg.startswith("rgba(")
    selected_alpha = float(selected_bg.split(",")[-1].rstrip(")"))
    assert 0 < selected_alpha < 0.3
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-interaction-actions-dark.png"),
        full_page=True,
    )

    page.evaluate(
        """() => {
            window.answerRestartedCoworkPoller = false;
            startCoworkPoller = () => {
                window.answerRestartedCoworkPoller = true;
            };
        }"""
    )
    page.locator('[data-testid="cw-answer-submit"]').click()
    page.wait_for_function("() => !document.querySelector('[data-testid=\"cw-blocked\"]')")
    expect(page.locator('[data-testid="cw-open-cowork"]')).to_have_text(
        "Open in Cowork"
    )
    assert page.evaluate("window.answerRestartedCoworkPoller") is True
    assert posted == {
        "invocation_id": "invoke-1",
        "answers": {"0": "A", "1": "A\nB"},
    }


def test_raw_html_and_unsafe_images_stay_inert(page: Page, base_url):
    page.goto(base_url + "/")
    page.evaluate(
        """() => {
            document.body.innerHTML = cwInteractionFields(1, {
                questions: [{
                    id: '0',
                    question: 'Show <img src=x onerror=alert(1)> literally',
                    options: [{
                        value: 'unsafe',
                        label: '<b>Unsafe image</b>',
                        description: '',
                        image_url: 'http://example.test/not-secure.png'
                    }]
                }]
            });
        }"""
    )

    expect(page.locator("body")).to_contain_text("<img src=x onerror=alert(1)>")
    expect(page.locator("body")).to_contain_text("<b>Unsafe image</b>")
    expect(page.locator("img")).to_have_count(0)
    expect(page.locator(".cw-choice-emoji")).to_have_count(1)
