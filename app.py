"""
My Uni Vault - multi-user student file vault (Flask + Supabase Storage).

Required environment variables:
    SECRET_KEY           - Flask session signing key
    GOOGLE_CLIENT_ID     - Google OAuth client ID (Google Cloud console)
    GOOGLE_CLIENT_SECRET - Google OAuth client secret
    SUPABASE_URL         - Supabase project URL
    SUPABASE_KEY         - Supabase API key (use the secret key, server-side only)
    SUPABASE_BUCKET      - Supabase Storage bucket name

Optional:
    USER_QUOTA_MB - storage allowed per user, in MB (default 100)
    OWNER_EMAIL   - Gmail address that inherits the old single-user files and
                    timetable the first time it signs in

Each user's data lives in the bucket under users/<google-id>/:
    their files and folders, plus .vault/settings.json and .vault/timetable.json
"""

import io
import os
import re
import json
import uuid
import functools
from datetime import date, datetime, timedelta, timezone

from authlib.integrations.flask_client import OAuth
from flask import (Flask, session, request, redirect, url_for, render_template,
                   flash, send_file, abort, g)
from supabase import create_client
from storage3.exceptions import StorageApiError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MAX_UPLOAD_MB = 50  # Supabase's free plan rejects single files over 50 MB anyway
USER_QUOTA_BYTES = int(float(os.environ.get("USER_QUOTA_MB", "100")) * 1024 * 1024)
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "").strip().lower()

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
)
# Behind a host's HTTPS proxy (Render etc.), build https:// callback URLs.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.environ["GOOGLE_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
BUCKET = os.environ["SUPABASE_BUCKET"]

# Supabase has no real folders; a folder exists while it holds at least one
# object, so empty folders get this placeholder (same name the dashboard uses).
PLACEHOLDER = ".emptyFolderPlaceholder"

USERS_DIR = "users"
# Per-user app data lives in a hidden folder inside each user's root.
# secure_filename() strips leading dots, so users can't create or upload over it.
DATA_DIR = ".vault"
SETTINGS_PATH = f"{DATA_DIR}/settings.json"
TIMETABLE_PATH = f"{DATA_DIR}/timetable.json"

THEMES = ("system", "light", "dark")
UTC_OFFSETS = [-12, -11, -10, -9, -8, -7, -6, -5, -4, -3, -2, -1, 0, 1, 2, 3,
               3.5, 4, 4.5, 5, 5.5, 5.75, 6, 6.5, 7, 8, 9, 9.5, 10, 11, 12, 13, 14]

FILE_KINDS = {
    "pdf": "pdf",
    "doc": "doc", "docx": "doc", "txt": "doc", "md": "doc", "rtf": "doc", "odt": "doc",
    "xls": "sheet", "xlsx": "sheet", "csv": "sheet", "ods": "sheet",
    "ppt": "slides", "pptx": "slides", "odp": "slides",
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "webp": "image", "svg": "image",
    "zip": "archive", "rar": "archive", "7z": "archive", "tar": "archive", "gz": "archive",
    "py": "code", "js": "code", "html": "code", "css": "code", "java": "code", "c": "code",
    "cpp": "code", "ipynb": "code", "json": "code", "sql": "code",
    "mp3": "media", "wav": "media", "mp4": "media", "mov": "media", "mkv": "media",
}

# How each extension is shown on the preview page; anything else gets a
# "no preview" panel with a download button.
PREVIEW_MODES = {
    **dict.fromkeys(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif"], "image"),
    "pdf": "pdf",
    **dict.fromkeys(["mp4", "webm", "mov", "m4v"], "video"),
    **dict.fromkeys(["mp3", "wav", "ogg", "m4a", "aac", "flac"], "audio"),
    **dict.fromkeys(["txt", "md", "csv", "tsv", "log", "json", "xml", "yml", "yaml", "ini",
                     "py", "js", "ts", "html", "css", "java", "c", "h", "cpp", "cs", "sql",
                     "sh", "bat", "ps1", "r", "m", "ipynb", "tex"], "text"),
}
TEXT_PREVIEW_LIMIT = 2 * 1024 * 1024
SIGNED_URL_SECONDS = 3600

ENTRY_TYPES = {
    "class": "Class",
    "assignment": "Assignment",
    "exam": "Exam / Quiz",
    "study": "Study",
    "event": "Event",
    "other": "Other",
}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# ---------- storage helpers ----------

def bucket():
    return supabase.storage.from_(BUCKET)


def current_user():
    return session.get("user")


def user_root(uid=None):
    return f"{USERS_DIR}/{uid or session['user']['id']}"


def full(path):
    """Turn a path relative to the signed-in user's vault into a bucket key."""
    root = user_root()
    return f"{root}/{path}" if path else root


def is_not_found(e):
    return isinstance(e, StorageApiError) and str(getattr(e, "status", "")) in ("400", "404")


def load_json(key, default):
    """Read a JSON object from the bucket; `default` only if it doesn't exist yet.

    Any other error is raised, so a failed read is never mistaken for "empty"
    and then saved over the real data.
    """
    try:
        raw = bucket().download(key)
    except StorageApiError as e:
        if is_not_found(e):
            return default
        raise
    return json.loads(raw)


def save_json(key, obj):
    bucket().upload(key, json.dumps(obj, indent=2).encode(), {
        "content-type": "application/json",
        "upsert": "true",
    })


def walk_raw(prefix):
    """Every object under a bucket prefix, recursively, keyed by full bucket path."""
    out = []
    for item in bucket().list(prefix, {"limit": 1000}):
        key = f"{prefix}/{item['name']}" if prefix else item["name"]
        if item.get("id") is None:
            out.extend(walk_raw(key))
        else:
            out.append({
                "key": key,
                "size": (item.get("metadata") or {}).get("size", 0),
                "created_at": item.get("created_at"),
                "placeholder": item["name"] == PLACEHOLDER,
            })
    return out


def walk(folder):
    """Every object under one of the user's folders, with vault-relative paths.

    Skips the hidden .vault data folder; includes folder placeholders.
    """
    cut = len(user_root()) + 1
    out = []
    for o in walk_raw(full(folder)):
        rel = o["key"][cut:]
        if rel.split("/", 1)[0] == DATA_DIR:
            continue
        out.append(dict(o, path=rel))
    return out


def usage_bytes():
    return sum(o["size"] for o in walk_raw(user_root()))


def folder_paths(objects):
    """Every folder path implied by a list of objects from walk()."""
    paths = set()
    for o in objects:
        parts = o["path"].split("/")[:-1]
        paths.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return paths


def signed_url(path):
    res = bucket().create_signed_url(full(path), SIGNED_URL_SECONDS)
    return res.get("signedUrl") or res.get("signedURL")


def list_folder(folder):
    """Return (folders, files) directly inside one of the user's folders."""
    offset = load_settings()["utc_offset"]
    items = bucket().list(full(folder), {"limit": 1000, "sortBy": {"column": "name", "order": "asc"}})
    folders, files = [], []
    for item in items:
        name = item["name"]
        if name.startswith("."):  # placeholder and the hidden .vault data folder
            continue
        path = join(folder, name)
        if item.get("id") is None:
            folders.append({"name": name, "path": path})
        else:
            meta = item.get("metadata") or {}
            files.append({
                "name": name,
                "path": path,
                "kind": file_kind(name),
                "bytes": meta.get("size", 0),
                "size": human_size(meta.get("size", 0)),
                "uploaded": local_time(item.get("created_at"), offset),
            })
    return folders, files


def remove_keys(keys):
    for i in range(0, len(keys), 100):
        bucket().remove(keys[i:i + 100])


def migrate_legacy_data(uid, source=""):
    """Move the old single-user data (everything outside users/) into uid's vault.

    Returns (moved count, list of paths that could not be moved).
    """
    keys = []
    for item in bucket().list(source, {"limit": 1000}):
        if not source and item["name"] == USERS_DIR:
            continue
        key = f"{source}/{item['name']}" if source else item["name"]
        if item.get("id") is None:
            keys.extend(o["key"] for o in walk_raw(key))
        else:
            keys.append(key)

    dest = user_root(uid)
    moved, failed = 0, []
    for key in keys:
        rel = key[len(source) + 1:] if source else key
        try:
            bucket().move(key, f"{dest}/{rel}")
            moved += 1
        except Exception:
            failed.append(rel)

    # The single-user version kept preferences in a local settings.json.
    legacy_settings = os.path.join(BASE_DIR, "settings.json")
    if not source and os.path.exists(legacy_settings):
        try:
            if load_json(f"{dest}/{SETTINGS_PATH}", None) is None:
                with open(legacy_settings, encoding="utf-8") as f:
                    save_json(f"{dest}/{SETTINGS_PATH}", json.load(f))
            os.replace(legacy_settings, legacy_settings + ".migrated")
        except Exception:
            pass
    return moved, failed


# ---------- settings ----------

def load_settings():
    """The signed-in user's preferences, read once per request."""
    if "settings" not in g:
        user = current_user()
        settings = {"display_name": user["name"] if user else "", "utc_offset": 8, "theme": "system"}
        if user:
            try:
                settings.update(load_json(full(SETTINGS_PATH), {}))
            except Exception:
                pass
        g.settings = settings
    return g.settings


def save_settings(settings):
    save_json(full(SETTINGS_PATH), settings)
    g.settings = settings


# ---------- formatting helpers ----------

def clean_path(path):
    """Normalise a folder path: no empty parts, no '.' or '..'."""
    parts = [p.strip() for p in (path or "").split("/")]
    return "/".join(p for p in parts if p and p not in (".", ".."))


def join(folder, name):
    return f"{folder}/{name}" if folder else name


def parent_of(path):
    return path.rsplit("/", 1)[0] if "/" in path else ""


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def format_offset(offset):
    sign = "+" if offset >= 0 else "-"
    hours, minutes = divmod(round(abs(offset) * 60), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def local_time(iso, offset):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:16]
    return dt.astimezone(timezone(timedelta(hours=offset))).strftime("%d %b %Y, %H:%M")


def extension(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def file_kind(name):
    return FILE_KINDS.get(extension(name), "file")


def breadcrumbs(path):
    crumbs, acc = [], ""
    for part in path.split("/") if path else []:
        acc = join(acc, part)
        crumbs.append({"name": part, "path": acc})
    return crumbs


# ---------- timetable helpers ----------

def load_timetable():
    """The user's timetable entries; an empty list if none saved yet."""
    return load_json(full(TIMETABLE_PATH), {"entries": []}).get("entries", [])


def save_timetable(entries):
    entries.sort(key=lambda e: (e["date"], e["start"] or "99:99", e["title"].lower()))
    save_json(full(TIMETABLE_PATH), {"entries": entries})


def today_local():
    offset = load_settings()["utc_offset"]
    return datetime.now(timezone(timedelta(hours=offset))).date()


def week_start(d):
    return d - timedelta(days=d.weekday())


def parse_date(value):
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return None


def occurs_on(entry, d):
    """Whether an entry happens on date d; weekly entries repeat from their first date."""
    first = date.fromisoformat(entry["date"])
    if entry.get("repeat") != "weekly":
        return d == first
    if d < first or (d - first).days % 7:
        return False
    until = parse_date(entry.get("until"))
    if until and d > until:
        return False
    return d.isoformat() not in entry.get("skip", [])


def entries_on(entries, d):
    """The occurrences on date d, earliest first; each carries its date as 'on'."""
    out = [dict(e, on=d.isoformat()) for e in entries if occurs_on(e, d)]
    out.sort(key=lambda e: (e["start"] or "99:99", e["title"].lower()))
    return out


def upcoming(entries, today, days=7):
    """Assignments and exams from today through the next `days` days."""
    out = []
    for i in range(days + 1):
        d = today + timedelta(days=i)
        for e in entries_on(entries, d):
            if e["type"] in ("assignment", "exam"):
                when = "Today" if i == 0 else "Tomorrow" if i == 1 else f"in {i} days"
                out.append(dict(e, when=when, day_label=d.strftime("%a %d %b"), days_left=i))
    return out


def entry_from_form(form):
    """Validate a timetable form. Returns (fields, None) or (None, error message)."""
    d = parse_date(form.get("date"))
    title = form.get("title", "").strip()[:100]
    start = form.get("start", "").strip()
    end = form.get("end", "").strip()
    kind = form.get("type", "other")
    note = form.get("note", "").strip()[:300]
    repeat = form.get("repeat") == "weekly"
    until_raw = form.get("until", "").strip() if repeat else ""
    until = parse_date(until_raw)

    if not d or not title:
        return None, "Give the entry a title and a day."
    if (start and not TIME_RE.match(start)) or (end and not TIME_RE.match(end)):
        return None, "Times must look like 09:00."
    if end and not start:
        return None, "Add a start time before an end time."
    if start and end and end <= start:
        return None, "The end time must be after the start time."
    if until_raw and not until:
        return None, "The repeat end date isn't a valid date."
    if until and until < d:
        return None, "The repeat end date must be on or after the first day."

    return {
        "date": d.isoformat(),
        "start": start,
        "end": end,
        "title": title,
        "type": kind if kind in ENTRY_TYPES else "other",
        "note": note,
        "repeat": "weekly" if repeat else "",
        "until": until.isoformat() if until else "",
    }, None


# ---------- app wiring ----------

def login_required(view):
    """Redirect to /login unless a Google account is signed in."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.template_filter("nice_date")
def nice_date(value):
    d = parse_date(value)
    return d.strftime("%d %b %Y") if d else value


@app.context_processor
def inject_globals():
    ctx = {"settings": load_settings(), "user": current_user()}
    if current_user() and request.endpoint not in ("login", "privacy", "static"):
        try:
            ctx["sidebar_folders"], ctx["sidebar_files"] = list_folder("")
        except Exception:
            ctx["sidebar_folders"], ctx["sidebar_files"] = [], []
    return ctx


@app.errorhandler(413)
def upload_too_large(e):
    flash(f"That upload is too big. The limit is {MAX_UPLOAD_MB} MB at a time.", "error")
    return redirect(url_for("files"))


# ---------- auth ----------

@app.route("/login")
def login():
    if current_user():
        return redirect(url_for("files"))
    return render_template("login.html")


@app.route("/login/google")
def login_google():
    redirect_uri = url_for("auth_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri, prompt="select_account")


@app.route("/auth/callback")
def auth_callback():
    try:
        token = oauth.google.authorize_access_token()
    except Exception:
        flash("Google sign-in was cancelled or failed. Please try again.", "error")
        return redirect(url_for("login"))

    info = token.get("userinfo") or {}
    if not info.get("sub") or not info.get("email") or not info.get("email_verified"):
        flash("Your Google account needs a verified email address.", "error")
        return redirect(url_for("login"))

    session.clear()
    session.permanent = True
    session["user"] = {
        "id": info["sub"],
        "email": info["email"],
        "name": info.get("name") or info["email"].split("@")[0],
        "picture": info.get("picture", ""),
    }

    if OWNER_EMAIL and info["email"].lower() == OWNER_EMAIL:
        try:
            moved, failed = migrate_legacy_data(info["sub"])
            if moved:
                flash(f"Moved {moved} of your earlier files into your account.", "success")
            if failed:
                flash(f"Could not move {len(failed)} earlier file(s): {', '.join(failed[:5])}", "error")
        except Exception as e:
            flash(f"Could not move your earlier files: {e}", "error")

    return redirect(url_for("files"))


@app.route("/privacy")
def privacy():
    return render_template("privacy.html", quota=human_size(USER_QUOTA_BYTES),
                           contact=OWNER_EMAIL, updated=date(2026, 10, 24))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- files ----------

@app.route("/")
@login_required
def index():
    return redirect(url_for("files"))


@app.route("/files/", defaults={"folder": ""})
@app.route("/files/<path:folder>")
@login_required
def files(folder):
    folder = clean_path(folder)
    try:
        folders, file_list = list_folder(folder)
    except Exception as e:
        flash(f"Could not load this folder: {e}", "error")
        folders, file_list = [], []

    # The home page gets a greeting and today's timetable.
    home = None
    if not folder:
        offset = load_settings()["utc_offset"]
        now = datetime.now(timezone(timedelta(hours=offset)))
        greeting = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 18 else "Good evening"
        try:
            entries = load_timetable()
            today, soon = entries_on(entries, now.date()), upcoming(entries, now.date())
        except Exception:
            today = soon = None
        home = {"greeting": greeting, "date": now.strftime("%A, %d %B"),
                "today": today, "upcoming": soon}

    return render_template("files.html", folder=folder, crumbs=breadcrumbs(folder),
                           folders=folders, files=file_list, home=home,
                           entry_types=ENTRY_TYPES)


@app.route("/view/<path:path>")
@login_required
def preview(path):
    path = clean_path(path)
    folder, name = parent_of(path), path.rsplit("/", 1)[-1]
    try:
        info = next((f for f in list_folder(folder)[1] if f["name"] == name), None)
    except Exception:
        info = None
    if info is None:
        abort(404)

    mode = PREVIEW_MODES.get(extension(name), "none")
    src = text = None
    try:
        if mode == "text":
            if info["bytes"] > TEXT_PREVIEW_LIMIT:
                mode = "too_large"
            else:
                text = bucket().download(full(path)).decode("utf-8", errors="replace")
        elif mode != "none":
            # Media loads straight from Supabase via a short-lived link, which
            # gives the browser streaming/seeking for video and the PDF viewer.
            src = signed_url(path)
    except Exception as e:
        flash(f"Could not load preview: {e}", "error")
        mode = "none"

    return render_template("preview.html", file=info, mode=mode, src=src, text=text,
                           ext=extension(name), folder=folder, current_path=path,
                           crumbs=breadcrumbs(path))


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    folder = clean_path(request.form.get("folder"))
    incoming = [(secure_filename(f.filename or ""), f) for f in request.files.getlist("file")]
    incoming = [(name, f) for name, f in incoming if name]
    if not incoming:
        flash("No file selected.", "error")
        return redirect(url_for("files", folder=folder))

    try:
        used = usage_bytes()
    except Exception as e:
        flash(f"Upload failed: {e}", "error")
        return redirect(url_for("files", folder=folder))

    uploaded, failed = 0, []
    for name, f in incoming:
        data = f.read()
        if used + len(data) > USER_QUOTA_BYTES:
            left = max(USER_QUOTA_BYTES - used, 0)
            failed.append(f"{name} (not enough space: {human_size(left)} left of {human_size(USER_QUOTA_BYTES)})")
            continue
        try:
            bucket().upload(full(join(folder, name)), data, {
                "content-type": f.mimetype or "application/octet-stream",
                "upsert": "true",
            })
            uploaded += 1
            used += len(data)
        except Exception as e:
            failed.append(f"{name} ({e})")

    if uploaded:
        flash(f"Uploaded {uploaded} file{'s' if uploaded != 1 else ''}.", "success")
    for msg in failed:
        flash(f"Upload failed: {msg}", "error")
    return redirect(url_for("files", folder=folder))


@app.route("/folder", methods=["POST"])
@login_required
def new_folder():
    parent = clean_path(request.form.get("folder"))
    name = secure_filename(request.form.get("name", ""))
    if not name:
        flash("Enter a folder name (letters, numbers, - and _).", "error")
        return redirect(url_for("files", folder=parent))
    try:
        bucket().upload(full(join(join(parent, name), PLACEHOLDER)), b"",
                        {"content-type": "text/plain", "upsert": "true"})
        flash(f"Created folder {name}.", "success")
    except Exception as e:
        flash(f"Could not create folder: {e}", "error")
    return redirect(url_for("files", folder=parent))


@app.route("/delete", methods=["POST"])
@login_required
def delete():
    path = clean_path(request.form.get("path"))
    is_folder = request.form.get("kind") == "folder"
    if not path or path.split("/", 1)[0] == DATA_DIR:
        abort(400)
    try:
        remove_keys([o["key"] for o in walk(path)] if is_folder else [full(path)])
        flash(f"Deleted {path.rsplit('/', 1)[-1]}.", "success")
    except Exception as e:
        flash(f"Delete failed: {e}", "error")
    return redirect(url_for("files", folder=parent_of(path)))


@app.route("/download/<path:path>")
@login_required
def download(path):
    path = clean_path(path)
    try:
        data = bucket().download(full(path))
    except Exception:
        abort(404)
    return send_file(io.BytesIO(data), download_name=path.rsplit("/", 1)[-1], as_attachment=True)


# ---------- timetable ----------

@app.route("/timetable")
@login_required
def timetable():
    today = today_local()
    start = week_start(parse_date(request.args.get("week")) or today)
    try:
        entries = load_timetable()
    except Exception as e:
        flash(f"Could not load timetable: {e}", "error")
        entries = []

    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        iso = d.isoformat()
        days.append({
            "date": iso,
            "weekday": d.strftime("%A"),
            "short": d.strftime("%a"),
            "label": d.strftime("%d %b"),
            "is_today": d == today,
            "entries": entries_on(entries, d),
        })

    end = start + timedelta(days=6)
    if start.year != end.year:
        week_label = f"{start:%d %b %Y} – {end:%d %b %Y}"
    elif start.month != end.month:
        week_label = f"{start:%d %b} – {end:%d %b %Y}"
    else:
        week_label = f"{start:%d} – {end:%d %b %Y}"

    this_week = week_start(today)
    return render_template(
        "timetable.html", days=days, week_label=week_label, entry_types=ENTRY_TYPES,
        week=start.isoformat(),
        prev_week=(start - timedelta(days=7)).isoformat(),
        next_week=(start + timedelta(days=7)).isoformat(),
        is_this_week=start == this_week, is_next_week=start == this_week + timedelta(days=7),
        this_week=this_week.isoformat(),
        total=sum(len(d["entries"]) for d in days),
    )


@app.route("/timetable/add", methods=["POST"])
@login_required
def timetable_add():
    fields, error = entry_from_form(request.form)
    back = request.form.get("week") or request.form.get("date", "")
    if error:
        flash(error, "error")
        return redirect(url_for("timetable", week=back))
    try:
        entries = load_timetable()
        entries.append({"id": uuid.uuid4().hex, **fields, "skip": []})
        save_timetable(entries)
        first = date.fromisoformat(fields["date"])
        if fields["repeat"]:
            msg = f"Added \"{fields['title']}\" every {first:%A}"
            msg += f" until {date.fromisoformat(fields['until']):%d %b %Y}." if fields["until"] else "."
        else:
            msg = f"Added \"{fields['title']}\" on {first:%A %d %b}."
        flash(msg, "success")
    except Exception as e:
        flash(f"Could not save entry: {e}", "error")
    return redirect(url_for("timetable", week=back))


@app.route("/timetable/edit/<entry_id>", methods=["GET", "POST"])
@login_required
def timetable_edit(entry_id):
    week = request.values.get("week", "")
    try:
        entries = load_timetable()
    except Exception as e:
        flash(f"Could not load timetable: {e}", "error")
        return redirect(url_for("timetable", week=week))
    entry = next((e for e in entries if e["id"] == entry_id), None)
    if entry is None:
        flash("That entry no longer exists.", "error")
        return redirect(url_for("timetable", week=week))

    if request.method == "POST":
        fields, error = entry_from_form(request.form)
        if error:
            flash(error, "error")
            # Re-show what was typed so nothing is lost.
            typed = {**entry, **request.form.to_dict(), "repeat": request.form.get("repeat", "")}
            return render_template("timetable_edit.html", entry=typed, entry_types=ENTRY_TYPES, week=week)
        entry.update(fields)
        if not entry["repeat"]:
            entry["skip"] = []
        try:
            save_timetable(entries)
            flash(f"Saved \"{entry['title']}\".", "success")
        except Exception as e:
            flash(f"Could not save entry: {e}", "error")
        return redirect(url_for("timetable", week=week or entry["date"]))

    return render_template("timetable_edit.html", entry=entry, entry_types=ENTRY_TYPES, week=week)


@app.route("/timetable/delete", methods=["POST"])
@login_required
def timetable_delete():
    entry_id = request.form.get("id", "")
    on = request.form.get("on", "")
    only_this = request.form.get("scope") == "one"
    try:
        entries = load_timetable()
        entry = next((e for e in entries if e["id"] == entry_id), None)
        if entry is not None:
            if only_this and entry.get("repeat") == "weekly" and parse_date(on):
                entry.setdefault("skip", []).append(on)
                flash(f"Removed \"{entry['title']}\" on {date.fromisoformat(on):%a %d %b} only.", "success")
            else:
                entries.remove(entry)
                flash(f"Removed \"{entry['title']}\""
                      + (" from every week." if entry.get("repeat") == "weekly" else "."), "success")
            save_timetable(entries)
    except Exception as e:
        flash(f"Could not remove entry: {e}", "error")
    return redirect(url_for("timetable", week=request.form.get("week", "")))


# ---------- search ----------

@app.route("/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    found_folders, found_files = [], []
    if q:
        needle = q.lower()
        try:
            objects = walk("")
        except Exception as e:
            flash(f"Search failed: {e}", "error")
            objects = []
        offset = load_settings()["utc_offset"]
        for o in objects:
            name = o["path"].rsplit("/", 1)[-1]
            if o["placeholder"] or needle not in name.lower():
                continue
            found_files.append({
                "name": name,
                "path": o["path"],
                "folder": parent_of(o["path"]),
                "kind": file_kind(name),
                "size": human_size(o["size"]),
                "uploaded": local_time(o["created_at"], offset),
            })
        for p in sorted(folder_paths(objects), key=str.lower):
            name = p.rsplit("/", 1)[-1]
            if needle in name.lower():
                found_folders.append({"name": name, "path": p, "parent": parent_of(p)})
        found_files.sort(key=lambda f: f["name"].lower())
    return render_template("search.html", q=q, found_folders=found_folders[:100],
                           found_files=found_files[:200])


# ---------- settings & account ----------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    settings = dict(load_settings())
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()[:40]
        try:
            offset = float(request.form.get("utc_offset", settings["utc_offset"]))
        except ValueError:
            offset = settings["utc_offset"]
        theme = request.form.get("theme")

        settings["display_name"] = display_name or current_user()["name"]
        if offset in UTC_OFFSETS:
            settings["utc_offset"] = int(offset) if offset.is_integer() else offset
        if theme in THEMES:
            settings["theme"] = theme
        try:
            save_settings(settings)
            flash("Settings saved.", "success")
        except Exception as e:
            flash(f"Could not save settings: {e}", "error")
        return redirect(url_for("settings_page"))

    stats = None
    try:
        objects = walk("")
        real = [o for o in objects if not o["placeholder"]]
        used = usage_bytes()
        stats = {
            "files": len(real),
            "folders": len(folder_paths(objects)),
            "size": human_size(sum(o["size"] for o in real)),
            "used": human_size(used),
            "quota": human_size(USER_QUOTA_BYTES),
            "percent": min(100, round(used * 100 / USER_QUOTA_BYTES, 1)) if USER_QUOTA_BYTES else 0,
        }
    except Exception as e:
        flash(f"Could not read storage usage: {e}", "error")

    offsets = [(o, format_offset(o)) for o in UTC_OFFSETS]
    return render_template("settings.html", offsets=offsets, themes=THEMES, stats=stats)


@app.route("/account/delete", methods=["POST"])
@login_required
def delete_account():
    email = current_user()["email"]
    if request.form.get("confirm", "").strip().lower() != email.lower():
        flash("To delete your account, type your email address exactly.", "error")
        return redirect(url_for("settings_page"))
    try:
        remove_keys([o["key"] for o in walk_raw(user_root())])
    except Exception as e:
        flash(f"Could not delete your account: {e}", "error")
        return redirect(url_for("settings_page"))
    session.clear()
    flash("Your account and all of its files were deleted.", "success")
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(debug=True)
