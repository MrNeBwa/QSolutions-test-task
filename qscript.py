import base64
import json
import mimetypes
import os
import random
import re
import time
from urllib.parse import quote_plus, urlsplit

PATH_TO_VIDEO_FOLDER = "./video"

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

  const parts = [hl, arrow, ripple];
  const attach = () => { for (const el of parts) if (!el.isConnected) root.appendChild(el); };
  attach();
  setInterval(attach, 250);

  const move = (x, y) => {
    attach();
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
  window.addEventListener('mousedown', (e) => doRipple(e.clientX, e.clientY), true);

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


def _build_init_script():
    script = INIT_TEMPLATE
    script = script.replace("__QSIZE__", str(CURSOR_SIZE))
    script = script.replace("__QHL_R__", str(HIGHLIGHT_RADIUS))
    script = script.replace("__QHL_COLOR__", HIGHLIGHT_COLOR)
    return script


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
        record_video_size={"width": 1280, "height": 720},
        viewport={"width": 1280, "height": 720},
        user_agent=USER_AGENT,
    )

    src = None
    try:
        init_script = _build_init_script()
        with sync_playwright() as p:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,720",
                "--window-position=100,100"
            ]
            browser = p.chromium.launch(headless=HEADLESS, args=launch_args)
            try:
                context = browser.new_context(**ctx_args)
                try:
                    if has_stealth:
                        stealth_sync(context)
                    
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

                    query = "Magento developer in %s" % city
                    for ch in query:
                        page.keyboard.type(ch)
                        time.sleep(random.uniform(0.06, 0.18))
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
                    _click(page, pos, rx, ry)

                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                    page.mouse.move(pos["x"], pos["y"])
                    try:
                        page.evaluate(f"window.__qMoveCursor({pos['x']}, {pos['y']})")
                    except Exception:
                        pass
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
    return dst