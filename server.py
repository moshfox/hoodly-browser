from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import base64
import urllib.parse
import re
import time
import os
import queue
import threading

app = Flask(__name__)
CORS(app)

# ---------------------------------------------------------------------------
# All Playwright work runs on a SINGLE dedicated thread (_pw_thread).
# Flask handler threads communicate with it via _task_queue (send) and
# per-task result queues (receive).  This is the only correct way to use
# sync_playwright in a multi-threaded server.
# ---------------------------------------------------------------------------

_task_queue = queue.Queue()

VIEWPORT_WIDTH  = 1280
VIEWPORT_HEIGHT = 720

HOODLY_URL = "https://hodely.net/Hoodly/"
HOME_URL   = "https://www.google.com"

TARGET_FPS   = 15
JPEG_QUALITY = 42


# ── helpers ────────────────────────────────────────────────────────────────

def normalize_url_or_search(text):
    value = (text or "").strip()
    if not value:
        return HOME_URL
    if re.match(r"^https?://", value, re.I):
        return value
    if re.match(r"^[a-zA-Z0-9-]+\.[a-zA-Z]{2,}", value):
        return "https://" + value
    return "https://www.google.com/search?q=" + urllib.parse.quote(value)


def _safe_goto(page, url):
    from playwright.sync_api import TimeoutError as PTE
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except PTE:
        pass
    except Exception:
        try:
            page.goto("about:blank", timeout=10_000)
        except Exception:
            pass


def _sync_meta(tab):
    page = tab["page"]
    try:
        tab["url"] = page.url or tab["url"]
        if tab["pinned"]:
            tab["title"] = "Hoodly"
        else:
            t = page.title()
            tab["title"] = t[:60] if t else "Nueva pestaña"
    except Exception:
        pass


# ── Playwright worker thread ────────────────────────────────────────────────

def _pw_worker():
    """
    Runs forever on its own thread.
    Pulls (fn, result_q) items from _task_queue and executes them here,
    so every Playwright call happens on the thread that owns the objects.
    """
    from playwright.sync_api import sync_playwright

    pw      = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--window-size=1280,720",
        ],
    )

    # ── state (lives entirely inside this thread) ──────────────────────────
    tabs        = {}
    active_tab  = [None]   # list so closures can mutate it
    tab_counter = [0]

    _frame_b64 = [None]
    _dirty     = [True]

    def _new_page():
        return browser.new_page(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            device_scale_factor=1,
        )

    def _create_tab(url=None, pinned=False, title="Nueva pestaña"):
        tab_counter[0] += 1
        tid = f"tab_{tab_counter[0]}"
        page = _new_page()
        tabs[tid] = {"id": tid, "page": page, "title": title,
                     "url": url or HOME_URL, "pinned": pinned,
                     "created": time.time()}
        _safe_goto(page, url or HOME_URL)
        active_tab[0] = tid
        _sync_meta(tabs[tid])
        _dirty[0] = True
        return tid

    def _tabs_payload():
        return [
            {"id": t["id"], "title": t["title"], "url": t["url"],
             "pinned": t["pinned"], "active": t["id"] == active_tab[0]}
            for t in tabs.values()
        ]

    def _ensure_active():
        if not tabs:
            _create_tab(HOME_URL, title="Google")
        if active_tab[0] not in tabs:
            active_tab[0] = next(iter(tabs))
        return active_tab[0]

    def _capture():
        try:
            _ensure_active()
            shot = tabs[active_tab[0]]["page"].screenshot(
                type="jpeg", quality=JPEG_QUALITY, full_page=False,
                timeout=5_000,
                clip={"x": 0, "y": 0,
                      "width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            )
            _frame_b64[0] = "data:image/jpeg;base64," + base64.b64encode(shot).decode()
            _dirty[0] = False
        except Exception:
            pass

    # ── initial tabs ───────────────────────────────────────────────────────
    _create_tab(HOODLY_URL, pinned=True,  title="Hoodly")
    _create_tab(HOME_URL,   pinned=False, title="Google")

    # ── background capture loop (runs *inside* this thread via the queue) ──
    _last_capture = [0.0]

    # ── main event loop ────────────────────────────────────────────────────
    while True:
        # non-blocking so we can also drive the capture loop
        try:
            fn, result_q = _task_queue.get(timeout=0.015)
            try:
                result_q.put(("ok", fn(tabs, active_tab, tab_counter,
                                        _frame_b64, _dirty,
                                        _create_tab, _ensure_active,
                                        _capture, _tabs_payload,
                                        _sync_meta)))
            except Exception as e:
                result_q.put(("err", e))
        except queue.Empty:
            pass

        # drive the capture loop
        now = time.monotonic()
        if _dirty[0] and (now - _last_capture[0]) >= 1.0 / TARGET_FPS:
            _capture()
            _last_capture[0] = now


def _call(fn):
    """Send fn to the Playwright thread and block until it returns."""
    rq = queue.Queue()
    _task_queue.put((fn, rq))
    status, value = rq.get()
    if status == "err":
        raise value
    return value


# ── start the worker thread once at import time ────────────────────────────

_pw_thread = threading.Thread(target=_pw_worker, daemon=True, name="pw-worker")
_pw_thread.start()


# ── Flask routes ────────────────────────────────────────────────────────────

@app.route("/")
def home_route():
    return send_from_directory(".", "index.html")

@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/tabs")
def get_tabs():
    try:
        def fn(tabs, active_tab, *_rest):
            return {"ok": True, "active": active_tab[0],
                    "tabs": [{"id": t["id"], "title": t["title"],
                               "url": t["url"], "pinned": t["pinned"],
                               "active": t["id"] == active_tab[0]}
                              for t in tabs.values()]}
        return jsonify(_call(fn))
    except Exception as e:
        return jsonify({"ok": False, "tabs": [], "error": str(e)}), 500


@app.route("/screenshot")
def screenshot():
    try:
        def fn(tabs, active_tab, _tc, frame_b64, dirty,
               create_tab, ensure_active, capture, tabs_payload, sync_meta):
            ensure_active()
            if dirty[0] or frame_b64[0] is None:
                capture()
            return {"ok": True, "image": frame_b64[0] or "",
                    "active": active_tab[0], "tabs": tabs_payload()}
        return jsonify(_call(fn))
    except Exception as e:
        return jsonify({"ok": False, "image": None, "tabs": [],
                        "error": str(e)}), 500


@app.route("/tab/new", methods=["POST"])
def new_tab():
    url = (request.get_json(silent=True) or {}).get("url") or HOME_URL
    try:
        def fn(tabs, active_tab, _tc, frame_b64, dirty,
               create_tab, ensure_active, capture, tabs_payload, sync_meta):
            tid = create_tab(url, pinned=False, title="Nueva pestaña")
            active_tab[0] = tid
            dirty[0] = True
            return {"ok": True, "active": active_tab[0], "tabs": tabs_payload()}
        return jsonify(_call(fn))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/tab/switch", methods=["POST"])
def switch_tab():
    tab_id = (request.get_json(silent=True) or {}).get("id")
    try:
        def fn(tabs, active_tab, _tc, frame_b64, dirty,
               create_tab, ensure_active, capture, tabs_payload, sync_meta):
            if tab_id not in tabs:
                raise KeyError("La pestaña no existe")
            active_tab[0] = tab_id
            dirty[0] = True
            return {"ok": True, "active": active_tab[0], "tabs": tabs_payload()}
        return jsonify(_call(fn))
    except KeyError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/tab/close", methods=["POST"])
def close_tab():
    tab_id = (request.get_json(silent=True) or {}).get("id")
    try:
        def fn(tabs, active_tab, _tc, frame_b64, dirty,
               create_tab, ensure_active, capture, tabs_payload, sync_meta):
            if tab_id not in tabs:
                raise KeyError("La pestaña no existe")
            if tabs[tab_id]["pinned"]:
                raise PermissionError("La pestaña fijada no se puede cerrar")
            try:
                tabs[tab_id]["page"].close()
            except Exception:
                pass
            del tabs[tab_id]
            if active_tab[0] == tab_id:
                active_tab[0] = next(iter(tabs), None)
            ensure_active()
            dirty[0] = True
            return {"ok": True, "active": active_tab[0], "tabs": tabs_payload()}
        return jsonify(_call(fn))
    except KeyError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/navigate", methods=["POST"])
def navigate():
    text = (request.get_json(silent=True) or {}).get("text", "")
    url  = normalize_url_or_search(text)
    try:
        def fn(tabs, active_tab, _tc, frame_b64, dirty,
               create_tab, ensure_active, capture, tabs_payload, sync_meta):
            ensure_active()
            page = tabs[active_tab[0]]["page"]
            _safe_goto(page, url)
            sync_meta(tabs[active_tab[0]])
            dirty[0] = True
            return {"ok": True, "url": url,
                    "active": active_tab[0], "tabs": tabs_payload()}
        return jsonify(_call(fn))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _nav_action(page_method, **kwargs):
    try:
        def fn(tabs, active_tab, _tc, frame_b64, dirty,
               create_tab, ensure_active, capture, tabs_payload, sync_meta):
            ensure_active()
            page = tabs[active_tab[0]]["page"]
            try:
                getattr(page, page_method)(
                    wait_until="domcontentloaded", timeout=20_000, **kwargs)
                sync_meta(tabs[active_tab[0]])
            except Exception:
                pass
            dirty[0] = True
            return {"ok": True, "tabs": tabs_payload()}
        return jsonify(_call(fn))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/nav/back",    methods=["POST"])
def nav_back():    return _nav_action("go_back")

@app.route("/nav/forward", methods=["POST"])
def nav_forward(): return _nav_action("go_forward")

@app.route("/nav/reload",  methods=["POST"])
def nav_reload():  return _nav_action("reload")


@app.route("/act_shot", methods=["POST"])
def act_shot():
    data   = request.get_json(silent=True) or {}
    action = data.get("action")
    try:
        def fn(tabs, active_tab, _tc, frame_b64, dirty,
               create_tab, ensure_active, capture, tabs_payload, sync_meta):
            ensure_active()
            page = tabs[active_tab[0]]["page"]

            if action == "click":
                page.mouse.click(int(data.get("x", 0)), int(data.get("y", 0)))
            elif action == "dblclick":
                page.mouse.dblclick(int(data.get("x", 0)), int(data.get("y", 0)))
            elif action == "scroll":
                page.mouse.wheel(0, int(data.get("amount", 180)))
            elif action == "type":
                page.keyboard.type(data.get("text", ""), delay=2)
            elif action == "key":
                page.keyboard.press(data.get("key", "Enter"))
            else:
                raise ValueError("Acción desconocida")

            dirty[0] = True
            capture()
            return {"ok": True, "image": frame_b64[0] or "",
                    "active": active_tab[0], "tabs": tabs_payload()}
        return jsonify(_call(fn))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


if __name__ == "__main__":
    print("Hoodly Remote Browser iniciado")
    print("Abre: http://127.0.0.1:5050")
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5050)),
        debug=False,
        threaded=True,
        use_reloader=False,
    )
