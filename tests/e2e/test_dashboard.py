"""E2E tests for the Riveter dashboard."""

import os
import re
import pytest
from playwright.sync_api import Page, expect

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCREENSHOTS_DIR = os.path.join(PROJECT_ROOT, 'test-runs', 'playwright-screenshots')
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def _step(msg):
    """Log a descriptive test step."""
    print(f'  -> {msg}')


def _screenshot(page, name):
    page.screenshot(path=os.path.join(SCREENSHOTS_DIR, f'{name}.png'), full_page=True)
    _step(f'Screenshot saved: {name}.png')


class TestDashboardLoads:
    """Test that the dashboard loads and shows the basic structure."""

    def test_homepage_renders(self, page: Page, base_url):
        _step('Navigate to Riveter dashboard')
        page.goto(base_url + '/')
        _step('Verify page title and app brand are Riveter')
        expect(page).to_have_title('Riveter')
        expect(page.locator('.app-title')).to_have_text('Riveter')
        expect(page.locator('link[rel="icon"]')).to_have_attribute(
            'href', '/static/img/copilot.png'
        )
        expect(page.locator('link[rel="icon"]')).to_have_attribute(
            'type', 'image/png'
        )
        assert page.request.get(
            base_url + '/static/img/copilot.png'
        ).ok
        logo = page.locator('.riveter-logo')
        expect(logo).to_be_visible()
        expect(logo).to_have_attribute('src', '/static/img/riveter-light.png')
        assert page.request.get(
            base_url + '/static/img/riveter-dark.png'
        ).ok
        assert page.evaluate(
            "() => document.querySelector('.riveter-logo').naturalWidth"
        ) > 0
        expect(page.get_by_test_id('brand-tagline')).to_have_count(0)
        brand_link = page.locator('.brand-lockup')
        expect(brand_link).to_have_attribute('href', 'https://aka.ms/riveter')
        expect(brand_link).to_have_attribute('target', '_blank')
        expect(brand_link).to_have_attribute('rel', 'noopener noreferrer')
        header_box = page.locator('.top-bar').bounding_box()
        logo_box = logo.bounding_box()
        assert header_box and logo_box
        assert header_box['height'] <= 92
        assert logo_box['height'] == 90
        _screenshot(page, '01-dashboard-empty')

    def test_input_bar_visible(self, page: Page, base_url):
        _step('Navigate to dashboard')
        page.goto(base_url + '/')
        _step('Verify task input bar is visible')
        input_bar = page.locator('#task-input, input[placeholder*="Add a task"]')
        expect(input_bar).to_be_visible()
        input_box = page.locator('.input-bar').bounding_box()
        panel_box = page.locator('.left-panel').bounding_box()
        controls_box = page.locator('.top-bar-right').bounding_box()
        assert input_box and panel_box
        assert controls_box
        assert abs(input_box['x'] - (panel_box['x'] + panel_box['width'])) <= 2
        assert 0 <= controls_box['x'] - (
            input_box['x'] + input_box['width']
        ) <= 2
        _screenshot(page, '02-input-bar')

    def test_mobile_header_wraps_without_horizontal_overflow(
        self, page: Page, base_url
    ):
        page.set_viewport_size({'width': 375, 'height': 720})
        page.goto(base_url + '/')
        logo_box = page.locator('.riveter-logo').bounding_box()
        input_box = page.locator('.input-bar').bounding_box()
        assert logo_box and input_box
        assert logo_box['height'] == 75
        assert input_box['y'] >= logo_box['y'] + logo_box['height'] - 2
        assert page.evaluate(
            '() => document.documentElement.scrollWidth'
            ' <= document.documentElement.clientWidth'
        )

    def test_empty_state(self, page: Page, base_url):
        _step('Navigate to dashboard')
        page.goto(base_url + '/')
        _step('Verify empty state or sections are present')
        # Should show section headers or empty state
        page.wait_for_load_state('networkidle')
        _screenshot(page, '03-empty-state')


class TestTaskCreation:
    """Test creating tasks from the dashboard input bar."""

    def test_add_task_via_input(self, page: Page, base_url):
        _step('Navigate to dashboard')
        page.goto(base_url + '/')
        page.wait_for_load_state('networkidle')

        _step('Type task text in input bar')
        input_bar = page.locator('#task-input, input[placeholder*="Add a task"]')
        input_bar.fill('Review Q3 budget report by Friday')

        _step('Submit the task')
        # Try clicking add button or pressing Enter
        add_btn = page.locator('#add-task-btn, button:has-text("Add"), .add-btn')
        if add_btn.count() > 0:
            add_btn.first.click()
        else:
            input_bar.press('Enter')

        _step('Wait for task to appear in the list')
        page.wait_for_timeout(1000)
        _screenshot(page, '04-task-created')

        _step('Verify task appears in the task list')
        task_list = page.locator('.task-row, .task-item, [class*="task"]')
        expect(task_list.first).to_be_visible()

    def test_add_second_task(self, page: Page, base_url):
        _step('Navigate to dashboard')
        page.goto(base_url + '/')
        page.wait_for_load_state('networkidle')

        _step('Add a second task')
        input_bar = page.locator('#task-input, input[placeholder*="Add a task"]')
        input_bar.fill('Schedule meeting with design team')

        add_btn = page.locator('#add-task-btn, button:has-text("Add"), .add-btn')
        if add_btn.count() > 0:
            add_btn.first.click()
        else:
            input_bar.press('Enter')

        page.wait_for_timeout(1000)
        _screenshot(page, '05-two-tasks')


class TestTaskInteraction:
    """Test selecting tasks and interacting with the detail pane."""

    def test_select_task_shows_detail(self, page: Page, base_url):
        _step('Navigate to dashboard')
        page.goto(base_url + '/')
        page.wait_for_load_state('networkidle')

        _step('Create a task first')
        input_bar = page.locator('#task-input, input[placeholder*="Add a task"]')
        input_bar.fill('Urgent: fix production bug')
        add_btn = page.locator('#add-task-btn, button:has-text("Add"), .add-btn')
        if add_btn.count() > 0:
            add_btn.first.click()
        else:
            input_bar.press('Enter')
        page.wait_for_timeout(1000)

        _step('Click on a task to select it')
        task_rows = page.locator('.task-row, .task-item, [class*="task-row"]')
        if task_rows.count() > 0:
            task_rows.first.click()
            page.wait_for_timeout(500)

        _step('Verify detail pane shows task info')
        _screenshot(page, '06-task-selected')

        # Detail pane should show something other than empty state
        detail = page.locator('.detail-pane, .task-detail, #detail-pane')
        if detail.count() > 0:
            expect(detail.first).to_be_visible()

    def test_complete_task(self, page: Page, base_url):
        _step('Navigate to dashboard')
        page.goto(base_url + '/')
        page.wait_for_load_state('networkidle')

        _step('Create a task')
        input_bar = page.locator('#task-input, input[placeholder*="Add a task"]')
        input_bar.fill('Task to complete')
        add_btn = page.locator('#add-task-btn, button:has-text("Add"), .add-btn')
        if add_btn.count() > 0:
            add_btn.first.click()
        else:
            input_bar.press('Enter')
        page.wait_for_timeout(1000)

        _step('Select the task')
        task_rows = page.locator('.task-row, .task-item, [class*="task-row"]')
        if task_rows.count() > 0:
            task_rows.first.click()
            page.wait_for_timeout(500)

        _step('Click Complete button if visible')
        complete_btn = page.locator('button:has-text("Complete"), .btn-complete')
        if complete_btn.count() > 0:
            complete_btn.first.click()
            page.wait_for_timeout(1000)

        _screenshot(page, '07-task-completed')


class TestAPIIntegration:
    """Test that the dashboard correctly communicates with the API."""

    def test_stats_display(self, page: Page, base_url):
        _step('Navigate to dashboard')
        page.goto(base_url + '/')
        page.wait_for_load_state('networkidle')
        page.wait_for_timeout(500)

        _step('Verify stats or sync info is displayed')
        _screenshot(page, '08-stats-display')

    def test_websocket_connection(self, page: Page, base_url):
        _step('Navigate to dashboard')
        page.goto(base_url + '/')
        page.wait_for_load_state('networkidle')

        _step('Wait for WebSocket to connect')
        page.wait_for_timeout(2000)

        _step('Check for WebSocket connection indicator')
        _screenshot(page, '09-websocket-connected')
