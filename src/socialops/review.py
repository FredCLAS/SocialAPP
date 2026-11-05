"""Weekly approval inbox: a local web page for reviewing one brand's week.

- Binds to 127.0.0.1 only. Every request needs a random per-run token.
- Host header must be 127.0.0.1 or localhost (blocks DNS-rebinding).
- Every decision goes through post_queue.transition or gates.record_decision.
  Approval still needs a passing QA run for the current content.
- Images are served only from the brand's asset folder, after path checks.
- Approving does not publish. The publisher runs later, on schedule.
"""

from __future__ import annotations

import hmac
import html
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, urlparse

from .config import Config
from .content_engine import week_bounds_utc
from .db import connect, migrate_up
from .gates import record_decision
from .post_queue import (
    InvalidTransition,
    NotApprovable,
    Post,
    UnauthorizedApprover,
    completeness_problems,
    list_posts,
    qa_problems,
    transition,
)
from .qa import latest_result

DEFAULT_ROOT = Path.home() / "Library" / "Application Support" / "SocialOps"
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost"})
MAX_BODY = 8192


@dataclass
class ReviewItem:
    post: Post
    qa_passed: bool | None
    qa_failures: list[str]
    blockers: list[str]
    rejection_note: str | None


def build_items(conn: sqlite3.Connection, cfg: Config, brand_key: str, week_start: date) -> list[ReviewItem]:
    start, end = week_bounds_utc(week_start, cfg.timezone)
    posts = list_posts(conn, brand_key=brand_key, scheduled_from=start, scheduled_to=end)
    items = []
    for post in posts:
        qa = latest_result(conn, post.id)
        failures = [f["reason"] for f in json.loads(qa["failures"])] if qa else []
        blockers = (completeness_problems(post) + qa_problems(conn, post)) if post.status == "draft" else []
        last = conn.execute(
            "SELECT decision, note FROM approvals WHERE kind='post' AND ref_id=? ORDER BY id DESC LIMIT 1",
            (post.id,),
        ).fetchone()
        note = last["note"] if last and last["decision"] == "rejected" else None
        items.append(ReviewItem(post, bool(qa["passed"]) if qa else None, failures, blockers, note))
    return items


def safe_asset_path(root: Path, visual_path: str | None, brand_subdir: str) -> Path | None:
    """Resolve a post's visual inside its brand folder, or None if unsafe or missing."""
    if not visual_path:
        return None
    rel = PurePosixPath(visual_path.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        return None
    allowed = (root / "assets" / brand_subdir).resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(allowed)
    except ValueError:
        return None
    return target if target.is_file() else None


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def render_page(items: list[ReviewItem], cfg: Config, brand_key: str, week_start: date,
                token: str, message: str = "") -> str:
    brand = cfg.brands[brand_key]
    tok = quote(token)
    rows = []
    for it in items:
        p = it.post
        when = _local_time(p.scheduled_at, cfg)
        if it.qa_passed is None:
            qa_html = "<span class='tag'>QA not run</span>"
        elif it.qa_passed:
            qa_html = "<span class='tag ok'>QA passed</span>"
        else:
            qa_html = "<span class='tag bad'>QA failed</span>"
        problems = list(dict.fromkeys(it.blockers + it.qa_failures))
        problems_html = "".join(f"<li>{_esc(x)}</li>" for x in problems)
        thumb = (f"<img src='/asset/{p.id}?token={tok}' alt=''>"
                 if p.visual_path else "<div class='noimg'>no visual</div>")
        note_html = (f"<p class='note'>Last rejection: {_esc(it.rejection_note)}</p>"
                     if it.rejection_note else "")
        actions = ""
        if p.status == "draft":
            actions = f"""
            <form method='post' action='/approve' class='inline'>
              <input type='hidden' name='token' value='{_esc(token)}'>
              <input type='hidden' name='post_id' value='{p.id}'>
              <button>Approve</button>
            </form>
            <form method='post' action='/reject' class='inline'>
              <input type='hidden' name='token' value='{_esc(token)}'>
              <input type='hidden' name='post_id' value='{p.id}'>
              <input name='reason' placeholder='Reason to reject' required>
              <button class='secondary'>Reject</button>
            </form>"""
        else:
            actions = f"<p class='muted'>Status: {_esc(p.status)}</p>"
        rows.append(f"""
        <article class='card'>
          <div class='thumb'>{thumb}</div>
          <div class='body'>
            <h3>{_esc(when)} · {_esc(p.platform)} · {_esc(p.content_type)} · {_esc(p.format)}</h3>
            <p><strong>{_esc(p.subject)}</strong> {qa_html}</p>
            <pre class='caption'>{_esc(p.caption)}</pre>
            <p class='muted'>{_esc(' '.join(p.hashtags))}</p>
            <p class='muted'>CTA: {_esc(p.cta_url or '—')} · Visual: {_esc(p.visual_source)}</p>
            {f"<ul class='problems'>{problems_html}</ul>" if problems else ""}
            {note_html}
            {actions}
          </div>
        </article>""")
    drafts_ready = sum(1 for it in items if it.post.status == "draft" and not it.blockers)
    msg_html = f"<div class='msg'>{_esc(message)}</div>" if message else ""
    return f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Review · {_esc(brand.display_name)} · week of {week_start.isoformat()}</title>
<style>
 body {{ font: 15px/1.45 -apple-system, system-ui, sans-serif; margin: 0; padding: 16px; background: #fafafa; color: #111; }}
 h1 {{ font-size: 20px; margin: 0 0 8px; }}
 .card {{ display: flex; gap: 16px; background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 12px; margin: 12px 0; }}
 .thumb img {{ width: 160px; height: 160px; object-fit: cover; border-radius: 6px; }}
 .noimg {{ width: 160px; height: 160px; display: grid; place-items: center; background: #eee; color: #666; border-radius: 6px; }}
 .body {{ flex: 1; min-width: 0; }}
 .caption {{ white-space: pre-wrap; background: #f5f5f5; padding: 8px; border-radius: 6px; }}
 .tag {{ font-size: 12px; padding: 2px 6px; border-radius: 4px; background: #eee; margin-left: 6px; }}
 .tag.ok {{ background: #d7f5dd; }} .tag.bad {{ background: #fbd7d7; }}
 .problems {{ color: #9b1c1c; }}
 .muted {{ color: #666; font-size: 13px; }}
 .note {{ color: #7a4b00; }}
 .msg {{ background: #eef4ff; border: 1px solid #b7cdf5; padding: 8px; border-radius: 6px; margin-bottom: 8px; }}
 button {{ padding: 6px 12px; border-radius: 6px; border: 0; background: #1f5eff; color: #fff; cursor: pointer; }}
 button.secondary {{ background: #666; }}
 input[name=reason] {{ padding: 5px; min-width: 200px; }}
 form.inline {{ display: inline-block; margin: 6px 8px 0 0; }}
 @media (max-width: 600px) {{ .card {{ flex-direction: column; }} .thumb img, .noimg {{ width: 100%; height: auto; }} }}
</style></head>
<body>
<h1>{_esc(brand.display_name)} · week of {week_start.isoformat()}</h1>
<p class='muted'>Approving records your decision. Publishing still runs on schedule, and only for approved posts.</p>
{msg_html}
<form method='post' action='/approve-passing' class='inline'>
  <input type='hidden' name='token' value='{_esc(token)}'>
  <button>Approve all {drafts_ready} that passed QA</button>
</form>
{''.join(rows) if rows else "<p>No posts for this week.</p>"}
</body></html>"""


def _local_time(scheduled_at: str, cfg: Config) -> str:
    from datetime import datetime
    dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00")).astimezone(cfg.timezone)
    return dt.strftime("%a %b %d %H:%M")


def handle_action(conn: sqlite3.Connection, cfg: Config, actor: str, action: str, form: dict) -> str:
    """Apply one decision. Returns a message for the page. Never raises for user errors."""
    if action == "approve-passing":
        brand_key = form.get("brand", [""])[0]
        week = date.fromisoformat(form.get("week", [""])[0])
        items = build_items(conn, cfg, brand_key, week)
        approved, held = 0, 0
        for it in items:
            if it.post.status != "draft":
                continue
            if it.blockers:
                held += 1
                continue
            try:
                transition(conn, it.post.id, "approved", actor=actor)
                approved += 1
            except (NotApprovable, InvalidTransition, UnauthorizedApprover) as exc:
                held += 1
        return f"Approved {approved}. Held back {held} (QA or completeness not passing)."

    if action not in ("approve", "reject"):
        return "Unknown action."
    try:
        post_id = int(form.get("post_id", [""])[0])
    except ValueError:
        return "Bad post id."

    if action == "approve":
        try:
            transition(conn, post_id, "approved", actor=actor)
            return f"Post {post_id} approved."
        except (NotApprovable, InvalidTransition, UnauthorizedApprover, KeyError) as exc:
            return f"Post {post_id} not approved: {exc}"

    if action == "reject":
        reason = form.get("reason", [""])[0].strip()
        if not reason:
            return "A reason is required to reject."
        try:
            record_decision(conn, "post", post_id, actor, "rejected", note=reason[:500])
            return f"Post {post_id} rejected. It stays a draft."
        except Exception as exc:  # GateError or DB issue: show, don't crash the page
            return f"Post {post_id} not rejected: {exc}"

    return "Unknown action."


def make_handler(db_path: Path, cfg: Config, brand_key: str, week: date, actor: str,
                 root: Path, token: str):
    subdir = cfg.brands[brand_key].assets_subdir

    class Handler(BaseHTTPRequestHandler):
        server_version = "socialops-review"

        def log_message(self, *args):  # keep request lines out of logs
            return

        def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8",
                  extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("content-type", ctype)
            self.send_header("content-length", str(len(body)))
            self.send_header("cache-control", "no-store")
            self.send_header("x-content-type-options", "nosniff")
            self.send_header("referrer-policy", "no-referrer")
            self.send_header("content-security-policy", "default-src 'self'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ALLOWED_HOSTS

        def _token_ok(self, supplied: str | None) -> bool:
            return bool(supplied) and hmac.compare_digest(supplied, token)

        def _db(self) -> sqlite3.Connection:
            conn = connect(db_path)
            migrate_up(conn)
            return conn

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._send(400, b"bad host")
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            if not self._token_ok(qs.get("token", [None])[0]):
                return self._send(403, b"forbidden")
            conn = self._db()
            try:
                if url.path == "/":
                    items = build_items(conn, cfg, brand_key, week)
                    msg = qs.get("msg", [""])[0]
                    page = render_page(items, cfg, brand_key, week, token, msg)
                    return self._send(200, page.encode("utf-8"))
                if url.path.startswith("/asset/"):
                    try:
                        post_id = int(url.path.rsplit("/", 1)[-1])
                    except ValueError:
                        return self._send(404, b"not found")
                    post = conn.execute("SELECT visual_path FROM posts WHERE id = ?", (post_id,)).fetchone()
                    target = safe_asset_path(root, post["visual_path"] if post else None, subdir)
                    if target is None:
                        return self._send(404, b"not found")
                    ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                             ".gif": "image/gif", ".webp": "image/webp"}.get(target.suffix.lower(),
                                                                             "application/octet-stream")
                    return self._send(200, target.read_bytes(), ctype)
                return self._send(404, b"not found")
            finally:
                conn.close()

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._send(400, b"bad host")
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY:
                return self._send(413, b"too large")
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
            if not self._token_ok(form.get("token", [None])[0]):
                return self._send(403, b"forbidden")
            action = urlparse(self.path).path.strip("/")
            form.setdefault("brand", [brand_key])
            form.setdefault("week", [week.isoformat()])
            conn = self._db()
            try:
                msg = handle_action(conn, cfg, actor, action, form)
            finally:
                conn.close()
            location = f"/?token={quote(token)}&msg={quote(msg)}"
            return self._send(303, b"", extra={"location": location})

    return Handler


def make_server(db_path: Path, cfg: Config, brand_key: str, week: date, actor: str,
                root: Path = DEFAULT_ROOT, token: str | None = None, port: int = 8777) -> ThreadingHTTPServer:
    token = token or secrets.token_urlsafe(24)
    handler = make_handler(db_path, cfg, brand_key, week, actor, root, token)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.review_token = token  # type: ignore[attr-defined]
    return server


def serve(db_path: Path, cfg: Config, brand_key: str, week: date, actor: str,
          root: Path = DEFAULT_ROOT, port: int = 8777) -> str:
    server = make_server(db_path, cfg, brand_key, week, actor, root, port=port)
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={server.review_token}"  # type: ignore[attr-defined]
    print(f"Review page: {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return url
