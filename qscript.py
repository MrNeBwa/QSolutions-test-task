import base64
import json
import mimetypes
import os
import random
import re
import time
from urllib.parse import quote_plus, urlsplit

PATH_TO_VIDEO_FOLDER = "./video"

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 720
VIDEO_FPS = 25
ZOOM_FACTOR = 2.0
ZOOM_RAMP_SEC = 0.5

CURSOR_SIZE = 32
HIGHLIGHT_COLOR = "rgba(255,215,0,0.35)"
HIGHLIGHT_RADIUS = 32

HEADLESS = False
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

RESULT_SEL = 'a[data-testid="result-title-a"], a.result__a'

INIT_TEMPLATE = r"""
(() => {
  try { if (window.self !== window.top) return; } catch (e) {}
  if (window.__qInit) return;
  window.__qInit = true;

  const SIZE = __QSIZE__;
  const HL_R = __QHL_R__;
  const HL_COLOR = "__QHL_COLOR__";
  const VIEWPORT_W = __QVIEWPORT_W__;
  const VIEWPORT_H = __QVIEWPORT_H__;

  let lastX = VIEWPORT_W / 2;
  let lastY = VIEWPORT_H / 2;
  let localZoomed = false;
  let showZoomRing = null;

  const updateMousePos = (x, y) => {
    if (typeof x === 'number' && Number.isFinite(x)) lastX = x;
    if (typeof y === 'number' && Number.isFinite(y)) lastY = y;
  };

  window.addEventListener('mousemove', (e) => updateMousePos(e.clientX, e.clientY), true);
  window.addEventListener('pointermove', (e) => updateMousePos(e.clientX, e.clientY), true);

  const toggleZoom = (x, y) => {
    const zx = (typeof x === 'number' && Number.isFinite(x)) ? x : lastX;
    const zy = (typeof y === 'number' && Number.isFinite(y)) ? y : lastY;
    updateMousePos(zx, zy);

    if (typeof window.__qRecordZoomToggle === 'function') {
      return Promise.resolve(window.__qRecordZoomToggle(zx, zy)).then((ev) => {
        const mode = (ev && ev.type) ? ev.type : (localZoomed ? 'out' : 'in');
        localZoomed = (mode === 'in');
        if (typeof showZoomRing === 'function') showZoomRing(zx, zy, mode);
        return ev;
      });
    }

    const mode = localZoomed ? 'out' : 'in';
    localZoomed = !localZoomed;
    if (typeof showZoomRing === 'function') showZoomRing(zx, zy, mode);
    const ev = { x: zx, y: zy, type: mode };
    window.__qZoomEvents = window.__qZoomEvents || [];
    window.__qZoomEvents.push(ev);
    return Promise.resolve(ev);
  };
  window.__zoomAt = toggleZoom;

  window.addEventListener('keydown', (e) => {
    if (e.key === 'F5' || e.code === 'F5' || e.keyCode === 116) {
      e.preventDefault();
      e.stopPropagation();
      e.stopImmediatePropagation();
      if (e.repeat) return;
      toggleZoom(lastX, lastY);
    }
  }, true);

  const start = () => {
  const root = document.documentElement;
  const SVGNS = 'http://www.w3.org/2000/svg';
  const MAXZ = 2147483647;

  const css = (el, o) => { for (const k in o) el.style.setProperty(k, String(o[k]), 'important'); };

  const hl = document.createElement('div');
  hl.id = 'qcursor-hl';
  css(hl, {
    position: 'fixed', left: '0px', top: '0px',
    width: (2 * HL_R) + 'px', height: (2 * HL_R) + 'px',
    margin: '0',
    'margin-left': (-HL_R) + 'px', 'margin-top': (-HL_R) + 'px',
    'border-radius': '50%', background: HL_COLOR,
    'pointer-events': 'none', 'z-index': MAXZ - 1, display: 'none',
  });

  const buildCursor = () => {
    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('width', SIZE);
    svg.setAttribute('height', SIZE);
    svg.setAttribute('viewBox', '0 0 32 32');
    const p = document.createElementNS(SVGNS, 'path');
    p.setAttribute('d', 'M2 2 L2 26 L9 19 L13 29 L17 27 L13 17 L22 17 Z');
    p.setAttribute('fill', 'white');
    p.setAttribute('stroke', 'black');
    p.setAttribute('stroke-width', '2');
    p.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(p);
    return svg;
  };

  const arrow = document.createElement('div');
  arrow.id = 'qcursor-arrow';
  css(arrow, {
    position: 'fixed', left: '0px', top: '0px', width: SIZE + 'px', height: SIZE + 'px',
    'pointer-events': 'none', 'z-index': MAXZ, display: 'none', margin: '0', padding: '0',
    transform: 'translate(' + (-2 * SIZE / 32) + 'px,' + (-2 * SIZE / 32) + 'px)',
  });
  arrow.appendChild(buildCursor());

  const ripple = document.createElement('div');
  ripple.id = 'qcursor-ripple';
  css(ripple, {
    position: 'fixed', left: '0px', top: '0px', width: '0px', height: '0px',
    'border-radius': '50%', border: '3px solid rgba(255,190,0,0.95)', 'box-sizing': 'border-box',
    background: 'rgba(255,215,0,0.25)', 'pointer-events': 'none', 'z-index': MAXZ - 2,
    transform: 'translate(-50%,-50%)', opacity: '0', margin: '0',
  });

  const zoomRing = document.createElement('div');
  zoomRing.id = 'qzoom-ring';
  css(zoomRing, {
    position: 'fixed', left: '0px', top: '0px', width: '0px', height: '0px',
    'border-radius': '50%', border: '4px solid rgba(0,200,83,0.95)', 'box-sizing': 'border-box',
    background: 'rgba(0,200,83,0.14)', 'pointer-events': 'none', 'z-index': MAXZ,
    transform: 'translate(-50%,-50%)', opacity: '0', margin: '0',
  });

  const parts = [hl, arrow, ripple, zoomRing];
  const attach = () => {
    const curRoot = document.documentElement;
    if (!curRoot) return;
    for (const el of parts) if (!el.isConnected || el.parentNode !== curRoot) curRoot.appendChild(el);
  };
  attach();
  setInterval(attach, 250);

  let zoomAnimId = 0;
  showZoomRing = (x, y, mode) => {
    attach();
    if (zoomAnimId) cancelAnimationFrame(zoomAnimId);
    const isIn = (mode === 'in');
    const borderColor = isIn ? 'rgba(0,200,83,0.95)' : 'rgba(255,61,0,0.95)';
    const bgColor = isIn ? 'rgba(0,200,83,0.16)' : 'rgba(255,61,0,0.16)';
    zoomRing.setAttribute('data-zoom-state', mode);
    css(zoomRing, {
      left: x + 'px',
      top: y + 'px',
      border: '4px solid ' + borderColor,
      background: bgColor,
      opacity: '1',
    });
    const startTs = performance.now();
    const durationMs = 450;
    const anim = (ts) => {
      const p = Math.min((ts - startTs) / durationMs, 1);
      const sizePx = isIn ? (96 - 56 * p) : (40 + 56 * p);
      zoomRing.style.setProperty('width', sizePx + 'px', 'important');
      zoomRing.style.setProperty('height', sizePx + 'px', 'important');
      zoomRing.style.setProperty('opacity', String(1 - p), 'important');
      if (p < 1) {
        zoomAnimId = requestAnimationFrame(anim);
      } else {
        zoomAnimId = 0;
      }
    };
    zoomAnimId = requestAnimationFrame(anim);
  };
  window.__qZoomRing = showZoomRing;

  const move = (x, y) => {
    attach();
    updateMousePos(x, y);
    arrow.style.setProperty('display', 'block', 'important');
    hl.style.setProperty('display', 'block', 'important');
    arrow.style.setProperty('left', x + 'px', 'important');
    arrow.style.setProperty('top', y + 'px', 'important');
    hl.style.setProperty('left', x + 'px', 'important');
    hl.style.setProperty('top', y + 'px', 'important');
  };
  window.__qMoveCursor = move;

  const doRipple = (x, y) => {
    attach();
    ripple.style.setProperty('left', x + 'px', 'important');
    ripple.style.setProperty('top', y + 'px', 'important');
    ripple.style.setProperty('opacity', '1', 'important');
    const start = performance.now();
    const anim = (t) => {
      const p = Math.min((t - start) / 450, 1);
      const d = Math.max(p * 2 * SIZE, 0) + 'px';
      ripple.style.setProperty('width', d, 'important');
      ripple.style.setProperty('height', d, 'important');
      ripple.style.setProperty('opacity', String(1 - p), 'important');
      if (p < 1) requestAnimationFrame(anim);
    };
    requestAnimationFrame(anim);
  };
  window.__qRipple = doRipple;
  window.addEventListener('mousedown', (e) => {
    updateMousePos(e.clientX, e.clientY);
    doRipple(e.clientX, e.clientY);
  }, true);

  window.__qShowCallout = (text, bgColor, textColor, fade) => {
    if (fade === undefined || fade === null) fade = 2;
    let el = document.getElementById('qcallout');
    if (!el) {
      el = document.createElement('div');
      el.id = 'qcallout';
      css(el, {
        position: 'fixed', left: '50%', top: '24px', transform: 'translateX(-50%)',
        'z-index': MAXZ, padding: '10px 16px', font: '16pt sans-serif', border: '1px solid #000',
        'border-radius': '4px', 'pointer-events': 'none', 'white-space': 'nowrap', opacity: '0',
      });
      document.documentElement.appendChild(el);
    }
    clearTimeout(el._t);
    el.textContent = text;
    css(el, { background: bgColor || 'yellow', color: textColor || 'black', transition: 'none', opacity: '0' });
    void el.offsetWidth;
    css(el, { transition: 'opacity ' + fade + 's ease', opacity: '1' });
    el._t = setTimeout(() => { el.style.setProperty('opacity', '0', 'important'); }, (fade + 1) * 1000);
  };
  };

  if (document.documentElement) {
    start();
  } else {
    const mo = new MutationObserver(() => {
      if (document.documentElement) { mo.disconnect(); start(); }
    });
    mo.observe(document, { childList: true });
  }
})();
"""


def _build_init_script(viewport_width=VIEWPORT_WIDTH, viewport_height=VIEWPORT_HEIGHT):
    script = INIT_TEMPLATE
    script = script.replace("__QSIZE__", str(CURSOR_SIZE))
    script = script.replace("__QHL_R__", str(HIGHLIGHT_RADIUS))
    script = script.replace("__QHL_COLOR__", HIGHLIGHT_COLOR)
    script = script.replace("__QVIEWPORT_W__", str(int(viewport_width)))
    script = script.replace("__QVIEWPORT_H__", str(int(viewport_height)))
    return script


def get_sidecar_path(video_path):
    """Return the zoom sidecar path (<video>.zoom.json) corresponding to ``video_path``."""
    return os.path.splitext(video_path)[0] + ".zoom.json"


class ZoomRecorder:
    """Tracks zoom toggle events during Playwright recording and writes the sidecar JSON."""

    def __init__(
        self,
        width=VIEWPORT_WIDTH,
        height=VIEWPORT_HEIGHT,
        fps=VIDEO_FPS,
        zoom_factor=ZOOM_FACTOR,
        ramp_sec=ZOOM_RAMP_SEC,
        clock=None,
    ):
        self.width = int(width)
        self.height = int(height)
        self.fps = int(fps)
        self.zoom_factor = float(zoom_factor)
        self.ramp_sec = float(ramp_sec)
        self._clock = clock or time.monotonic
        self._t0 = self._clock()
        self._zoomed = False
        self.events = []

    def reset_clock(self):
        self._t0 = self._clock()

    @property
    def is_zoomed(self):
        return self._zoomed

    def toggle(self, x, y):
        """Record the next zoom toggle ('in' -> 'out' -> 'in' ...) at viewport coords (x, y)."""
        t = max(0.0, round(float(self._clock() - self._t0), 3))
        px = max(0.0, min(float(self.width), round(float(x), 2)))
        py = max(0.0, min(float(self.height), round(float(y), 2)))
        event_type = "out" if self._zoomed else "in"
        self._zoomed = not self._zoomed
        event = {"t": t, "x": px, "y": py, "type": event_type}
        self.events.append(event)
        return event

    def attach_to_context(self, context):
        """Calibrate t=0 to context creation and expose toggle callback across navigations."""
        self.reset_clock()
        context.expose_function("__qRecordZoomToggle", self.toggle)

    def to_sidecar_dict(self):
        return {
            "viewport": {"width": self.width, "height": self.height},
            "fps": self.fps,
            "zoom_factor": self.zoom_factor,
            "ramp_sec": self.ramp_sec,
            "events": list(self.events),
        }

    def write_sidecar(self, video_path):
        """Write ``<video>.zoom.json`` only if at least one zoom event was recorded.

        If no events exist, no sidecar is created (and any stale sidecar from a
        previous recording of the same target path is removed).
        """
        sidecar_path = get_sidecar_path(video_path)
        if not self.events:
            if os.path.exists(sidecar_path):
                try:
                    os.remove(sidecar_path)
                except OSError:
                    pass
            return None
        with open(sidecar_path, "w", encoding="utf-8") as f:
            json.dump(self.to_sidecar_dict(), f, indent=2)
        return sidecar_path


def ZoomAt(page, x=None, y=None):
    """Programmatically trigger the zoom toggle hook ``window.__zoomAt(x, y)``."""
    try:
        if x is None or y is None:
            return page.evaluate("() => window.__zoomAt && window.__zoomAt()")
        return page.evaluate("([x, y]) => window.__zoomAt && window.__zoomAt(x, y)", [x, y])
    except Exception:
        return None


def ShowCallout(page, text, bgColor="yellow", textColor="black", fade=2):
    try:
        page.evaluate(
            "([t, b, c, f]) => window.__qShowCallout && window.__qShowCallout(t, b, c, f)",
            [text, bgColor, textColor, fade],
        )
    except Exception:
        pass


def _smooth_move(page, pos, x, y):
    sx, sy = pos["x"], pos["y"]
    dist = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
    steps = max(40, int(dist / 6))
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)
        cx = sx + (x - sx) * e
        cy = sy + (y - sy) * e
        page.mouse.move(cx, cy)
        try:
            page.evaluate(f"window.__qMoveCursor({cx}, {cy})")
        except Exception:
            pass
        time.sleep(0.016)
    pos["x"], pos["y"] = x, y


def _click(page, pos, x, y):
    page.mouse.move(x, y)
    try:
        page.evaluate(f"window.__qMoveCursor({x}, {y})")
    except Exception:
        pass
    page.mouse.down()
    time.sleep(0.4)
    page.mouse.up()
    pos["x"], pos["y"] = x, y


def _center(locator, timeout_s=30):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            bb = locator.bounding_box()
            if bb and bb["width"] > 0 and bb["height"] > 0:
                return bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
        except Exception:
            pass
        locator.page.wait_for_timeout(300)
    raise TimeoutError("element has no bounding box")


def _safe_filename(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "").strip(" .")
    return name or "video"


def _search_url(start_url, query):
    parts = urlsplit(start_url)
    return "%s://%s/?q=%s&ia=web" % (parts.scheme or "https", parts.netloc, quote_plus(query))


def CaptureVideo_Magento(
    url,
    company_name,
    language,
    country,
    postcode,
    city,
    street,
    house_no,
    *,
    auto_zoom=False,
    zoom_factor=ZOOM_FACTOR,
    ramp_sec=ZOOM_RAMP_SEC,
):
    from playwright.sync_api import sync_playwright
    try:
        from playwright_stealth import stealth_sync
        has_stealth = True
    except ImportError:
        has_stealth = False

    folder = os.path.abspath(PATH_TO_VIDEO_FOLDER)
    os.makedirs(folder, exist_ok=True)

    ctx_args = dict(
        record_video_dir=folder,
        record_video_size={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
        user_agent=USER_AGENT,
    )

    zoom_recorder = ZoomRecorder(
        width=VIEWPORT_WIDTH,
        height=VIEWPORT_HEIGHT,
        fps=VIDEO_FPS,
        zoom_factor=zoom_factor,
        ramp_sec=ramp_sec,
    )

    src = None
    try:
        init_script = _build_init_script(VIEWPORT_WIDTH, VIEWPORT_HEIGHT)
        with sync_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                f"--window-size={VIEWPORT_WIDTH},{VIEWPORT_HEIGHT}",
                "--window-position=100,100"
            ]
            browser = p.chromium.launch(headless=HEADLESS, args=launch_args)
            try:
                context = browser.new_context(**ctx_args)
                try:
                    if has_stealth:
                        stealth_sync(context)

                    zoom_recorder.attach_to_context(context)
                    context.add_init_script(init_script)
                    page = context.new_page()
                    src = page.video.path()
                    pos = {"x": 100.0, "y": 100.0}

                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    page.mouse.move(pos["x"], pos["y"])
                    try:
                        page.evaluate(f"window.__qMoveCursor({pos['x']}, {pos['y']})")
                    except Exception:
                        pass
                    time.sleep(1)

                    box = page.locator('textarea[name="q"]')
                    box.wait_for(state="visible", timeout=30000)
                    ShowCallout(page, "Searching for: Magento developer in %s" % city)
                    cx, cy = _center(box)
                    _smooth_move(page, pos, cx, cy)
                    time.sleep(0.3)
                    _click(page, pos, cx, cy)
                    time.sleep(0.3)

                    if auto_zoom:
                        ZoomAt(page, cx, cy)
                        time.sleep(0.3)

                    query = "Magento developer in %s" % city
                    for ch in query:
                        page.keyboard.type(ch)
                        time.sleep(random.uniform(0.06, 0.18))
                    time.sleep(0.5)

                    if auto_zoom and zoom_recorder.is_zoomed:
                        ZoomAt(page, cx, cy)
                        time.sleep(0.5)

                    form = box.locator("xpath=ancestor::form[1]")
                    btn = form.locator('button[type="submit"]:visible').first
                    try:
                        btn.wait_for(state="visible", timeout=3000)
                    except Exception:
                        btn = page.locator('button[type="submit"]:visible').first

                    try:
                        bx, by = _center(btn, 8)
                        _smooth_move(page, pos, bx, by)
                        time.sleep(0.3)
                        _click(page, pos, bx, by)
                        page.wait_for_selector(RESULT_SEL, state="visible", timeout=10000)
                    except Exception:
                        page.goto(_search_url(url, query), wait_until="domcontentloaded", timeout=60000)
                        page.mouse.move(pos["x"], pos["y"])
                        try:
                            page.evaluate(f"window.__qMoveCursor({pos['x']}, {pos['y']})")
                        except Exception:
                            pass
                        page.wait_for_selector(RESULT_SEL, state="visible", timeout=20000)

                    res = page.locator(RESULT_SEL).first
                    try:
                        res.scroll_into_view_if_needed(timeout=5000)
                    except Exception:
                        pass
                    try:
                        res.evaluate("e => { e.removeAttribute('target'); e.setAttribute('rel', 'noopener'); }")
                    except Exception:
                        pass

                    ShowCallout(page, "Opening the first result")
                    rx, ry = _center(res)
                    _smooth_move(page, pos, rx, ry)
                    time.sleep(0.3)
                    if auto_zoom and not zoom_recorder.is_zoomed:
                        ZoomAt(page, rx, ry)
                        time.sleep(0.5)
                    _click(page, pos, rx, ry)

                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    page.mouse.move(pos["x"], pos["y"])
                    try:
                        page.evaluate(f"window.__qMoveCursor({pos['x']}, {pos['y']})")
                    except Exception:
                        pass
                    if auto_zoom and zoom_recorder.is_zoomed:
                        time.sleep(1.0)
                        ZoomAt(page, pos["x"], pos["y"])
                    time.sleep(3)
                finally:
                    context.close()
            finally:
                browser.close()
    except Exception as e:
        print(f"Error for {company_name}:", e)
        return None

    if not src or not os.path.exists(src):
        return None

    dst = os.path.join(folder, _safe_filename(company_name) + ".webm")
    try:
        if os.path.abspath(src) != dst:
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(src, dst)
    except Exception as e:
        print("Error renaming video:", e)
        return None

    sidecar_path = zoom_recorder.write_sidecar(dst)
    if sidecar_path:
        print(f"Saved zoom sidecar: {sidecar_path} ({len(zoom_recorder.events)} events)")
    return dst