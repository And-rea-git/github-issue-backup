#!/usr/bin/env python3
"""
Archive a GitHub repository's issue tracker to plain JSON files, with the images
and files attached to issues, a readable Markdown rendering of every issue, and a
CSV index. Safe to stop and re-run at any time.

Usage: edit the CONFIGURATION block below, then run
    python archive_github_issues.py

Output layout
    OUT_DIR/
      archive.log
      index.csv                      one row per issue, for reviewing in a spreadsheet
      index/pages/page-0001.json     raw issue-list pages (resume point for step 1)
      issues/00042/
          issue.json                 the issue itself (body, labels, assignees, ...)
          comments.json              every comment
          timeline.json              every event: labels, assignments, renames, closes,
                                     cross-references, commits, ... (includes comments)
          reactions.json             who reacted to the issue / comments
          issue.md                   readable rendering (attachment links point at local copies)
          DONE                       marker, written last; contains the issue's updated_at
      users/<login>.json             full profile of every person who appears anywhere
      attachments/
          manifest.json              original URL -> local file, status, which issues use it
          <hash>.png, ...            the downloaded images / files

Restart behaviour
    * Every file is written atomically (temp file + rename), so a crash never leaves
      a half-written file.
    * An issue is skipped only if its DONE marker exists and matches updated_at.
      Partially fetched issues reuse whatever pieces were already saved.
    * Attachments already downloaded (status "ok" in the manifest) are skipped;
      failed ones are retried on the next run.
    * Re-running later does an incremental refresh: only issues whose updated_at
      changed are re-fetched. Set REFRESH_INDEX = True to re-read the issue list.
"""
import csv
import hashlib
import http.client
import json
import mimetypes
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

# =============================== CONFIGURATION ===============================
REPO = "user/repo-name"
TOKEN = "your-token-here"
OUT_DIR = "github-archive"

INCLUDE_PULL_REQUESTS = False                # GitHub lists PRs as issues
FETCH_REACTIONS = True                       # who reacted to the issue / each comment
DOWNLOAD_ATTACHMENTS = True                  # images and files uploaded to GitHub
DOWNLOAD_EXTERNAL_IMAGES = False             # probably unsafe because it's external images
REFRESH_INDEX = True                         # True = ignore cached issue-list pages

API_BASE = "https://api.github.com"          # change for GitHub Enterprise
ATTACHMENT_TOKEN_HOSTS = {"github.com"}      # web hosts that may receive your token
# =============================================================================

# URLs of files uploaded through GitHub's "attach files" feature
GITHUB_ATTACHMENT_RE = re.compile(
    r"https://(?:github\.com/user-attachments/(?:assets|files)/"
    r"|user-images\.githubusercontent\.com/"
    r"|private-user-images\.githubusercontent\.com/"
    r"|github\.com/[\w.-]+/[\w.-]+/assets/)[^\s)\"'<>\]\\]+")
# Markdown images and <img> tags pointing anywhere (used if DOWNLOAD_EXTERNAL_IMAGES).
EXTERNAL_IMAGE_RE = re.compile(
    r"""!\[[^\]]*\]\((https?://[^)\s]+)|<img\s[^>]*?src=["'](https?://[^"']+)["']""", re.I)

UA = "github-issue-archiver"
API_HEADERS = {"Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": UA}
DOWNLOAD_HEADERS = {"Accept": "*/*", "User-Agent": UA}
NET_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError)

LOGFILE = None
_token_hosts = set()
_requests = 0


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    if LOGFILE:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ----------------------------------------------------------------- file helpers
def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_text(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


# ------------------------------------------------------------------ HTTP layer
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None          # make 3xx responses visible so we can follow them ourselves


_opener = urllib.request.build_opener(_NoRedirect)


def follow(url, headers, timeout=60, max_hops=10):
    """GET a URL, following redirects by hand so that the token is only ever sent to
    trusted hosts (the API host and ATTACHMENT_TOKEN_HOSTS), never to the storage
    servers GitHub redirects to. Returns (status, headers, response); the caller
    must close the response."""
    global _requests
    for _ in range(max_hops):
        h = dict(headers)
        if TOKEN and urlparse(url).netloc in _token_hosts:
            h["Authorization"] = f"Bearer {TOKEN}"
        try:
            r = _opener.open(urllib.request.Request(url, headers=h), timeout=timeout)
            status = r.status
        except urllib.error.HTTPError as e:
            r, status = e, e.code
        _requests += 1
        if status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            url = urljoin(url, r.headers["Location"])
            r.close()
            continue
        return status, r.headers, r
    raise RuntimeError("too many redirects")


def _sleep(seconds, why):
    seconds = max(1, int(seconds))
    log(f"sleeping {seconds}s ({why})")
    time.sleep(seconds)


def _sleep_until_reset(headers):
    reset = headers.get("X-RateLimit-Reset")
    wait = (int(reset) - time.time() + 5) if reset else 60
    _sleep(wait, "primary rate limit exhausted; waiting for reset")


class Resp:
    def __init__(self, headers, body):
        self.headers, self.body = headers, body
        self.links = {rel: u for u, rel in re.findall(
            r'<([^>]+)>\s*;\s*rel="([^"]+)"', headers.get("Link") or "")}

    def json(self):
        return json.loads(self.body)


def api_get(url, params=None, allow_404=False):
    """One API call with rate-limit handling and retries. Returns Resp (None on an
    allowed 404/410)."""
    if url.startswith("/"):
        url = API_BASE.rstrip("/") + url
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    for attempt in range(10):
        try:
            status, hdrs, resp = follow(url, API_HEADERS)
            try:
                body = resp.read()
            finally:
                resp.close()
        except NET_ERRORS as e:
            _sleep(min(2 ** attempt, 120), f"network error: {e!r}")
            continue

        remaining = hdrs.get("X-RateLimit-Remaining")
        text = body.decode("utf-8", "replace")
        if status == 200:
            if remaining is not None and int(remaining) < 10:
                _sleep_until_reset(hdrs)
            return Resp(hdrs, body)
        if status in (404, 410) and allow_404:
            return None
        if status in (403, 429):
            retry_after = hdrs.get("Retry-After")
            if retry_after:
                _sleep(int(retry_after) + 1, "secondary rate limit")
            elif remaining == "0":
                _sleep_until_reset(hdrs)
            elif "rate limit" in text.lower():
                _sleep(60 * (attempt + 1), "rate limit (no headers)")
            else:
                raise RuntimeError(f"HTTP {status} for {url}: {text[:300]}")
            continue
        if status >= 500:
            _sleep(min(2 ** attempt, 120), f"server error {status}")
            continue
        raise RuntimeError(f"HTTP {status} for {url}: {text[:300]}")
    raise RuntimeError(f"giving up on {url} after repeated failures")


def get_all(url, params=None):
    """Follow Link: rel=next pagination and return all items as one list."""
    params = dict(params or {})
    params.setdefault("per_page", 100)
    items = []
    while url:
        r = api_get(url, params)
        items.extend(r.json())
        url = r.links.get("next")
        params = None        # the 'next' URL already carries its query string
    return items


# ------------------------------------------------- step 1: list all the issues
def list_issues(out):
    pages_dir = out / "index" / "pages"
    base = {"state": "all", "sort": "created", "direction": "asc", "per_page": 100}
    items_all, page = [], 1
    while True:
        pf = pages_dir / f"page-{page:04d}.json"
        saved = read_json(pf) if (pf.exists() and not REFRESH_INDEX) else None
        if saved and saved["has_next"]:
            items, has_next = saved["items"], True      # trusted: a full page
        else:                                           # missing, or the last page: (re)fetch
            r = api_get(f"/repos/{REPO}/issues", {**base, "page": page})
            items, has_next = r.json(), "next" in r.links
            write_json(pf, {"has_next": has_next, "items": items})
        items_all.extend(items)
        log(f"index page {page}: {len(items)} items")
        if not has_next:
            break
        page += 1
    # The issues endpoint also returns pull requests; drop them unless asked.
    if not INCLUDE_PULL_REQUESTS:
        items_all = [i for i in items_all if "pull_request" not in i]
    return items_all


# --------------------------------------------------- step 2: per-issue details
def fetch_reactions(issue, comments):
    res = {"issue": [], "comments": {}}
    if (issue.get("reactions") or {}).get("total_count"):
        res["issue"] = get_all(f"/repos/{REPO}/issues/{issue['number']}/reactions")
    for cm in comments:
        if (cm.get("reactions") or {}).get("total_count"):
            res["comments"][str(cm["id"])] = get_all(
                f"/repos/{REPO}/issues/comments/{cm['id']}/reactions")
    return res


def fetch_issue(item, out):
    n = item["number"]
    d = out / "issues" / f"{n:05d}"
    done = d / "DONE"
    if done.exists():
        if done.read_text().strip() == str(item["updated_at"]):
            return False
        for name in ("comments.json", "timeline.json", "reactions.json"):
            (d / name).unlink(missing_ok=True)
        done.unlink()

    write_json(d / "issue.json", item)

    cf = d / "comments.json"
    write_json(cf, get_all(f"/repos/{REPO}/issues/{n}/comments"))
    comments = read_json(cf)

    tf = d / "timeline.json"
    write_json(tf, get_all(f"/repos/{REPO}/issues/{n}/timeline"))

    rf = d / "reactions.json"
    if FETCH_REACTIONS:
        write_json(rf, fetch_reactions(item, comments))

    done.write_text(str(item["updated_at"]))             # written last = commit point
    return True


# ------------------------------------------------------------ step 3: people
def collect_users(obj, found):
    """Find every GitHub user object anywhere in a parsed JSON document."""
    if isinstance(obj, dict):
        if "login" in obj and isinstance(obj.get("url"), str) and "/users/" in obj["url"]:
            found.setdefault(obj["login"], obj)
        for v in obj.values():
            collect_users(v, found)
    elif isinstance(obj, list):
        for v in obj:
            collect_users(v, found)


def fetch_users(out):
    found = {}
    for d in sorted((out / "issues").iterdir()):
        for name in ("issue.json", "comments.json", "timeline.json", "reactions.json"):
            f = d / name
            if f.exists():
                collect_users(read_json(f), found)
    log(f"{len(found)} distinct people found")
    for i, (login, stub) in enumerate(sorted(found.items()), 1):
        uf = out / "users" / f"{safe_name(login)}.json"
        if uf.exists():
            continue
        r = api_get(stub["url"], allow_404=True)
        if r is not None:
            write_json(uf, r.json())
        else:
            write_json(uf, {**stub, "_note": "full profile unavailable (404); summary only"})
        if i % 25 == 0:
            log(f"users: {i}/{len(found)}")


# --------------------------------------------------------- step 4: attachments
def issue_texts(d):
    """Yield the user-written text of one issue: description, comments, review bodies."""
    issue = read_json(d / "issue.json")
    yield issue.get("body") or ""
    for cm in read_json(d / "comments.json"):
        yield cm.get("body") or ""
    for ev in read_json(d / "timeline.json"):
        if ev.get("event") in ("reviewed", "line-commented", "commit-commented"):
            yield ev.get("body") or ""


def find_attachment_urls(text):
    urls = {m.group(0) for m in GITHUB_ATTACHMENT_RE.finditer(text)}
    if DOWNLOAD_EXTERNAL_IMAGES:
        for m in EXTERNAL_IMAGE_RE.finditer(text):
            urls.add(m.group(1) or m.group(2))
    return {u.rstrip(".,;:!?*_'\"") for u in urls}


def filename_for(url, content_type):
    h = hashlib.sha256(url.encode()).hexdigest()[:16]
    base = os.path.basename(urlparse(url).path)
    if os.path.splitext(base)[1]:                         # e.g. .../files/123/crash.log
        return f"{h}-{safe_name(base)[-100:]}"
    return h + (mimetypes.guess_extension(content_type) or "")   # .../assets/<uuid> has no extension


def download(url, dest_dir):
    """Returns (status, filename, content_type, size); status is 'ok' or 'failed: ...'."""
    last = "failed: network error"
    for attempt in range(4):
        try:
            status, hdrs, resp = follow(url, DOWNLOAD_HEADERS, timeout=120)
        except NET_ERRORS as e:
            last = f"failed: {e!r}"
            time.sleep(2 ** attempt)
            continue
        try:
            if status == 200:
                ctype = hdrs.get_content_type()
                if ctype == "text/html":
                    return "failed: got an HTML page (probably needs a login)", None, ctype, 0
                name = filename_for(url, ctype)
                tmp = dest_dir / (name + ".tmp")
                try:
                    with open(tmp, "wb") as f:
                        shutil.copyfileobj(resp, f)
                except NET_ERRORS as e:
                    last = f"failed: {e!r}"
                    time.sleep(2 ** attempt)
                    continue
                os.replace(tmp, dest_dir / name)
                return "ok", name, ctype, (dest_dir / name).stat().st_size
            if status == 429 or status >= 500:
                last = f"failed: HTTP {status}"
                time.sleep(2 ** attempt * 2)
                continue
            return f"failed: HTTP {status}", None, None, 0
        finally:
            resp.close()
    return last, None, None, 0


def download_attachments(out):
    adir = out / "attachments"
    adir.mkdir(parents=True, exist_ok=True)
    mf = adir / "manifest.json"
    manifest = read_json(mf) if mf.exists() else {}

    found = {}                                            # url -> issue numbers using it
    for d in sorted((out / "issues").iterdir()):
        if not (d / "DONE").exists():
            continue
        for text in issue_texts(d):
            for url in find_attachment_urls(text):
                found.setdefault(url, set()).add(int(d.name))
    log(f"{len(found)} distinct attachment URLs found")

    new_ok = 0
    for i, (url, nums) in enumerate(sorted(found.items()), 1):
        e = manifest.get(url)
        if e and e.get("status") == "ok" and (adir / e["file"]).exists():
            e["issues"] = sorted(nums)
            continue
        status, name, ctype, size = download(url, adir)
        manifest[url] = {"status": status, "file": name, "content_type": ctype,
                         "bytes": size, "issues": sorted(nums)}
        write_json(mf, manifest)                          # saved after every download
        new_ok += status == "ok"
        log(f"attachment {i}/{len(found)}: {status}  {url}")
    write_json(mf, manifest)
    failed = {u: e for u, e in manifest.items() if e["status"] != "ok" and u in found}
    log(f"attachments: {new_ok} downloaded this run, {len(failed)} failed")
    return manifest, failed


# ------------------------------------------- step 5: readable output + index
def uname(u):
    return u.get("login", "?") if isinstance(u, dict) else "unknown"


def reaction_str(r):
    if not r:
        return ""
    names = ["+1", "-1", "laugh", "hooray", "confused", "heart", "rocket", "eyes"]
    return ", ".join(f"{k} x{r[k]}" for k in names if r.get(k))


def describe(ev):
    bits = []
    if ev.get("label"):
        bits.append(ev["label"].get("name", ""))
    if ev.get("assignee"):
        bits.append(uname(ev["assignee"]))
    if ev.get("rename"):
        bits.append(f'"{ev["rename"].get("from")}" -> "{ev["rename"].get("to")}"')
    if ev.get("milestone"):
        bits.append(ev["milestone"].get("title", ""))
    if ev.get("commit_id"):
        bits.append(ev["commit_id"][:7])
    src = (ev.get("source") or {}).get("issue")
    if src:
        bits.append(src.get("html_url", ""))
    if ev.get("state_reason"):
        bits.append(ev["state_reason"])
    return ev.get("event", "?") + (": " + ", ".join(b for b in bits if b) if bits else "")


def render_issue_md(d):
    issue = read_json(d / "issue.json")
    timeline = read_json(d / "timeline.json") if (d / "timeline.json").exists() else []
    L = [f"# #{issue['number']}: {issue['title']}", ""]
    L.append(f"- State: {issue['state']}" +
             (f" ({issue['state_reason']})" if issue.get("state_reason") else ""))
    L.append(f"- Author: {uname(issue.get('user'))}")
    L.append(f"- Created: {issue.get('created_at')}   Updated: {issue.get('updated_at')}"
             f"   Closed: {issue.get('closed_at')}")
    L.append("- Labels: " + ", ".join(l["name"] for l in issue.get("labels", [])))
    L.append("- Assignees: " + ", ".join(uname(a) for a in issue.get("assignees", [])))
    if issue.get("milestone"):
        L.append(f"- Milestone: {issue['milestone'].get('title')}")
    rs = reaction_str(issue.get("reactions"))
    if rs:
        L.append(f"- Reactions: {rs}")
    L.append(f"- URL: {issue.get('html_url')}")
    L += ["", issue.get("body") or "_(no description)_", "", "---", "", "## Timeline", ""]
    for ev in timeline:
        kind = ev.get("event", "?")
        when = (ev.get("created_at") or ev.get("submitted_at")
                or (ev.get("author") or {}).get("date") or "")
        if kind == "committed":
            who = (ev.get("author") or {}).get("name", "?")
            first = (ev.get("message") or "").splitlines()[0] if ev.get("message") else ""
            L.append(f"- {when}  **{who}** committed {str(ev.get('sha', ''))[:7]}: {first}")
            continue
        who = uname(ev.get("actor") or ev.get("user"))
        if ev.get("body"):
            extra = reaction_str(ev.get("reactions"))
            L += ["", f"### {when}  {who}  ({kind}{', ' + extra if extra else ''})", "",
                  ev["body"], "", "---", ""]
        else:
            L.append(f"- {when}  **{who}**  {describe(ev)}")
    return "\n".join(L) + "\n"


def localize(text, manifest):
    """Point attachment links at the downloaded copies (paths relative to issue.md)."""
    for url in sorted(manifest, key=len, reverse=True):
        e = manifest[url]
        if e.get("status") == "ok" and url in text:
            text = text.replace(url, "../../attachments/" + e["file"])
    return text


def build_outputs(out, issues, manifest):
    rows, problems = [], []
    for item in sorted(issues, key=lambda i: i["number"]):
        n = item["number"]
        d = out / "issues" / f"{n:05d}"
        if not (d / "DONE").exists():
            problems.append(f"#{n}: not fully fetched")
            continue
        comments = read_json(d / "comments.json")
        timeline = read_json(d / "timeline.json")
        ok = len(comments) == item.get("comments", len(comments))
        if not ok:
            problems.append(f"#{n}: GitHub reports {item.get('comments')} comments, "
                            f"saved {len(comments)} (new comment since listing?)")
        write_text(d / "issue.md", localize(render_issue_md(d), manifest))
        rows.append({
            "number": n, "state": item["state"], "title": item["title"],
            "author": uname(item.get("user")), "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"), "closed_at": item.get("closed_at"),
            "labels": ";".join(l["name"] for l in item.get("labels", [])),
            "comments_expected": item.get("comments"), "comments_saved": len(comments),
            "timeline_events": len(timeline), "comment_count_ok": ok,
        })
    if rows:
        tmp = out / "index.csv.tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, out / "index.csv")
    return rows, problems


# --------------------------------------------------------------------- main
def main():
    global LOGFILE, _token_hosts
    if "/" not in REPO or REPO == "user/repo-name":
        print("Edit REPO at the top of the script first.")
        return
    if not TOKEN:
        print("Set TOKEN at the top of the script")
        return
    _token_hosts = {urlparse(API_BASE).netloc} | set(ATTACHMENT_TOKEN_HOSTS)

    out = Path(OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    LOGFILE = out / "archive.log"

    log(f"=== archiving {REPO} into {out}/ ===")
    issues = list_issues(out)
    log(f"{len(issues)} issues to archive")

    fetched = 0
    for i, item in enumerate(sorted(issues, key=lambda x: x["number"]), 1):
        if fetch_issue(item, out):
            fetched += 1
            if fetched % 10 == 0:
                log(f"progress {i}/{len(issues)} (#{item['number']}), "
                    f"{_requests} requests this run")
    log(f"issues: {fetched} fetched this run, {len(issues) - fetched} already up to date")

    fetch_users(out)

    manifest, failed = {}, {}
    if DOWNLOAD_ATTACHMENTS:
        manifest, failed = download_attachments(out)
    elif (out / "attachments" / "manifest.json").exists():
        manifest = read_json(out / "attachments" / "manifest.json")

    rows, problems = build_outputs(out, issues, manifest)
    log(f"wrote {len(rows)} issue.md files and index.csv")

    if problems or failed:
        log("things to look at:")
        for p in problems:
            log("  " + p)
        for url, e in failed.items():
            log(f"  attachment {e['status']}: {url}  (issues {e['issues']})")
    else:
        log("verification: every issue fetched, every comment count matches, "
            "every attachment downloaded")
    log("=== done ===")


if __name__ == "__main__":
    main()
