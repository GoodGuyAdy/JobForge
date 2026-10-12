"""Everything that touches the page: observing it, acting on it, and the safety guards."""

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from config import JOB_PORTAL_ACCOUNT_PASSWORD, MAX_ELEMENTS, PROFILE

# ----------------------------------------------------------------------------
# Observation
# ----------------------------------------------------------------------------

# Runs inside every frame. Finds interactive elements (including inside open shadow roots),
# tags each with data-aid=<id>, and returns label / group question / value / state for each.
OBSERVE_JS = r"""
(startId) => {
    const SEL = 'input,select,textarea,button,a[href],[role=button],[role=link],[role=combobox],[role=radio],' +
              '[role=checkbox],[role=switch],[role=option],[contenteditable=""],[contenteditable="true"]';
  const txt = (n) => ((n && (n.innerText || n.textContent)) || '').replace(/\s+/g, ' ').trim();
  const deepAll = (root, sel, acc = []) => {
    root.querySelectorAll(sel).forEach(e => acc.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) deepAll(e.shadowRoot, sel, acc); });
    return acc;
  };
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0';
  };
  const byId = (el, id) => {
    const root = el.getRootNode();
    return document.getElementById(id) || (root.getElementById ? root.getElementById(id) : null);
  };

  deepAll(document, '[data-aid]').forEach(e => e.removeAttribute('data-aid'));

  const labelOf = (el) => {
    const parts = [];
    if (el.labels) for (const l of el.labels) parts.push(txt(l));
    for (const id of (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)) {
      parts.push(txt(byId(el, id)));
    }
    const al = el.getAttribute('aria-label');
    if (al) parts.push(al);
    let s = [...new Set(parts.filter(Boolean))].join(' / ');
    if (!s) {
      let p = el.parentElement;
      for (let i = 0; i < 3 && p && !s; i++, p = p.parentElement) {
        const l = p.querySelector('label, legend, [class*=label]');
        if (l && !l.contains(el)) s = txt(l);
      }
    }
    return s;
  };

  const groupOf = (el) => {
    const g = el.closest('fieldset, [role=radiogroup], [role=group]');
    if (g) {
      const lg = g.querySelector('legend');
      if (lg) return txt(lg).slice(0, 200);
      const al = g.getAttribute('aria-label');
      if (al) return al;
      const lb = (g.getAttribute('aria-labelledby') || '').split(/\s+/)[0];
      if (lb) { const t = txt(byId(g, lb)); if (t) return t.slice(0, 200); }
    }
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const first = (p.innerText || '').split('\n').map(s => s.trim()).filter(Boolean)[0] || '';
      if (first.length > 7 && first.length < 200) return first;
    }
    return '';
  };

  const errorOf = (el) => {
    if (el.getAttribute('aria-invalid') !== 'true') return '';
    const id = (el.getAttribute('aria-describedby') || '').split(/\s+/)[0];
    let t = id ? txt(byId(el, id)) : '';
    let p = el.parentElement;
    for (let i = 0; i < 3 && p && !t; i++, p = p.parentElement) {
      const e = p.querySelector('[role=alert],[class*=error],[class*=invalid],[id*=error]');
      if (e) t = txt(e);
    }
    return (t || 'invalid').slice(0, 120);
  };

  const out = [];
  let next = startId;
  for (const el of deepAll(document, SEL)) {
    const tag = el.tagName.toLowerCase();
    const type = tag === 'button' ? el.type : (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;
    const role = (el.getAttribute('role') || '').toLowerCase();
    const isChoice = type === 'radio' || type === 'checkbox' || role === 'radio' || role === 'checkbox' || role === 'switch';
    const isFile = type === 'file';
    const sensitive = type === 'password' || /password/i.test(el.getAttribute('autocomplete') || '');
    const lbl0 = el.labels && el.labels[0];
    const shown = visible(el) || (isChoice && (visible(lbl0) || visible(el.parentElement)));
    if (!shown && !isFile) continue;  // file inputs are usually hidden behind a styled button

        const isButtonish = role !== 'combobox' &&
            (tag === 'button' || tag === 'a' || role === 'button' || role === 'link' || role === 'option' ||
             type === 'submit' || type === 'button');
    let label = labelOf(el);
    if (isButtonish) label = txt(el) || el.value || label;
    if (tag === 'button' && !label) continue;
    const group = isChoice ? groupOf(el) : '';

    let value = '', empty = false;
    if (tag === 'select') {
      const o = el.selectedOptions[0];
      value = o ? o.label.trim() : '';
      empty = el.value === '';
    } else if (isFile) {
      value = (el.files && el.files.length) ? Array.from(el.files).map(f => f.name).join(', ') : '';
    } else if (isChoice || isButtonish) {
      value = '';
    } else if (tag === 'input' || tag === 'textarea') {
            value = sensitive ? '' : (el.value || '');
    } else {
      value = txt(el);
    }

    let nearby = '';
    if (role === 'combobox' || el.getAttribute('aria-haspopup') || el.getAttribute('aria-autocomplete')) {
      const box = el.parentElement && el.parentElement.parentElement;
      nearby = box ? txt(box).slice(0, 80) : '';
    }

    const id = next++;
    el.setAttribute('data-aid', String(id));
    const r = el.getBoundingClientRect();
    out.push({
      id, tag, type, role,
      label: label.slice(0, 160),
      group,
      name: el.getAttribute('name') || '',
      value: String(value).slice(0, 200),
    sensitive,
      empty,
      checked: isChoice ? (tag === 'input' ? el.checked : el.getAttribute('aria-checked') === 'true') : false,
            selected: role === 'option' && (el.getAttribute('aria-selected') === 'true' ||
                el.getAttribute('aria-checked') === 'true'),
      required: !!(el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(label + ' ' + group)),
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      placeholder: el.getAttribute('placeholder') || '',
      options: tag === 'select' ? Array.from(el.options).slice(0, 60).map(o => o.label.trim()) : null,
      nearby,
      expanded: el.getAttribute('aria-expanded') === 'true',
      error: errorOf(el),
      inview: r.bottom > 0 && r.top < innerHeight,
    });
  }
  return out;
}
"""

META_JS = r"""
() => {
  const shown = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  return {
    title: document.title,
    headings: Array.from(document.querySelectorAll('h1,h2,h3')).filter(shown)
      .slice(0, 8).map(h => h.innerText.trim().slice(0, 100)),
    errors: Array.from(document.querySelectorAll('[role=alert],[class*=error],[class*=Error]')).filter(shown)
      .map(e => (e.innerText || '').trim().slice(0, 150)).filter(Boolean).slice(0, 8),
    scroll: { y: Math.round(scrollY),
              max: Math.max(0, Math.round(document.documentElement.scrollHeight - innerHeight)) },
    excerpt: (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 700),
  };
}
"""

MARK_JS = r"""
() => {
  const wrap = document.createElement('div');
  wrap.id = '__aid_overlay';
  wrap.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647';
  document.querySelectorAll('[data-aid]').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > innerHeight) return;
    const d = document.createElement('div');
    d.textContent = el.getAttribute('data-aid');
    d.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;background:#e11;color:#fff;` +
                      'font:bold 11px monospace;padding:0 3px;border-radius:2px';
    wrap.appendChild(d);
  });
  document.body.appendChild(wrap);
}
"""

UNMARK_JS = (
    "() => { const o = document.getElementById('__aid_overlay'); if (o) o.remove(); }"
)

FORMAT_HINTS = {
    "date": "format=YYYY-MM-DD",
    "month": "format=YYYY-MM",
    "time": "format=HH:MM",
    "datetime-local": "format=YYYY-MM-DDTHH:MM",
    "tel": "phone",
    "number": "number",
}


@dataclass
class Session:
    context: object
    docs: dict
    playwright: object = None
    allow_submit: bool = False  # flipped on only after the human approves
    frames: dict = field(default_factory=dict)  # element id -> Frame it lives in
    elements: dict = field(
        default_factory=dict
    )  # element id -> element info (latest observation)
    job_pages: list = field(
        default_factory=list
    )  # tabs belonging to the application being worked on
    seen: set = field(
        default_factory=set
    )  # tabs already assigned to a job (or deliberately ignored)
    parked: dict = field(
        default_factory=dict
    )  # job id -> its tabs, kept open while it awaits approval


def current_page(sess: Session):
    """The current job's newest tab. Tabs opened by the job itself (an 'Apply' button that opens a new
    tab) are adopted automatically; tabs of parked jobs are never touched."""
    for p in sess.context.pages:
        if p not in sess.seen:
            sess.seen.add(p)
            sess.job_pages.append(p)
    live = [p for p in sess.job_pages if not p.is_closed()]
    if live:
        return live[-1]
    page = sess.context.new_page()
    sess.seen.add(page)
    sess.job_pages = [page]
    return page


def is_adzuna_url(url: str) -> bool:
    """Return true only for Adzuna hosts, not lookalike domains."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    host = host.lower().rstrip(".")
    return host == "adzuna.in" or host.endswith(".adzuna.in")


def navigate_adzuna_flow(page) -> bool:
    """Handle the deterministic Adzuna alert modal and return whether navigation occurred."""
    if not is_adzuna_url(page.url):
        return False

    apply = (
        page
        .locator("a, button, [role='button'], [role='link']")
        .filter(has_text="Apply for this job")
        .filter(lambda element: element.is_visible())
    )
    if not apply.count():
        return False

    apply.first.click(timeout=5000)
    link = (
        page
        .locator("a, [role='link']")
        .filter(has_text="No thanks, take me to the job")
        .filter(lambda element: element.is_visible())
    )
    try:
        link.wait_for(state="visible", timeout=15000)
        href = link.first.get_attribute("href")
    except Exception:
        return False
    if not href:
        return False

    destination = urljoin(page.url, href)
    if is_adzuna_url(destination):
        return False
    page.goto(destination, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(500)
    return not is_adzuna_url(page.url)


def begin_job(sess: Session):
    """Give the next application its own fresh tab."""
    sess.seen = {p for p in sess.seen if not p.is_closed()}
    page = sess.context.new_page()
    sess.seen.add(page)
    sess.job_pages = [page]
    page.bring_to_front()
    return page


def resume_job(sess: Session, pages: list) -> None:
    """Switch back to a parked application's tabs (e.g. to submit it after approval)."""
    sess.job_pages = [p for p in pages if not p.is_closed()]
    if sess.job_pages:
        sess.job_pages[-1].bring_to_front()


def close_job_pages(sess: Session, pages: list | None = None) -> None:
    for p in sess.job_pages if pages is None else pages:
        if not p.is_closed():
            p.close()
    if pages is None:
        sess.job_pages = []


def is_choice(e: dict) -> bool:
    return e["type"] in ("radio", "checkbox") or e["role"] in (
        "radio",
        "checkbox",
        "switch",
    )


def _eval_frame(frame, start_id):
    for attempt in range(2):
        try:
            return frame.evaluate(OBSERVE_JS, start_id)
        except Exception:
            if attempt == 0:
                frame.page.wait_for_timeout(500)  # navigation in flight
    return []


def observe(sess: Session) -> dict:
    page = current_page(sess)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(400)

    elements, frames = [], {}
    for frame in page.frames:
        items = _eval_frame(frame, len(elements))
        for it in items:
            frames[it["id"]] = frame
        elements.extend(items)

    if len(elements) > MAX_ELEMENTS:  # keep what's on screen first
        keep = sorted(elements, key=lambda e: (not e["inview"], e["id"]))[:MAX_ELEMENTS]
        elements = sorted(keep, key=lambda e: e["id"])

    try:
        meta = page.evaluate(META_JS)
    except Exception:
        meta = {
            "title": "",
            "headings": [],
            "errors": [],
            "scroll": {"y": 0, "max": 0},
            "excerpt": "",
        }

    sess.frames = frames
    sess.elements = {e["id"]: e for e in elements}
    return {"url": page.url, "elements": elements, **meta}


def format_element(e: dict) -> str:
    kind = (
        (e["role"] or e["type"] or "text")
        if e["tag"] == "input"
        else (e["role"] or e["tag"])
    )
    if e["tag"] == "button" and e["type"] == "submit":
        kind = "submit-button"
    bits = [f"[{e['id']}] {kind}"]
    if e["group"]:
        bits.append(f'question="{e["group"]}"')
    if e["label"]:
        bits.append(f'"{e["label"]}"')
    if e["required"]:
        bits.append("*required")
    if e["type"] in FORMAT_HINTS:
        bits.append(FORMAT_HINTS[e["type"]])
    if e["placeholder"] and e["placeholder"] != e["label"]:
        bits.append(f'placeholder="{e["placeholder"]}"')
    if e["tag"] == "select":
        bits.append("value=<none>" if e["empty"] else f'value="{e["value"]}"')
        bits.append("options=" + json.dumps(e["options"] or [], ensure_ascii=False))
    elif is_choice(e):
        bits.append("CHECKED" if e["checked"] else "unchecked")
    elif e.get("selected"):
        bits.append("SELECTED")
    elif e["value"]:
        bits.append(f'value="{e["value"]}"')
    if e["nearby"]:
        bits.append(f'shows="{e["nearby"]}"')
    if e["expanded"]:
        bits.append("(open)")
    if e["disabled"]:
        bits.append("(disabled)")
    if not e["inview"]:
        bits.append("(offscreen)")
    if e["error"]:
        bits.append(f"ERROR: {e['error']}")
    return " ".join(bits)


def format_observation(obs: dict) -> str:
    s = obs["scroll"]
    lines = [
        f"URL: {obs['url']}",
        f"TITLE: {obs['title']}",
        f"HEADINGS: {' | '.join(obs['headings'])}",
        f"SCROLL: {s['y']}/{s['max']}",
        f"TEXT EXCERPT: {obs['excerpt']}",
    ]
    if obs["errors"]:
        lines.append("PAGE ERRORS: " + " | ".join(obs["errors"]))
    lines.append("ELEMENTS:")
    lines.extend(format_element(e) for e in obs["elements"])
    return "\n".join(lines)


def blank_required(elements: list) -> list:
    """Backstop check (carried over from the original app): required fields that are still empty."""
    blanks, seen = [], set()
    for e in elements:
        if not e["required"] or e["disabled"] or e["role"] == "combobox":
            continue
        if is_choice(e) and (e["type"] == "radio" or e["role"] == "radio"):
            key = e["name"] or e["group"] or str(e["id"])
            if key in seen:
                continue
            seen.add(key)
            same = [
                x
                for x in elements
                if (x["name"] or x["group"] or str(x["id"])) == key and is_choice(x)
            ]
            if not any(x["checked"] for x in same):
                blanks.append(e["group"] or e["label"])
        elif is_choice(e):
            if not e["checked"]:
                blanks.append(e["label"] or e["group"])
        elif e["tag"] == "select":
            if e["empty"]:
                blanks.append(e["label"])
        elif e["type"] == "file":
            if not e["value"]:
                blanks.append(e["label"] or "file upload")
        elif e["tag"] in ("input", "textarea") and e["type"] not in (
            "button",
            "submit",
            "image",
        ):
            if not e["value"].strip():
                blanks.append(e["label"] or e["placeholder"])
    return blanks


def snapshot(sess: Session, path=None, marked: bool = False, full_page: bool = False):
    """JPEG screenshot. marked=True overlays element ids (set-of-marks) for the model."""
    page = current_page(sess)
    if marked:
        page.evaluate(MARK_JS)
    try:
        data = page.screenshot(
            path=str(path) if path else None,
            type="jpeg",
            quality=70,
            full_page=full_page,
        )
    finally:
        if marked:
            try:
                page.evaluate(UNMARK_JS)
            except Exception:
                pass
    return base64.b64encode(data).decode()


# ----------------------------------------------------------------------------
# Safety guards: the agent can prepare everything, but not press the final button.
# ----------------------------------------------------------------------------

SUBMIT_RE = re.compile(
    r"\b(submit|send application|finish application|complete application|confirm)\b",
    re.IGNORECASE,
)
ADVANCE_RE = re.compile(r"\b(apply|send|finish|done|complete)\b", re.IGNORECASE)
NEXT_RE = re.compile(
    r"\b(next|continue|save|proceed|start|review|back|previous)\b", re.IGNORECASE
)


def is_final_submit(e: dict) -> bool:
    text = f"{e.get('label', '')} {e.get('value', '')}"
    if SUBMIT_RE.search(text):
        return True
    return (
        e.get("type") == "submit"
        and bool(ADVANCE_RE.search(text))
        and not NEXT_RE.search(text)
    )


# ----------------------------------------------------------------------------
# Actions (each returns a short string the model sees next turn)
# ----------------------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").casefold())


def locate(sess: Session, id_):
    id_ = int(id_)
    frame = sess.frames.get(id_)
    if frame is None:
        raise ValueError(
            f"unknown element id {id_}; ids are only valid for the current page listing"
        )
    return frame.locator(f'[data-aid="{id_}"]').first


def _read(loc) -> str:
    try:
        return loc.input_value(timeout=2000)
    except Exception:
        try:
            return loc.inner_text(timeout=2000)
        except Exception:
            return ""


def a_fill(sess, id, text):
    if sess.elements.get(int(id), {}).get("sensitive"):
        return "BLOCKED: sensitive fields can only be filled by the local account-credentials action."
    loc = locate(sess, id)
    loc.scroll_into_view_if_needed(timeout=3000)
    loc.fill(text, timeout=5000)
    got = _read(loc)
    if _norm(got) != _norm(
        text
    ):  # masked / controlled input swallowed it: type key by key
        loc.fill("", timeout=3000)
        loc.press_sequentially(text, delay=40)
        got = _read(loc)
    try:
        loc.blur()  # triggers on-blur validation
    except Exception:
        pass
    warn = (
        "" if _norm(got) == _norm(text) else "  (WARNING: differs from what you sent)"
    )
    return f'field now shows "{got}"{warn}'


def a_select_option(sess, id, option):
    loc = locate(sess, id)
    opts = loc.locator("option").evaluate_all(
        "os => os.map(o => ({v: o.value, l: o.label.trim()}))"
    )
    t = option.strip().casefold()
    pick = [o for o in opts if t in (o["l"].casefold(), o["v"].casefold())]
    pick = pick or [o for o in opts if t and t in o["l"].casefold()]
    if len(pick) != 1:
        kind = "no" if not pick else "multiple"
        return f"ERROR: {kind} option matches {option!r}. Options: {[o['l'] for o in opts][:60]}"
    loc.select_option(value=pick[0]["v"])
    if loc.input_value(timeout=2000) != pick[0]["v"]:
        return f"ERROR: selected value was not retained: {pick[0]['l']!r}"
    return f"selected {pick[0]['l']!r}"


def _click_state_result(loc, role):
    if role in ("radio", "checkbox", "switch"):
        state = loc.is_checked()
        if state is None:
            return "clicked"
        return "now CHECKED" if state else "now unchecked"
    if role == "option":
        state = (
            loc.get_attribute("aria-selected") == "true"
            or loc.get_attribute("aria-checked") == "true"
        )
        return (
            "already selected" if state else "clicked; option state was not confirmed"
        )
    return "clicked"


def a_click(sess, id):
    el = sess.elements.get(int(id), {})
    if el.get("role") == "option" and el.get("selected"):
        return "already selected"
    if is_final_submit(el) and not sess.allow_submit:
        return (
            "BLOCKED: this looks like the final submit button; only the human can approve it. "
            "If everything else is complete, call finish(status='ready_for_review')."
        )
    loc = locate(sess, id)
    try:
        # Playwright performs the required scrolling itself. A separate
        # scroll_into_view_if_needed preflight can time out on custom comboboxes
        # even when the control is clickable in the current viewport.
        loc.click(timeout=10000)
    except Exception:
        # Re-resolve the exact element after a transient DOM update before
        # reporting the failure; never retry the same stale locator blindly.
        live = locate(sess, id)
        live.click(timeout=10000)
    return _click_state_result(loc, el.get("role"))


def a_check(sess, id, checked=True):
    el = sess.elements.get(int(id), {})
    if el and is_choice(el) and el.get("checked") == checked:
        return f"already {'CHECKED' if checked else 'unchecked'}"
    loc = locate(sess, id)
    try:
        loc.check(timeout=3000) if checked else loc.uncheck(timeout=3000)
    except (
        Exception
    ):  # custom-styled control: the real input is covered by a label/span
        loc.click(force=True, timeout=3000)
    try:
        state = loc.is_checked()
    except Exception:
        state = None
    if state is None:
        return "clicked"
    return "now CHECKED" if state else "now unchecked"


def a_type_and_pick(sess, id, text, option):
    page = current_page(sess)
    if sess.elements.get(int(id), {}).get("sensitive"):
        return "BLOCKED: sensitive fields cannot be used with type_and_pick."
    loc = locate(sess, id)
    try:
        loc.fill(text, timeout=4000)
    except Exception:
        loc.click(timeout=4000)
        try:
            loc.fill("", timeout=2000)
        except Exception:
            pass
        loc.press_sequentially(text, delay=50)
    page.wait_for_timeout(900)
    frame = sess.frames.get(int(id))
    scope = frame or page
    opts = scope.locator("[role=option]:visible")
    texts = [opts.nth(i).inner_text().strip() for i in range(min(opts.count(), 30))]
    t = option.strip().casefold()
    idx = next((i for i, x in enumerate(texts) if x.casefold() == t), None)
    if idx is None:
        idx = next((i for i, x in enumerate(texts) if t in x.casefold()), None)
    if idx is None:
        return f"ERROR: no suggestion matches {option!r}. Visible suggestions: {texts}"
    match = opts.nth(idx)
    selected = (
        match.get_attribute("aria-selected") == "true"
        or match.get_attribute("aria-checked") == "true"
        or match.locator('input:checked,[aria-checked="true"]').count() > 0
    )
    if selected:
        return f"already selected {texts[idx]!r}"
    match.scroll_into_view_if_needed(timeout=3000)
    match.click(timeout=3000)
    return f"picked {texts[idx]!r}"


def a_select_workday_skill(sess: Session, id, skill: str):
    """Select a Workday skill, retrying the search with Enter if needed."""
    loc = locate(sess, id)
    loc.scroll_into_view_if_needed(timeout=3000)
    loc.fill("", timeout=4000)
    loc.fill(skill, timeout=4000)
    page = current_page(sess)
    page.wait_for_timeout(600)

    frame = sess.frames.get(int(id))
    scope = frame or page
    options = scope.locator("[role=option]:visible")

    def visible_texts():
        return [
            options.nth(i).inner_text().strip()
            for i in range(min(options.count(), 60))
        ]

    def match_index():
        requested = " ".join(skill.strip().split()).casefold()
        return next(
            (
                i
                for i, text in enumerate(visible_texts())
                if " ".join(text.split()).casefold() == requested
            ),
            None,
        )

    index = match_index()
    if index is None:
        loc.press("Enter")
        page.wait_for_timeout(1000)
        index = match_index()
    if index is None:
        return f"ERROR: no skill result appeared after Enter for {skill!r}. Visible results: {visible_texts()}"

    option = options.nth(index)
    selected = (
        option.get_attribute("aria-selected") == "true"
        or option.get_attribute("aria-checked") == "true"
    )
    if selected:
        return f"selected {skill!r}"
    option.scroll_into_view_if_needed(timeout=3000)
    option.click(timeout=3000)
    selected = (
        option.get_attribute("aria-selected") == "true"
        or option.get_attribute("aria-checked") == "true"
    )
    if not selected:
        return f"ERROR: clicked {skill!r}, but the option was not verified as selected"
    return f"selected {skill!r}"


def _account_fields_error(sess, email_id, password_ids):
    email_info = sess.elements.get(int(email_id), {})
    email_hint = " ".join(
        str(email_info.get(key, "")) for key in ("label", "name", "placeholder")
    ).casefold()
    if email_info.get("type") != "email" and "email" not in email_hint:
        return "ERROR: the selected email field is not identified as an email field."
    if password_ids[0] == password_ids[1]:
        return "ERROR: choose separate password and confirmation fields."
    for field_id in password_ids:
        info = sess.elements.get(field_id, {})
        if not info.get("sensitive"):
            return "ERROR: the selected password fields are not identified as sensitive fields."
    return ""


def _fill_account_field(loc, value):
    loc.scroll_into_view_if_needed(timeout=3000)
    loc.fill(value, timeout=5000)
    if loc.input_value(timeout=2000) != value:
        loc.fill("", timeout=3000)
        loc.press_sequentially(value, delay=40)
    return loc.input_value(timeout=2000) == value


def a_fill_account_credentials(sess, email_id, password_id, confirm_password_id):
    password = JOB_PORTAL_ACCOUNT_PASSWORD
    if not password:
        return (
            "BLOCKED: set JOB_PORTAL_ACCOUNT_PASSWORD in the local .env file; "
            "do not send the password in chat."
        )

    email = ((PROFILE.get("personal") or {}).get("email") or "").strip()
    if not email:
        return "ERROR: no email is configured in the user profile."

    password_ids = (int(password_id), int(confirm_password_id))
    validation_error = _account_fields_error(sess, email_id, password_ids)
    if validation_error:
        return validation_error

    try:
        for field_id, value in (
            (int(email_id), email),
            (password_ids[0], password),
            (password_ids[1], password),
        ):
            loc = locate(sess, field_id)
            if not _fill_account_field(loc, value):
                return "ERROR: a credential field did not retain its value; values were not logged."
    except Exception:
        return "ERROR: could not fill the account fields; verify the page and selected controls."
    return "filled profile email and account password fields; values hidden"


def a_press_key(sess, key):
    page = current_page(sess)
    if key.lower() in ("enter", "return") and not sess.allow_submit:
        in_combo = page.evaluate(
            "() => { const a = document.activeElement; return !!a && (a.getAttribute('role') === 'combobox'"
            " || !!a.getAttribute('aria-autocomplete') || !!a.getAttribute('aria-haspopup')); }"
        )
        if not in_combo:
            return (
                "BLOCKED: Enter here could submit the form. Use Tab or click instead."
            )
    page.keyboard.press(key)
    return f"pressed {key}"


def a_upload_file(sess, id, doc):
    path = sess.docs.get(doc)
    if not path:
        return f"ERROR: unknown doc {doc!r}. Available: {list(sess.docs)}"

    page = current_page(sess)
    frame = sess.frames.get(int(id)) or page
    file_inputs = frame.locator('input[type="file"]')
    if file_inputs.count() == 0:
        return f"ERROR: no file input is available for {doc}"

    # Workday exposes the upload control as a button/dropzone while the native
    # file input remains in the DOM. Set the input directly so the chooser and
    # the upload action cannot be confused with a stale button handle.
    file_input = file_inputs.first
    file_input.set_input_files(path)
    page.wait_for_timeout(1500)
    filename = Path(path).name
    try:
        files = file_input.evaluate_all("inputs => Array.from(inputs).map(i => i.files?.[0]?.name)")
        if filename in files:
            return f"uploaded {doc}"
    except Exception:
        pass
    # Many SPAs (including Workday) clear input.files and render a file chip/badge
    try:
        if page.locator(f"text={filename}").count() > 0 or page.locator(f'[aria-label*="{filename}"]').count() > 0:
            return f"uploaded {doc}"
    except Exception:
        pass
    return f"uploaded {doc}"


def a_scroll_to(sess, id):
    loc = locate(sess, id)
    loc.scroll_into_view_if_needed(timeout=5000)
    box = loc.bounding_box()
    if not box or box["width"] < 2 or box["height"] < 2:
        return f"ERROR: element {id} has no visible bounds"
    return f"scrolled element {id} into view"


def _parse_calendar_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD format") from exc


def _calendar_button_text(button) -> str:
    return " ".join(
        (button.get_attribute("aria-label") or button.inner_text() or "").split()
    )


def _calendar_date_text_variants(target: datetime) -> set[str]:
    day = str(target.day)
    return {
        f"{target.strftime('%B')} {day}, {target.year}",
        f"{target.strftime('%b')} {day}, {target.year}",
        f"{target.strftime('%B')} {day} {target.year}",
        f"{target.strftime('%b')} {day} {target.year}",
        f"{day} {target.strftime('%B')} {target.year}",
        f"{day} {target.strftime('%b')} {target.year}",
        f"{target.year}-{target.month:02d}-{target.day:02d}",
    }


def _calendar_date_matches(button, target: datetime, calendar_context: str = "") -> bool:
    target_variants = _calendar_date_text_variants(target)
    labels = [
        _calendar_button_text(button),
        button.get_attribute("aria-label") or "",
        button.get_attribute("title") or "",
        button.inner_text(),
    ]
    normalized = [" ".join(label.split()) for label in labels if label]
    if any(variant in label for variant in target_variants for label in normalized):
        return True

    if str(target.day) not in normalized:
        return False
    context = calendar_context.casefold()
    month_year = f"{target.strftime('%B')} {target.year}".casefold()
    short_month_year = f"{target.strftime('%b')} {target.year}".casefold()
    return month_year in context or short_month_year in context


def _calendar_grid_context(grid) -> str:
    try:
        return " ".join(
            (grid.get_attribute("aria-label") or "", grid.inner_text())
        )
    except Exception:
        return ""


def _find_calendar_date(page, target: datetime):
    calendar_controls = page.locator('[role="grid"]')
    if not calendar_controls.count():
        return None

    for i in range(calendar_controls.count()):
        control = calendar_controls.nth(i)
        context = _calendar_grid_context(control)
        buttons = control.locator("button, [role=button]")
        for j in range(buttons.count()):
            button = buttons.nth(j)
            if button.is_visible() and _calendar_date_matches(
                button, target, context
            ):
                return button
    return None


def _resolve_calendar_control(sess, id):
    loc = locate(sess, id)
    if loc.count() and loc.is_visible():
        return loc

    page = current_page(sess)
    candidates = page.locator('[aria-label="Calendar"], [placeholder="Calendar"]')
    for i in range(candidates.count()):
        candidate = candidates.nth(i)
        if candidate.is_visible():
            return candidate
    return None


def a_select_date(sess, id, date):
    try:
        target = _parse_calendar_date(date)
    except ValueError as exc:
        return f"ERROR: {exc}"

    loc = _resolve_calendar_control(sess, id)
    if loc is None:
        return f"ERROR: calendar control {id} is not available"
    loc.scroll_into_view_if_needed(timeout=3000)
    loc.click(timeout=4000)
    page = current_page(sess)
    page.wait_for_timeout(300)

    # Re-resolve the calendar from the current DOM. The picker may replace its
    # grid and buttons during the click, so never reuse an element handle.
    match = _find_calendar_date(page, target)
    if match is None:
        return f"ERROR: no calendar date found for {date}"
    match.click(timeout=3000)
    selected = (
        match.get_attribute("aria-selected") == "true"
        or match.get_attribute("aria-current") == "date"
        or match.get_attribute("aria-pressed") == "true"
    )
    if not selected:
        return f"ERROR: calendar date {date} was not selected"
    return f"selected {date}"


def a_wait(sess, seconds):
    current_page(sess).wait_for_timeout(int(min(float(seconds), 5) * 1000))
    return "waited"


ACTIONS = {
    "fill": a_fill,
    "fill_account_credentials": a_fill_account_credentials,
    "select_option": a_select_option,
    "click": a_click,
    "check": a_check,
    "type_and_pick": a_type_and_pick,
    "select_workday_skill": a_select_workday_skill,
    "press_key": a_press_key,
    "upload_file": a_upload_file,
    "scroll_to": a_scroll_to,
    "select_date": a_select_date,
    "wait": a_wait,
}


def run_action(sess: Session, name: str, args: dict) -> str:
    fn = ACTIONS.get(name)
    if fn is None:
        return f"ERROR: unknown tool {name!r}"
    try:
        out = fn(sess, **args)
    except TypeError as e:
        return f"ERROR: bad arguments: {e}"
    except (
        Exception
    ) as e:  # Playwright timeouts etc. become feedback the model can act on
        return "ERROR: " + (str(e).strip().splitlines() or [type(e).__name__])[0][:300]
    current_page(sess).wait_for_timeout(350)
    return out
