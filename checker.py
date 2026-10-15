"""jobwatch checker v3 - reads employer career pages and stores real job postings in data/jobs.json"""
import asyncio, json, re, time, pathlib, hashlib, html
from datetime import datetime, timezone
from urllib.parse import urldefrag, urlparse
from playwright.async_api import async_playwright

VERSION = "v4"
KEYWORDS = re.compile(r"werkstudent|working.?student|student|praktik|intern(ship)?\b|thesis|abschlussarbeit|hilfskraft|hiwi|berufseinsteig|einsteiger|trainee|absolvent|graduate|nachwuchs|junior|entry.?level", re.I)
BAD_TITLE = re.compile(r"^(skip to|jump to|jetzt bewerben|top of page|go to|zum inhalt|nach oben|to the glossary|more information|view vacancy:?$)", re.I)
JOBISH = re.compile(r"job|detail|posting|vacanc|requisition|position|stelle|[?&](id|jid|jobid|req\w*)=", re.I)
LINE = re.compile(r"^\s*(\*)?\s*(?:\d+\.\s*)?(?:(.+?)\s*:\s*)?(https?://\S+)")   # a leading * marks a priority company
JOBHREF = re.compile(r"/job/[^/?#]+|/jobs?/[^/?#]{6,}|jobdetail|job-detail|jobposting|FolderDetail|/details/\?id=|[?&](jobid|jid|id)=\d{4,}|/vacancies/[^/?#]+/|Offre/Detail", re.I)
OUT = pathlib.Path("data/jobs.json")
MAX_SCORE = 60
MAX_TRIES = 4

def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)

def read_list(name):
    p = pathlib.Path(name)
    if not p.exists(): return []
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]

def rx(words): return [(k, re.compile(r"\b" + re.escape(k), re.I)) for k in words]
SKILLS, ROLES = read_list("skills.txt"), read_list("roles.txt")
SKILL_RE, ROLE_RE = rx(SKILLS), rx(ROLES)

def load_companies():
    seen, items = set(), []
    for line in pathlib.Path("companies.txt").read_text(encoding="utf-8").splitlines():
        m = LINE.match(line)
        if not m: continue
        star, name, url = bool(m.group(1)), (m.group(2) or "").strip(), m.group(3)
        key = urldefrag(url)[0]
        if key in seen: continue
        seen.add(key)
        items.append((name or re.sub(r"^https?://(www\.)?", "", url).split("/")[0], url, star))
    items.sort(key=lambda x: not x[2])      # priority companies are checked first (stable sort)
    return items

def clean(text, n=1500):
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
    return "\n".join(l for l in lines if len(l) > 25 or l.endswith(":"))[:n]

def find_jobposting(blobs):
    def walk(o):
        if isinstance(o, list):
            for x in o:
                r = walk(x)
                if r: return r
        elif isinstance(o, dict):
            t = o.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t): return o
            for v in o.values():
                if isinstance(v, (dict, list)):
                    r = walk(v)
                    if r: return r
        return None
    for b in blobs:
        try: r = walk(json.loads(b))
        except Exception: continue
        if r: return r
    return None

def jp_fields(jp):
    x = jp.get("jobLocation")
    if isinstance(x, list): x = x[0] if x else {}
    a = x.get("address", {}) if isinstance(x, dict) else {}
    loc = ""
    if isinstance(a, dict):
        c = a.get("addressCountry")
        loc = ", ".join(v for v in (a.get("addressLocality"), a.get("addressRegion"), c if isinstance(c, str) else None) if v)
    d = str(jp.get("description") or "")
    d = re.sub(r"(?i)</(p|li|h\d|div|ul)>|<br\s*/?>", "\n", d)
    d = html.unescape(re.sub(r"<[^>]+>", " ", d))
    lines = [re.sub(r"\s+", " ", l).strip() for l in d.splitlines()]
    return {"title": (jp.get("title") or "").strip(), "loc": loc,
            "posted": str(jp.get("datePosted") or "")[:10], "deadline": str(jp.get("validThrough") or "")[:10],
            "jdesc": "\n".join(l for l in lines if len(l) > 3)[:1500]}

async def check(ctx, sem, name, url, prio=False):
    base = urlparse(url)
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
                if not text or len(text) <= 8 or "#" in href or href.startswith("javascript"): continue
                if BAD_TITLE.search(text): continue
                if not (KEYWORDS.search(text) or KEYWORDS.search(href) or (prio and JOBHREF.search(href))): continue   # priority companies: keep every job link
                u = urlparse(href)
                if u.netloc == base.netloc and u.path == base.path and not JOBISH.search(u.query): continue  # filter/page link of the list itself
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
            blobs = await page.eval_on_selector_all('script[type="application/ld+json"]', "els => els.map(e => e.textContent)")
            jp = find_jobposting(blobs)
            extra = jp_fields(jp) if jp else {}
            text = ""
            for sel in ("main", "article", "[role=main]", "body"):
                try: t = await page.inner_text(sel, timeout=3000)
                except Exception: continue
                if len(t.strip()) > 300 or sel == "body":
                    text = t
                    break
            full = (extra.get("jdesc") or "") + "\n" + text
            out = {"matches": [k for k, r in SKILL_RE if r.search(full)], "desc": extra.get("jdesc") or clean(text)}
            for k in ("title", "loc", "posted", "deadline"):
                if extra.get(k): out[k] = extra[k]
            return href, out
        except Exception:
            return href, None
        finally:
            await page.close()

async def score_pending(db):
    pend = [h for h, j in db["jobs"].items() if ("matches" not in j or "desc" not in j) and j.get("tries", 0) < MAX_TRIES]
    pend = sorted(pend, key=lambda h: (not db["jobs"][h].get("priority"), db["jobs"][h].get("tries", 0)))[:MAX_SCORE]
    if not pend or not SKILL_RE: return 0
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent="Mozilla/5.0 (jobwatch personal)")
        sem = asyncio.Semaphore(8)
        try:
            res = await asyncio.wait_for(asyncio.gather(*(score(ctx, sem, h) for h in pend)), 150)
        except Exception:
            res = []
        for href, out in res:
            j = db["jobs"][href]
            if out is None:
                j["tries"] = j.get("tries", 0) + 1
                if j["tries"] >= MAX_TRIES: j.update({"matches": [], "desc": "", "unreadable": True})
            else:
                j.update(out)
        try: await asyncio.wait_for(browser.close(), 20)
        except Exception: pass
    return len(res)

async def main():
    t0 = time.time()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log("jobwatch checker", VERSION, "skills:", len(SKILLS), "roles:", len(ROLES))
    db = json.loads(OUT.read_text()) if OUT.exists() else {"baseline": now, "jobs": {}, "status": {}}
    db.setdefault("jobs", {}); db.setdefault("status", {})
    db["baseline"] = db.get("baseline") or now
    sh = hashlib.md5("\n".join(SKILLS).encode()).hexdigest()
    if db.get("skills_hash") != sh:
        for j in db["jobs"].values(): j.pop("matches", None)
        db["skills_hash"] = sh
    comps = load_companies()
    prio = {n: p for n, u, p in comps}
    log("companies:", len(comps))
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(user_agent="Mozilla/5.0 (jobwatch personal)")
        sem = asyncio.Semaphore(8)
        tasks = {asyncio.create_task(check(ctx, sem, n, u, p)): (n, u) for n, u, p in comps}
        done, pending = await asyncio.wait(tasks, timeout=420)
        results = [t.result() for t in done]
        for t in pending:
            t.cancel()
            n, u = tasks[t]
            results.append((n, u, {}, "timed out (run too slow)"))
        try: await asyncio.wait_for(browser.close(), 20)
        except Exception: pass
    new = 0
    for name, url, jobs, err in results:
        old = db["status"].get(name, {})
        db["status"][name] = {"url": url, "checked": now, "ok": err is None, "error": err, "priority": prio.get(name, False),
                              "count": len(jobs) if not err else old.get("count", 0)}
        log("FAIL" if err else "ok  ", ("* " if prio.get(name) else "") + name, "-", err or f"{len(jobs)} links")
        if err: continue
        for href, title in jobs.items():
            if href not in db["jobs"]:
                db["jobs"][href] = {"company": name, "title": title, "first_seen": now}; new += 1
            db["jobs"][href]["last_seen"] = now
    for j in db["jobs"].values():
        j["roles"] = [k for k, r in ROLE_RE if r.search(j["title"])]
        j["priority"] = prio.get(j["company"], False)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(db, ensure_ascii=False, indent=1))   # save first
    scored = 0
    try: scored = await score_pending(db)
    except Exception as e: log("scoring skipped:", repr(e))
    for j in db["jobs"].values():
        j["roles"] = [k for k, r in ROLE_RE if r.search(j["title"])]
    failed = sum(not s["ok"] for s in db["status"].values())
    db["run"] = {"version": VERSION, "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "seconds": round(time.time() - t0), "new": new, "scored": scored, "failed": failed}
    OUT.write_text(json.dumps(db, ensure_ascii=False, indent=1))
    log(f"DONE {len(db['jobs'])} jobs, {new} new, {scored} pages read, {failed} companies failing, {round(time.time()-t0)}s")

asyncio.run(main())
