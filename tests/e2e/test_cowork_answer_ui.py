import json
import os

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
            task.parse_status = 'parsed';
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
        arg=task_id,
    )

    blocked = page.locator('[data-testid="cw-blocked"]')
    expect(blocked).to_be_visible()
    expect(blocked).to_contain_text("Which account should Cowork use?")
    expect(blocked.locator("img")).to_have_count(1)
    expect(page.locator('[data-testid="cw-answer"]')).to_have_count(2)
    submit = page.locator('[data-testid="cw-answer-submit"]')
    expect(submit).to_have_text("Answer and continue")
    cowork_link = page.locator('[data-testid="cw-open-cowork"]')
    expect(cowork_link).to_have_count(1)
    expect(cowork_link).to_have_text("Open in Cowork")
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


def test_schedule_evidence_fallback_is_text_only(page: Page, base_url):
    page.set_viewport_size({"width": 1280, "height": 900})
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule a timezone-safe review"},
    )
    task_id = created.json()["task"]["id"]
    page.goto(base_url + "/")
    page.wait_for_function(
        f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
    )
    page.evaluate(
        """taskId => {
            const task = tasks.find(t => t.id === taskId);
            task.parse_status = 'parsed';
            task.action_type = 'schedule-meeting';
            task.key_people = JSON.stringify([{
                name: 'Jay Padimiti',
                email: 'jay.padimiti@microsoft.com'
            }]);
            selectedTaskId = taskId;
            _cwActions[taskId] = {
                task_id: taskId,
                state: 'previewing',
                waiting_on_user: true,
                interaction_request: {
                    invocation_id: 'schedule-fallback',
                    questions: [{
                        id: '0',
                        producer_id: 'slot',
                        header: 'Availability needs another check',
                        question: 'I could not verify suitable working-hours slots '
                            + 'for every attendee. Tell me what to check or change.',
                        options: [],
                        multi_select: false
                    }],
                    schedule_evidence: {
                        valid: false,
                        source: 'FindMeetingTimes+interaction',
                        attendees: ['jay.padimiti@microsoft.com'],
                        query_backed: false
                    }
                },
                conversation_id: 't:u:schedule-fallback'
            };
            renderDetailPane(task);
        }""",
        task_id,
    )

    blocked = page.get_by_test_id("cw-blocked")
    expect(blocked).to_contain_text("Availability needs another check")
    expect(blocked).to_contain_text(
        "could not verify suitable working-hours slots"
    )
    expect(page.get_by_test_id("cw-choice")).to_have_count(0)
    answer = page.get_by_test_id("cw-answer")
    expect(answer).to_be_visible()
    answer_box = answer.bounding_box()
    assert answer_box and answer_box["width"] >= 300
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-schedule-text-only-dev.png"),
        full_page=True,
    )


def test_execution_question_can_be_answered_in_place(page: Page, base_url):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Resolve an execution question"},
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
                    "state": "executing",
                    "waiting_on_user": False,
                    "conversation_id": "t:u:execution-blocked",
                },
            }),
        )

    page.route(f"**/api/tasks/{task_id}/cowork/answer", answer_route)
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """taskId => {
                const task = tasks.find(t => t.id === taskId);
                task.parse_status = 'parsed';
                task.action_type = 'respond-email';
                selectedTaskId = taskId;
                _cwActions[taskId] = {
                    id: 135,
                    task_id: taskId,
                    state: 'executing',
                    waiting_on_user: true,
                    interaction_request: {
                        invocation_id: 'execution-question',
                        questions: [{
                            id: '0',
                            question: 'Use the earlier draft or cancel?',
                            options: [
                                {value: 'earlier', label: 'Use earlier draft'},
                                {value: 'cancel', label: 'Cancel for now'}
                            ]
                        }]
                    },
                    blocked_question: '{"invocation_id":"execution-question"}',
                    conversation_id: 't:u:execution-blocked',
                    destination_display: 'Phil Topness',
                    destination_ref: 'phil@topness.com'
                };
                renderDetailPane(task);
            }""",
            task_id,
        )

        blocked = page.get_by_test_id("cw-blocked")
        expect(blocked).to_be_visible()
        expect(blocked).to_contain_text("Use the earlier draft or cancel?")
        expect(page.get_by_test_id("cw-stop")).to_have_count(0)
        page.get_by_test_id("cw-choice").nth(1).click()
        page.screenshot(
            path=os.path.join(TEMP_DIR, "cowork-execution-question-dark.png"),
            full_page=True,
        )
        page.get_by_test_id("cw-answer-submit").click()
        expect(blocked).to_have_count(0)
        assert posted == {
            "invocation_id": "execution-question",
            "answers": {"0": "cancel"},
        }
    finally:
        page.request.delete(f"{base_url}/api/tasks/{task_id}")


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


def test_choice_question_accepts_a_free_text_redirect(page: Page, base_url):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Choose a meeting time"},
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
                    "conversation_id": "t:u:meeting-redirect",
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
            task.parse_status = 'parsed';
            task.action_type = 'schedule-meeting';
            clearInterval(parsePollerInterval);
            parsePollerInterval = null;
            startCoworkPoller = function() {};
            selectedTaskId = taskId;
            _cwActions[taskId] = {
                task_id: taskId,
                state: 'previewing',
                waiting_on_user: true,
                interaction_request: {
                    invocation_id: 'meeting-times-1',
                    questions: [{
                        id: '0',
                        question: 'Which time should I book?',
                        options: [
                            {value: 'Mon 10:05', label: 'Mon 10:05 AM'},
                            {value: 'Tue 1:05', label: 'Tue 1:05 PM'}
                        ]
                    }]
                },
                blocked_question: '{"invocation_id":"meeting-times-1"}',
                conversation_id: 't:u:meeting-redirect'
            };
            renderDetailPane(task);
        }""",
        task_id,
    )

    choices = page.get_by_test_id("cw-choice")
    choices.first.click()
    expect(choices.first).to_have_attribute("aria-pressed", "true")
    redirect = page.get_by_test_id("cw-answer-redirect")
    expect(redirect).to_be_visible()
    redirect.fill("Find something later in the day")
    expect(choices.first).to_have_attribute("aria-pressed", "false")
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-time-redirect-light.png"),
        full_page=True,
    )
    page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-time-redirect-dark.png"),
        full_page=True,
    )

    page.get_by_test_id("cw-answer-submit").click()
    page.wait_for_function("() => !document.querySelector('[data-testid=\"cw-blocked\"]')")
    assert posted == {
        "invocation_id": "meeting-times-1",
        "answers": {"0": "Find something later in the day"},
    }


def test_multi_select_buffer_restores_choices_not_redirect(page: Page, base_url):
    page.goto(base_url + "/")
    page.evaluate(
        """() => {
            _cwAnswerBuf[99] = {'0': 'A\\nB'};
            document.body.innerHTML = cwInteractionFields(99, {
                questions: [{
                    id: '0',
                    question: 'Which scopes?',
                    multi_select: true,
                    options: [
                        {value: 'A', label: 'Scope A'},
                        {value: 'B', label: 'Scope B'}
                    ]
                }]
            });
        }"""
    )

    choices = page.get_by_test_id("cw-choice")
    expect(choices).to_have_count(2)
    expect(choices.nth(0)).to_have_attribute("aria-pressed", "true")
    expect(choices.nth(1)).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_test_id("cw-answer-redirect")).to_have_value("")


def test_unverified_times_do_not_claim_the_calendars_were_checked(
    page: Page, base_url
):
    """The banner must not certify a check that did not happen.

    Task 2558 showed "WorkIQ checked the exact calendars" directly above its
    own sentence saying it could not read them, with every attendee green and
    marked Free. The banner keyed on query_backed, which only says a query
    ran -- not that it came back with anything.
    """
    page.set_viewport_size({"width": 1280, "height": 900})
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Coordinate the Pega meeting"},
    )
    task_id = created.json()["task"]["id"]

    page.goto(base_url + "/")
    page.wait_for_function(
        f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
    )
    page.evaluate(
        """taskId => {
            const task = tasks.find(t => t.id === taskId);
            task.parse_status = 'parsed';
            task.action_type = 'schedule-meeting';
            task.key_people = JSON.stringify([
                {name: 'Rima Reyes', email: 'rima.reyes@microsoft.com'},
                {name: 'Greg Howard', email: 'greg.howard@microsoft.com'}
            ]);
            clearInterval(parsePollerInterval);
            parsePollerInterval = null;
            startCoworkPoller = function() {};
            selectedTaskId = taskId;
            _cwActions[taskId] = {
                task_id: taskId,
                state: 'previewing',
                waiting_on_user: true,
                structured_payload: '{"channel":"calendar"}',
                delivery_channel: 'calendar',
                interaction_request: {
                    invocation_id: 'structured-calendar-1',
                    schedule_evidence: {
                        valid: true,
                        source: 'copilot-ask',
                        query_backed: true,
                        availability_verified: false,
                        duration_minutes: 25,
                        attendees: [
                            'greg.howard@microsoft.com',
                            'rima.reyes@microsoft.com'
                        ],
                        slots: [{
                            value: '0',
                            label: 'Wed Aug 26, 12:00 PM-12:25 PM ET',
                            availability: {
                                'rima.reyes@microsoft.com': 'unknown',
                                'greg.howard@microsoft.com': 'unknown'
                            }
                        }]
                    },
                    questions: [{
                        id: '0',
                        header: 'Select & create meeting',
                        question: 'I could not read the attendees\\u2019 '
                            + 'calendars, so these times are unchecked.',
                        options: [{
                            value: '0',
                            label: 'Wed Aug 26, 12:00 PM-12:25 PM ET',
                            description: 'Availability not checked - this '
                                + 'time may clash.'
                        }]
                    }]
                },
                blocked_question: '{"invocation_id":"structured-calendar-1"}',
                conversation_id: 't:u:pega'
            };
            renderDetailPane(task);
        }""",
        task_id,
    )

    blocked = page.get_by_test_id("cw-blocked")
    expect(blocked).to_be_visible()
    # The choice must still be offered -- measurement is best-effort, not a
    # gate. What must not survive is the claim that it succeeded.
    expect(page.get_by_test_id("cw-answer-submit")).to_be_visible()
    assert "exact calendars" not in blocked.inner_text()
    # And no attendee may be shown as Free on availability nobody measured.
    for cell in page.get_by_test_id("cw-avail-cell").all():
        assert cell.get_attribute("data-status") != "free"


def test_unverified_evidence_suppresses_the_availability_grid(page: Page, base_url):
    """A stored slot can still claim "free" after the probe returned nothing.

    Action 270 was written that way -- both attendees "free" beside
    availability_verified: false -- so the grid rendered all green. The grid
    reads as measurement, so evidence that measured nothing must not make one.
    """
    page.set_viewport_size({"width": 1280, "height": 900})
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Coordinate the Pega meeting"},
    )
    task_id = created.json()["task"]["id"]

    page.goto(base_url + "/")
    page.wait_for_function(
        f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
    )
    page.evaluate(
        """taskId => {
            const task = tasks.find(t => t.id === taskId);
            task.parse_status = 'parsed';
            task.action_type = 'schedule-meeting';
            task.key_people = JSON.stringify([
                {name: 'Rima Reyes', email: 'rima.reyes@microsoft.com'},
                {name: 'Greg Howard', email: 'greg.howard@microsoft.com'}
            ]);
            clearInterval(parsePollerInterval);
            parsePollerInterval = null;
            startCoworkPoller = function() {};
            selectedTaskId = taskId;
            _cwActions[taskId] = {
                task_id: taskId,
                state: 'previewing',
                waiting_on_user: true,
                structured_payload: '{"channel":"calendar"}',
                delivery_channel: 'calendar',
                interaction_request: {
                    invocation_id: 'structured-calendar-270',
                    schedule_evidence: {
                        valid: true,
                        source: 'copilot-ask',
                        query_backed: true,
                        availability_verified: false,
                        duration_minutes: 25,
                        attendees: [
                            'greg.howard@microsoft.com',
                            'rima.reyes@microsoft.com'
                        ],
                        slots: [{
                            value: '0',
                            label: 'Wed Aug 26, 12:00 PM-12:25 PM ET',
                            availability: {
                                'rima.reyes@microsoft.com': 'free',
                                'greg.howard@microsoft.com': 'free'
                            }
                        }]
                    },
                    questions: [{
                        id: '0',
                        header: 'Select & create meeting',
                        question: 'These times are unchecked.',
                        options: [{
                            value: '0',
                            label: 'Wed Aug 26, 12:00 PM-12:25 PM ET',
                            description: 'All confirmed attendees are available.'
                        }]
                    }]
                },
                blocked_question: '{"invocation_id":"structured-calendar-270"}',
                conversation_id: 't:u:pega'
            };
            renderDetailPane(task);
        }""",
        task_id,
    )

    expect(page.get_by_test_id("cw-blocked")).to_be_visible()
    expect(page.get_by_test_id("cw-avail-matrix")).to_have_count(0)
    expect(page.get_by_test_id("cw-avail-cell")).to_have_count(0)
    # The time is still choosable -- only the false certainty is gone.
    expect(page.get_by_test_id("cw-answer-submit")).to_be_visible()


def test_multi_attendee_times_render_as_availability_matrix(page: Page, base_url):
    page.set_viewport_size({"width": 1280, "height": 900})
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule the planning review"},
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
                    "conversation_id": "t:u:matrix",
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
            task.parse_status = 'parsed';
            task.action_type = 'schedule-meeting';
            task.key_people = JSON.stringify([
                {name: 'Rima Reyes', email: 'rima.reyes@microsoft.com'},
                {name: 'Greg Howard', email: 'greg.howard@microsoft.com'},
                {name: 'Sarah Chen', email: 'sarah.chen@microsoft.com'}
            ]);
            clearInterval(parsePollerInterval);
            parsePollerInterval = null;
            startCoworkPoller = function() {};
            selectedTaskId = taskId;
            _cwActions[taskId] = {
                task_id: taskId,
                state: 'previewing',
                waiting_on_user: true,
                interaction_request: {
                    invocation_id: 'matrix-1',
                    schedule_evidence: {
                        valid: true,
                        source: 'FindMeetingTimes+interaction',
                        query_backed: true,
                        attendees: [
                            'greg.howard@microsoft.com',
                            'rima.reyes@microsoft.com',
                            'sarah.chen@microsoft.com'
                        ],
                        slots: [{
                            value: 'Mon 10:05',
                            availability: {
                                'rima.reyes@microsoft.com': 'free',
                                'greg.howard@microsoft.com': 'tentative',
                                'sarah.chen@microsoft.com': 'tentative'
                            }
                        }, {
                            value: 'Tue 1:05',
                            availability: {
                                'rima.reyes@microsoft.com': 'free',
                                'greg.howard@microsoft.com': 'free',
                                'sarah.chen@microsoft.com': 'free'
                            }
                        }, {
                            value: 'Wed 3:05',
                            availability: {
                                'rima.reyes@microsoft.com': 'free',
                                'greg.howard@microsoft.com': 'free',
                                'sarah.chen@microsoft.com': 'tentative'
                            }
                        }]
                    },
                    questions: [{
                        id: '0',
                        question: 'Which time should I book?',
                        options: [{
                            value: 'Mon 10:05',
                            label: 'Mon Aug 17 · 10:05 AM',
                            description: '[avail:{"rima.reyes@microsoft.com":"free","greg.howard@microsoft.com":"tentative","sarah.chen@microsoft.com":"tentative"}]'
                        }, {
                            value: 'Tue 1:05',
                            label: 'Tue Aug 18 · 1:05 PM',
                            description: '[avail:{"rima.reyes@microsoft.com":"free","greg.howard@microsoft.com":"free","sarah.chen@microsoft.com":"free"}]'
                        }, {
                            value: ' Wed 3:05 ',
                            label: 'Wed Aug 19 · 3:05 PM',
                            description: '[avail:{"rima.reyes@microsoft.com":"busy"}] Agenda: review launch readiness.'
                        }]
                    }]
                },
                blocked_question: '{"invocation_id":"matrix-1"}',
                conversation_id: 't:u:matrix'
            };
            renderDetailPane(task);
        }""",
        task_id,
    )

    matrix = page.get_by_test_id("cw-avail-matrix")
    expect(matrix).to_be_visible()
    note = page.get_by_test_id("cw-query-backed-note")
    expect(note).to_contain_text("Query-backed suggestions")
    expect(note).to_contain_text("Review the time before booking")
    note_box = note.bounding_box()
    assert note_box and note_box["height"] >= 28 and note_box["width"] >= 400
    expect(page.get_by_test_id("cw-choice")).to_have_count(0)
    expect(page.get_by_test_id("cw-avail-col-header")).to_have_count(3)
    expect(page.get_by_test_id("cw-avail-row")).to_have_count(3)
    expect(page.get_by_test_id("cw-avail-cell")).to_have_count(9)
    expect(page.locator('[data-status="free"]')).to_have_count(6)
    expect(page.locator('[data-status="tentative"]')).to_have_count(3)
    expect(page.locator('[data-status="busy"]')).to_have_count(0)
    expect(page.locator('[data-status="unknown"]')).to_have_count(0)
    first_header = page.get_by_test_id("cw-avail-col-header").nth(0)
    expect(first_header.get_by_test_id("cw-avail-head-pill")).to_contain_text(
        "Rima Reyes"
    )
    expect(first_header.get_by_test_id("cw-avail-head-pill")).to_have_attribute(
        "aria-label", "Rima Reyes"
    )
    expect(first_header.get_by_test_id("cw-avail-head-avatar")).to_have_text("RR")
    expect(first_header.get_by_test_id("cw-avail-head-avatar")).to_have_attribute(
        "title", "Rima Reyes"
    )
    expect(first_header.locator(".cw-avail-head-name")).to_be_hidden()
    expect(page.get_by_test_id("cw-avail-cell").nth(0)).to_have_attribute(
        "aria-label", "Rima Reyes: free"
    )
    redirect = page.get_by_test_id("cw-answer-redirect")
    redirect.fill("Find something later in the day")
    page.get_by_test_id("cw-avail-row").nth(0).click()
    expect(redirect).to_have_value("")
    expect(page.get_by_test_id("cw-avail-row").nth(0)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.get_by_test_id("cw-avail-row").nth(1).click()
    expect(page.get_by_test_id("cw-avail-row").nth(0)).to_have_attribute(
        "aria-pressed", "false"
    )
    expect(page.get_by_test_id("cw-avail-row").nth(1)).to_have_attribute(
        "aria-pressed", "true"
    )
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-availability-matrix-light.png"),
        full_page=True,
    )
    page.locator(".cw-avail-wrap").evaluate(
        """element => {
            const clone = element.cloneNode(true);
            clone.classList.add('cw-avail-visual-wide');
            clone.style.cssText = [
                'position:fixed', 'left:24px', 'top:24px', 'z-index:99999',
                'width:760px', 'max-width:none', 'background:var(--bg-primary)'
            ].join(';');
            document.body.appendChild(clone);
        }"""
    )
    wide = page.locator(".cw-avail-visual-wide")
    expect(wide.locator(".cw-avail-head-name").first).to_be_visible()
    wide.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-availability-matrix-wide-light.png")
    )
    wide.evaluate("element => element.remove()")
    page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-availability-matrix-dark.png"),
        full_page=True,
    )
    page.get_by_test_id("cw-answer-submit").click()
    page.wait_for_function("() => !document.querySelector('[data-testid=\"cw-blocked\"]')")
    assert posted == {
        "invocation_id": "matrix-1",
        "answers": {"0": "Tue 1:05"},
    }


def test_single_verified_time_stays_structured_and_hides_metadata(page: Page, base_url):
    page.set_viewport_size({"width": 1280, "height": 900})
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule the one available review"},
    )
    task_id = created.json()["task"]["id"]
    page.goto(base_url + "/")
    page.wait_for_function(
        f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
    )
    page.evaluate(
        """taskId => {
            const task = tasks.find(t => t.id === taskId);
            task.parse_status = 'parsed';
            task.action_type = 'schedule-meeting';
            task.key_people = JSON.stringify([
                {name: 'Rima Reyes', email: 'rima.reyes@microsoft.com'},
                {name: 'Bobby Chang', email: 'bobby.chang@microsoft.com'}
            ]);
            clearInterval(parsePollerInterval);
            parsePollerInterval = null;
            startCoworkPoller = function() {};
            selectedTaskId = taskId;
            _cwActions[taskId] = {
                task_id: taskId,
                state: 'previewing',
                waiting_on_user: true,
                interaction_request: {
                    invocation_id: 'matrix-one',
                    schedule_evidence: {
                        valid: true,
                        source: 'FindMeetingTimes+interaction',
                        query_backed: true,
                        attendees: [
                            'bobby.chang@microsoft.com',
                            'rima.reyes@microsoft.com'
                        ]
                    },
                    questions: [{
                        id: '0',
                        question: 'Only one verified time is open. Should I book it?',
                        options: [{
                            value: 'Thu 3:35',
                            label: 'Thu Aug 20 · 3:35 PM [slot:{"start":"2099-08-20T15:35:00-04:00","end":"2099-08-20T16:00:00-04:00","timezone":"Eastern Standard Time"}]',
                            description: '[slot:{"start":"2099-08-20T15:35:00-04:00","end":"2099-08-20T16:00:00-04:00","timezone":"Eastern Standard Time"}] [avail:{"rima.reyes@microsoft.com":"free"}] [avail:{"bobby.chang@microsoft.com":"tentative"}] Agenda: review launch readiness.'
                        }]
                    }]
                },
                blocked_question: '{"invocation_id":"matrix-one"}',
                conversation_id: 't:u:matrix-one'
            };
            renderDetailPane(task);
        }""",
        task_id,
    )

    expect(page.get_by_test_id("cw-avail-matrix")).to_be_visible()
    expect(page.get_by_test_id("cw-choice-grid")).to_have_count(0)
    expect(page.get_by_test_id("cw-avail-row-label")).not_to_contain_text("Agenda")
    expect(page.get_by_test_id("cw-avail-row")).to_have_count(1)
    expect(page.get_by_test_id("cw-avail-cell")).to_have_count(2)
    expect(page.get_by_test_id("cw-avail-row-label")).not_to_contain_text("[slot:")
    page.screenshot(
        path=os.path.join(TEMP_DIR, "cowork-availability-single-slot.png"),
        full_page=True,
    )


def test_invalid_availability_matrix_falls_back_to_time_pills(page: Page, base_url):
    page.goto(base_url + "/")
    # The dashboard defines `tasks` when its script runs, so evaluating
    # straight after goto is a race -- it surfaced once as an undefined task
    # when an added test shifted the timing.
    page.wait_for_function("typeof tasks !== 'undefined'")
    page.evaluate(
        """() => {
            clearInterval(parsePollerInterval);
            parsePollerInterval = null;
            tasks.push({
                id: 98,
                action_type: 'schedule-meeting',
                key_people: JSON.stringify([
                    {name: 'Rima Reyes', email: 'rima.reyes@microsoft.com'},
                    {name: 'Greg Howard', email: 'greg.howard@microsoft.com'}
                ])
            });
            document.body.innerHTML = cwInteractionFields(98, {
                questions: [{
                    id: '0',
                    question: 'Which time?',
                    options: [{
                        value: 'A',
                        label: 'Monday',
                        description: '[avail:{"rima.reyes@microsoft.com":"free"}]'
                    }, {
                        value: 'B',
                        label: 'Tuesday',
                        description: '[avail:{"rima.reyes@microsoft.com":"free","greg.howard@microsoft.com":"free"}]'
                    }]
                }]
            });
        }"""
    )

    expect(page.get_by_test_id("cw-avail-matrix")).to_have_count(0)
    expect(page.get_by_test_id("cw-choice")).to_have_count(2)

    page.evaluate(
        """() => {
            document.body.innerHTML = cwInteractionFields(98, {
                schedule_evidence: {
                    slots: [{
                        value: 'Different option',
                        availability: {
                            'rima.reyes@microsoft.com': 'free',
                            'greg.howard@microsoft.com': 'free'
                        }
                    }]
                },
                questions: [{
                    id: '0',
                    question: 'Which time?',
                    options: [{
                        value: 'A',
                        label: 'Monday',
                        description: ''
                    }, {
                        value: 'B',
                        label: 'Tuesday',
                        description: ''
                    }]
                }]
            });
        }"""
    )

    expect(page.get_by_test_id("cw-avail-matrix")).to_have_count(0)
    expect(page.get_by_test_id("cw-choice")).to_have_count(2)

    page.evaluate(
        """() => {
            document.body.innerHTML = cwInteractionFields(98, {
                questions: [{
                    id: '0',
                    question: 'Which time?',
                    options: [{
                        value: 'A',
                        label: 'Monday',
                        description: '[avail:{"rima.reyes@microsoft.com":"free"}] [avail:{"rima.reyes@microsoft.com":"tentative","greg.howard@microsoft.com":"free"}]'
                    }, {
                        value: 'B',
                        label: 'Tuesday',
                        description: '[avail:{"rima.reyes@microsoft.com":"free","greg.howard@microsoft.com":"free"}]'
                    }]
                }]
            });
        }"""
    )

    expect(page.get_by_test_id("cw-avail-matrix")).to_have_count(0)
    expect(page.get_by_test_id("cw-choice")).to_have_count(2)

    page.evaluate(
        """() => {
            const task = tasks.find(item => item.id === 98);
            task.key_people = JSON.stringify([
                {name: 'Rima Reyes', email: 'rima.reyes@microsoft.com'},
                {name: 'Greg Howard', email: 'greg.howard@microsoft.com'},
                {name: 'Sarah Chen'}
            ]);
            document.body.innerHTML = cwInteractionFields(98, {
                questions: [{
                    id: '0',
                    question: 'Which time?',
                    options: [{
                        value: 'A',
                        label: 'Monday',
                        description: '[avail:{"rima.reyes@microsoft.com":"free","greg.howard@microsoft.com":"free"}]'
                    }, {
                        value: 'B',
                        label: 'Tuesday',
                        description: '[avail:{"rima.reyes@microsoft.com":"tentative","greg.howard@microsoft.com":"free"}]'
                    }]
                }]
            });
        }"""
    )

    expect(page.get_by_test_id("cw-avail-matrix")).to_have_count(0)
    expect(page.get_by_test_id("cw-choice")).to_have_count(2)


def test_new_key_person_is_queued_for_identity_resolution(page: Page, base_url):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule a review", "parse_status": "parsed"},
    )
    task_id = created.json()["task"]["id"]
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """taskId => {
                const input = document.createElement('input');
                input.id = `add-person-name-${taskId}`;
                input.value = 'Henry James';
                document.body.appendChild(input);
            }""",
            task_id,
        )

        with page.expect_request(
            lambda request: request.method == "POST"
            and request.url.endswith(f"/api/tasks/{task_id}/refresh")
        ):
            page.evaluate("taskId => saveNewPerson(taskId)", task_id)

        page.wait_for_function(
            """async taskId => {
                const response = await fetch(`/api/tasks/${taskId}`);
                const task = (await response.json()).task;
                return task.parse_status === 'unparsed';
            }""",
            arg=task_id,
        )
        stored = page.request.get(f"{base_url}/api/tasks/{task_id}").json()["task"]
        people = json.loads(stored["key_people"])
        assert people == [{
            "name": "Henry James",
            "alternatives": [],
            "unresolved": True,
        }]
    finally:
        page.request.delete(f"{base_url}/api/tasks/{task_id}")


def test_start_over_blocks_and_refreshes_unresolved_attendee(page: Page, base_url):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule a review", "parse_status": "parsed"},
    )
    task_id = created.json()["task"]["id"]
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """taskId => {
                const task = tasks.find(t => t.id === taskId);
                task.action_type = 'schedule-meeting';
                task.key_people = JSON.stringify([
                    {name: 'Rima Reyes', email: 'rima@microsoft.com'},
                    {name: 'Henry James', alternatives: []}
                ]);
            }""",
            task_id,
        )

        with page.expect_request(
            lambda request: request.method == "POST"
            and request.url.endswith(f"/api/tasks/{task_id}/refresh")
        ):
            page.evaluate("taskId => cwStart(taskId, true)", task_id)

        assert dialogs == [
            "Resolve Henry James in Key People before scheduling. "
            "Riveter is refreshing identity matches now."
        ]
    finally:
        page.request.delete(f"{base_url}/api/tasks/{task_id}")


def test_start_blocks_and_refreshes_empty_attendee_list(page: Page, base_url):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule from linked chat", "parse_status": "parsed"},
    )
    task_id = created.json()["task"]["id"]
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """taskId => {
                const task = tasks.find(t => t.id === taskId);
                task.action_type = 'schedule-meeting';
                task.key_people = '[]';
            }""",
            task_id,
        )

        with page.expect_request(
            lambda request: request.method == "POST"
            and request.url.endswith(f"/api/tasks/{task_id}/refresh")
        ):
            page.evaluate("taskId => cwStart(taskId, true)", task_id)

        assert dialogs == [
            "Add and confirm at least one attendee before scheduling. "
            "Riveter is resolving the linked Teams participants now."
        ]
    finally:
        page.request.delete(f"{base_url}/api/tasks/{task_id}")


def test_unresolved_person_pill_explains_identity_resolution(page: Page, base_url):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule a review", "parse_status": "parsed"},
    )
    task_id = created.json()["task"]["id"]
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """taskId => {
                const task = tasks.find(t => t.id === taskId);
                task.parse_status = 'parsed';
                task.action_type = 'schedule-meeting';
                task.key_people = JSON.stringify([
                    {name: 'Henry James', alternatives: [], unresolved: true}
                ]);
                selectedTaskId = taskId;
                renderDetailPane(task);
            }""",
            task_id,
        )

        unresolved = page.locator(".person-pill.is-unresolved")
        expect(unresolved).to_contain_text("Henry James")
        unresolved.click()
        expect(page.locator(".alternatives-dropdown.open")).to_contain_text(
            "Resolving identity"
        )
        expect(page.locator(".alternatives-dropdown.open")).to_contain_text(
            "Choose the right person here when they appear"
        )
        page.screenshot(
            path=os.path.join(TEMP_DIR, "cowork-unresolved-person-light.png"),
            full_page=True,
        )
        page.evaluate(
            "document.documentElement.setAttribute('data-theme', 'dark')"
        )
        page.screenshot(
            path=os.path.join(TEMP_DIR, "cowork-unresolved-person-dark.png"),
            full_page=True,
        )
    finally:
        page.request.delete(f"{base_url}/api/tasks/{task_id}")


def test_cowork_pane_waits_for_identity_confirmation_during_reparse(
    page: Page, base_url
):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule a review", "parse_status": "parsed"},
    )
    task_id = created.json()["task"]["id"]
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """taskId => {
                clearInterval(parsePollerInterval);
                parsePollerInterval = null;
                const task = tasks.find(t => t.id === taskId);
                task.parse_status = 'queued';
                task.action_type = 'schedule-meeting';
                task.key_people = JSON.stringify([{
                    name: 'Henry Jammes',
                    email: 'Henry.Jammes@microsoft.com',
                    role: 'Principal PM Manager',
                    unresolved: true,
                    alternatives: [{
                        name: 'James Henry',
                        email: 'jameshenry@microsoft.com',
                        role: 'Principal Data Scientist'
                    }]
                }]);
                _cwActions[taskId] = {
                    id: taskId,
                    task_id: taskId,
                    state: 'ready',
                    finding: 'Previous finding',
                    draft: 'Previous draft'
                };
                selectedTaskId = taskId;
                renderDetailPane(task);
            }""",
            task_id,
        )

        pending = page.get_by_test_id("cw-identity-pending")
        expect(page.locator(".detail-workspace .cw-card")).to_be_visible()
        expect(pending).to_contain_text("Henry Jammes")
        expect(pending).to_contain_text("Choose")
        expect(page.get_by_test_id("cw-execute-action")).to_have_count(0)
        expect(page.get_by_text("Preview with Cowork", exact=True)).to_have_count(0)

        page.evaluate(
            """taskId => {
                const task = tasks.find(t => t.id === taskId);
                task.parse_status = 'parsed';
                renderDetailPane(task);
            }""",
            task_id,
        )
        expect(page.get_by_test_id("cw-identity-pending")).to_contain_text(
            "Henry Jammes"
        )

        page.screenshot(
            path=os.path.join(TEMP_DIR, "cowork-identity-pending-light.png"),
            full_page=True,
        )

        page.evaluate(
            """taskId => {
                const task = tasks.find(t => t.id === taskId);
                task.key_people = JSON.stringify([{
                    name: 'Henry Jammes',
                    email: 'Henry.Jammes@microsoft.com',
                    role: 'Principal PM Manager'
                }]);
                _cwActions[taskId] = null;
                renderDetailPane(task);
            }""",
            task_id,
        )
        expect(page.get_by_test_id("cw-identity-pending")).to_have_count(0)
        expect(page.get_by_text("Preview with WorkIQ", exact=True)).to_be_visible()
    finally:
        page.request.delete(f"{base_url}/api/tasks/{task_id}")


def test_identity_refresh_does_not_hide_live_cowork_action(page: Page, base_url):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule a review", "parse_status": "parsed"},
    )
    task_id = created.json()["task"]["id"]
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """taskId => {
                clearInterval(parsePollerInterval);
                parsePollerInterval = null;
                const task = tasks.find(t => t.id === taskId);
                task.parse_status = 'queued';
                task.action_type = 'schedule-meeting';
                task.key_people = JSON.stringify([{
                    name: 'Henry Jammes',
                    email: 'Henry.Jammes@microsoft.com',
                    unresolved: true,
                    alternatives: []
                }]);
                _cwActions[taskId] = {
                    id: taskId,
                    task_id: taskId,
                    state: 'previewing',
                    progress: ['Checking attendee calendars']
                };
                _cwPollers[taskId] = setInterval(function() {}, 60000);
                selectedTaskId = taskId;
                renderDetailPane(task);
            }""",
            task_id,
        )

        expect(page.get_by_test_id("cw-identity-pending")).to_have_count(0)
        expect(page.locator(".cw-card")).to_contain_text(
            "Checking attendee calendars"
        )
        expect(page.get_by_test_id("cw-stop")).to_be_visible()

        page.evaluate(
            """taskId => {
                const task = tasks.find(t => t.id === taskId);
                task.parse_status = 'error';
                renderDetailPane(task);
            }""",
            task_id,
        )
        expect(page.locator(".cw-card")).to_contain_text(
            "Checking attendee calendars"
        )
    finally:
        page.evaluate(
            """taskId => {
                clearInterval(_cwPollers[taskId]);
                delete _cwPollers[taskId];
            }""",
            task_id,
        )
        page.request.delete(f"{base_url}/api/tasks/{task_id}")


def test_identity_refresh_loads_persisted_live_cowork_action(page: Page, base_url):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule a review", "parse_status": "parsed"},
    )
    task_id = created.json()["task"]["id"]
    action = {
        "id": task_id,
        "task_id": task_id,
        "state": "previewing",
        "action_type": "schedule-meeting",
        "cowork_revision": 0,
        "progress": ["Loading the saved Cowork preview"],
    }
    page.route(
        f"**/api/tasks/{task_id}/cowork*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"action": action}),
        ),
    )
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """taskId => {
                clearInterval(parsePollerInterval);
                parsePollerInterval = null;
                const task = tasks.find(t => t.id === taskId);
                task.parse_status = 'error';
                task.action_type = 'schedule-meeting';
                task.key_people = JSON.stringify([{
                    name: 'Henry Jammes',
                    email: 'Henry.Jammes@microsoft.com',
                    unresolved: true,
                    alternatives: []
                }]);
                delete _cwActions[taskId];
                selectedTaskId = taskId;
                renderDetailPane(task);
            }""",
            task_id,
        )

        expect(page.locator(".cw-card")).to_contain_text(
            "Loading the saved Cowork preview"
        )
        expect(page.get_by_test_id("cw-identity-pending")).to_have_count(0)
        expect(page.get_by_test_id("cw-stop")).to_be_visible()
    finally:
        page.evaluate(
            """taskId => {
                clearInterval(_cwPollers[taskId]);
                delete _cwPollers[taskId];
            }""",
            task_id,
        )
        page.request.delete(f"{base_url}/api/tasks/{task_id}")


def test_directory_match_requires_explicit_dropdown_confirmation(
    page: Page, base_url
):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule a review", "parse_status": "parsed"},
    )
    task_id = created.json()["task"]["id"]
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """taskId => {
                clearInterval(parsePollerInterval);
                parsePollerInterval = null;
                const task = tasks.find(t => t.id === taskId);
                task.parse_status = 'parsed';
                task.action_type = 'schedule-meeting';
                task.key_people = JSON.stringify([{
                    name: 'Henry James',
                    email: 'henry@microsoft.com',
                    unresolved: true,
                    alternatives: [{
                        name: 'Henry Jamison',
                        email: 'henry.jamison@microsoft.com'
                    }]
                }]);
                selectedTaskId = taskId;
                renderDetailPane(task);
            }""",
            task_id,
        )

        unresolved = page.locator(".person-pill.is-unresolved")
        unresolved.click()
        primary = page.locator(".alternative-item.selected")
        expect(primary).to_contain_text("Henry James")
        expect(primary).to_contain_text("henry@microsoft.com")

        refresh_requests = []
        page.on(
            "request",
            lambda request: refresh_requests.append(request.url)
            if request.method == "POST"
            and request.url.endswith(f"/api/tasks/{task_id}/refresh")
            else None,
        )
        with page.expect_request(
            lambda request: request.method == "PUT"
            and request.url.endswith(f"/api/tasks/{task_id}")
        ) as request_info:
            primary.click()

        people = json.loads(request_info.value.post_data_json["key_people"])
        assert people[0]["name"] == "Henry James"
        assert people[0]["email"] == "henry@microsoft.com"
        assert "unresolved" not in people[0]
        page.wait_for_timeout(300)
        assert refresh_requests == []
        persisted = page.request.get(f"{base_url}/api/tasks/{task_id}").json()["task"]
        persisted_people = json.loads(persisted["key_people"])
        assert persisted["parse_status"] == "parsed"
        assert persisted_people[0]["name"] == "Henry James"
        assert "unresolved" not in persisted_people[0]

        page.reload()
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate("taskId => selectTask(taskId)", task_id)
        expect(page.locator(".person-pill.is-unresolved")).to_have_count(0)
        expect(page.locator(".person-pill")).to_contain_text("Henry James")
    finally:
        page.request.delete(f"{base_url}/api/tasks/{task_id}")


def test_selecting_an_alternate_identity_refreshes_once(page: Page, base_url):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule a review", "parse_status": "parsed"},
    )
    task_id = created.json()["task"]["id"]
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """taskId => {
                const task = tasks.find(t => t.id === taskId);
                task.parse_status = 'parsed';
                task.action_type = 'schedule-meeting';
                task.key_people = JSON.stringify([{
                    name: 'Henry James',
                    email: 'henry@microsoft.com',
                    unresolved: true,
                    alternatives: [{
                        name: 'Henry Jamison',
                        email: 'henry.jamison@microsoft.com'
                    }]
                }]);
                window.identityRefreshes = [];
                window.attendanceAlerts = [];
                refreshTask = id => window.identityRefreshes.push(id);
                window.alert = message => window.attendanceAlerts.push(message);
                _cwActions[taskId] = null;
                selectedTaskId = taskId;
                renderDetailPane(task);
            }""",
            task_id,
        )

        page.locator(".person-pill.is-unresolved").click()
        alternate = page.locator(".alternative-item").filter(
            has_text="Henry Jamison"
        )
        alternate.click()
        page.wait_for_function(
            "taskId => window.identityRefreshes.length === 1", arg=task_id
        )

        assert page.evaluate("window.identityRefreshes") == [task_id]
        persisted = page.request.get(f"{base_url}/api/tasks/{task_id}").json()["task"]
        people = json.loads(persisted["key_people"])
        assert people[0]["name"] == "Henry Jamison"
        assert people[0]["email"] == "henry.jamison@microsoft.com"
        assert "unresolved" not in people[0]
    finally:
        page.request.delete(f"{base_url}/api/tasks/{task_id}")


def test_exact_group_member_requires_attendance_confirmation(page: Page, base_url):
    created = page.request.post(
        base_url + "/api/tasks",
        data={"title": "Schedule a group review", "parse_status": "parsed"},
    )
    task_id = created.json()["task"]["id"]
    try:
        page.goto(base_url + "/")
        page.wait_for_function(
            f"typeof tasks !== 'undefined' && tasks.some(t => t.id === {task_id})"
        )
        page.evaluate(
            """taskId => {
                const task = tasks.find(t => t.id === taskId);
                task.parse_status = 'parsed';
                task.action_type = 'schedule-meeting';
                task.key_people = JSON.stringify([{
                    name: 'Exact Chat Member',
                    email: 'member@microsoft.com',
                    aad_object_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                    attendance_uncertain: true,
                    alternatives: []
                }]);
                window.identityRefreshes = [];
                window.attendanceAlerts = [];
                refreshTask = id => window.identityRefreshes.push(id);
                window.alert = message => window.attendanceAlerts.push(message);
                _cwActions[taskId] = null;
                selectedTaskId = taskId;
                renderDetailPane(task);
            }""",
            task_id,
        )

        pill = page.locator(".person-pill.is-unresolved")
        expect(pill).to_have_attribute(
            "title", "Confirm this attendee before scheduling"
        )
        pill.click()
        dropdown = page.locator(".alternatives-dropdown.open")
        expect(dropdown).to_contain_text(
            "Confirm attendee"
        )
        expect(page.get_by_test_id("cw-identity-pending")).to_contain_text(
            "Confirm who should attend"
        )
        pill_box = pill.bounding_box()
        dropdown_box = dropdown.bounding_box()
        assert pill_box and pill_box["height"] >= 24 and pill_box["width"] >= 120
        assert dropdown_box and dropdown_box["height"] >= 70
        page.screenshot(
            path=os.path.join(TEMP_DIR, "cowork-attendance-confirmation.png"),
            full_page=True,
        )

        page.evaluate(
            """taskId => {
                cwStart(taskId);
                _cwActions[taskId] = {
                    task_id: taskId,
                    state: 'ready',
                    action_type: 'schedule-meeting',
                    draft: 'Meeting review'
                };
                cwOpenExecuteConfirm(taskId);
            }""",
            task_id,
        )
        assert page.evaluate("window.attendanceAlerts") == [
            "Confirm whether Exact Chat Member should attend before scheduling.",
            "Confirm whether Exact Chat Member should attend before scheduling.",
        ]
        assert page.evaluate("window.identityRefreshes") == []

        page.locator(".alternative-item.selected").click()
        page.wait_for_timeout(300)

        assert page.evaluate("window.identityRefreshes") == []
        persisted = page.request.get(f"{base_url}/api/tasks/{task_id}").json()["task"]
        people = json.loads(persisted["key_people"])
        assert "attendance_uncertain" not in people[0]
        assert "unresolved" not in people[0]
    finally:
        page.request.delete(f"{base_url}/api/tasks/{task_id}")
