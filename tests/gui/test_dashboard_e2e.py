import pytest
from playwright.sync_api import Page, expect

@pytest.mark.skip(reason="Requires the FastAPI and Vite servers to be running manually for now")
def test_dashboard_loads_and_controls_work(page: Page):
    # Navigate to the React frontend
    page.goto("http://localhost:5173")

    # Verify the title
    expect(page).to_have_title("Vite + React + TS") # Default vite title, we can update it later
    
    # Verify the header text
    expect(page.get_by_text("pytest-mes-core")).to_be_visible()

    # The Operator ID input should be present
    operator_input = page.get_by_placeholder("Scan badge...")
    expect(operator_input).to_be_visible()
    
    # Fill in a new operator ID
    operator_input.fill("TEST-OP-99")

    # The START button should be visible
    start_btn = page.get_by_role("button", name="START TEST")
    expect(start_btn).to_be_visible()
    expect(start_btn).not_to_be_disabled()

    # Click the START button
    start_btn.click()

    # In a fully mocked environment, the test would start, the status would change,
    # and the TelemetryViewer would start appending lines. 
    # For now, we just verify the network request was fired or the UI state changed.
    
    # The status should change to "Starting test..." or "Testing in progress..."
    # We will wait for the status text
    status_text = page.locator("text=Testing in progress...")
    expect(status_text).to_be_visible(timeout=5000)

    # Click E-STOP
    stop_btn = page.get_by_role("button", name="E-STOP")
    expect(stop_btn).to_be_visible()
    stop_btn.click()

    # The status should revert to aborted
    expect(page.locator("text=Test Aborted. Idle.")).to_be_visible(timeout=5000)
