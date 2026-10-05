import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

load_dotenv()

SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/")
WORKER_API_KEY = os.getenv("WORKER_API_KEY", "").strip()
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
DELAY_BETWEEN_ITEMS = float(os.getenv("DELAY_BETWEEN_ITEMS", "2"))
HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() in {"1", "true", "yes", "on"}

if not SERVER_URL or not WORKER_API_KEY:
    raise RuntimeError("SERVER_URL and WORKER_API_KEY must be set in worker .env")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("rozmir_worker")


def normalize_size(value: str) -> str:
    val = re.sub(r"\s+", " ", value.strip()).upper()
    val = re.sub(r"^(?:EU|UK|US|IT|FR)\s*", "", val)
    return val.split(" ")[0]


def size_equals(a: str, b: str) -> bool:
    return normalize_size(a) == normalize_size(b)


def extract_title(content: str) -> Optional[str]:
    soup = BeautifulSoup(content, "html.parser")
    m = soup.find("meta", attrs={"property": "og:title"})
    if m and m.get("content"):
        return m["content"].strip()
    return soup.title.get_text(strip=True) if soup.title else None


def extract_image(content: str) -> Optional[str]:
    soup = BeautifulSoup(content, "html.parser")
    m = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "og:image"})
    if m and m.get("content"):
        url = m["content"].strip()
        return "https:" + url if url.startswith("//") else url
    return None


def normalize_stock(value) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if not isinstance(value, str):
        return None
    t = value.strip().lower().replace("_", " ").replace("-", " ")
    t = re.sub(r"\s+", " ", t)
    if t in {"in stock", "instock", "available", "true", "buyable", "purchasable", "low on stock", "low stock", "few left", "few items left"}:
        return True
    if t in {"out of stock", "outofstock", "unavailable", "false", "coming soon", "sold out", "soldout", "not available"}:
        return False
    return None


def recursive_find_variant_sizes(obj) -> list[tuple[str, bool]]:
    found = []
    def walk(node):
        if isinstance(node, dict):
            size = None
            for k in ("size", "name", "sizeName", "displayName", "size_name"):
                v = node.get(k)
                if isinstance(v, (str, int, float)):
                    s = str(v).strip()
                    if k != "name" or len(s) <= 12:
                        size = s
                        break
            if size is not None:
                avail = None
                for k in ("availability", "availabilityStatus", "stockStatus", "isAvailable", "available", "inStock", "in_stock", "stock", "quantity", "availableQuantity", "isBuyable", "is_buyable", "purchasable", "buyable"):
                    if k not in node:
                        continue
                    value = node[k]
                    avail = normalize_stock(value)
                    if avail is None and isinstance(value, dict):
                        for nk in ("value", "status", "state", "available", "in_stock"):
                            if nk in value:
                                avail = normalize_stock(value[nk])
                                if avail is not None:
                                    break
                    if avail is not None:
                        break
                if avail is not None:
                    found.append((size, bool(avail)))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(obj)
    return found


async def dismiss_popups(page: Page):
    for sel in [
        'button:has-text("Accept all")', 'button:has-text("Accept")',
        'button:has-text("Прийняти всі")', 'button:has-text("Прийняти")',
        'button:has-text("Zgadzam się")', 'button:has-text("Akceptuj wszystkie")',
        '[data-testid*="accept"]', '[id*="accept"]', '[class*="accept"]'
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=300):
                await loc.click(timeout=1200)
                await page.wait_for_timeout(300)
                return
        except Exception:
            pass


async def click_size_picker(page: Page):
    for sel in [
        'button:has-text("Розмір")', 'button:has-text("Size")',
        'button:has-text("Select size")', 'button:has-text("Choose size")',
        '[aria-label*="size" i]', '[aria-label*="розмір" i]',
        '[data-qa="size-selector"]', '[class*="size-list"]',
        'li[role="radio"]', 'button[class*="size"]'
    ]:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=400):
                await loc.click(timeout=1200)
                await page.wait_for_timeout(500)
                return
        except Exception:
            pass


@dataclass
class Result:
    state: str
    title: Optional[str] = None
    image_url: Optional[str] = None
    detail: Optional[str] = None


class BrowserManager:
    def __init__(self):
        self.pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None

    async def start(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        self.context = await self.browser.new_context(
            viewport={"width": 1440, "height": 1100},
            locale="uk-UA",
            timezone_id="Europe/Kyiv",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

    async def new_page(self):
        page = await self.context.new_page()
        await page.set_extra_http_headers({"Accept-Language": "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7"})
        return page

    async def close(self):
        for obj_name in ("context", "browser", "pw"):
            obj = getattr(self, obj_name, None)
            if not obj:
                continue
            try:
                await (obj.stop() if obj_name == "pw" else obj.close())
            except Exception as exc:
                log.debug("close %s ignored: %s", obj_name, exc)
            setattr(self, obj_name, None)


async def inspect_rendered_size(page: Page, target_size: str):
    target = normalize_size(target_size)
    rows = await page.locator("body *").evaluate_all(r"""
    els => els.map(el => {
      const txt = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || el.value || '').trim().replace(/\s+/g,' ');
      if (!txt || txt.length > 40) return null;
      let n = el, attrs = '', disabled = false;
      for (let i=0; i<4 && n; i++, n=n.parentElement) {
        attrs += ' ' + [n.className||'', n.id||'', n.getAttribute?.('aria-disabled')||'', n.getAttribute?.('data-testid')||'', n.getAttribute?.('data-qa')||'', n.getAttribute?.('title')||'', n.getAttribute?.('value')||''].join(' ');
        if (n.disabled === true || n.getAttribute?.('aria-disabled') === 'true') disabled = true;
      }
      if (/disabled|unavailable|outofstock|out-of-stock|soldout|sold-out|not available/i.test(attrs)) disabled = true;
      return {text: txt, attrs: attrs.toLowerCase(), disabled, tag: el.tagName||''};
    }).filter(Boolean)
    """)
    exact = [x for x in rows if normalize_size(str(x.get("text", ""))) == target]
    if not exact:
        return None
    controls = [x for x in exact if re.search(r"BUTTON|INPUT|LABEL|OPTION", x.get("tag", "")) or re.search(r"button|radio|option|size|swatch", x.get("attrs", ""))]
    candidates = controls or exact
    return any(not x.get("disabled", False) for x in candidates)


async def check_page(browser: BrowserManager, url: str, size: str) -> Result:
    page = await browser.new_page()
    payloads = []

    async def capture(resp):
        u = resp.url.lower()
        c = resp.headers.get("content-type", "").lower()
        interesting = "json" in c or resp.request.resource_type in {"xhr", "fetch"} or any(k in u for k in ("api", "graphql", "availability", "stock", "variant", "product", "article", "sku", "catalog"))
        if not interesting:
            return
        try:
            body = await resp.text()
            if body and len(body) <= 5_000_000:
                payloads.append((resp.url, body))
        except Exception:
            pass

    page.on("response", lambda r: asyncio.create_task(capture(r)))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000)
        await dismiss_popups(page)
        await page.wait_for_timeout(1500)
        await click_size_picker(page)
        await page.wait_for_timeout(1500)
        content = await page.content()
        low = content.lower()
        if "access denied" in low or "akamai" in low and "denied" in low:
            return Result("error", detail="Сайт заблокував перевірку (Access Denied/Akamai)")
        title = extract_title(content)
        image = extract_image(content)

        dom = await inspect_rendered_size(page, size)
        if dom is not None:
            return Result("in_stock" if dom else "out_of_stock", title, image)

        soup = BeautifulSoup(content, "html.parser")
        for script in soup.find_all("script"):
            txt = (script.string or script.get_text(strip=True)).strip()
            if not txt or not txt.startswith(("{", "[")) or len(txt) > 4_000_000:
                continue
            try:
                data = json.loads(txt)
            except Exception:
                continue
            exact = [(s,a) for s,a in recursive_find_variant_sizes(data) if size_equals(s,size)]
            if exact:
                return Result("in_stock" if any(a for _,a in exact) else "out_of_stock", title, image)

        await page.wait_for_timeout(700)
        for _, txt in payloads:
            if not txt.lstrip().startswith(("{", "[")):
                continue
            try:
                data = json.loads(txt)
            except Exception:
                continue
            exact = [(s,a) for s,a in recursive_find_variant_sizes(data) if size_equals(s,size)]
            if exact:
                return Result("in_stock" if any(a for _,a in exact) else "out_of_stock", title, image)

        return Result("error", title, image, f"Не знайдено розмір {size} у даних сторінки; не робимо висновок про відсутність")
    except Exception as exc:
        return Result("error", detail=str(exc))
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def http_json(session: aiohttp.ClientSession, method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["X-Worker-Key"] = WORKER_API_KEY
    async with session.request(method, SERVER_URL + path, headers=headers, **kwargs) as resp:
        if resp.status >= 400:
            body = await resp.text()
            raise RuntimeError(f"HTTP {resp.status}: {body[:300]}")
        return await resp.json()


async def post_result(session, item_id, result: Result, request_id=None):
    payload = {
        "item_id": item_id,
        "state": result.state,
        "title": result.title,
        "image_url": result.image_url,
        "detail": result.detail,
    }
    if request_id is not None:
        payload["request_id"] = request_id
    await http_json(session, "POST", "/worker/results", json=payload)


async def check_item(item, browser, session, request_id=None):
    result = await check_page(browser, item["url"], item["target_size"])
    await post_result(session, item["id"], result, request_id)
    log.info("Item #%s %s/%s -> %s", item["id"], item["store"], item["target_size"], result.state)


async def run_once():
    timeout = aiohttp.ClientTimeout(total=40)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        browser = BrowserManager()
        await browser.start()
        try:
            # Manual checks first: these are created by Telegram /check.
            jobs = (await http_json(session, "GET", "/worker/jobs")).get("jobs", [])
            log.info("Manual jobs: %s", len(jobs))
            for job in jobs:
                item = {
                    "id": job["item_id"],
                    "url": job["url"],
                    "store": job["store"],
                    "target_size": job["target_size"],
                }
                await check_item(item, browser, session, int(job["request_id"]))
                await asyncio.sleep(DELAY_BETWEEN_ITEMS)

            items = (await http_json(session, "GET", "/worker/items")).get("items", [])
            log.info("Full check: %s active item(s)", len(items))
            for item in items:
                await check_item(item, browser, session)
                await asyncio.sleep(DELAY_BETWEEN_ITEMS)
        finally:
            await browser.close()


async def main():
    await run_once()

if __name__ == "__main__":
    asyncio.run(main())
