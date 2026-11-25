#!/usr/bin/env python3
"""Daily SNU eTL (Canvas LMS) assignment digest, sent by email.

A Debian/stdlib-only port of the read-only eTL client from hyukjunlim/claude-pet
(src/etl/client.js, model.js, queries.js). It only sends GET requests to eTL.

Usage:
  etl_digest.py --check        verify the eTL token and print your name
  etl_digest.py --dry-run      print the digest instead of mailing it
  etl_digest.py                build the digest and email it

Config: ~/.config/etl-digest/config.json (chmod 600), see config.example.json.
"""
import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

BASE = "https://myetl.snu.ac.kr"
KST = timezone(timedelta(hours=9))
WEEKDAYS = "월화수목금토일"  # datetime.weekday(): Monday = 0
MAX_PAGES = 20
TIMEOUT = 20
XSSI = re.compile(r"^\s*while\(1\);")
SUBMITTED_STATES = {"submitted", "graded", "pending_review"}
OFFLINE_TYPES = {"none", "on_paper", "not_graded"}
MAX_DOWNLOAD = 300 << 20  # bytes
DOWNLOAD_ROOT = os.path.realpath(os.path.expanduser(
    os.environ.get("ETL_DOWNLOAD_DIR", "~/Downloads/etl")))
CONFIG_PATH = os.path.expanduser(
    os.environ.get("ETL_DIGEST_CONFIG", "~/.config/etl-digest/config.json"))


class EtlError(Exception):
    pass


class EtlAuthError(EtlError):
    pass


# ---------------------------------------------------------------- client

class Client:
    def __init__(self, token):
        if not token or not re.fullmatch(r"[\x21-\x7e]+", token):
            raise EtlError("eTL token is missing or malformed")
        self._token = token
        self._origin = BASE

    def _scrub(self, s):
        return str(s).replace(self._token, "***")

    def _request(self, url):
        path = urllib.parse.urlsplit(url).path
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                body = res.read().decode("utf-8", "replace")
                link = res.headers.get("Link")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                text = e.read().decode("utf-8", "replace")
                if "not authorized" in text.lower() and "token" not in text.lower():
                    raise EtlError(f"no permission (401): {path}")
                raise EtlAuthError("eTL rejected the token (401): expired or invalid")
            raise EtlError(f"eTL request failed ({e.code}): {path}")
        except Exception as e:  # noqa: BLE001 - never echo lower-layer text
            raise EtlError(f"eTL connection failed: {path} ({type(e).__name__})")
        try:
            data = json.loads(XSSI.sub("", body))
        except ValueError:
            raise EtlError(f"unreadable eTL response: {path}")
        return data, link

    def get(self, path):
        return self._request(self._origin + path)[0]

    def get_all(self, path):
        out, url = [], self._origin + path
        for _ in range(MAX_PAGES):
            data, link = self._request(url)
            if not isinstance(data, list):
                raise EtlError(f"expected a list: {urllib.parse.urlsplit(url).path}")
            out.extend(data)
            nxt = _next_link(link)
            if not nxt:
                break
            nxt = urllib.parse.urljoin(url, nxt)
            if not nxt.startswith(self._origin + "/"):
                raise EtlError("next-page link points outside eTL")  # token must stay on eTL
            url = nxt
        return out

    def download(self, url, dest_dir, name, max_bytes=MAX_DOWNLOAD):
        """Stream an eTL file URL into dest_dir/name. Returns (path, bytes, sha256).

        The token is sent only to eTL itself: it is an *unredirected* header, so the
        redirect to eTL's file storage (a signed URL) does not carry it.
        """
        url = urllib.parse.urljoin(self._origin + "/", url)
        if not url.startswith(self._origin + "/"):
            raise EtlError("download URL is not on eTL")
        path = urllib.parse.urlsplit(url).path
        req = urllib.request.Request(url)
        req.add_unredirected_header("Authorization", f"Bearer {self._token}")
        final = os.path.join(dest_dir, name)
        tmp = final + ".part"
        h, n = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res, open(tmp, "wb") as out:
                while True:
                    chunk = res.read(1 << 16)
                    if not chunk:
                        break
                    n += len(chunk)
                    if n > max_bytes:
                        raise EtlError(f"file is larger than {max_bytes >> 20} MB: {path}")
                    h.update(chunk)
                    out.write(chunk)
        except EtlError:
            _unlink(tmp)
            raise
        except urllib.error.HTTPError as e:
            _unlink(tmp)
            if e.code == 401:
                raise EtlAuthError("eTL rejected the token (401): expired or invalid")
            raise EtlError(f"download failed ({e.code}): {path}")
        except Exception as e:  # noqa: BLE001 - never echo lower-layer text (may hold signed URLs)
            _unlink(tmp)
            raise EtlError(f"download failed: {path} ({type(e).__name__})")
        os.replace(tmp, final)
        return final, n, h.hexdigest()

    def self_user(self):
        return self.get("/api/v1/users/self")

    def courses(self):
        return self.get_all("/api/v1/courses?enrollment_state=active"
                            "&include[]=total_scores&include[]=term&per_page=100")

    def assignments(self, course_id):
        return self.get_all(f"/api/v1/courses/{int(course_id)}/assignments"
                            "?include[]=submission&order_by=due_at&per_page=100")

    def announcements(self, course_ids, start, end):
        if not course_ids:
            return []
        ctx = "&".join(f"context_codes[]=course_{int(i)}" for i in course_ids)
        return self.get_all(f"/api/v1/announcements?{ctx}"
                            f"&start_date={start}&end_date={end}&per_page=50")


def _unlink(p):
    try:
        os.unlink(p)
    except OSError:
        pass


def _next_link(header):
    if not header:
        return None
    for part in header.split(","):
        m = re.match(r'\s*<([^>]*)>(.*)', part)
        if m and re.search(r'rel\s*=\s*"?([^";]*\bnext\b[^";]*)"?', m.group(2), re.I):
            return m.group(1)
    return None


# ---------------------------------------------------------------- model

SEASONS = [(r"1학기", 1), (r"하계|여름", 2), (r"2학기", 3), (r"동계|겨울", 4)]


def term_rank(name):
    s = str(name or "")
    y = re.search(r"(\d{4})\s*(?:년|-)", s)
    season = next((n for pat, n in SEASONS if re.search(pat, s)), None)
    return int(y.group(1)) * 10 + season if y and season else None


def current_courses(courses):
    usable = [c for c in courses or [] if c and c.get("name") and not c.get("access_restricted_by_date")]
    ranks = [term_rank((c.get("term") or {}).get("name")) for c in usable]
    known = [r for r in ranks if r is not None]
    if not known:
        return usable
    latest = max(known)
    return [c for c, r in zip(usable, ranks) if r == latest]


def to_dt(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_submitted(a):
    sub = a.get("submission") or {}
    return sub.get("workflow_state") in SUBMITTED_STATES or bool(sub.get("submitted_at"))


def is_online(a):
    return any(t not in OFFLINE_TYPES for t in a.get("submission_types") or [])


def is_missing(a, now):
    if is_submitted(a):
        return False
    if (a.get("submission") or {}).get("missing"):
        return True
    due = to_dt(a.get("due_at"))
    return due is not None and due < now and is_online(a)


def fmt_kst(dt):
    if dt is None:
        return "마감일 없음"
    d = dt.astimezone(KST)
    return f"{d:%m-%d}({WEEKDAYS[d.weekday()]}) {d:%H:%M}"


def remaining(dt, now):
    if dt is None:
        return ""
    secs = int((dt - now).total_seconds())
    suffix = " 남음" if secs >= 0 else " 지남"
    a = abs(secs)
    d, h, m = a // 86400, (a % 86400) // 3600, (a % 3600) // 60
    body = f"{d}일 {h}시간" if d else f"{h}시간 {m}분" if h else f"{m}분"
    return body + suffix


def html_to_text(s, limit=600):
    if not s:
        return ""
    s = re.sub(r"<(script|style)\b.*?</\1\s*>", "", s, flags=re.S | re.I)
    s = re.sub(r"<\s*(br|/p|/div|/li|/h\d|/tr)\b[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li\b[^>]*>", "- ", s, flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s if len(s) <= limit else s[:limit] + "…"


# ---------------------------------------------------------------- digest

def build_digest(client, upcoming_days=7, announcement_days=1):
    now = datetime.now(timezone.utc)
    courses = current_courses(client.courses())
    errors, items = [], []

    def fetch(c):
        try:
            return c, client.assignments(c["id"]), None
        except EtlAuthError:
            raise
        except EtlError as e:
            return c, [], str(e)

    with ThreadPoolExecutor(max_workers=6) as ex:
        for c, assigns, err in ex.map(fetch, courses):
            if err:
                errors.append(f"{c['name']}: {err}")
            for a in assigns:
                if a.get("published", True):
                    items.append((c, a))

    horizon = now + timedelta(days=upcoming_days)
    upcoming, missing = [], []
    for c, a in items:
        due = to_dt(a.get("due_at"))
        row = {"course": c["name"], "name": a.get("name") or "(제목 없음)", "due": due,
               "url": a.get("html_url"), "online": is_online(a),
               "points": a.get("points_possible")}
        if is_missing(a, now):
            missing.append(row)
        elif not is_submitted(a) and due is not None and now <= due <= horizon:
            upcoming.append(row)
    key = lambda r: r["due"] or datetime.max.replace(tzinfo=timezone.utc)
    upcoming.sort(key=key)
    missing.sort(key=key)

    announcements = []
    try:
        start = (now - timedelta(days=announcement_days)).astimezone(KST).strftime("%Y-%m-%d")
        end = (now + timedelta(days=1)).astimezone(KST).strftime("%Y-%m-%d")
        names = {int(c["id"]): c["name"] for c in courses}
        cutoff = now - timedelta(days=announcement_days)
        for it in client.announcements([c["id"] for c in courses], start, end):
            posted = to_dt(it.get("posted_at"))
            if posted and posted < cutoff:
                continue
            cid = int(str(it.get("context_code", "course_0")).split("_")[-1] or 0)
            announcements.append({"course": names.get(cid, str(cid)), "title": it.get("title"),
                                  "posted": posted, "url": it.get("html_url") or it.get("url"),
                                  "text": html_to_text(it.get("message"))})
        announcements.sort(key=lambda r: r["posted"] or now, reverse=True)
    except EtlAuthError:
        raise
    except EtlError as e:
        errors.append(f"공지: {e}")

    return {"now": now, "courses": courses, "upcoming": upcoming, "missing": missing,
            "announcements": announcements, "errors": errors,
            "upcoming_days": upcoming_days, "announcement_days": announcement_days}


def render_text(d):
    now = d["now"]
    out = [f"eTL 과제 요약 — {fmt_kst(now)} 기준", ""]
    out.append(f"■ 마감 예정 (미제출, {d['upcoming_days']}일 이내): {len(d['upcoming'])}개")
    for r in d["upcoming"]:
        flag = "" if r["online"] else " [오프라인]"
        out.append(f"  - [{r['course']}] {r['name']}{flag}")
        out.append(f"      마감 {fmt_kst(r['due'])} · {remaining(r['due'], now)}")
        if r["url"]:
            out.append(f"      {r['url']}")
    if not d["upcoming"]:
        out.append("  없음 🎉")
    out.append("")
    out.append(f"■ 미제출/누락: {len(d['missing'])}개")
    for r in d["missing"]:
        out.append(f"  - [{r['course']}] {r['name']} (마감 {fmt_kst(r['due'])})")
        if r["url"]:
            out.append(f"      {r['url']}")
    if not d["missing"]:
        out.append("  없음")
    out.append("")
    out.append(f"■ 새 공지 (최근 {d['announcement_days']}일): {len(d['announcements'])}개")
    for a in d["announcements"]:
        out.append(f"  - [{a['course']}] {a['title']} ({fmt_kst(a['posted'])})")
        if a["url"]:
            out.append(f"      {a['url']}")
        for line in a["text"].splitlines()[:8]:
            out.append(f"      | {line}")
    if not d["announcements"]:
        out.append("  없음")
    if d["errors"]:
        out += ["", "■ 오류"] + [f"  - {e}" for e in d["errors"]]
    return "\n".join(out) + "\n"


def render_html(d):
    now, e = d["now"], html.escape

    def link(text, url):
        return f'<a href="{e(url)}">{e(text)}</a>' if url else e(text)

    def urgency(due):
        hrs = (due - now).total_seconds() / 3600 if due else 999
        return "#c0392b" if hrs < 24 else "#d68910" if hrs < 72 else "#444"

    p = [f"<div style='font-family:sans-serif;font-size:14px;color:#222'>",
         f"<h2 style='margin:0 0 4px'>eTL 과제 요약</h2>",
         f"<div style='color:#777;margin-bottom:16px'>{e(fmt_kst(now))} 기준</div>",
         f"<h3>마감 예정 · 미제출 ({len(d['upcoming'])})</h3>"]
    if d["upcoming"]:
        p.append("<table cellpadding='6' style='border-collapse:collapse'>")
        for r in d["upcoming"]:
            off = " <small>[오프라인]</small>" if not r["online"] else ""
            p.append(f"<tr style='border-bottom:1px solid #eee'><td style='color:#666'>{e(r['course'])}</td>"
                     f"<td>{link(r['name'], r['url'])}{off}</td>"
                     f"<td style='white-space:nowrap'>{e(fmt_kst(r['due']))}</td>"
                     f"<td style='white-space:nowrap;color:{urgency(r['due'])};font-weight:bold'>"
                     f"{e(remaining(r['due'], now))}</td></tr>")
        p.append("</table>")
    else:
        p.append("<p>없음 🎉</p>")
    p.append(f"<h3>미제출 · 누락 ({len(d['missing'])})</h3>")
    if d["missing"]:
        p.append("<ul>" + "".join(
            f"<li><span style='color:#666'>{e(r['course'])}</span> — {link(r['name'], r['url'])} "
            f"<span style='color:#c0392b'>(마감 {e(fmt_kst(r['due']))})</span></li>" for r in d["missing"]) + "</ul>")
    else:
        p.append("<p>없음</p>")
    p.append(f"<h3>새 공지 · 최근 {d['announcement_days']}일 ({len(d['announcements'])})</h3>")
    for a in d["announcements"]:
        p.append(f"<div style='margin-bottom:12px'><b>{e(a['course'])}</b> — {link(a['title'] or '', a['url'])} "
                 f"<span style='color:#777'>({e(fmt_kst(a['posted']))})</span>"
                 f"<div style='white-space:pre-wrap;color:#444;border-left:3px solid #ddd;padding-left:8px;margin-top:4px'>"
                 f"{e(a['text'])}</div></div>")
    if not d["announcements"]:
        p.append("<p>없음</p>")
    if d["errors"]:
        p.append("<h3>오류</h3><ul>" + "".join(f"<li>{e(x)}</li>" for x in d["errors"]) + "</ul>")
    p.append("</div>")
    return "".join(p)


def to_json(d):
    now = d["now"]

    def row(r):
        out = dict(r)
        if "due" in out:
            out["due"] = fmt_kst(r["due"]) if r["due"] else None
            out["remaining"] = remaining(r["due"], now)
        if "posted" in out:
            out["posted"] = fmt_kst(r["posted"]) if r["posted"] else None
        return out
    return json.dumps({
        "now_kst": fmt_kst(now),
        "courses": [c["name"] for c in d["courses"]],
        "upcoming_unsubmitted": [row(r) for r in d["upcoming"]],
        "missing": [row(r) for r in d["missing"]],
        "announcements": [row(a) for a in d["announcements"]],
        "errors": d["errors"],
        "upcoming_days": d["upcoming_days"],
        "announcement_days": d["announcement_days"],
    }, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- files & submissions

def safe_name(name, fallback):
    name = os.path.basename(str(name or "")).strip()
    name = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", name).lstrip(".")
    return name[:180] or fallback


def out_dir(sub=None, override=None):
    """A directory inside DOWNLOAD_ROOT (never outside it)."""
    d = os.path.realpath(os.path.join(DOWNLOAD_ROOT, override or sub or ""))
    if d != DOWNLOAD_ROOT and not d.startswith(DOWNLOAD_ROOT + os.sep):
        raise EtlError(f"--out must stay inside {DOWNLOAD_ROOT}")
    os.makedirs(d, exist_ok=True)
    return d


def file_id(ref):
    """'12345', '/courses/1/files/12345', or a full eTL file URL -> 12345."""
    s = str(ref).strip()
    if s.isdigit():
        return int(s)
    m = re.search(r"/files/(\d+)", s)
    if not m:
        raise EtlError("give a file id or an eTL file URL (…/files/<id>)")
    return int(m.group(1))


def download_file(client, ref, dest):
    meta = client.get(f"/api/v1/files/{file_id(ref)}")
    return fetch_attachment(client, meta, dest)


def fetch_attachment(client, meta, dest):
    """Download one Canvas file/attachment object; check size against eTL's record."""
    fid = meta.get("id")
    if meta.get("locked_for_user"):
        raise EtlError(f"file {fid} is locked: {meta.get('lock_explanation') or 'not available yet'}")
    if not meta.get("url"):
        raise EtlError(f"file {fid} has no download URL")
    name = safe_name(meta.get("display_name") or meta.get("filename"), f"file-{fid}")
    path, n, digest = client.download(meta["url"], dest, name)
    expected = meta.get("size")
    return {"id": fid, "name": meta.get("display_name"), "saved": path, "bytes": n,
            "expected_bytes": expected, "size_ok": expected is None or int(expected) == n,
            "content_type": meta.get("content-type") or meta.get("mime_class"),
            "uploaded": fmt_kst(to_dt(meta.get("created_at"))) if meta.get("created_at") else None,
            "sha256": digest}


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def assignment_ref(ref):
    """'<course>/<assignment>' or an eTL assignment URL -> (course_id, assignment_id)."""
    m = (re.search(r"/courses/(\d+)/assignments/(\d+)", ref)
         or re.fullmatch(r"\s*(\d+)\s*[/ ,:]\s*(\d+)\s*", ref))
    if not m:
        raise EtlError("give COURSE_ID/ASSIGNMENT_ID or an eTL assignment URL")
    return int(m.group(1)), int(m.group(2))


def verify_submission(client, ref, compare=(), fetch=True):
    """Check that an assignment was handed in properly; optionally download the
    submitted files and compare them with local copies by SHA-256."""
    cid, aid = assignment_ref(ref)
    a = client.get(f"/api/v1/courses/{cid}/assignments/{aid}")
    s = client.get(f"/api/v1/courses/{cid}/assignments/{aid}/submissions/self"
                   "?include[]=submission_comments&include[]=submission_history")
    now = datetime.now(timezone.utc)
    due = to_dt(s.get("cached_due_date") or a.get("due_at"))
    lock = to_dt(a.get("lock_at"))
    sub_at = to_dt(s.get("submitted_at"))
    allowed_types = a.get("submission_types") or []
    allowed_ext = [e.lower().lstrip(".") for e in a.get("allowed_extensions") or []]
    problems, warnings = [], []

    submitted = s.get("workflow_state") in SUBMITTED_STATES or sub_at is not None
    if not submitted:
        if is_online(a):
            problems.append("제출 기록 없음" + (" (마감 지남)" if due and due < now else ""))
        else:
            warnings.append(f"온라인 제출 과제가 아님 ({', '.join(allowed_types) or '?'}) — eTL로 확인 불가")
    if s.get("late"):
        problems.append("지각 제출로 표시됨")
    elif submitted and due and sub_at and sub_at > due:
        problems.append("마감 후 제출됨")
    if s.get("missing"):
        problems.append("eTL에 '누락'으로 표시됨")
    if s.get("excused"):
        warnings.append("면제(excused) 처리됨")
    st = s.get("submission_type")
    if submitted and st and allowed_types and st not in allowed_types:
        problems.append(f"제출 방식 {st} 은(는) 허용되지 않음 ({', '.join(allowed_types)})")

    files = []
    dest = out_dir(f"submissions/{cid}-{aid}") if fetch and s.get("attachments") else None
    for att in s.get("attachments") or []:
        name = att.get("display_name") or att.get("filename") or ""
        row = {"id": att.get("id"), "name": name, "expected_bytes": att.get("size"),
               "content_type": att.get("content-type")}
        if att.get("size") == 0:
            problems.append(f"빈 파일(0바이트): {name}")
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if allowed_ext and ext not in allowed_ext:
            problems.append(f"허용되지 않은 확장자 .{ext}: {name} (허용: {', '.join(allowed_ext)})")
        if dest:
            try:
                row.update(fetch_attachment(client, att, dest))
                if not row["size_ok"]:
                    problems.append(f"내려받은 크기가 eTL 기록과 다름: {name}")
            except EtlAuthError:
                raise
            except EtlError as e:
                warnings.append(f"다운로드 실패 {name}: {e}")
        files.append(row)
    if submitted and st == "online_upload" and not files:
        problems.append("파일 업로드 과제인데 첨부 파일이 없음")

    comparisons = []
    for p in compare:
        p = os.path.expanduser(p)
        if not os.path.isfile(p):
            problems.append(f"비교할 로컬 파일 없음: {p}")
            continue
        digest = sha256_file(p)
        match = next((f["name"] for f in files if f.get("sha256") == digest), None)
        comparisons.append({"local": p, "bytes": os.path.getsize(p), "sha256": digest, "matches": match})
        if not match:
            problems.append(f"로컬 파일과 일치하는 제출 파일 없음: {os.path.basename(p)}"
                            + ("" if any("sha256" in f for f in files) else " (제출 파일을 내려받지 못함)"))

    history = s.get("submission_history") or []
    return {
        "course_id": cid, "assignment": a.get("name"), "url": a.get("html_url"),
        "due": fmt_kst(due), "lock_at": fmt_kst(lock) if lock else None,
        "allowed_types": allowed_types, "allowed_extensions": allowed_ext or None,
        "submitted": submitted, "state": s.get("workflow_state"),
        "submitted_at": fmt_kst(sub_at) if sub_at else None,
        "attempt": s.get("attempt"), "attempts_total": len(history) or None,
        "allowed_attempts": a.get("allowed_attempts") if (a.get("allowed_attempts") or -1) > 0 else None,
        "submission_type": st, "late": bool(s.get("late")), "missing": bool(s.get("missing")),
        "text_entry": html_to_text(s.get("body"), 1500) if st == "online_text_entry" else None,
        "url_entry": s.get("url") if st == "online_url" else None,
        "files": files, "compare": comparisons,
        "grade": s.get("grade"), "score": s.get("score"), "points_possible": a.get("points_possible"),
        "comments": [{"author": c.get("author_name"), "at": fmt_kst(to_dt(c.get("created_at"))),
                      "text": html_to_text(c.get("comment"), 400)}
                     for c in (s.get("submission_comments") or [])[-5:]],
        "verdict": "ok" if submitted and not problems else "problem",
        "problems": problems, "warnings": warnings,
    }


# ---------------------------------------------------------------- mail

def send_mail(cfg, subject, text, html_body):
    m = cfg["smtp"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = m.get("from") or m["user"]
    msg["To"] = cfg["mail_to"]
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    host, port = m.get("host", "smtp.gmail.com"), int(m.get("port", 465))
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(m["user"], m["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(m["user"], m["password"])
            s.send_message(msg)


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        sys.exit(f"config not found: {CONFIG_PATH} (copy config.example.json there)")
    if os.stat(CONFIG_PATH).st_mode & 0o077:
        print(f"warning: {CONFIG_PATH} is readable by others; run chmod 600 on it", file=sys.stderr)
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="verify the eTL token")
    ap.add_argument("--dry-run", action="store_true", help="print the digest, don't send")
    ap.add_argument("--test-mail", action="store_true", help="send a short test email only")
    ap.add_argument("--json", action="store_true", help="print the digest as JSON (for Claude)")
    ap.add_argument("--days", type=int, help="upcoming-deadline horizon in days")
    ap.add_argument("--ann-days", type=int, help="announcement lookback in days")
    ap.add_argument("--courses", action="store_true", help="list this term's courses as JSON")
    ap.add_argument("--api", metavar="PATH", help="GET any /api/v1/... path and print the JSON")
    ap.add_argument("--download", metavar="FILE", nargs="+",
                    help="download eTL files (id or …/files/<id> URL) into the download dir")
    ap.add_argument("--out", metavar="SUBDIR", help="subfolder of the download dir for --download")
    ap.add_argument("--verify", metavar="COURSE/ASSIGNMENT",
                    help="check an assignment was handed in properly (ids or the assignment URL)")
    ap.add_argument("--compare", metavar="LOCAL_FILE", nargs="+", default=[],
                    help="with --verify: local file(s) that should match what was submitted")
    ap.add_argument("--no-fetch", action="store_true", help="with --verify: don't download submitted files")
    args = ap.parse_args()
    cfg = load_config()

    if args.test_mail:
        send_mail(cfg, "[eTL] 테스트 메일", "etl-digest 메일 설정이 정상입니다.", None)
        print("test mail sent to", cfg["mail_to"])
        return

    try:
        client = Client(cfg.get("etl_token", ""))
    except EtlError:
        sys.exit(f"eTL token not set: put it in etl_token of {CONFIG_PATH}")
    try:
        if args.check:
            me = client.self_user()
            print("eTL OK:", me.get("name"))
            return
        if args.courses:
            cs = current_courses(client.courses())
            print(json.dumps([{"id": c["id"], "name": c["name"], "term": (c.get("term") or {}).get("name")}
                              for c in cs], ensure_ascii=False, indent=1))
            return
        if args.download:
            dest = out_dir("files", args.out)
            results = []
            for ref in args.download:
                try:
                    results.append(download_file(client, ref, dest))
                except EtlAuthError:
                    raise
                except EtlError as e:
                    results.append({"ref": ref, "error": str(e)})
            print(json.dumps(results, ensure_ascii=False, indent=1))
            return
        if args.verify:
            print(json.dumps(verify_submission(client, args.verify, args.compare,
                                               bool(args.compare) or not args.no_fetch),
                             ensure_ascii=False, indent=1))
            return
        if args.api:
            if not re.fullmatch(r"/api/v1/[\w\-./?=&\[\]%:,]*", args.api):
                sys.exit("path must start with /api/v1/")
            data = client.get_all(args.api) if "per_page" in args.api else client.get(args.api)
            print(json.dumps(data, ensure_ascii=False, indent=1)[:200000])
            return
        d = build_digest(client, args.days or int(cfg.get("upcoming_days", 7)),
                         args.ann_days or int(cfg.get("announcement_days", 1)))
    except EtlAuthError as e:
        if not args.dry_run and cfg.get("mail_to"):
            send_mail(cfg, "[eTL] 토큰 만료 — 다시 발급해 주세요",
                      f"{e}\n\nhttps://myetl.snu.ac.kr/profile/settings 에서 새 액세스 토큰을 발급해 "
                      f"{CONFIG_PATH} 의 etl_token 을 바꿔 주세요.\n", None)
        sys.exit(str(e))
    except EtlError as e:
        sys.exit(f"error: {e}")

    if args.json:
        print(to_json(d))
        return
    text = render_text(d)
    if args.dry_run:
        print(text)
        return
    if not d["upcoming"] and not d["missing"] and not d["announcements"] and cfg.get("skip_if_empty"):
        print("nothing to report; skipped")
        return
    urgent = sum(1 for r in d["upcoming"] if (r["due"] - d["now"]).total_seconds() < 86400)
    subject = (f"[eTL] {d['now'].astimezone(KST):%m/%d} 마감예정 {len(d['upcoming'])}"
               + (f" (24시간 내 {urgent})" if urgent else "")
               + (f" · 누락 {len(d['missing'])}" if d["missing"] else "")
               + (f" · 공지 {len(d['announcements'])}" if d["announcements"] else ""))
    send_mail(cfg, subject, text, render_html(d))
    print("sent:", subject)


if __name__ == "__main__":
    main()
