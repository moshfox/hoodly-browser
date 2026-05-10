from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from threading import RLock, Thread
import base64
import urllib.parse
import re
import time
import os

app = Flask(__name__)
CORS(app)

pw = None
browser = None
tabs = {}
active_tab = None
tab_counter = 0
browser_lock = RLock()

runtime_started = False
runtime_lock = RLock()

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 720

HOODLY_URL = "https://hodely.net/Hoodly/"
HOME_URL = "https://www.google.com"

_frame_b64 = None
_frame_ts = 0.0
_frame_lock = RLock()

_dirty = True
_dirty_lock = RLock()

TARGET_FPS = 15
JPEG_QUALITY = 42


def mark_dirty():
    global _dirty
    with _dirty_lock:
        _dirty = True


def get_frame():
    with _frame_lock:
        return _frame_b64

def capture_now():
    global _frame_b64, _frame_ts

    try:
        with browser_lock:
            page = get_active_page()
            page.wait_for_timeout(700)

            shot = page.screenshot(
                type="jpeg",
                quality=70,
                full_page=False,
                timeout=10000,
            )

        encoded = "data:image/jpeg;base64," + base64.b64encode(shot).decode()

        with _frame_lock:
            _frame_b64 = encoded
            _frame_ts = time.monotonic()

        return encoded

    except Exception as e:
        print("CAPTURE_NOW ERROR:", repr(e), flush=True)
        return ""
def normalize_url_or_search(text):
    value = (text or "").strip()

    if not value:
        return HOME_URL

    if re.match(r"^https?://", value, re.I):
        return value

    if re.match(r"^[a-zA-Z0-9-]+\.[a-zA-Z]{2,}", value):
        return "https://" + value

    return "https://www.google.com/search?q=" + urllib.parse.quote(value)


def safe_goto(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except PlaywrightTimeoutError:
        pass
    except Exception:
        try:
            page.goto("about:blank", timeout=10000)
        except Exception:
            pass


def start_browser():
    global pw, browser

    with browser_lock:
        if browser is not None:
            return

        pw = sync_playwright().start()

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
                "--disable-software-rasterizer",
"--disable-extensions",
"--disable-background-networking",
"--single-process",
"--no-zygote",
"--window-size=1280,720",
            ],
        )

        create_tab(HOODLY_URL, pinned=True, title="Hoodly")
        create_tab(HOME_URL, pinned=False, title="Google")


def create_tab(url=None, pinned=False, title="Nueva pestaña"):
    global tab_counter, active_tab

    if browser is None:
        return None

    tab_counter += 1
    tab_id = f"tab_{tab_counter}"
    final_url = url or HOME_URL

    page = browser.new_page(
        viewport={
            "width": VIEWPORT_WIDTH,
            "height": VIEWPORT_HEIGHT,
        },
        device_scale_factor=1,
    )

    tabs[tab_id] = {
        "id": tab_id,
        "page": page,
        "title": title,
        "url": final_url,
        "pinned": pinned,
        "created": time.time(),
    }

    safe_goto(page, final_url)
    active_tab = tab_id
    sync_tab_meta(tab_id)
    mark_dirty()

    return tab_id


def ensure_active_tab():
    global active_tab

    start_browser()

    if not tabs:
        active_tab = create_tab(HOME_URL, pinned=False, title="Google")

    if active_tab not in tabs:
        active_tab = next(iter(tabs.keys()))

    return active_tab


def get_active_page():
    return tabs[ensure_active_tab()]["page"]


def sync_tab_meta(tab_id):
    if tab_id not in tabs:
        return

    page = tabs[tab_id]["page"]

    try:
        tabs[tab_id]["url"] = page.url or tabs[tab_id]["url"]

        if tabs[tab_id]["pinned"]:
            tabs[tab_id]["title"] = "Hoodly"
        else:
            title = page.title()
            tabs[tab_id]["title"] = title[:60] if title else "Nueva pestaña"

    except Exception:
        pass


def tabs_payload():
    ensure_active_tab()

    return [
        {
            "id": t["id"],
            "title": t["title"],
            "url": t["url"],
            "pinned": t["pinned"],
            "active": t["id"] == active_tab,
        }
        for t in tabs.values()
    ]


def capture_worker():
    global _frame_b64, _frame_ts, _dirty

    while True:
        with _dirty_lock:
            need = _dirty

        if not need:
            time.sleep(0.012)
            continue

        try:
            with browser_lock:
                if browser is None or not tabs or active_tab not in tabs:
                    time.sleep(0.05)
                    continue

                shot = tabs[active_tab]["page"].screenshot(
                    type="jpeg",
                    quality=JPEG_QUALITY,
                    full_page=False,
                    timeout=5000,
                    clip={
                        "x": 0,
                        "y": 0,
                        "width": VIEWPORT_WIDTH,
                        "height": VIEWPORT_HEIGHT,
                    },
                )

            encoded = "data:image/jpeg;base64," + base64.b64encode(shot).decode()

            with _frame_lock:
                _frame_b64 = encoded
                _frame_ts = time.monotonic()

            with _dirty_lock:
                _dirty = False

        except Exception:
            time.sleep(0.05)

        time.sleep(1.0 / TARGET_FPS)


def ensure_runtime_started():
    global runtime_started

    with runtime_lock:
        if runtime_started:
            return

        start_browser()

        worker = Thread(target=capture_worker, daemon=True)
        worker.start()

        runtime_started = True


@app.route("/")
def home_route():
    return send_from_directory(".", "index.html")


@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/tabs", methods=["GET"])
def get_tabs():
    try:
        ensure_runtime_started()
        ensure_active_tab()

        return jsonify({
            "ok": True,
            "active": active_tab,
            "tabs": tabs_payload(),
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "active": None,
            "tabs": [],
            "error": str(e),
        }), 500


@app.route("/screenshot", methods=["GET"])
def screenshot():
    try:
        ensure_runtime_started()
        ensure_active_tab()

        image = get_frame()

if not image:
    image = capture_now()

        return jsonify({
            "ok": True,
            "image": image,
            "active": active_tab,
            "tabs": tabs_payload(),
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "image": None,
            "active": None,
            "tabs": [],
            "error": str(e),
        }), 500


@app.route("/tab/new", methods=["POST"])
def new_tab():
    global active_tab

    ensure_runtime_started()

    data = request.get_json(silent=True) or {}
    url = data.get("url") or HOME_URL

    try:
        with browser_lock:
            tab_id = create_tab(url, pinned=False, title="Nueva pestaña")
            active_tab = tab_id

        mark_dirty()

        return jsonify({
            "ok": True,
            "active": active_tab,
            "tabs": tabs_payload(),
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "tabs": tabs_payload(),
            "error": str(e),
        }), 500


@app.route("/tab/switch", methods=["POST"])
def switch_tab():
    global active_tab

    ensure_runtime_started()

    data = request.get_json(silent=True) or {}
    tab_id = data.get("id")

    if tab_id not in tabs:
        return jsonify({
            "ok": False,
            "error": "La pestaña no existe",
            "tabs": tabs_payload(),
        }), 404

    active_tab = tab_id
    mark_dirty()

    return jsonify({
        "ok": True,
        "active": active_tab,
        "tabs": tabs_payload(),
    })


@app.route("/tab/close", methods=["POST"])
def close_tab():
    global active_tab

    ensure_runtime_started()

    data = request.get_json(silent=True) or {}
    tab_id = data.get("id")

    if tab_id not in tabs:
        return jsonify({
            "ok": False,
            "error": "La pestaña no existe",
            "tabs": tabs_payload(),
        }), 404

    if tabs[tab_id]["pinned"]:
        return jsonify({
            "ok": False,
            "error": "La pestaña fijada no se puede cerrar",
            "tabs": tabs_payload(),
        }), 400

    with browser_lock:
        try:
            tabs[tab_id]["page"].close()
        except Exception:
            pass

        del tabs[tab_id]

        if active_tab == tab_id:
            active_tab = next(iter(tabs.keys()), None)

        ensure_active_tab()

    mark_dirty()

    return jsonify({
        "ok": True,
        "active": active_tab,
        "tabs": tabs_payload(),
    })


@app.route("/navigate", methods=["POST"])
def navigate():
    ensure_runtime_started()

    data = request.get_json(silent=True) or {}
    url = normalize_url_or_search(data.get("text", ""))

    try:
        with browser_lock:
            page = get_active_page()
            safe_goto(page, url)
            sync_tab_meta(active_tab)

        mark_dirty()

        return jsonify({
            "ok": True,
            "url": url,
            "active": active_tab,
            "tabs": tabs_payload(),
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "tabs": tabs_payload(),
        }), 500


@app.route("/nav/back", methods=["POST"])
def nav_back():
    ensure_runtime_started()

    try:
        with browser_lock:
            get_active_page().go_back(
                wait_until="domcontentloaded",
                timeout=20000,
            )
            sync_tab_meta(active_tab)
    except Exception:
        pass

    mark_dirty()

    return jsonify({
        "ok": True,
        "tabs": tabs_payload(),
    })


@app.route("/nav/forward", methods=["POST"])
def nav_forward():
    ensure_runtime_started()

    try:
        with browser_lock:
            get_active_page().go_forward(
                wait_until="domcontentloaded",
                timeout=20000,
            )
            sync_tab_meta(active_tab)
    except Exception:
        pass

    mark_dirty()

    return jsonify({
        "ok": True,
        "tabs": tabs_payload(),
    })


@app.route("/nav/reload", methods=["POST"])
def nav_reload():
    ensure_runtime_started()

    try:
        with browser_lock:
            get_active_page().reload(
                wait_until="domcontentloaded",
                timeout=20000,
            )
            sync_tab_meta(active_tab)
    except Exception:
        pass

    mark_dirty()

    return jsonify({
        "ok": True,
        "tabs": tabs_payload(),
    })


@app.route("/act_shot", methods=["POST"])
def act_shot():
    ensure_runtime_started()

    data = request.get_json(silent=True) or {}
    action = data.get("action")

    try:
        with browser_lock:
            page = get_active_page()

            if action == "click":
                page.mouse.click(
                    int(data.get("x", 0)),
                    int(data.get("y", 0)),
                )

            elif action == "dblclick":
                page.mouse.dblclick(
                    int(data.get("x", 0)),
                    int(data.get("y", 0)),
                )

            elif action == "scroll":
                page.mouse.wheel(
                    0,
                    int(data.get("amount", 180)),
                )

            elif action == "type":
                page.keyboard.type(
                    data.get("text", ""),
                    delay=2,
                )

            elif action == "key":
                page.keyboard.press(
                    data.get("key", "Enter"),
                )

            else:
                return jsonify({
                    "ok": False,
                    "error": "Acción desconocida",
                }), 400

        mark_dirty()

        return jsonify({
            "ok": True,
            "image": get_frame() or "",
            "active": active_tab,
            "tabs": tabs_payload(),
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
        }), 500


if __name__ == "__main__":
    print("Hoodly Remote Browser iniciado")
    print("Abre: http://127.0.0.1:5050")

    ensure_runtime_started()

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5050)),
        debug=False,
        threaded=True,
        use_reloader=False,
    )
