import asyncio
import json
import os
import traceback
from typing import Optional
from datetime import datetime, date, timedelta

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page
from playwright_stealth import Stealth


load_dotenv()

DRY_RUN = os.getenv('DRY_RUN', 'true').lower() == 'true'


class TableSnagBot:
    def __init__(self, email: str, password: str, proxy: Optional[str] = None) -> None:
        self.email = email
        self.password = password
        self.proxy = proxy
        self.auth_token = ''
        self.api_key = 'ResyAPI api_key="VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"'
        self.venue_id_cache: dict[str, str] = {}
        self._first_check_done: bool = False
        self.alerted_slots: set[str] = set()

    async def send_slot_sms_alert_if_new(self, slug: str, date: str, slot_time: str) -> None:
        key = f'{slug}_{date}_{slot_time}'
        if key in self.alerted_slots:
            return

        account_sid = os.getenv('TWILIO_ACCOUNT_SID', '').strip()
        auth_token = os.getenv('TWILIO_AUTH_TOKEN', '').strip()
        from_num = os.getenv('TWILIO_FROM_NUMBER', '').strip()
        to_num = os.getenv('TWILIO_TO_NUMBER', '').strip()

        body = (
            f'TableSnag: slot at {slug} on {date} at {slot_time} (party check in app).'
        )

        if not (account_sid and auth_token and from_num and to_num):
            print('SMS not configured (set Twilio env vars); would alert:', key)
            self.alerted_slots.add(key)
            return

        def _send() -> None:
            from twilio.rest import Client

            client = Client(account_sid, auth_token)
            client.messages.create(body=body, from_=from_num, to=to_num)

        await asyncio.to_thread(_send)
        self.alerted_slots.add(key)
        print('SMS sent for', key)

    async def login(self, page: Page) -> bool:
        def _on_request(request) -> None:
            headers = request.headers
            if 'authorization' in headers and headers['authorization'].startswith('ResyAPI'):
                authz = headers['authorization']
                if authz:
                    self.api_key = authz

        async def _handle_auth_response(response) -> None:
            if 'api.resy.com/3/auth/refresh' in response.url:
                try:
                    body = await response.json()
                    print('AUTH REFRESH RESPONSE:', str(body)[:300])
                    token = body.get('token')
                    if not token:
                        token = body.get('access_token')
                    if token:
                        self.auth_token = token
                        print('Got real access token from refresh endpoint')
                except Exception as e:
                    print('Auth refresh parse error:', e)

        page.on('request', _on_request)
        try:
            await page.goto('https://resy.com')
            await page.wait_for_load_state('domcontentloaded')
            await asyncio.sleep(2)

            # Dismiss cookie consent banner if present
            try:
                consent = page.locator(
                    '#user-consent-management-granular-banner-overlay, [id*="consent"], [id*="cookie"], [class*="cookie-banner"], [class*="consent-banner"]'
                )
                accept_btn = page.locator(
                    'button:has-text("Accept"), button:has-text("Accept All"), button:has-text("Agree"), button:has-text("OK"), button:has-text("Got it")'
                )
                await accept_btn.first.click(timeout=5000)
                print('Cookie banner dismissed')
                await asyncio.sleep(1)
            except Exception:
                print('No cookie banner found, continuing')

            await asyncio.sleep(2)

            await page.get_by_text('Log in', exact=True).click()
            await asyncio.sleep(2)

            texts = await page.locator('a, button').all_text_contents()
            print('MODAL ELEMENTS:', texts)

            await page.screenshot(path='debug_modal.png')

            await page.get_by_text('Log in with email & password').click()
            await asyncio.sleep(2)

            await page.locator('input[type=email]').fill(self.email)
            await asyncio.sleep(1)

            await page.locator('input[type=password]').fill(self.password)
            await asyncio.sleep(1)

            await page.locator('button[type=submit]').click()
            await asyncio.sleep(3)

            await page.screenshot(path='debug_after_login.png')
            page.on('response', _handle_auth_response)
            await page.goto('https://resy.com/cities/ny')
            await page.wait_for_load_state('domcontentloaded')
            await asyncio.sleep(4)
            await asyncio.sleep(3)
            await asyncio.sleep(5)
            print(f'AUTH TOKEN AFTER REFRESH: {(self.auth_token or "")[:50]}')
            page.remove_listener('response', _handle_auth_response)

            cookies = await page.context.cookies()
            auth_token: Optional[str] = None
            for cookie in cookies:
                name = (cookie.get('name') or '').lower()
                if 'token' in name or 'auth' in name or 'resy' in name:
                    value = cookie.get('value')
                    if value:
                        auth_token = value
                        break

            api_key: Optional[str] = await page.evaluate(
                '''() => { const keys = Object.keys(localStorage); for (const k of keys) { const lk = k.toLowerCase(); const v = localStorage.getItem(k); if (!v) continue; if (lk.includes('authorization') || (lk.includes('api') && lk.includes('key')) || v.toLowerCase().includes('resyapi api_key')) { return v; } } return null; }'''
            )

            _access_jwt_prefix = 'eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9'
            if auth_token and not (
                self.auth_token and self.auth_token.startswith(_access_jwt_prefix)
            ):
                self.auth_token = auth_token
            if api_key:
                self.api_key = api_key

            print(f'FINAL API KEY: {self.api_key}')
            print(f'FINAL AUTH TOKEN: {(self.auth_token or "")[:40]}')

            return True
        finally:
            page.remove_listener('request', _on_request)

    async def check_availability(self, page, venue_slug, date, party_size):
        try:
            url = f'https://resy.com/cities/ny/{venue_slug}?date={date}&seats={party_size}'
            print('Navigating to:', url)

            captured = []

            async def capture_response(response):
                if 'api.resy.com/4/find' in response.url:
                    try:
                        body = await response.body()
                        captured.append(body)
                        print('CAPTURED /4/find, length:', len(body))
                    except Exception as e:
                        print('Capture error:', e)

            page.on('response', capture_response)
            await page.goto(url)
            await page.wait_for_load_state('domcontentloaded')

            print('Waiting for reservation widget to load...')
            try:
                await page.wait_for_selector(
                    '[data-test=venue-availability], .ReservationButton, [class*=ReservationWidget], [class*=availability], button[class*=Button--primary]',
                    timeout=10000,
                )
                print('Widget found')
            except Exception:
                print('Widget selector timed out, waiting 8s anyway')

            await asyncio.sleep(8)
            await page.screenshot(path='debug_venue_page.png', full_page=True)
            page.remove_listener('response', capture_response)

            if not captured:
                print('Still no /4/find captured')
                return []

            import json

            raw = captured[0]
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode('utf-8', errors='ignore')

            data = json.loads(raw)
            venues = data.get('results', {}).get('venues', [])
            print(f'VENUES FOUND: {len(venues)}')
            if not venues:
                print('Fully booked or invalid slug')
                return []

            slots = venues[0].get('slots', [])
            print(f'SLOTS IN RESPONSE: {len(slots)}')
            result = []
            for slot in slots:
                result.append(
                    {
                        'time': slot['date']['start'],
                        'type': slot['config']['type'],
                        'token': slot['config']['token'],
                    }
                )
            print(f'PARSED SLOTS: {result[:3]}')
            return result

        except Exception as e:
            print('Error:', e)
            return []

    async def check_availability_fast(self, client, venue_slug, date, party_size):
        try:
            url = 'https://api.resy.com/4/find'
            params = {
                'lat': '0',
                'long': '0',
                'day': date,
                'party_size': str(party_size),
                'venue_id': self.venue_id_cache.get(venue_slug, ''),
            }
            headers = {
                'authorization': self.api_key if self.api_key is not None else '',
                'x-resy-auth-token': self.auth_token if self.auth_token is not None else '',
                'x-resy-universal-app': 'true',
                'accept': 'application/json, text/plain, */*',
                'accept-language': 'en-US,en;q=0.9',
                'cache-control': 'no-cache',
                'origin': 'https://resy.com',
                'referer': 'https://resy.com/',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-site',
                'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            }
            r = await client.get(url, params=params, headers=headers)
            if not self._first_check_done:
                print(
                    f'Fast check {venue_slug} {date}: status={r.status_code} body={r.text[:200]}'
                )
                self._first_check_done = True
            data = r.json()
            venues = data.get('results', {}).get('venues', [])
            if not venues:
                return []
            slots = venues[0].get('slots', [])
            return [
                {
                    'time': s['date']['start'],
                    'type': s['config']['type'],
                    'token': s['config']['token'],
                }
                for s in slots
            ]
        except Exception as e:
            print(f'Fast check error {venue_slug} {date}: {e}')
            return []

    async def book_slot(
        self,
        page,
        slug,
        date,
        time_str,
        config_token,
        party_size=4,
    ):
        if DRY_RUN:
            print(
                f'DRY RUN - would book: {slug} {date} {time_str} with config token {config_token[:50]}'
            )
            return True
        try:
            # Extract just the time portion e.g. "17:00"
            time_display = (
                time_str.split(' ')[1][:5] if ' ' in time_str else time_str[:5]
            )
            hour = int(time_display.split(':')[0])
            minute = int(time_display.split(':')[1])
            if hour >= 12:
                ampm = 'PM'
                display_hour = hour if hour == 12 else hour - 12
            else:
                ampm = 'AM'
                display_hour = hour if hour != 0 else 12
            time_label = f'{display_hour}:{minute:02d} {ampm}'

            url = f'https://resy.com/cities/ny/{slug}?date={date}&seats={party_size}'
            print(f'Booking: navigating to {url}')

            booked: list[dict] = []

            async def capture_book(response):
                if 'api.resy.com/3/book' in response.url:
                    try:
                        body = await response.body()
                        booked.append(
                            {'status': response.status, 'body': body.decode()}
                        )
                        print(
                            f'BOOK RESPONSE: status={response.status} body={body.decode()[:300]}'
                        )
                    except Exception as e:
                        print(f'Book capture error: {e}')

            page.on('response', capture_book)
            await page.goto(url)
            await page.wait_for_load_state('domcontentloaded')
            await asyncio.sleep(4)

            print(f'Looking for time slot: {time_label}')
            clicked = False
            for selector in [
                f'button:has-text("{time_label}")',
                f'[class*="ReservationButton"]:has-text("{time_label}")',
                f'[class*="Button"]:has-text("{time_label}")',
                f'button[data-test*="slot"]:has-text("{time_label}")',
            ]:
                try:
                    await page.locator(selector).first.click(timeout=3000)
                    print(f'Clicked time slot: {time_label}')
                    clicked = True
                    break
                except Exception:
                    continue

            if not clicked:
                try:
                    buttons = await page.locator('button').all_text_contents()
                    print(
                        f'Available buttons: {[b for b in buttons if b.strip()][:20]}'
                    )
                except Exception:
                    pass
                print(f'Could not find time slot button for {time_label}')
                page.remove_listener('response', capture_book)
                return False

            await asyncio.sleep(2)

            await page.screenshot(path='debug_after_slot_click.png')
            print('Screenshot saved - checking for booking drawer')

            try:
                all_buttons = await page.locator('button').all_text_contents()
                print(
                    f'Buttons after slot click: {[b.strip() for b in all_buttons if b.strip()]}'
                )
            except Exception as e:
                print(f'Button scan error: {e}')

            await asyncio.sleep(3)

            book_clicked = False
            for selector in [
                'button:has-text("Book Now")',
                'button:has-text("Reserve")',
                'button:has-text("Confirm")',
                'button:has-text("Book")',
                '[class*="book"]',
                '[class*="Book"]',
                '[class*="reserve"]',
                '[class*="Reserve"]',
                '[class*="checkout"]',
                '[class*="Checkout"]',
                'button[type="submit"]',
            ]:
                try:
                    el = page.locator(selector).first
                    if await el.is_visible(timeout=1000):
                        await el.click(timeout=3000)
                        print(f'Clicked book button with selector: {selector}')
                        book_clicked = True
                        break
                except Exception:
                    continue

            if not book_clicked:
                print('Could not find any book button')
                await page.screenshot(path='debug_no_book_button.png')

            await asyncio.sleep(4)
            page.remove_listener('response', capture_book)

            if booked and booked[0]['status'] == 201:
                print(
                    f'*** SUCCESSFULLY BOOKED: {slug} {date} {time_str} ***'
                )
                return True
            if booked:
                print(f'Booking attempted, status: {booked[0]["status"]}')
                return False
            print('No booking response captured')
            return False

        except Exception as e:
            print(f'book_slot error: {e}')
            return False

    async def resolve_venue_ids(self, page, slugs):
        self.venue_id_cache = {}
        for slug in slugs:
            try:
                captured_id: list[str] = []

                def handle(request):
                    if 'api.resy.com/2/config?venue_id=' in request.url:
                        vid = request.url.split('venue_id=')[1].split('&')[0]
                        captured_id.append(vid)

                page.on('request', handle)
                await page.goto(f'https://resy.com/cities/ny/{slug}')
                await page.wait_for_load_state('domcontentloaded')
                await asyncio.sleep(3)
                actual_url = page.url
                print(f'Navigated to: {actual_url}')
                page.remove_listener('request', handle)

                if captured_id:
                    self.venue_id_cache[slug] = captured_id[0]
                    print(f'Resolved {slug} -> venue_id {captured_id[0]}')
                else:
                    print(f'Could not resolve venue_id for {slug}')
            except Exception as e:
                print(f'Resolve error {slug}: {e}')

    async def poll(self, page, targets, interval_seconds=30, on_slot_found=None):
        while True:
            try:
                for target in targets:
                    slug = target.get('slug')
                    date = target.get('date')
                    size = target.get('party_size')
                    try:
                        slots = await self.check_availability(page, slug, date, size)
                        if slots:
                            for slot in slots:
                                print(f'SLOT FOUND: {slug} {date} - {slot}')
                                if on_slot_found is not None:
                                    await on_slot_found(target, slot)
                    except Exception as e:
                        print('Check failed for target:', target, 'error:', e)
                    await asyncio.sleep(2)

                heartbeat_time = datetime.now().strftime('%H:%M:%S')
                n = len(targets)
                print(
                    f'[{heartbeat_time}] Cycle complete — checked {n} targets. Sleeping {interval_seconds}s.'
                )
                await asyncio.sleep(interval_seconds)
            except Exception as e:
                print('Poll loop error:', e)
                await asyncio.sleep(interval_seconds)


async def main() -> None:
    email = os.getenv('RESY_EMAIL', '').strip()
    password = os.getenv('RESY_PASSWORD', '').strip()
    proxy = os.getenv('RESY_PROXY') or None

    if not email or not password:
        print('Set RESY_EMAIL and RESY_PASSWORD in .env')
        return

    bot = TableSnagBot(email=email, password=password, proxy=proxy)
    proxy_config = {'server': proxy} if proxy else None

    restaurants = [
        '4-charles-prime-rib',
        'atoboy',
        'laser-wolf-brooklyn',
        'carbone',
    ]

    def build_targets() -> list[dict]:
        targets_local: list[dict] = []
        d = date(2026, 3, 18)
        end = date(2026, 4, 30)
        while d <= end:
            if d.weekday() in [4, 5, 6]:
                for slug in restaurants:
                    targets_local.append(
                        {
                            'slug': slug,
                            'date': d.strftime('%Y-%m-%d'),
                            'party_size': 4,
                        }
                    )
            d += timedelta(days=1)
        return targets_local

    while True:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False)
                context = await browser.new_context(
                    viewport={'width': 1280, 'height': 800},
                    proxy=proxy_config,
                )

                stealth = Stealth()
                await stealth.apply_stealth_async(context)

                page = await context.new_page()
                ok = await bot.login(page)
                print('Login result:', ok)
                if not ok:
                    raise RuntimeError('Login failed')

                print(f'Using auth token: {(bot.auth_token or "")[:50]}')

                await bot.resolve_venue_ids(page, restaurants)
                print('Venue ID cache:', bot.venue_id_cache)

                targets = build_targets()
                print(f'Total targets: {len(targets)} (Fri/Sat/Sun only)')

                cycle = 0

                async def refresh_browser_session() -> None:
                    nonlocal browser, context, page
                    print('Refreshing browser session (token / context reset)...')
                    await browser.close()
                    browser = await p.chromium.launch(headless=False)
                    context = await browser.new_context(
                        viewport={'width': 1280, 'height': 800},
                        proxy=proxy_config,
                    )
                    await stealth.apply_stealth_async(context)
                    page = await context.new_page()
                    login_ok = await bot.login(page)
                    if not login_ok:
                        raise RuntimeError('Login failed after browser refresh')
                    await bot.resolve_venue_ids(page, restaurants)
                    print('Venue ID cache after refresh:', bot.venue_id_cache)

                async with httpx.AsyncClient(timeout=10) as client:
                    while True:
                        if cycle >= 50:
                            await refresh_browser_session()
                            cycle = 0

                        cycle += 1
                        found = []
                        bot._first_check_done = False
                        for target in targets:
                            slots = await bot.check_availability_fast(
                                client,
                                target['slug'],
                                target['date'],
                                target['party_size'],
                            )
                            if slots:
                                for slot in slots:
                                    hour = int(slot['time'].split(' ')[1].split(':')[0])
                                    if 17 <= hour <= 21:
                                        found.append(
                                            {
                                                'restaurant': target['slug'],
                                                'date': target['date'],
                                                'slot': slot,
                                            }
                                        )
                                        print(
                                            '*** SLOT FOUND: '
                                            + target['slug']
                                            + ' '
                                            + target['date']
                                            + ' '
                                            + slot['time']
                                            + ' ***'
                                        )
                                        await bot.send_slot_sms_alert_if_new(
                                            target['slug'],
                                            target['date'],
                                            slot['time'],
                                        )
                                        await bot.book_slot(
                                            page,
                                            target['slug'],
                                            target['date'],
                                            slot['time'],
                                            slot['token'],
                                            target['party_size'],
                                        )
                            await asyncio.sleep(0.5)

                        print(
                            '['
                            + datetime.now().strftime('%H:%M:%S')
                            + '] Cycle '
                            + str(cycle)
                            + ' complete — '
                            + str(len(targets))
                            + ' targets, '
                            + str(len(found))
                            + ' prime slots found. Sleeping 60s.'
                        )
                        await asyncio.sleep(60)

        except KeyboardInterrupt:
            raise
        except Exception as e:
            traceback.print_exc()
            print('Session crashed or error:', e)
            print('Restarting full session in 30 seconds...')
            await asyncio.sleep(30)


if __name__ == '__main__':
    asyncio.run(main())
