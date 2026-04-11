"""E2E tests for the TodoIQ alternate UI."""

import os
import re
import json
import pytest
from playwright.sync_api import Page, expect

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCREENSHOTS_DIR = os.path.join(PROJECT_ROOT, 'test-runs', 'playwright-screenshots')
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def _step(msg):
    print(f'  -> {msg}')


def _screenshot(page, name):
    page.screenshot(path=os.path.join(SCREENSHOTS_DIR, f'{name}.png'), full_page=True)


def _seed_task(page, base_url, title='Test task', **kwargs):
    """Create a task via API and return its ID."""
    body = {'title': title, 'description': 'E2E test task', **kwargs}
    resp = page.request.post(f'{base_url}/api/tasks', data=body)
    assert resp.ok, f'Failed to create task: {resp.status}'
    data = resp.json()
    return data['task']['id']


def _delete_task(page, base_url, task_id):
    """Delete a task via API."""
    page.request.delete(f'{base_url}/api/tasks/{task_id}')


class TestTodoIQLoads:
    """Test that the TodoIQ UI loads and renders correctly."""

    def test_todoiq_route_exists(self, page: Page, base_url):
        _step('Navigate to /todo')
        resp = page.goto(base_url + '/todo')
        assert resp.status == 200
        _step('Verify page title is TodoIQ')
        expect(page).to_have_title('TodoIQ')
        _screenshot(page, 'todoiq-01-loaded')

    def test_old_dashboard_still_works(self, page: Page, base_url):
        _step('Navigate to / (old dashboard)')
        resp = page.goto(base_url + '/')
        assert resp.status == 200
        expect(page).to_have_title(re.compile('TodoNess'))
        _step('Both routes coexist')

    def test_sidebar_nav_renders(self, page: Page, base_url):
        page.goto(base_url + '/todo')
        page.wait_for_function('typeof tasks !== "undefined" && tasks.length >= 0')
        _step('Verify sidebar nav has expected items')
        nav_items = page.locator('.nav-item')
        expect(nav_items).to_have_count(7)
        # Check key labels exist
        nav_text = page.locator('nav').text_content()
        for label in ['My Day', 'Ready', 'Quick Tasks', 'All Tasks',
                       'Waiting', 'Suggestions', 'Done']:
            assert label in nav_text, f'Nav missing: {label}'
        _screenshot(page, 'todoiq-02-nav')

    def test_api_adapter_loads(self, page: Page, base_url):
        page.goto(base_url + '/todo')
        page.wait_for_function('typeof tasks !== "undefined"')
        _step('Verify API adapter loaded')
        has_api = page.evaluate('typeof fetchTasks === "function" && typeof connectWS === "function"')
        assert has_api, 'API adapter functions not found'

    def test_websocket_connects(self, page: Page, base_url):
        page.goto(base_url + '/todo')
        page.wait_for_function('typeof _ws !== "undefined" && _ws && _ws.readyState === 1',
                               timeout=10000)
        _step('WebSocket connected')


class TestTodoIQViews:
    """Test that all views render correctly."""

    def test_myday_view(self, page: Page, base_url):
        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length >= 0')
        _step('Verify My Day is the default view')
        title = page.locator('#list-title')
        expect(title).to_have_text('My Day')
        _screenshot(page, 'todoiq-03-myday')

    def test_all_views_render(self, page: Page, base_url):
        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length >= 0')
        views = ['ready', 'quick', 'all', 'waiting', 'review', 'done', 'myday']
        for view_id in views:
            page.evaluate(f"setView('{view_id}')")
            page.wait_for_timeout(200)
            _step(f'View "{view_id}" rendered')
        _screenshot(page, 'todoiq-04-views')


class TestTodoIQTaskCRUD:
    """Test task creation, reading, updating, and deletion."""

    def test_create_task(self, page: Page, base_url):
        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length >= 0')

        _step('Create a task via the add bar')
        initial_count = page.evaluate('tasks.length')
        page.fill('#add-input', 'E2E test task from TodoIQ')
        page.locator('#add-input').press('Enter')
        page.wait_for_timeout(2000)  # Wait for API + WS

        new_count = page.evaluate('tasks.length')
        assert new_count > initial_count, f'Task count did not increase: {initial_count} -> {new_count}'

        # Find and clean up
        task_id = page.evaluate(
            "tasks.find(t => t.title === 'E2E test task from TodoIQ')?.id"
        )
        assert task_id, 'Created task not found in tasks array'
        _step(f'Task created with ID {task_id}')

        # Clean up
        _delete_task(page, base_url, task_id)
        _screenshot(page, 'todoiq-05-create')

    def test_promote_suggestion(self, page: Page, base_url):
        # Seed a suggested task
        task_id = _seed_task(page, base_url, 'E2E suggestion to promote')
        page.request.post(f'{base_url}/api/tasks/{task_id}/action',
                          data={'action': 'transition', 'status': 'suggested'})

        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length > 0')

        _step('Promote the suggestion')
        result = page.evaluate(f'''async () => {{
            await promoteTask({task_id});
            const t = tasks.find(t => t.id === {task_id});
            return t ? t.status : 'not found';
        }}''')
        assert result == 'active', f'Expected active, got {result}'

        _delete_task(page, base_url, task_id)
        _screenshot(page, 'todoiq-06-promote')

    def test_complete_and_restore(self, page: Page, base_url):
        task_id = _seed_task(page, base_url, 'E2E task to complete')
        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length > 0')

        _step('Complete the task')
        status = page.evaluate(f'''async () => {{
            await toggleComplete({task_id});
            return tasks.find(t => t.id === {task_id})?.status;
        }}''')
        assert status == 'completed', f'Expected completed, got {status}'

        _step('Restore the task')
        status = page.evaluate(f'''async () => {{
            await toggleComplete({task_id});
            return tasks.find(t => t.id === {task_id})?.status;
        }}''')
        assert status == 'active', f'Expected active, got {status}'

        _delete_task(page, base_url, task_id)

    def test_dismiss_and_undo(self, page: Page, base_url):
        task_id = _seed_task(page, base_url, 'E2E task to dismiss')
        page.request.post(f'{base_url}/api/tasks/{task_id}/action',
                          data={'action': 'transition', 'status': 'suggested'})

        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length > 0')

        _step('Dismiss the suggestion')
        status = page.evaluate(f'''async () => {{
            await dismissTask({task_id});
            return tasks.find(t => t.id === {task_id})?.status;
        }}''')
        assert status == 'dismissed', f'Expected dismissed, got {status}'

        _step('Check undo toast appeared')
        has_toast = page.evaluate("!!document.querySelector('.undo-toast')")
        assert has_toast, 'Undo toast not found'

        _step('Undo the dismiss')
        status = page.evaluate(f'''async () => {{
            await undoDismiss({task_id}, 'suggested');
            return tasks.find(t => t.id === {task_id})?.status;
        }}''')
        assert status == 'suggested', f'Expected suggested, got {status}'

        _delete_task(page, base_url, task_id)

    def test_delete_task(self, page: Page, base_url):
        task_id = _seed_task(page, base_url, 'E2E task to delete')
        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length > 0')

        _step('Delete the task')
        result = page.evaluate(f'''async () => {{
            await deleteTask({task_id});
            return tasks.find(t => t.id === {task_id});
        }}''')
        assert result is None, 'Task still in array after delete'


class TestTodoIQDetailPane:
    """Test the detail pane interactions."""

    def test_select_task_opens_detail(self, page: Page, base_url):
        task_id = _seed_task(page, base_url, 'E2E detail pane test')
        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length > 0')

        _step('Select task to open detail pane')
        page.evaluate(f'selectTask({task_id})')
        page.wait_for_timeout(300)

        detail = page.locator('#detail-panel')
        expect(detail).to_have_class(re.compile('open'))

        title_text = page.locator('.detail-title').text_content()
        assert 'E2E detail pane test' in title_text

        _step('Close detail pane')
        page.evaluate('closeDetail()')
        expect(detail).not_to_have_class(re.compile('open'))

        _delete_task(page, base_url, task_id)
        _screenshot(page, 'todoiq-07-detail')

    def test_inline_edit_title(self, page: Page, base_url):
        task_id = _seed_task(page, base_url, 'E2E old title')
        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length > 0')
        page.evaluate(f'selectTask({task_id})')
        page.wait_for_timeout(300)

        _step('Edit title inline')
        result = page.evaluate(f'''async () => {{
            await saveTitle({task_id}, 'E2E new title');
            return tasks.find(t => t.id === {task_id})?.title;
        }}''')
        assert result == 'E2E new title', f'Title not updated: {result}'

        # Verify via API
        resp = page.request.get(f'{base_url}/api/tasks/{task_id}')
        api_title = resp.json()['task']['title']
        assert api_title == 'E2E new title', f'API title: {api_title}'

        _delete_task(page, base_url, task_id)

    def test_set_priority(self, page: Page, base_url):
        task_id = _seed_task(page, base_url, 'E2E priority test', priority=3)
        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length > 0')
        page.evaluate(f'selectTask({task_id})')

        _step('Set priority to P1')
        result = page.evaluate(f'''async () => {{
            await setPriority({task_id}, 'P1');
            return tasks.find(t => t.id === {task_id})?.priority;
        }}''')
        assert result == 'P1', f'Priority not set: {result}'

        # Verify via API
        resp = page.request.get(f'{base_url}/api/tasks/{task_id}')
        api_pri = resp.json()['task']['priority']
        assert api_pri == 1, f'API priority: {api_pri}'

        _delete_task(page, base_url, task_id)

    def test_set_due_date(self, page: Page, base_url):
        task_id = _seed_task(page, base_url, 'E2E due date test')
        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length > 0')
        page.evaluate(f'selectTask({task_id})')

        _step('Set due date')
        result = page.evaluate(f'''async () => {{
            if (typeof setDueDate === 'function') {{
                await setDueDate({task_id}, '2026-04-20');
                return tasks.find(t => t.id === {task_id})?.due_date;
            }}
            return 'setDueDate not found';
        }}''')
        assert '2026-04-20' in str(result), f'Due date not set: {result}'

        _delete_task(page, base_url, task_id)


class TestTodoIQStatusTransitions:
    """Test task status transitions via API."""

    def test_start_task(self, page: Page, base_url):
        task_id = _seed_task(page, base_url, 'E2E start test')
        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length > 0')

        status = page.evaluate(f'''async () => {{
            await startTask({task_id});
            return tasks.find(t => t.id === {task_id})?.status;
        }}''')
        assert status == 'in_progress', f'Expected in_progress, got {status}'
        _delete_task(page, base_url, task_id)

    def test_snooze_task(self, page: Page, base_url):
        task_id = _seed_task(page, base_url, 'E2E snooze test')
        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length > 0')

        _step('Snooze for 1 hour')
        status = page.evaluate(f'''async () => {{
            if (typeof doSnoozeHours === 'function') {{
                await doSnoozeHours({task_id}, 1);
                return tasks.find(t => t.id === {task_id})?.status;
            }}
            return 'doSnoozeHours not found';
        }}''')
        assert status == 'snoozed', f'Expected snoozed, got {status}'

        _step('Wake task')
        status = page.evaluate(f'''async () => {{
            await wakeTask({task_id});
            return tasks.find(t => t.id === {task_id})?.status;
        }}''')
        assert status == 'active', f'Expected active after wake, got {status}'
        _delete_task(page, base_url, task_id)

    def test_waiting_transition(self, page: Page, base_url):
        task_id = _seed_task(page, base_url, 'E2E waiting test')
        page.goto(base_url + '/todo')
        page.wait_for_function('typeof transitionTask === "function" && tasks.length > 0',
                               timeout=10000)

        status = page.evaluate(f'''async () => {{
            await transitionTask({task_id}, 'waiting');
            return tasks.find(t => t.id === {task_id})?.status;
        }}''')
        assert status == 'waiting', f'Expected waiting, got {status}'
        _delete_task(page, base_url, task_id)


class TestTodoIQSync:
    """Test sync functionality."""

    def test_sync_triggers_without_error(self, page: Page, base_url):
        page.goto(base_url + '/todo')
        page.wait_for_function('tasks.length >= 0')

        _step('Trigger sync')
        # doSync is async and uses setTimeout internally; just verify no JS error
        error = page.evaluate('''async () => {
            try {
                const b = document.getElementById('sync-btn');
                if (b) b.classList.remove('syncing'); // reset
                await doSync();
                return null;
            } catch(e) { return e.message; }
        }''')
        assert error is None, f'Sync error: {error}'
        _screenshot(page, 'todoiq-08-sync')
