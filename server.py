"""Dynasty server. Python 3.8+, standard library only.
Run:  python server.py   (opens http://127.0.0.1:4737)
Data is stored in dynasty.db next to this file."""
import calendar, datetime, io, json, os, platform, shutil, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.request, webbrowser, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "dynasty.db")
PORT = 4737

# Your MyTimetable feed. Used the first time the app runs; you can change it on the
# University page. Delete this line before sharing the code with anyone.
DEFAULT_FEED = "https://timetable.soton.ac.uk/Feed/Index/oyFZdK0jVyiM8fxhfuhTjbAShwrUXeWnmxGCmcpWzMDbYABFgTVer0TIKUqVWHTRYMtHEH21W2FWuKDKqXOCXg2"
# The server fetches feeds for the page (browsers block this), so only these hosts are allowed.
ALLOWED_HOSTS = ("soton.ac.uk",)

TABLES = {
    "tasks": ["title", "space", "module", "priority", "due", "done", "done_at", "star", "today", "notes", "source_key"],
    "events": ["title", "module", "kind", "starts", "ends", "location", "source_key"],
    "modules": ["code", "name"],
    "notes": ["title", "body", "updated", "module"],
    "goals": ["title", "space", "target", "progress"],
    "habits": ["name"],
    "habit_logs": ["habit_id", "day"],
    "sessions": ["minutes", "day", "task_id"],
    "settings": ["name", "val"],
    "occasions": ["kind", "name", "date"],
    "cards": ["note_id", "front", "back", "answer", "kind", "ckey", "state", "step", "interval", "ease", "reps", "lapses", "due", "created"],
    "reviews": ["card_id", "day", "rating", "prev_state", "ts"],
    "kingdom": ["kind", "x", "y", "cost", "built"],
    "trash": ["kind", "data", "deleted_at"],
}


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init():
    con = db()
    for table, cols in TABLES.items():
        con.execute("CREATE TABLE IF NOT EXISTS %s (id INTEGER PRIMARY KEY AUTOINCREMENT, %s)" % (table, ", ".join(cols)))
        have = {r["name"] for r in con.execute("PRAGMA table_info(%s)" % table)}
        for col in cols:
            if col not in have:  # add columns introduced by newer versions to an existing database
                con.execute("ALTER TABLE %s ADD COLUMN %s" % (table, col))
    con.execute("CREATE TABLE IF NOT EXISTS fired (key TEXT PRIMARY KEY, at TEXT)")
    if DEFAULT_FEED and not con.execute("SELECT 1 FROM settings WHERE name='feed_url'").fetchone():
        con.execute("INSERT INTO settings (name, val) VALUES ('feed_url', ?)", [DEFAULT_FEED])
    con.commit()
    con.close()


def clean(table, data):
    return {k: data[k] for k in TABLES[table] if k in data}


SYSTEM = platform.system()
NATIVE = SYSTEM in ("Darwin", "Windows") or (SYSTEM == "Linux" and shutil.which("notify-send") is not None)


def notify(title, body):
    """Show a real desktop notification (works with every browser window closed)."""
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "timeout": 15}
    try:
        if SYSTEM == "Darwin":
            subprocess.run(["osascript", "-e", "on run argv", "-e",
                            "display notification (item 2 of argv) with title (item 1 of argv)",
                            "-e", "end run", title, body], **quiet)
        elif SYSTEM == "Windows":
            ps = ("[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
                  "[void][Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime];"
                  "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02');"
                  "$t=$x.GetElementsByTagName('text');"
                  "$t[0].AppendChild($x.CreateTextNode($env:DYN_TITLE))|Out-Null;"
                  "$t[1].AppendChild($x.CreateTextNode($env:DYN_BODY))|Out-Null;"
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                  "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
                  ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
            env = dict(os.environ, DYN_TITLE=title, DYN_BODY=body)
            subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                           env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **quiet)
        elif NATIVE:
            subprocess.run(["notify-send", "-a", "Dynasty", title, body], **quiet)
    except (OSError, subprocess.SubprocessError) as e:
        print("notification failed:", e)


def setting(con, name, default):
    r = con.execute("SELECT val FROM settings WHERE name=?", [name]).fetchone()
    return r["val"] if r and r["val"] not in (None, "") else default


def occ_on(date_str, year):
    y, m, d = (int(x) for x in date_str.split("-"))
    if m == 2 and d == 29 and not calendar.isleap(year):
        d = 28
    return datetime.date(year, m, d)


def occ_label(kind, name, date_str, on):
    n = on.year - int(date_str[:4])
    if int(date_str[:4]) >= 1900 and n > 0:
        if kind == "birthday":
            return "%s turns %d" % (name, n)
        return "%s · %d %s" % (name, n, "year" if n == 1 else "years")
    return name


def collect(con, now):
    """Everything that should be announced right now, as (key, title, body)."""
    out = []
    try:
        lead = datetime.timedelta(minutes=int(setting(con, "lead", "10")))
    except ValueError:
        lead = datetime.timedelta(minutes=10)
    for t in con.execute("SELECT * FROM tasks"):
        if t["done"] or not t["due"] or len(t["due"]) < 16:
            continue
        try:
            at = datetime.datetime.fromisoformat(t["due"][:16])
        except ValueError:
            continue
        if at - lead <= now < at + datetime.timedelta(hours=6):
            out.append(("t%d|%s" % (t["id"], t["due"]), "Task due", t["title"]))
    for e in con.execute("SELECT * FROM events"):
        try:
            at = datetime.datetime.fromisoformat(e["starts"][:16])
        except (ValueError, TypeError):
            continue
        if at - lead <= now < at:
            out.append(("e%d" % e["id"], "Class starting", e["title"] + (" @ " + e["location"] if e["location"] else "")))
    today = now.date()
    tomorrow = today + datetime.timedelta(days=1)
    for o in con.execute("SELECT * FROM occasions"):
        try:
            birthday = o["kind"] == "birthday"
            if now.hour >= 9 and occ_on(o["date"], today.year) == today:
                out.append(("o%d|%s" % (o["id"], today), "Birthday today" if birthday else "Anniversary today",
                            occ_label(o["kind"], o["name"], o["date"], today)))
            if now.hour >= 18 and occ_on(o["date"], tomorrow.year) == tomorrow:
                out.append(("ob%d|%s" % (o["id"], tomorrow), "Tomorrow",
                            ("Birthday: " if birthday else "Anniversary: ") + occ_label(o["kind"], o["name"], o["date"], tomorrow)))
        except (ValueError, TypeError):
            continue
    return out


def notifier():
    while True:
        con = db()
        try:
            now = datetime.datetime.now()
            for key, title, body in collect(con, now):
                if con.execute("SELECT 1 FROM fired WHERE key=?", [key]).fetchone():
                    continue
                con.execute("INSERT INTO fired (key, at) VALUES (?, ?)", [key, now.isoformat()])
                con.commit()
                notify(title, body)
            con.execute("DELETE FROM fired WHERE at < ?", [(now - datetime.timedelta(days=3)).isoformat()])
            con.commit()
        except Exception as e:
            print("notifier error:", e)
        finally:
            con.close()
        time.sleep(30)


OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA
PREFER = ("qwen2.5", "qwen3", "llama3", "gemma", "mistral", "phi")
AI_SYSTEM = (
    "You turn a student's notes into study flashcards. Reply with ONLY a JSON object of the form "
    '{"cards":[{"front":"question","back":"answer"}]}. The student will TYPE each answer from memory, so keep every '
    "answer short (a term, a phrase or one short sentence, ideally under 15 words) and make every question "
    "self-contained and specific. Cover definitions, key ideas, relationships, formulas, steps and worked-example "
    "takeaways. No duplicates and no trivia. Keep LaTeX exactly as written in the notes. Use British English."
)


def ollama(path, payload=None, timeout=10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(OLLAMA + path, data, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ai_status(con):
    """Is a local Ollama server running, and which model will be used?"""
    try:
        tags = ollama("/api/tags")
    except Exception:
        return {"ok": False, "models": [], "model": "", "error": "Ollama isn't running (or isn't installed)."}
    models = [m["name"] for m in tags.get("models", []) if "embed" not in m["name"]]
    if not models:
        return {"ok": False, "models": [], "model": "", "error": "Ollama is running but has no models yet."}
    chosen = setting(con, "ai_model", "")
    if chosen not in models:
        chosen = next((m for p in PREFER for m in models if m.startswith(p)), models[0])
    return {"ok": True, "models": models, "model": chosen, "error": ""}


def parse_cards(text):
    text = (text or "").strip()
    try:
        obj = json.loads(text)
    except ValueError:
        try:
            obj = json.loads(text[text.index("["): text.rindex("]") + 1])
        except ValueError:
            return None
    items = obj.get("cards", obj.get("flashcards", [])) if isinstance(obj, dict) else obj
    cards, seen = [], set()
    for i in items if isinstance(items, list) else []:
        if not isinstance(i, dict):
            continue
        front, back = str(i.get("front", "")).strip(), str(i.get("back", "")).strip()
        if front and back and front != back and len(back) <= 400 and front.lower() not in seen:
            seen.add(front.lower())
            cards.append({"front": front, "back": back})
    return cards


def ai_cards(con, text, count):
    st = ai_status(con)
    if not st["ok"]:
        return {"error": st["error"] + " Install it from ollama.com, then run: ollama pull qwen2.5:3b"}
    count = max(3, min(int(count or 8), 30))
    payload = {
        "model": st["model"], "stream": False, "format": "json",
        "options": {"temperature": 0.2, "num_ctx": 8192},
        "messages": [
            {"role": "system", "content": AI_SYSTEM},
            {"role": "user", "content": "Make about %d flashcards from these notes.\n\n%s" % (count, text[:9000])},
        ],
    }
    try:
        data = ollama("/api/chat", payload, timeout=600)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", "HTTP %d" % e.code)
        except Exception:
            msg = "HTTP %d" % e.code
        return {"error": "Ollama error: " + msg}
    except Exception as e:
        return {"error": "Ollama didn't answer: %s" % e}
    cards = parse_cards(data.get("message", {}).get("content", ""))
    if cards is None:
        return {"error": "The model's reply wasn't valid JSON. Try again, or pick a larger model in Settings."}
    return {"cards": cards, "model": st["model"]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def parts(self):
        return [p for p in urlparse(self.path).path.split("/") if p]

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"null")

    def do_GET(self):
        p = self.parts()
        if p == ["api", "ping"]:
            return self.reply(200, {"ok": True, "native": NATIVE})
        if p == ["api", "ai-status"]:
            c = db()
            try:
                return self.reply(200, ai_status(c))
            finally:
                c.close()
        if p == ["katex.js"]:
            f = os.path.join(HERE, "katex.js")
            if not os.path.exists(f):
                return self.reply(404, b"", "text/plain")
            with open(f, "rb") as fh:
                return self.reply(200, fh.read(), "application/javascript; charset=utf-8")
        if p == ["api", "all"]:
            con = db()
            out = {t: [dict(r) for r in con.execute("SELECT * FROM %s" % t)] for t in TABLES}
            con.close()
            return self.reply(200, out)
        with open(os.path.join(HERE, "index.html"), "rb") as f:
            self.reply(200, f.read(), "text/html; charset=utf-8")

    def import_zip(self):
        """Read the markdown files out of a Notion export (including zips nested inside the zip)."""
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        out = []

        def walk(blob, depth):
            try:
                z = zipfile.ZipFile(io.BytesIO(blob))
            except zipfile.BadZipFile:
                return
            for info in z.infolist():
                name = info.filename
                base = name.split("/")[-1]
                if info.is_dir() or "__MACOSX" in name or base.startswith("._"):
                    continue
                low = name.lower()
                if low.endswith(".zip") and depth < 2:
                    walk(z.read(info), depth + 1)
                elif low.endswith((".md", ".markdown")) and info.file_size < 5 * 2 ** 20:
                    out.append({"path": name, "text": z.read(info).decode("utf-8", "replace")})

        walk(raw, 0)
        self.reply(200, {"files": out})

    def do_POST(self):
        p = self.parts()
        if p == ["api", "import-zip"]:
            return self.import_zip()
        data = self.body()
        if p == ["api", "notify-test"]:
            notify("Dynasty", "Notifications are working")
            return self.reply(200, {"ok": True, "native": NATIVE})
        if p == ["api", "fetch-feed"]:
            url = (data or {}).get("url", "")
            u = urlparse(url)
            host = u.hostname or ""
            if u.scheme != "https" or not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
                return self.reply(400, {"error": "Only https links on soton.ac.uk are allowed"})
            try:
                with urllib.request.urlopen(url, timeout=20) as r:
                    return self.reply(200, {"text": r.read().decode("utf-8", "replace")})
            except Exception as e:
                return self.reply(502, {"error": "Could not fetch the timetable: %s" % e})
        con = db()
        if p == ["api", "ai-cards"]:
            try:
                out = ai_cards(con, str((data or {}).get("text", "")), (data or {}).get("count", 20))
            finally:
                con.close()
            return self.reply(200, out)
        if p == ["api", "purge-timetable"]:
            con.execute("DELETE FROM events")
            con.execute("DELETE FROM tasks WHERE source_key LIKE 'rev:%' AND COALESCE(done,0)=0")
            con.commit()
            con.close()
            return self.reply(200, {"ok": True})
        if len(p) == 2 and p[0] == "api" and p[1] in TABLES:
            table = p[1]
            items = data if isinstance(data, list) else [data]
            out = []
            for item in items:
                d = clean(table, item)
                cur = con.execute(
                    "INSERT INTO %s (%s) VALUES (%s)" % (table, ",".join(d), ",".join("?" * len(d))),
                    list(d.values()),
                )
                d["id"] = cur.lastrowid
                out.append(d)
            con.commit()
            con.close()
            return self.reply(200, out if isinstance(data, list) else out[0])
        con.close()
        self.reply(404, {"error": "not found"})

    def do_PUT(self):
        p = self.parts()
        data = self.body()
        if len(p) == 3 and p[1] in TABLES:
            d = clean(p[1], data)
            if d:
                con = db()
                con.execute(
                    "UPDATE %s SET %s WHERE id=?" % (p[1], ",".join(k + "=?" for k in d)),
                    list(d.values()) + [int(p[2])],
                )
                con.commit()
                con.close()
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})

    def do_DELETE(self):
        p = self.parts()
        if len(p) == 3 and p[1] in TABLES:
            con = db()
            con.execute("DELETE FROM %s WHERE id=?" % p[1], [int(p[2])])
            if p[1] == "notes":
                con.execute("DELETE FROM cards WHERE note_id=?", [int(p[2])])
            if p[1] == "habits":
                con.execute("DELETE FROM habit_logs WHERE habit_id=?", [int(p[2])])
            con.commit()
            con.close()
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})


if __name__ == "__main__":
    init()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        raise SystemExit("Port %d is already in use. Close the other Dynasty (or your friend's version) first." % PORT)
    url = "http://127.0.0.1:%d/#today" % PORT
    print("Dynasty running at", url, "(Ctrl+C to stop)")
    print("Background notifications:", "on" if NATIVE else "off (install notify-send on Linux)")
    threading.Thread(target=notifier, daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass