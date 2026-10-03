"""Checks every career page in companies.txt, records matching job links in data/jobs.json."""
import asyncio, json, re, sys, pathlib
from datetime import datetime, timezone
from urllib.parse import urldefrag
from playwright.async_api import async_playwright

KEYWORDS = re.compile(r"werkstudent|working.?student|student|praktik|intern(ship)?\b|thesis|abschlussarbeit|hilfskraft|hiwi", re.I)
OUT = pathlib.Path("data/jobs.json")
LINE = re.compile(r"^\s*(?:\d+\.\s*)?(?:(.+?)\s*:\s*)?(https?://\S+)")

def load_companies():
    seen, items = set(), []
    for line in pathlib.Path("companies.txt").read_text(encoding="utf-8").splitlines():
        m = LINE.match(line)
        if not m: continue
        name, url = (m.group(1) or "").strip(), m.group(2)
        key = urldefrag(url)[0]
        if key in seen: continue          # skips duplicates in your list
        seen.add(key)
        items.append((name or re.sub(r"^https?://(www\.)?", "", url).split("/")[0], url))
    return items

async def check(ctx, sem, name, url):
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(url, timeout=45000, wait_until="domcontentloaded")
            try: await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception: pass
            links = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => [e.innerText.trim().replace(/\\s+/g,' '), e.href])")
            jobs = {}
            for text, href in links:
                if text and len(text) > 8 and (KEYWORDS.search(text) or KEYWORDS.search(href)):
                    jobs[href] = text[:160]
            return name, url, jobs, None
        except Exception as e:
            return name, url, {}, str(e)[:120]
        finally:
            await page.close()

async def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = json.loads(OUT.read_text()) if OUT.exists() else {"baseline": now, "jobs": {}, "status": {}}
    db["baseline"] = db.get("baseline") or now
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent="Mozilla/5.0 (jobwatch personal)")
        sem = asyncio.Semaphore(6)
        results = await asyncio.gather(*(check(ctx, sem, n, u) for n, u in load_companies()))
        await browser.close()
    for name, url, jobs, err in results:
        db["status"][name] = {"url": url, "checked": now, "ok": err is None, "error": err, "count": len(jobs)}
        if err: continue
        for href, title in jobs.items():
            j = db["jobs"].setdefault(href, {"company": name, "title": title, "first_seen": now})
            j["last_seen"] = now
    OUT.write_text(json.dumps(db, ensure_ascii=False, indent=1))
    print(f"{len(db['jobs'])} jobs, {sum(not s['ok'] for s in db['status'].values())} failing")

asyncio.run(main())
