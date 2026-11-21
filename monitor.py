#!/usr/bin/env python3
"""Monitor de noticias oficiales de Nike → resumen HTML (+ email opcional)."""
import argparse, datetime as dt, html, json, os, re, smtplib, ssl, sys
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin

import feedparser, requests, yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
UA = {"User-Agent": "Mozilla/5.0 (compatible; NikeNewsMonitor/1.0)"}
TIMEOUT = 25


# ───────────── Obtención de fuentes ─────────────
def get(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def platform_of(src):
    return src.get("platform") or {"page": "Web", "youtube": "YouTube"}.get(src["type"], "Otros")


def item(src, title, link, summary="", published="", image="", date=""):
    return {"source": src["name"], "platform": platform_of(src), "priority": src.get("priority", 9),
            "image": image, "date": date or dt.date.today().isoformat(),
            "title": re.sub(r"\s+", " ", title).strip(), "link": link.strip(),
            "summary": BeautifulSoup(summary or "", "html.parser").get_text(" ", strip=True)[:300],
            "published": published}


def fetch_rss(src, url=None):
    feed = feedparser.parse(get(url or src["url"]).content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"feed ilegible: {feed.bozo_exception}")
    out = []
    for e in feed.entries:
        if not e.get("link"):
            continue
        img = (e.get("media_thumbnail") or [{}])[0].get("url", "")
        if not img:
            img = next((l.get("href", "") for l in e.get("links", []) if l.get("type", "").startswith("image")), "")
        p = e.get("published_parsed") or e.get("updated_parsed")
        date = dt.date(*p[:3]).isoformat() if p else ""
        out.append(item(src, e.get("title", ""), e["link"], e.get("summary", ""), image=img, date=date))
    return out


def fetch_youtube(src):
    cid = src.get("channel_id")
    if not cid:
        page = get(f"https://www.youtube.com/@{src['handle'].lstrip('@')}").text
        m = re.search(r'"channelId":"(UC[\w-]{22})"', page) or re.search(r'channel_id=(UC[\w-]{22})', page) \
            or re.search(r'youtube\.com/channel/(UC[\w-]{22})', page)
        if not m:
            raise ValueError(f"no se pudo resolver el canal @{src['handle']}")
        cid = m.group(1)
    base = "https://www.youtube.com/feeds/videos.xml?"
    if src.get("include_shorts"):
        return fetch_rss(src, base + f"channel_id={cid}")
    # UULF + id sin "UC" = lista solo de vídeos normales (sin Shorts ni directos)
    try:
        items = fetch_rss(src, base + f"playlist_id=UULF{cid[2:]}")
        if items:
            return [i for i in items if "/shorts/" not in i["link"]]
    except Exception:
        pass
    # respaldo: feed completo del canal quitando los enlaces /shorts/
    return [i for i in fetch_rss(src, base + f"channel_id={cid}") if "/shorts/" not in i["link"]]


def fetch_page(src):
    soup = BeautifulSoup(get(src["url"]).text, "html.parser")
    pat = re.compile(src["link_pattern"])
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        link = urljoin(src["url"], a["href"]).split("#")[0].split("?")[0]
        title = a.get("aria-label") or a.get_text(" ", strip=True)
        if not pat.search(link) or link.rstrip("/") == src["url"].rstrip("/") or link in seen:
            continue
        if len(title) < 12:        # descarta "Read the Release ›", iconos, etc.
            continue
        seen.add(link)
        img = a.find("img")
        img_url = urljoin(src["url"], img.get("src", "")) if img and img.get("src") else ""
        out.append(item(src, title, link, image=img_url))
    if not out:
        raise ValueError("0 enlaces encontrados (¿página cargada por JavaScript o bloqueada?)")
    return out


FETCHERS = {"rss": fetch_rss, "youtube": fetch_youtube, "page": fetch_page}


def enabled(src):
    if src.get("enabled", True) is False:
        return False
    return "PEGA_" not in str(src.get("url", ""))


# ───────────── Antiduplicados ─────────────
def _norm(t):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", t.lower())).strip()


def _is_dup(a, b, days=7):
    try:
        gap = abs((dt.date.fromisoformat(a["date"]) - dt.date.fromisoformat(b["date"])).days)
    except ValueError:
        gap = 0
    if gap > days:
        return False
    na, nb = _norm(a["title"]), _norm(b["title"])
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    return len(short) >= 12 and short in long_      # un título contenido en el otro


def dedupe(items):
    """Quita repeticiones (mismo vídeo/noticia con título igual o muy parecido).
    Se queda con la versión que no es un Short, de fuente más prioritaria y título más completo."""
    ranked = sorted(items, key=lambda i: ("/shorts/" in i["link"], i["priority"], -len(i["title"])))
    kept = []
    for it in ranked:
        if not any(_is_dup(it, k) for k in kept):
            kept.append(it)
    return kept


# ───────────── Estado y clasificación ─────────────
def load_state(path):
    return json.loads(path.read_text()) if path.exists() else {}


def categorize(it, cats):
    text = f" {it['title']} {it['summary']} ".lower()
    for c in cats:
        if any(k.lower() in text for k in c["keywords"]):
            return c["name"]
    return "General"


# ───────────── Salida ─────────────
def render(groups, errors, order, today):
    e = html.escape
    parts = [f"<h2>Noticias oficiales de Nike · {today}</h2>"]
    total = sum(len(v) for v in groups.values())
    parts.append(f"<p>{total} novedades.</p>")
    for cat in order + [c for c in groups if c not in order]:
        if cat not in groups:
            continue
        parts.append(f"<h3>{e(cat)}</h3><ul>")
        for it in sorted(groups[cat], key=lambda i: i["priority"]):
            parts.append(f'<li><a href="{e(it["link"])}">{e(it["title"])}</a> '
                         f'<small style="color:#777">— {e(it["source"])}</small></li>')
        parts.append("</ul>")
    if errors:
        parts.append("<hr><p><b>Fuentes con problemas</b> (revisar):</p><ul>" +
                     "".join(f"<li>{e(n)}: {e(m)}</li>" for n, m in errors) + "</ul>")
    return "<html><body style='font-family:sans-serif;max-width:720px'>" + "".join(parts) + "</body></html>"


# ───────────── Muro web ─────────────
WALL_CSS = """
:root{--bg:#fff;--fg:#111;--card:#f4f4f4;--mut:#666;--acc:#111}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#f2f2f2;--card:#1d1d1d;--mut:#999;--acc:#f2f2f2}}
*{box-sizing:border-box}body{margin:0;font-family:system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:24px 20px 8px;max-width:1200px;margin:auto}h1{margin:0 0 4px;font-size:1.5rem}
.sub{color:var(--mut);font-size:.9rem}.bar{display:flex;flex-wrap:wrap;gap:8px;padding:8px 20px;max-width:1200px;margin:auto}
.bar button{border:1px solid var(--mut);background:none;color:var(--fg);padding:6px 12px;border-radius:99px;cursor:pointer;font-size:.85rem}
.bar button.on{background:var(--acc);color:var(--bg);border-color:var(--acc)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px;padding:12px 20px 40px;max-width:1200px;margin:auto}
.card{background:var(--card);border-radius:12px;overflow:hidden;text-decoration:none;color:inherit;display:flex;flex-direction:column}
.card img{width:100%;aspect-ratio:16/9;object-fit:cover;background:#888}.card .t{padding:12px 14px 4px;font-weight:600;line-height:1.3}
.card .m{padding:0 14px 12px;color:var(--mut);font-size:.8rem;margin-top:auto}.card.noimg .t{padding-top:16px;font-size:1.05rem}
.hide{display:none}
"""
WALL_JS = """
const f={p:'',c:''};
function apply(){document.querySelectorAll('.card').forEach(el=>{
 const ok=(!f.p||el.dataset.p===f.p)&&(!f.c||el.dataset.c===f.c);el.classList.toggle('hide',!ok)})}
document.querySelectorAll('.bar button').forEach(b=>b.onclick=()=>{
 const k=b.dataset.k;f[k]=b.dataset.v;
 document.querySelectorAll('.bar button[data-k="'+k+'"]').forEach(x=>x.classList.toggle('on',x===b));apply()});
"""


def render_wall(items, today):
    e = html.escape
    plats = sorted({i["platform"] for i in items})
    cats = sorted({i["category"] for i in items})

    def btns(key, values):
        return (f'<button class="on" data-k="{key}" data-v="">Todo</button>' +
                "".join(f'<button data-k="{key}" data-v="{e(v)}">{e(v)}</button>' for v in values))

    cards = []
    for i in items:
        img = f'<img loading="lazy" src="{e(i["image"])}" alt="">' if i.get("image") else ""
        cards.append(
            f'<a class="card{"" if img else " noimg"}" href="{e(i["link"])}" target="_blank" rel="noopener" '
            f'data-p="{e(i["platform"])}" data-c="{e(i["category"])}">{img}'
            f'<div class="t">{e(i["title"])}</div>'
            f'<div class="m">{e(i["source"])} · {e(i["date"])} · {e(i["category"])}</div></a>')
    return ("<!doctype html><html lang='es'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Nike · Novedades oficiales</title><style>{WALL_CSS}</style></head><body>"
            f"<header><h1>Nike · Novedades oficiales</h1><div class='sub'>Actualizado el {today}</div></header>"
            f"<div class='bar'>{btns('p', plats)}</div><div class='bar'>{btns('c', cats)}</div>"
            f"<div class='grid'>{''.join(cards) or '<p>Aún no hay publicaciones.</p>'}</div>"
            f"<script>{WALL_JS}</script></body></html>")


def send_email(subject, body_html):
    env = {k: os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO")}
    if not all(env.values()):
        print("· Email no configurado (faltan variables SMTP_*/MAIL_TO); solo se genera el HTML.")
        return
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["To"] = subject, env["MAIL_TO"]
    msg["From"] = os.environ.get("MAIL_FROM", env["SMTP_USER"])
    msg.attach(MIMEText(re.sub(r"<[^>]+>", "", body_html), "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    if port == 465:
        s = smtplib.SMTP_SSL(env["SMTP_HOST"], port, context=ssl.create_default_context())
    else:
        s = smtplib.SMTP(env["SMTP_HOST"], port)
        s.starttls(context=ssl.create_default_context())
    with s:
        s.login(env["SMTP_USER"], env["SMTP_PASS"])
        s.send_message(msg)
    print(f"· Email enviado a {env['MAIL_TO']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "sources.yaml"))
    ap.add_argument("--state", default=str(ROOT / "seen.json"))
    ap.add_argument("--out", default=str(ROOT / "out"))
    ap.add_argument("--dry-run", action="store_true", help="no guarda estado ni envía email")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    state_path = Path(args.state)
    state = load_state(state_path)
    today = dt.date.today()
    cap = cfg.get("max_items_per_source", 10)

    groups, errors, new_ids = defaultdict(list), [], {}
    for src in sorted(cfg["sources"], key=lambda s: s.get("priority", 9)):
        if not enabled(src):
            continue
        src.setdefault("include_shorts", cfg.get("include_shorts", False))
        try:
            items = FETCHERS[src["type"]](src)
        except Exception as ex:  # una fuente rota no tumba las demás
            errors.append((src["name"], str(ex)[:160]))
            print(f"✗ {src['name']}: {ex}")
            continue
        fresh = [i for i in items if i["link"] not in state and i["link"] not in new_ids][:cap]
        print(f"✓ {src['name']}: {len(items)} leídos, {len(fresh)} nuevos")
        for i in fresh:
            new_ids[i["link"]] = today.isoformat()
            groups[categorize(i, cfg["categories"])].append(i)

    out = Path(args.out); (out / "site").mkdir(parents=True, exist_ok=True)
    hist_path = Path(args.state).with_name("items.json")
    history = json.loads(hist_path.read_text()) if hist_path.exists() else []
    for cat, lst in groups.items():
        for i in lst:
            history.append({**i, "category": cat})
    history = dedupe(history)
    history.sort(key=lambda i: (i["date"], -i["priority"]), reverse=True)
    history = history[:cfg.get("wall_items", 150)]
    (out / "site" / "index.html").write_text(render_wall(history, today.isoformat()), encoding="utf-8")
    print(f"→ {out/'site'/'index.html'} ({len(history)} tarjetas)")
    if not args.dry_run:
        hist_path.write_text(json.dumps(history, ensure_ascii=False, indent=1))
    if groups or errors:
        page = render(groups, errors, [c["name"] for c in cfg["categories"]], today.isoformat())
        (out / "digest.html").write_text(page, encoding="utf-8")
        print(f"→ {out/'digest.html'}")
        if groups and not args.dry_run:
            send_email(f"Nike · {sum(map(len, groups.values()))} novedades ({today})", page)
    else:
        print("Sin novedades.")

    if not args.dry_run:
        cutoff = (today - dt.timedelta(days=cfg.get("keep_days", 180))).isoformat()
        state = {k: v for k, v in {**state, **new_ids}.items() if v >= cutoff}
        state_path.write_text(json.dumps(state, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
