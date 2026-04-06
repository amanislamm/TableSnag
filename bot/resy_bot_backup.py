"""
TableSnag Resy Bot — Playwright-based bot for checking availability and booking on Resy.
Uses playwright-stealth and realistic delays to avoid detection.
"""

import asyncio
import json
import random
import os
from typing import Optional

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page
from playwright_stealth import Stealth

# Load credentials from .env (RESY_EMAIL, RESY_PASSWORD)
load_dotenv()

RESY_BASE = "https://resy.com"
RESY_API_BASE = "https://api.resy.com"


def _random_delay() -> float:
    """Return a random delay between 0.5 and 1.5 seconds for human-like pacing."""
    return random.uniform(0.5, 1.5)


class ResyBot:
    """
    Bot for logging into Resy, checking venue availability, and booking slots.
    Uses optional proxy and stealth to reduce detection risk.
    """

    def __init__(
        self,
        email: str,
        password: str,
        proxy: Optional[str] = None,
    ) -> None:
        """
        Args:
            email: Resy account email.
            password: Resy account password.
            proxy: Optional proxy URL (e.g. "http://user:pass@host:port").
        """
        self.email = email
        self.password = password
        self.proxy = proxy
        # Auth token set after login; used for API calls.
        self._auth_token: Optional[str] = None

    async def _get_auth_token(self, page: Page) -> Optional[str]:
        """
        Extract Resy auth token from the page after login.
        Tries cookies and localStorage; token key may need adjustment per Resy's current implementation.
        """
        # Try common cookie names Resy might use for the auth token.
        cookies = await page.context.cookies()
        for c in cookies:
            if "token" in c["name"].lower() or "auth" in c["name"].lower() or "resy" in c["name"].lower():
                if c.get("value"):
                    return c["value"]

        # Fallback: try localStorage (Resy sometimes stores token here).
        token = await page.evaluate(
            """() => {
            const keys = ['token', 'auth_token', 'resy_token', 'resy_auth_token'];
            for (const k of keys) {
                const v = localStorage.getItem(k);
                if (v) return v;
            }
            return null;
        }"""
        )
        return token

    async def login(self, page: Page) -> bool:
        """
        Navigate to Resy, take a debug screenshot, click Log In/Sign In, fill email/password, and submit.
        Uses random 0.5–1.5s delays between steps. Sets self._auth_token on success.
        """
        await page.goto(RESY_BASE, wait_until="domcontentloaded")
        await asyncio.sleep(_random_delay())

        # Take a debug screenshot right after the initial load so we can inspect the actual UI.
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        screenshot_path = os.path.join(project_root, "debug_screenshot.png")
        await page.screenshot(path=screenshot_path, full_page=True)

        # Ensure the page has finished loading network requests before looking for the login button.
        await page.wait_for_load_state("networkidle", timeout=60000)
        await asyncio.sleep(_random_delay())

        # Click the main Log In / Sign In entry point to open the modal.
        login_button = (
            page.locator('button:has-text("Log In")').first
            or page.locator('a:has-text("Log In")').first
        )
        try:
            await login_button.wait_for(state="visible", timeout=60000)
            await login_button.click(timeout=60000)
        except Exception:
            # Fallback: try a generic header text if the primary selectors fail.
            header_login = page.get_by_text("Log In").first
            await header_login.wait_for(state="visible", timeout=60000)
            await header_login.click(timeout=60000)

        # Allow the login modal to appear.
        await asyncio.sleep(2)

        # Debug: capture modal state and all clickable link/button text before interacting with it.
        await asyncio.sleep(3)
        await page.screenshot(path="debug_modal_open.png")
        all_links = await page.locator("a, button").all_text_contents()
        print("ALL CLICKABLE TEXT ON PAGE:", all_links)

        # Click the explicit "Log in with email & password" link in the modal.
        await page.get_by_text("Log in with email & password").click(timeout=60000)

        # Give the modal time to switch to the email+password form.
        await asyncio.sleep(2)

        # Fill email and password using direct selectors.
        await page.locator(\"input[type='email']\").fill(self.email)
        await asyncio.sleep(1)
        await page.locator(\"input[type='password']\").fill(self.password)
        await asyncio.sleep(1)

        # Click the submit button.
        await page.locator(\"button[type='submit']\").click()
        await asyncio.sleep(3)

        # Capture state right after submitting credentials.
        after_login_screenshot = os.path.join(project_root, "debug_after_login.png")
        await page.screenshot(path=after_login_screenshot, full_page=True)

        # Wait for navigation/post-login state and extract auth token.
        await page.wait_for_load_state("networkidle", timeout=15000)
        await asyncio.sleep(_random_delay())

        self._auth_token = await self._get_auth_token(page)
        return self._auth_token is not None

    async def check_availability(
        self,
        page: Page,
        venue_id: int,
        date: str,
        party_size: int,
    ) -> list[dict]:
        """
        Call Resy's find API and return available slots as dicts with keys: time, party_size, token.
        Requires prior login so x-resy-auth-token is available.
        """
        if not self._auth_token:
            raise RuntimeError("Not logged in; call login() first.")

        url = (
            f"{RESY_API_BASE}/4/find?"
            f"lat=0&long=0&day={date}&party_size={party_size}&venue_id={venue_id}&"
            f"x-resy-auth-token={self._auth_token}"
        )

        # Use the page's request context so cookies and origin are consistent.
        response = await page.request.get(
            url,
            headers={
                "Accept": "application/json",
                "Origin": RESY_BASE,
                "x-resy-auth-token": self._auth_token,
            },
        )

        if not response.ok:
            raise RuntimeError(f"Resy find API error: {response.status} {await response.text()}")

        data = await response.json()
        slots: list[dict] = []

        # Parse Resy response: results.venues[].slots[] with config.token and time.
        results = data.get("results") or {}
        venues = results.get("venues") or []
        for venue in venues:
            for slot in venue.get("slots") or []:
                config = slot.get("config") or {}
                token = config.get("token") or slot.get("token")
                time_str = slot.get("time") or slot.get("start_time") or ""
                slots.append({
                    "time": time_str,
                    "party_size": party_size,
                    "token": token or "",
                })

        return slots

    async def book_slot(
        self,
        page: Page,
        booking_token: str,
        payment_method_id: str,
    ) -> dict:
        """
        POST to Resy's booking endpoint to complete the reservation.
        Returns the API response as a dict; caller should check for success/errors.
        """
        if not self._auth_token:
            raise RuntimeError("Not logged in; call login() first.")

        # Resy booking endpoint (path may need adjustment if API changes).
        book_url = f"{RESY_API_BASE}/3/book"
        payload = {
            "config_id": booking_token,
            "payment_method_id": payment_method_id,
        }

        response = await page.request.post(
            book_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": RESY_BASE,
                "x-resy-auth-token": self._auth_token,
            },
            data=json.dumps(payload),
        )

        body = await response.json() if response.ok else {"error": await response.text(), "status": response.status}
        return body


async def main() -> None:
    """Launch Chromium with stealth, log in, check availability for venue 123 on 2026-04-01 for 2, print slots."""
    email = os.getenv("RESY_EMAIL", "").strip()
    password = os.getenv("RESY_PASSWORD", "").strip()
    if not email or not password:
        print("Set RESY_EMAIL and RESY_PASSWORD in .env")
        return

    # Stealth instance to apply to the browser context.
    stealth = Stealth()

    launch_args = [
        "--disable-blink-features=AutomationControlled",
    ]

    # Optional proxy from env (e.g. RESY_PROXY=http://user:pass@host:port).
    proxy = os.getenv("RESY_PROXY") or None
    bot = ResyBot(email=email, password=password, proxy=proxy)
    proxy_config = {"server": bot.proxy} if bot.proxy else None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=launch_args,
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            proxy=proxy_config,
        )
        await stealth.apply_stealth_async(context)

        page = await context.new_page()

        ok = await bot.login(page)
        if not ok:
            print("Login failed or could not obtain auth token.")
            await browser.close()
            return

        print("Login successful.")

        venue_id = 123
        date = "2026-04-01"
        party_size = 2
        slots = await bot.check_availability(page, venue_id=venue_id, date=date, party_size=party_size)
        print(f"Availability for venue_id={venue_id} on {date} (party_size={party_size}):")
        for s in slots:
            print(f"  {s}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                