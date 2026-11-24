#!/usr/bin/env python3
"""
GitHub Archive Viewer (Optimized & Secure)
Generates a static, GitHub-like HTML website from the output of archive_github_issues.py.
Uses only Python standard libraries. Minimal external sources.
"""
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# =============================== CONFIGURATION ===============================
OUT_DIR = "github-archive"                   # the folder written by archive_github_issues.py
# =============================================================================

# ================================== STYLES ===================================
GITHUB_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; color: #24292f; line-height: 1.5; max-width: 1012px; margin: 0 auto; padding: 24px; background: #fff; }
a { color: #0969da; text-decoration: none; }
a:hover { text-decoration: underline; }
.header { border-bottom: 1px solid #d0d7de; padding-bottom: 16px; margin-bottom: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
.issue-title { font-size: 28px; font-weight: 600; margin: 0 0 16px 0; color: #1f2328; }
.state { display: inline-block; padding: 4px 12px; border-radius: 20px; color: #fff; font-size: 14px; font-weight: 600; margin-right: 8px; }
.state-open { background: #1a7f37; }
.state-closed { background: #8250df; }
.label { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 12px; font-weight: 500; margin-right: 4px; background: #ddf4ff; color: #0969da; border: 1px solid #54aeff; }
.meta-grid { display: grid; grid-template-columns: 120px 1fr; gap: 8px; margin-bottom: 24px; font-size: 14px; }
.meta-key { font-weight: 600; color: #656d76; }
.timeline-item { border: 1px solid #d0d7de; border-radius: 6px; margin-bottom: 16px; }
.timeline-header { background: #f6f8fa; padding: 8px 16px; border-bottom: 1px solid #d0d7de; display: flex; justify-content: space-between; align-items: center; font-size: 13px; color: #656d76; }
.timeline-header strong { color: #24292f; }
.timeline-body { padding: 16px; font-size: 14px; }
.timeline-event { padding: 8px 16px; font-size: 13px; color: #656d76; border-left: 2px solid #d0d7de; margin-left: 16px; margin-bottom: 8px; background: #f6f8fa; border-radius: 0 6px 6px 0; }
.timeline-event strong { color: #24292f; }
.reactions { display: flex; gap: 6px; margin-top: 12px; flex-wrap: wrap; }
.reaction-pill { background: #ddf4ff; border: 1px solid #54aeff; border-radius: 16px; padding: 2px 8px; font-size: 12px; color: #0969da; }
table { border-collapse: collapse; width: 100%; margin-bottom: 16px; }
th, td { border: 1px solid #d0d7de; padding: 6px 12px; text-align: left; }
th { background: #f6f8fa; font-weight: 600; }
pre { background: #f6f8fa; padding: 16px; border-radius: 6px; overflow-x: auto; border: 1px solid #d0d7de; }
code { background: rgba(175,184,193,0.2); padding: 2px 5px; border-radius: 4px; font-size: 85%; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
pre code { background: none; padding: 0; }
blockquote { border-left: 4px solid #d0d7de; padding: 0 16px; color: #656d76; margin: 0; }
.search-box { padding: 8px 12px; border: 1px solid #d0d7de; border-radius: 6px; font-size: 14px; }
.filter-btn { padding: 6px 12px; border: 1px solid #d0d7de; background: #fff; border-radius: 6px; cursor: pointer; font-size: 14px; }
.filter-btn.active { background: #0969da; color: #fff; border-color: #0969da; }
tr:hover { background: #f6f8fa; }
.user-card { border: 1px solid #d0d7de; border-radius: 6px; padding: 16px; max-width: 300px; }
"""

# =============================== SECURITY & PARSING ==========================
EMOJIS = {
    "+1": "👍", "-1": "👎", "laugh": "😄", "hooray": "🎉", 
    "confused": "😕", "heart": "❤️", "rocket": "🚀", "eyes": "👀"
}

def safe_name(s: str) -> str:
    """Matches the sanitization logic used by the archive script for filenames."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)

def is_safe_url(url: str) -> bool:
    """Strictly allow only relative paths to prevent external requests and XSS."""
    if not url or not url.strip():
        return False
    if url.strip().startswith('//'):
        return False
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc:
        return False
    return True

def md_to_html(text: str) -> str:
    if not text:
        return "<p><em>No description provided.</em></p>"
    
    text = html.escape(text)
    
    # Code blocks
    text = re.sub(r'```(\w*)\n(.*?)```',
                  lambda m: f'<pre><code class="language-{re.sub(r"[^a-zA-Z0-9_-]", "", m.group(1))}">{m.group(2)}</code></pre>',
                  text, flags=re.DOTALL)
    
    # Inline code
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    
    # Images & Links
    def repl_img(m: re.Match) -> str:
        url, alt = m.group(2), m.group(1)
        return f'<img src="{url}" alt="{alt}" loading="lazy">' if is_safe_url(url) else f'[Invalid Image URL: {alt}]'
    text = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', repl_img, text)

    # Handle raw HTML <img> tags that were escaped by html.escape()
    def repl_raw_html_img(m: re.Match) -> str:
        tag = m.group(0)
        src_match = re.search(r'src=&quot;([^&]+)&quot;|src=&#x27;([^&]+)&#x27;', tag)
        if not src_match:
            return tag
        url_escaped = src_match.group(1) or src_match.group(2)
        url = html.unescape(url_escaped)
        if is_safe_url(url):
            alt_match = re.search(r'alt=&quot;([^&]*)&quot;|alt=&#x27;([^&]*)&#x27;', tag)
            alt = ""
            if alt_match:
                alt_escaped = alt_match.group(1) if alt_match.group(1) is not None else (alt_match.group(2) or "")
                alt = html.unescape(alt_escaped)
            return f'<img src="{html.escape(url)}" alt="{html.escape(alt)}" loading="lazy">'
        return '[Invalid Image URL]'
    text = re.sub(r'&lt;img\b.*?&gt;', repl_raw_html_img, text, flags=re.IGNORECASE)
    
    def repl_link(m: re.Match) -> str:
        url, label = m.group(2), m.group(1)
        return f'<a href="{url}" rel="noopener noreferrer">{label}</a>' if is_safe_url(url) else f'[Invalid Link URL: {label}]'
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', repl_link, text)
    
    # Tables (Basic GitHub Flavored Markdown)
    def repl_table(m: re.Match) -> str:
        rows = m.group(0).split('\n')
        if len(rows) < 2: return m.group(0)
        html_table = ['<table>']
        header = [c.strip() for c in rows[0].strip('|').split('|')]
        html_table.append('<thead><tr>' + ''.join(f'<th>{c}</th>' for c in header) + '</tr></thead>')
        html_table.append('<tbody>')
        for row in rows[2:]:
            if not row.strip(): continue
            cells = [c.strip() for c in row.strip('|').split('|')]
            html_table.append('<tr>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>')
        html_table.append('</tbody></table>')
        return '\n'.join(html_table)
    text = re.sub(r'(^\|.*\|\n)+', repl_table, text, flags=re.MULTILINE)

    # Headers
    text = re.sub(r'^### (.*$)', r'<h3>\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.*$)', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.*$)', r'<h1>\1</h1>', text, flags=re.MULTILINE)
    
    # Bold & Italic
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<em>\1</em>', text)
    
    # Lists (Unordered and Ordered)
    text = re.sub(r'^\s*[-*]\s+(.*)$', r'<li>\1</li>', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+(.*)$', r'<li>\1</li>', text, flags=re.MULTILINE)
    text = re.sub(r'(<li>.*?</li>\s*)+', lambda m: f'<ul>{m.group(0)}</ul>', text)
    
    # Blockquotes
    text = re.sub(r'^>\s?(.*)$', r'<blockquote>\1</blockquote>', text, flags=re.MULTILINE)
    
    # Horizontal Rules
    text = re.sub(r'^---$', r'<hr>', text, flags=re.MULTILINE)

    # Raw attachments (mp4, txt, etc.)
    def repl_raw_attachment(m: re.Match) -> str:
        prefix, url = m.group(1), m.group(2)
        if not is_safe_url(url):
            return m.group(0)
        
        ext = urlparse(url).path.split('.')[-1].lower()
        if ext in ('mp4', 'webm', 'mov'):
            return f'{prefix}<video src="{html.escape(url)}" controls style="max-width:100%; margin:8px 0; border-radius:6px; background:#000;"></video>'
        elif ext in ('txt', 'md', 'json', 'csv', 'log'):
            filename = url.split("/")[-1]
            return f'{prefix}<a href="{html.escape(url)}" download style="display:inline-flex; align-items:center; gap:6px; padding:6px 12px; background:#f6f8fa; border:1px solid #d0d7de; border-radius:6px; text-decoration:none; color:#0969da; font-size:14px;">📄 {html.escape(filename)}</a>'
        return m.group(0)

    # Match standalone relative/absolute URLs ending in media or text extensions
    text = re.sub(r'(^|\s)((?:\.\./|/)?[^\s]+\.(?:mp4|webm|mov|txt|md|json|csv|log))(?=[\s.,;!?)]|$)', repl_raw_attachment, text)
    
    # Line breaks
    text = text.replace('\n', '<br>\n')
    
    return text

def format_date(iso_str: str) -> str:
    if not iso_str: return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        return dt.strftime("%b %d, %Y • %H:%M")
    except Exception:
        return iso_str

def localize_text(text: str, manifest: dict) -> str:
    if not text: return ""
    for url, data in manifest.items():
        if data.get("status") == "ok" and url in text:
            text = text.replace(url, f"../../attachments/{data['file']}")
    return text

def render_reactions(reactions: list) -> str:
    if not reactions: return ""
    counts = {}
    for r in reactions:
        content = r.get("content")
        if content in EMOJIS:
            counts[content] = counts.get(content, 0) + 1
    pills = [f'<span class="reaction-pill">{EMOJIS[k]} {v}</span>' for k, v in counts.items() if v > 0]
    return f'<div class="reactions">{"".join(pills)}</div>' if pills else ""

def label_html(name: str, color: str = "") -> str:
    """A label pill in the label's own GitHub colour (falls back to the default style)."""
    style = ""
    if re.fullmatch(r"[0-9a-fA-F]{6}", color or ""):
        r, g, b = (int(color[i:i + 2], 16) for i in (0, 2, 4))
        text = "#1f2328" if (0.299 * r + 0.587 * g + 0.114 * b) >= 128 else "#fff"
        style = f' style="background:#{color}; color:{text}; border-color:#{color}"'
    return f'<span class="label"{style}>{html.escape(name)}</span>'

# ================================== BUILDERS =================================
def load_issues(out_dir: Path) -> list:
    """Read the index data straight from each issue's JSON files. Only fully fetched
    issues (those with a DONE marker) are listed, as in the archiver's index.csv."""
    issues = []
    issues_dir = out_dir / "issues"
    if not issues_dir.exists(): return issues
    for d in sorted(issues_dir.iterdir()):
        if not (d.is_dir() and (d / "DONE").exists() and (d / "issue.json").exists()):
            continue
        issue = json.loads((d / "issue.json").read_text(encoding="utf-8"))
        comments_path = d / "comments.json"
        comment_count = (len(json.loads(comments_path.read_text(encoding="utf-8")))
                         if comments_path.exists() else issue.get("comments", 0))
        issues.append({
            "number": issue["number"],
            "state": issue["state"],
            "title": issue["title"],
            "author": (issue.get("user") or {}).get("login") or "ghost",
            "created_at": issue.get("created_at"),
            "updated_at": issue.get("updated_at"),
            "comments": comment_count,
            "labels": [l for l in issue.get("labels", []) if isinstance(l, dict)],
        })
    return sorted(issues, key=lambda i: i["number"])

def build_index(out_dir: Path, issues_data: list):
    rows = []
    for item in issues_data:
        state_class = "state-open" if item['state'] == 'open' else "state-closed"
        labels = " ".join(label_html(l.get("name", ""), l.get("color", "")) for l in item['labels'])
        label_names = " ".join(l.get("name", "") for l in item['labels'])
        search_data = f"{item['title']} {item['author']} {label_names} {item['state']}".lower()
        author = html.escape(item['author'])
        if (out_dir / "users" / f"{safe_name(item['author'])}.html").exists():
            author = f'<a href="users/{safe_name(item["author"])}.html">{author}</a>'
        rows.append(f"""
<tr class="issue-row" data-search="{html.escape(search_data)}" data-state="{html.escape(item['state'])}">
<td><span class="state {state_class}">{html.escape(item['state'])}</span></td>
<td><a href="issues/{item['number']:05d}/index.html"><strong>#{item['number']}</strong> {html.escape(item['title'])}</a><br>{labels}</td>
<td>{author}</td>
<td>{format_date(item['created_at'])}</td>
<td>{format_date(item['updated_at'])}</td>
<td>{item['comments']}</td>
</tr>""")
    
    html_content = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>GitHub Archive</title>
<style>{GITHUB_CSS}</style></head><body>
<div class="header">
<h1>Repository Archive</h1>
<div>
<button class="filter-btn active" onclick="filterState('all')">All</button>
<button class="filter-btn" onclick="filterState('open')">Open</button>
<button class="filter-btn" onclick="filterState('closed')">Closed</button>
<input type="text" id="search" class="search-box" placeholder="Filter..." onkeyup="filterIssues()">
</div>
</div>
<table>
<thead><tr><th>State</th><th>Title</th><th>Author</th><th>Created</th><th>Updated</th><th>Comments</th></tr></thead>
<tbody>{"".join(rows)}</tbody>
</table>
<script>
let currentState = 'all';
function filterState(state) {{
currentState = state;
document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
event.target.classList.add('active');
filterIssues();
}}
function filterIssues() {{
let filter = document.getElementById('search').value.toLowerCase();
document.querySelectorAll('.issue-row').forEach(row => {{
let matchSearch = row.getAttribute('data-search').includes(filter);
let matchState = currentState === 'all' || row.getAttribute('data-state') === currentState;
row.style.display = (matchSearch && matchState) ? '' : 'none';
}});
}}
</script>
</body></html>"""
    (out_dir / "index.html").write_text(html_content, encoding="utf-8")

def build_issue_page(issue_dir: Path, manifest: dict) -> bool:
    issue_path = issue_dir / "issue.json"
    if not issue_path.exists(): return False
    
    issue = json.loads(issue_path.read_text(encoding="utf-8"))
    comments = json.loads((issue_dir / "comments.json").read_text(encoding="utf-8")) if (issue_dir / "comments.json").exists() else []
    timeline = json.loads((issue_dir / "timeline.json").read_text(encoding="utf-8")) if (issue_dir / "timeline.json").exists() else []
    reactions_data = json.loads((issue_dir / "reactions.json").read_text(encoding="utf-8")) if (issue_dir / "reactions.json").exists() else {}
    
    state_class = "state-open" if issue['state'] == 'open' else "state-closed"
    
    # Metadata
    meta_html = []
    labels = " ".join(f'<span class="label">{html.escape(l["name"])}</span>' for l in issue.get("labels", []))
    if labels: meta_html.append(f'<div class="meta-key">Labels:</div><div>{labels}</div>')
    
    assignees = ", ".join(f'<a href="../../users/{safe_name(a["login"])}.html">{html.escape(a["login"])}</a>' for a in issue.get("assignees", []))
    if assignees: meta_html.append(f'<div class="meta-key">Assignees:</div><div>{assignees}</div>')
    
    if issue.get("milestone"):
        meta_html.append(f'<div class="meta-key">Milestone:</div><div>{html.escape(issue["milestone"]["title"])}</div>')
    if issue.get("state_reason"):
        meta_html.append(f'<div class="meta-key">State Reason:</div><div>{html.escape(issue["state_reason"])}</div>')
    if issue.get("closed_at"):
        meta_html.append(f'<div class="meta-key">Closed:</div><div>{format_date(issue["closed_at"])}</div>')
    
    # Main Issue Body
    issue_body = localize_text(issue.get("body") or "", manifest)
    issue_reactions = render_reactions(reactions_data.get("issue", []))
    
    timeline_html = [f"""
<div class="timeline-item">
<div class="timeline-header">
<span><strong>{html.escape(issue["user"]["login"])}</strong> opened this issue on {format_date(issue["created_at"])}</span>
</div>
<div class="timeline-body">
{md_to_html(issue_body)}
{issue_reactions}
</div>
</div>"""]
    
    # Interleave Comments and Timeline Events
    events = []
    for c in comments:
        events.append({"type": "comment", "date": c["created_at"], "data": c})
    
    for t in timeline:
        # Include 'reviewed' events now, filter out standard 'commented' to avoid duplication
        if t["event"] != "commented":
            events.append({"type": "event", "date": t.get("created_at"), "data": t})
            
    events.sort(key=lambda x: x["date"] or "")
    
    for ev in events:
        if ev["type"] == "comment":
            c = ev["data"]
            body = localize_text(c.get("body") or "", manifest)
            c_reactions = render_reactions(reactions_data.get("comments", {}).get(str(c["id"]), []))
            timeline_html.append(f"""
<div class="timeline-item">
<div class="timeline-header">
<span><strong>{html.escape(c["user"]["login"])}</strong> commented</span>
<span>{format_date(c["created_at"])}</span>
</div>
<div class="timeline-body">
{md_to_html(body)}
{c_reactions}
</div>
</div>""")
        else:
            t = ev["data"]
            event_type = t.get("event", "ghost")
            if event_type == "committed":
                actor = (t.get("author") or {}).get("name", "ghost")
                sha = str(t.get("sha", ""))[:7]
                msg = (t.get("message") or "").splitlines()[0]
                event_desc = f"committed <code>{html.escape(sha)}</code>: {html.escape(msg)}"
            elif event_type == "reviewed":
                actor = t.get("user", {}).get("login", "ghost")
                state = t.get("state", "")
                event_desc = f"reviewed this ({html.escape(state)})"
                if t.get("body"):
                    timeline_html.append(f"""
<div class="timeline-item">
<div class="timeline-header">
<span><strong>{html.escape(actor)}</strong> reviewed</span>
<span>{format_date(t.get("submitted_at") or t.get("created_at"))}</span>
</div>
<div class="timeline-body">
{md_to_html(localize_text(t["body"], manifest))}
</div>
</div>""")
                    continue
            else:
                actor = (t.get("actor") or {}).get("login", "ghost")
                event_desc = event_type
                src_issue = (t.get("source") or {}).get("issue")
                if t.get("label"):
                    event_desc += f" '{html.escape(t['label']['name'])}'"
                if t.get("assignee"):
                    event_desc += f" '{html.escape(t['assignee'].get('login', ''))}'"
                if t.get("rename"):
                    event_desc += f" '{html.escape(t['rename'].get('from', ''))}' → '{html.escape(t['rename'].get('to', ''))}'"
                if t.get("milestone"):
                    event_desc += f" '{html.escape(t['milestone'].get('title', ''))}'"
                if t.get("state_reason"):
                    event_desc += f" ({html.escape(t['state_reason'])})"
                if src_issue and src_issue.get("number"):
                        num = src_issue["number"]
                        local_issue_json = issue_dir.parent / f"{num:05d}" / "issue.json"                        
                        if local_issue_json.exists():
                            event_desc += f" in <a href='../{num:05d}/index.html'>#{num}</a>"
                        else:
                            ext_url = html.escape(src_issue.get("html_url", "#"))
                            event_desc += f" in <a href='{ext_url}' rel='noopener noreferrer'>#{num} (external)</a>"
                    
            timeline_html.append(f"""
<div class="timeline-event">
<strong>{html.escape(actor)}</strong> {event_desc} <span style="float:right">{format_date(t.get("created_at"))}</span>
</div>""")
            
    html_content = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(issue['title'])}</title>
<style>{GITHUB_CSS}</style></head><body>
<a href="../../index.html">&larr; Back to Index</a>
<h1 class="issue-title"><span class="state {state_class}">{issue['state']}</span> {html.escape(issue['title'])}</h1>
<div class="meta-grid">{"".join(meta_html)}</div>
<hr>
{"".join(timeline_html)}
</body></html>"""
    (issue_dir / "index.html").write_text(html_content, encoding="utf-8")
    return True

def build_user_pages(out_dir: Path):
    users_dir = out_dir / "users"
    if not users_dir.exists(): return
    
    for user_file in users_dir.glob("*.json"):
        user = json.loads(user_file.read_text(encoding="utf-8"))
        login = html.escape(user.get("login", "ghost"))
        name = html.escape(user.get("name") or login)
        bio = html.escape(user.get("bio") or "No bio provided.")
        company = html.escape(user.get("company") or "")
        location = html.escape(user.get("location") or "")
        
        html_content = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{login}</title>
<style>{GITHUB_CSS}</style></head><body>
<a href="../index.html">&larr; Back to Index</a>
<div class="user-card">
<h2>{name}</h2>
<p><strong>@{login}</strong></p>
<p>{bio}</p>
<p>{company}</p>
<p>{location}</p>
</div>
</body></html>"""
        (users_dir / f"{user_file.stem}.html").write_text(html_content, encoding="utf-8")

# =================================== MAIN ====================================
def main():
    out_dir = Path(OUT_DIR)
    if not out_dir.exists():
        sys.exit(f"Error: Directory '{out_dir}' not found.")
        
    print("Loading attachment manifest...")
    manifest_path = out_dir / "attachments" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    
    print("Generating user profile pages...")
    build_user_pages(out_dir)
    
    print("Generating issue pages...")
    issues_dir = out_dir / "issues"
    if issues_dir.exists():
        for issue_dir in sorted(issues_dir.iterdir()):
            if issue_dir.is_dir() and (issue_dir / "issue.json").exists():
                build_issue_page(issue_dir, manifest)
                
    print("Generating index page...")
    issues_data = load_issues(out_dir)
            
    build_index(out_dir, issues_data)
    print(f"Done! Open {out_dir / 'index.html'} in your browser.")

if __name__ == "__main__":
    main()
