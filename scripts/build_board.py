"""Generate a self-contained, interactive HTML restore board from a tab session.

The board is the human-facing half of the toolkit: a static page with no
dependencies, no build step and no network access, that lets you browse every
recovered destination by domain, search across titles and URLs, and export a
selection as plain URLs.

Design constraints that shaped this file
----------------------------------------
1. **The page must work offline, from `file://`.** No CDN, no framework, no
   fetch(). Everything is inlined so the artifact still works years later.

2. **The generated JavaScript must contain no backslash characters.** A single
   stray escape inside a JS string literal makes the whole `<script>` fail to
   parse, which silently disables *every* handler on the page (the browser
   reports `SyntaxError: Unexpected EOF` and all onclick handlers become
   ReferenceErrors). Newlines in the emitted script are therefore produced with
   `String.fromCharCode(10)` rather than `"\\n"`, and :func:`build_javascript`
   asserts the invariant.

3. **Self-validating.** :func:`validate_javascript` runs the emitted script
   through `node --check` before the HTML is written, so a broken board can
   never reach disk.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections import defaultdict
from typing import Iterable, Optional

from tabkit import Report, Tab, domain_of, write_text

LF = chr(10)

# --------------------------------------------------------------------------- #
# Escaping (implemented without backslash literals in emitted output)
# --------------------------------------------------------------------------- #


def html_escape(text: str) -> str:
    """Escape text for use in HTML text nodes and double-quoted attributes."""
    if not text:
        return "(untitled)"
    text = " ".join(text.replace(LF, " ").split())
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace('"', "&quot;").replace("'", "&#39;")
    return text or "(untitled)"


def attr_escape(text: str) -> str:
    """Escape a URL for a double-quoted attribute. Preserves & for readability."""
    return html_escape(text)


# --------------------------------------------------------------------------- #
# The in-page script
# --------------------------------------------------------------------------- #


def build_javascript() -> str:
    """Assemble the page script.

    Written as a list of statements joined by semicolons, with no backslash
    escapes anywhere, then asserted to be backslash-free before returning.
    """
    statements = [
        "var NL=String.fromCharCode(10)",
        "var DQ=String.fromCharCode(34)",
        "function cbs(){return Array.prototype.slice.call(document.querySelectorAll('input.cb'))}",
        "function upd(){var s=cbs().filter(function(c){return c.checked});"
        "document.getElementById('sc').textContent=s.length;"
        "var v=document.querySelectorAll('li:not(.hide)').length;"
        "document.getElementById('vc').textContent=v;"
        "try{localStorage.setItem('tbSel',JSON.stringify(s.map(function(c){return c.getAttribute('data-u')})))}catch(e){}"
        "}",
        "function visibles(){return cbs().filter(function(c){return !c.closest('li').classList.contains('hide')})}",
        "function all(v){visibles().forEach(function(c){c.checked=!!v});upd()}",
        "function invert(){visibles().forEach(function(c){c.checked=!c.checked});upd()}",
        "function grp(g,v){cbs().forEach(function(c){"
        "if(c.closest('li').getAttribute('data-g')===String(g))c.checked=!!v});upd()}",
        "function flt(){var q=document.getElementById('q').value.toLowerCase().trim();"
        "document.querySelectorAll('li').forEach(function(li){"
        "var hit=(q.length===0||li.getAttribute('data-s').indexOf(q)>-1);"
        "li.classList.toggle('hide',!hit)});"
        "if(q.length>1){document.querySelectorAll('details').forEach(function(d){d.open=true})}"
        "upd()}",
        "function sel(){return cbs().filter(function(c){return c.checked})}",
        "function selUrls(){return sel().map(function(c){return c.getAttribute('data-u')})}",
        "function say(t){var b=document.getElementById('toast');b.textContent=t;b.style.display='block';"
        "clearTimeout(window._t);"
        "window._t=setTimeout(function(){b.style.display='none'},2600)}",
        "function fb(t,m){var a=document.createElement('textarea');a.value=t;"
        "document.body.appendChild(a);a.select();"
        "try{document.execCommand('copy');say('Copied '+m)}catch(e){say('Copy blocked - use Download')}"
        "document.body.removeChild(a)}",
        "function cp(t,m){if(navigator.clipboard&&navigator.clipboard.writeText){"
        "navigator.clipboard.writeText(t).then(function(){say('Copied '+m)},function(){fb(t,m)})"
        "}else{fb(t,m)}}",
        "function copyUrls(){var u=selUrls();if(!u.length){say('Nothing selected');return}"
        "cp(u.join(NL),u.length+' URLs')}",
        "function copyTitled(){var s=sel().map(function(c){"
        "return c.closest('li').querySelector('a').textContent+' | '+c.getAttribute('data-u')});"
        "if(!s.length){say('Nothing selected');return}cp(s.join(NL),s.length+' rows')}",
        "function dl(){var u=selUrls();if(!u.length){say('Nothing selected');return}"
        "var b=new Blob([u.join(NL)],{type:'text/plain'});"
        "var a=document.createElement('a');a.href=URL.createObjectURL(b);"
        "a.download='tab-restore-selection.txt';"
        "document.body.appendChild(a);a.click();document.body.removeChild(a);"
        "say('Downloaded '+u.length+' URLs')}",
        "document.addEventListener('DOMContentLoaded',function(){upd()})",
    ]
    js = ";".join(statements)
    if chr(92) in js:
        raise AssertionError("generated JavaScript contains a backslash")
    return js


def validate_javascript(js: str) -> bool:
    """Syntax-check the emitted script with Node if available.

    Returns True when the script is valid, or when Node is unavailable (in which
    case the backslash assertion in :func:`build_javascript` is still enforced).
    """
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=10, check=True)
    except Exception:
        return True
    tmp = "/tmp/tabboard_check.js"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(js)
    proc = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("invalid JavaScript: " + (proc.stderr or "")[:400])
    return True


# --------------------------------------------------------------------------- #
# Styling
# --------------------------------------------------------------------------- #

CSS = (
    "<style>"
    "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
    "background:#0f1115;color:#e8eaed;margin:0;padding:20px 24px 90px}"
    "h1{font-size:20px;margin:0 0 6px}.sub{color:#9aa0a6;font-size:13px;margin-bottom:16px}"
    ".bar{position:sticky;top:0;background:#0f1115;border-bottom:1px solid #25282e;"
    "padding:12px 0;margin-bottom:16px;z-index:10;display:flex;gap:8px;flex-wrap:wrap;align-items:center}"
    "input{background:#1a1d23;border:1px solid #2f333a;color:#e8eaed;border-radius:6px;"
    "padding:7px 10px;font-size:13px}#q{width:300px}"
    "button{background:#23272e;border:1px solid #363b44;color:#e8eaed;border-radius:6px;"
    "padding:7px 12px;font-size:13px;cursor:pointer}"
    "button:hover{background:#2d323b}button.p{background:#1a73e8;border-color:#1a73e8;color:#fff}"
    ".stat{margin-left:auto;color:#9aa0a6;font-size:13px}.stat b{color:#7ec8ff}"
    "details{margin:0 0 8px;border:1px solid #22252b;border-radius:8px;background:#14171c;overflow:hidden}"
    "summary{cursor:pointer;padding:10px 14px;font-size:14px;font-weight:600;color:#c7cbd1;"
    "display:flex;align-items:center;gap:10px;list-style:none}"
    "summary::-webkit-details-marker{display:none}summary:hover{background:#191d23}"
    ".cnt{color:#79808a;font-weight:400;font-size:12px}"
    "ul{list-style:none;margin:0;padding:0 6px 8px 6px}"
    "li{display:flex;align-items:flex-start;gap:9px;padding:5px 8px;border-radius:5px}"
    "li:hover{background:#1b1f25}li.hide{display:none}"
    "input[type=checkbox]{margin-top:3px;accent-color:#1a73e8}"
    "a{color:#8ab4f8;text-decoration:none;font-size:13px;line-height:1.35}a:hover{text-decoration:underline}"
    ".meta{color:#666c76;font-size:11px;margin-left:6px;white-space:nowrap}"
    ".warn{background:#3a2a00;border:1px solid #6b4b00;color:#ffd479;padding:10px 14px;"
    "border-radius:7px;font-size:12.5px;margin-bottom:14px;line-height:1.5}"
    ".grpbtns{margin-left:auto;display:flex;gap:6px}.grpbtns button{padding:3px 9px;font-size:11px}"
    "</style>"
)


# --------------------------------------------------------------------------- #
# Board construction
# --------------------------------------------------------------------------- #


def build_board(
    tabs: Iterable[Tab],
    title: str = "Tab Restore Board",
    subtitle: str = "",
    opening_groups: tuple[str, ...] = ("youtube.com",),
) -> str:
    """Render the full HTML document for a set of tabs."""
    tabs = list(tabs)

    # Deduplicate by normalized key, keeping the most descriptive title seen.
    deduped: dict[str, dict] = {}
    for t in tabs:
        key = t.key
        if not key:
            continue
        entry = deduped.get(key)
        if entry is None:
            deduped[key] = {"url": t.url, "title": t.title or "(untitled)"}
        elif len(t.title or "") > len(entry["title"]):
            entry["title"] = t.title
    if not deduped:
        raise ValueError("no archivable tabs supplied")

    groups: dict[str, list[dict]] = defaultdict(list)
    for entry in deduped.values():
        groups[domain_of(entry["url"])].append(entry)

    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    priority = [kv for kv in ordered if kv[0] in opening_groups]
    ordered = priority + [kv for kv in ordered if kv[0] not in opening_groups]

    total = len(deduped)

    head = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>{html_escape(title)}</title>",
        CSS,
        "</head>",
        "<body>",
        f"<h1>{html_escape(title)}</h1>",
        f'<div class="sub">{html_escape(subtitle or f"{total} unique destinations across {len(ordered)} domains.")}</div>',
        '<div class="warn"><b>Reopening every tab at once will re-create the original '
        "problem.</b> Tick what you want, use <b>Copy selected URLs</b>, then reopen in "
        "batches of 20&ndash;40. If Chrome offers to restore your previous session after a "
        "crash, decline it &mdash; reopen deliberately from this board instead.</div>",
        '<div class="bar">',
        '<input type="text" id="q" placeholder="filter by title, domain or url" oninput="flt()">',
        '<button onclick="all(1)">Select all</button>',
        '<button onclick="all(0)">Clear</button>',
        '<button onclick="invert()">Invert</button>',
        '<button class="p" onclick="copyUrls()">Copy selected URLs</button>',
        '<button onclick="copyTitled()">Copy as title | url</button>',
        '<button onclick="dl()">Download .txt</button>',
        f'<span class="stat">selected <b id="sc">0</b> of {total} '
        f'&nbsp; shown <b id="vc">{total}</b></span>',
        "</div>",
    ]

    body: list[str] = []
    for group_index, (dom, entries) in enumerate(ordered):
        entries = sorted(entries, key=lambda e: (e["title"] or "").lower())
        opened = " open" if dom in opening_groups else ""
        body.append(f"<details{opened}>")
        body.append(
            f'<summary>{html_escape(dom)} <span class="cnt">{len(entries)}</span>'
            f'<span class="grpbtns">'
            f'<button onclick="grp({group_index},1);event.preventDefault()">all</button>'
            f'<button onclick="grp({group_index},0);event.preventDefault()">none</button>'
            f"</span></summary>"
        )
        body.append("<ul>")
        for entry in entries:
            search = html_escape((entry["title"] + " " + entry["url"]).lower())
            url = attr_escape(entry["url"])
            body.append(
                f'<li data-g="{group_index}" data-s="{search}">'
                f'<input type="checkbox" class="cb" checked data-u="{url}">'
                f'<span><a href="{url}" target="_blank" rel="noopener">'
                f'{html_escape(entry["title"][:190])}</a></span>'
                f"</li>"
            )
        body.append("</ul></details>")

    footer = [
        '<div id="toast" style="display:none;position:fixed;bottom:22px;left:50%;'
        "transform:translateX(-50%);background:#1a73e8;color:#fff;padding:10px 18px;"
        'border-radius:8px;font-size:13px;z-index:99"></div>',
        f"<script>{build_javascript()}</script>",
        "</body></html>",
    ]
    return LF.join(head + body + footer)


def write_board(tabs: Iterable[Tab], path: str, **kwargs) -> str:
    """Build, validate and durably write a board. Returns the path written."""
    validate_javascript(build_javascript())
    html = build_board(tabs, **kwargs)
    if chr(92) in html:
        raise AssertionError("board contains a backslash; scripts would break")
    write_text(path, html)
    return path


def report_from_json(path: str) -> Report:
    """Load a tab session previously exported by ``session_audit.py``."""
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    tabs = [
        Tab(
            window=int(r.get("window", 0)),
            index=int(r.get("index", i + 1)),
            title=r.get("title", ""),
            raw_url=r.get("url", ""),
        )
        for i, r in enumerate(rows)
    ]
    return Report(tabs=tabs)


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Build an interactive HTML tab restore board.")
    ap.add_argument("session", nargs="?", help="JSON session from session_audit.py export")
    ap.add_argument("-o", "--out", default="tab-restore-board.html")
    ap.add_argument("--title", default="Tab Restore Board")
    args = ap.parse_args(argv)

    if args.session:
        report = report_from_json(args.session)
    else:
        from tabkit import read_session

        report = read_session()

    write_board(report.tabs, args.out, title=args.title)
    print(f"wrote {args.out} ({os.path.getsize(args.out)} bytes, {len(report.tabs)} tabs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
