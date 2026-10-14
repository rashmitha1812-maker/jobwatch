"""Checks every career page in companies.txt, records matching job links in data/jobs.json."""
import asyncio, json, re, sys, pathlib, hashlib
from datetime import datetime, timezone
from urllib.parse import urldefrag
from playwright.async_api import async_playwright

KEYWORDS = re.compile(r"werkstudent|working.?student|student|praktik|intern(ship)?\b|thesis|abschlussarbeit|hilfskraft|hiwi|berufseinsteig|einsteiger|trainee|absolvent|graduate|nachwuchs|junior|entry.?level", re.I)
OUT = pathlib.Path("data/jobs.json")
_sk = pathlib.Path("skills.txt")
SKILLS = [l.strip() for l in _sk.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")] if _sk.exists() else []
SKILL_RE = [(k, re.compile(r"\b" + re.escape(k), re.I)) for k in SKILLS]
_rl = pathlib.Path("roles.txt")
ROLES = [l.strip() for l in _rl.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")] if _rl.exists() else []
ROLE_RE = [(k, re.compile(r"\b" + re.escape(k), re.I)) for k in ROLES]
MAX_SCORE = 40
BAD_TITLE = re.compile(r"^(skip to|jump to|jetzt bewerben|top of page|go to|zum inhalt|nach oben|to the glossary|more information|view vacancy:?$)", re.I)
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
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            try: await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception: pass
            links = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => [e.innerText.trim().replace(/\\s+/g,' '), e.href])")
            jobs = {}
            for text, href in links:
                if text and len(text) > 8 and "#" not in href and not href.startswith("javascript") and not BAD_TITLE.search(text) and (KEYWORDS.search(text) or KEYWORDS.search(href)):
                    jobs[href] = text[:160]
            return name, url, jobs, None
        except Exception as e:
            return name, url, {}, str(e)[:120]
        finally:
            await page.close()

async def score(ctx, sem, href):
    async with sem:
        page = await ctx.new_page()
        try:
            await page.goto(href, timeout=30000, wait_until="domcontentloaded")
            try: await page.wait_for_load_state("networkidle", timeout=6000)
            except Exception: pass
            text = await page.inner_text("body")
            return href, [k for k, r in SKILL_RE if r.search(text)]
        except Exception:
            return href, None
        finally:
            await page.close()

async def score_pending(db):
    pend = [h for h, j in db["jobs"].items() if "matches" not in j]
    pend = sorted(pend, key=lambda h: db["jobs"][h].get("tries", 0))[:MAX_SCORE]
    if not pend or not SKILL_RE: return
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent="Mozilla/5.0 (jobwatch personal)")
        sem = asyncio.Semaphore(8)
        try:
            res = await asyncio.wait_for(asyncio.gather(*(score(ctx, sem, h) for h in pend)), 150)
        except Exception:
            res = []
        for href, m in res:
            if m is None: db["jobs"][href]["tries"] = db["jobs"][href].get("tries", 0) + 1
            else: db["jobs"][href]["matches"] = m
        try: await asyncio.wait_for(browser.close(), 20)
        except Exception: pass

async def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db = json.loads(OUT.read_text()) if OUT.exists() else {"baseline": now, "jobs": {}, "status": {}}
    db["baseline"] = db.get("baseline") or now
    sh = hashlib.md5("\n".join(SKILLS).encode()).hexdigest()
    if db.get("skills_hash") != sh:
        for j in db["jobs"].values(): j.pop("matches", None)
        db["skills_hash"] = sh
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent="Mozilla/5.0 (jobwatch personal)")
        sem = asyncio.Semaphore(8)
        comps = load_companies()
        tasks = {asyncio.create_task(check(ctx, sem, n, u)): (n, u) for n, u in comps}
        done, pending = await asyncio.wait(tasks, timeout=420)   # hard limit: 7 minutes
        results = [t.result() for t in done]
        for t in pending:
            t.cancel()
            n, u = tasks[t]
            results.append((n, u, {}, "timed out (run too slow)"))
        try: await asyncio.wait_for(browser.close(), 20)
        except Exception: pass
    for name, url, jobs, err in results:
        old = db["status"].get(name, {})
        db["status"][name] = {"url": url, "checked": now, "ok": err is None, "error": err, "count": len(jobs) if not err else old.get("count", 0)}
        if err: continue
        for href, title in jobs.items():
            j = db["jobs"].setdefault(href, {"company": name, "title": title, "first_seen": now})
            j["last_seen"] = now
    for href, j in db["jobs"].items():      # role keywords are matched against the job title
        j["roles"] = [k for k, r in ROLE_RE if r.search(j["title"])]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(db, ensure_ascii=False, indent=1))   # save first, so results are never lost
    try: await score_pending(db)
    except Exception as e: print("scoring skipped:", e)
    OUT.write_text(json.dumps(db, ensure_ascii=False, indent=1))
    print(f"{len(db['jobs'])} jobs, {sum(not s['ok'] for s in db['status'].values())} failing")

asyncio.run(main())
