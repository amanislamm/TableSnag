import asyncio
import os
from typing import Optional
from datetime import datetime, date, timedelta

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page
from playwright_stealth import Stealth


load_dotenv()


class ResyBot:
    def __init__(self, email: str, password: str, proxy: Optional[str] = None) -> None:
        self.email = email
        self.password = password
        self.proxy = proxy
        self.auth_token: Optional[str] = None
        self.api_key: Optional[str] = None
        self.venue_id_cache: dict[str, str] = {}

    async def login(self, page: Page) -> bool:
        await page.goto('https://resy.com')
        await page.wait_for_load_state('networkidle')

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
        await page.goto('https://resy.com/cities/ny')
        await page.wait_for_load_state('domcontentloaded')
        await asyncio.sleep(4)
        await asyncio.sleep(3)

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

        self.auth_token = auth_token
        self.api_key = api_key

        return True

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
                'authorization': self.api_key or '',
                'x-resy-auth-token': self.auth_token or '',
                'x-resy-universal-app': 'true',
                'accept': 'application/json',
                'origin': 'https://resy.com',
                'referer': 'https://resy.com/',
                'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            }
            r = await client.get(url, params=params, headers=headers)
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

    bot = ResyBot(email=email, password=password, proxy=proxy)
    proxy_config = {'server': proxy} if proxy else None

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
        if ok:
            restaurants = [
                '4-charles-prime-rib',
                'the-corner-store',
                'ato-boy',
                'carbone',
            ]

            # Resolve venue IDs once on startup
            await bot.resolve_venue_ids(page, restaurants)
            print('Venue ID cache:', bot.venue_id_cache)

            # Build targets: Friday/Saturday/Sunday only, dates March 18 - April 30
            targets = []
            d = date(2026, 3, 18)
            end = date(2026, 4, 30)
            while d <= end:
                if d.weekday() in [4, 5, 6]:  # Friday=4, Saturday=5, Sunday=6
                    for slug in restaurants:
                        targets.append(
                            {
                                'slug': slug,
                                'date': d.strftime('%Y-%m-%d'),
                                'party_size': 4,
                            }
                        )
                d += timedelta(days=1)

            print(f'Total targets: {len(targets)} (Fri/Sat/Sun only)')

            cycle = 0
            async with httpx.AsyncClient(timeout=10) as client:
                while True:
                    cycle += 1
                    found = []
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

        await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
