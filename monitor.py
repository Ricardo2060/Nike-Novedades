#!/usr/bin/env python3
"""Monitor de noticias oficiales de Nike → muro web (+ email opcional)."""
import argparse, datetime as dt, html, json, os, re, shutil, smtplib, ssl, subprocess, sys
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser, requests, yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
UA = {"User-Agent": "Mozilla/5.0 (compatible; NikeNewsMonitor/1.0)"}
TIMEOUT = 25
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}
_M = "|".join(MONTHS)
DATE_RES = [re.compile(rf"\b({_M})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I),   # November 19, 2026
            re.compile(rf"\b(\d{{1,2}})\s+({_M})\s+(\d{{4}})\b", re.I)]     # 19 November 2026


# ───────────── Fechas y títulos ─────────────
def find_date(text):
    """Devuelve (fecha ISO, posición en el texto) de la primera fecha escrita que encuentre."""
    m = DATE_RES[0].search(text)
    if m:
        mon, day, yr = m.group(1), m.group(2), m.group(3)
    else:
        m = DATE_RES[1].search(text)
        if not m:
            return None, None
        day, mon, yr = m.group(1), m.group(2), m.group(3)
    try:
        return dt.date(int(yr), MONTHS[mon.lower()], int(day)).isoformat(), m.span()
    except ValueError:
        return None, None


def clean_title(text, span=None):
    """Quita la fecha, el 'Read the Release ›' y textos de imagen repetidos."""
    if span:
        before, after = text[:span[0]].strip(), text[span[1]:].strip()
        text = after if len(after) >= 12 else before
    text = re.sub(r"read the release\s*[›>]*", "", text, flags=re.I)
    return re.sub(r"[\s›]+$", "", re.sub(r"\s+", " ", text)).strip()


# ───────────── Obtención de fuentes ─────────────
def get(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def platform_of(src):
    return src.get("platform") or {"page": "Web", "youtube": "YouTube"}.get(src["type"], "Otros")


def item(src, title, link, summary="", image="", date=""):
    return {"source": src["name"], "platform": platform_of(src), "priority": src.get("priority", 9),
            "image": image, "date": date or dt.date.today().isoformat(), "date_real": bool(date),
            "title": re.sub(r"\s+", " ", title).strip(), "link": link.strip(),
            "summary": BeautifulSoup(summary or "", "html.parser").get_text(" ", strip=True)[:300]}


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
    try:  # UULF + id sin "UC" = lista solo de vídeos normales (sin Shorts ni directos)
        items = fetch_rss(src, base + f"playlist_id=UULF{cid[2:]}")
        if items:
            return [i for i in items if "/shorts/" not in i["link"]]
    except Exception:
        pass
    return [i for i in fetch_rss(src, base + f"channel_id={cid}") if "/shorts/" not in i["link"]]


_BROWSER_READY = False


def render_page(src):
    """Abre la página con un navegador real (para webs que cargan la lista con JavaScript)."""
    global _BROWSER_READY
    from playwright.sync_api import sync_playwright
    if not _BROWSER_READY:
        subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
                       check=True, timeout=600, stdout=subprocess.DEVNULL)
        _BROWSER_READY = True
    marker = src.get("wait_for", "")
    sel = f'a[href*="{marker}"]' if marker else "a"
    more_btn = re.compile(r"load more|show more|view more|see more|cargar más|ver más", re.I)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=UA["User-Agent"], locale="en-US")
            page.goto(src["url"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector(sel, timeout=30000)
            last = -1
            for _ in range(src.get("max_loads", 10)):   # pulsa "ver más" / baja hasta que no salgan más
                page.wait_for_timeout(1500)
                n = page.locator(sel).count()
                if n == last:
                    break
                last = n
                btn = page.get_by_role("button", name=more_btn)
                if btn.count():
                    try:
                        btn.first.click(timeout=3000)
                    except Exception:
                        pass
                page.mouse.wheel(0, 30000)
            return page.content()
        finally:
            browser.close()


def filter_age(src, items):
    max_age = src.get("max_age_days")
    if not max_age:
        return items
    limit = (dt.date.today() - dt.timedelta(days=max_age)).isoformat()
    return [i for i in items if not i["date_real"] or i["date"] >= limit]


def article_info(link):
    """Abre un artículo y devuelve (título limpio, fecha ISO, imagen)."""
    soup = BeautifulSoup(get(link).content.decode("utf-8", errors="replace"), "html.parser")

    def meta(p):
        tag = soup.find("meta", attrs={"property": p}) or soup.find("meta", attrs={"name": p})
        return (tag.get("content") or "").strip() if tag else ""

    title = meta("og:title") or (soup.h1.get_text(" ", strip=True) if soup.h1 else "")
    title = re.sub(r"\s+[—|-]\s+NIKE,? Inc\.?$", "", title, flags=re.I).strip()
    pub = meta("article:published_time") or meta("datePublished")
    date = pub[:10] if re.match(r"\d{4}-\d{2}-\d{2}", pub) else ""
    if not date:   # la fecha de publicación aparece justo debajo del título
        date = find_date(soup.get_text(" ", strip=True)[:4000])[0] or ""
    return title, date, meta("og:image")


def parse_page_html(src, text):
    soup = BeautifulSoup(text, "html.parser")
    pat = re.compile(src["link_pattern"])
    clean = lambda a: urljoin(src["url"], a["href"]).split("#")[0].split("?")[0]
    n_links = lambda node: len({clean(x) for x in node.find_all("a", href=True) if pat.search(clean(x))})
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        link = clean(a)
        if not pat.search(link) or link.rstrip("/") == src["url"].rstrip("/") or link in seen:
            continue
        raw = a.get("aria-label") or a.get_text(" ", strip=True)
        date, span = find_date(raw)
        if not date:            # la fecha puede estar en el contenedor, si solo contiene este enlace
            node = a
            for _ in range(3):
                node = node.parent
                if node is None or n_links(node) > 1:
                    break
                date = find_date(node.get_text(" ", strip=True))[0]
                if date:
                    break
        title = clean_title(raw, span)
        if len(title) < 12:     # descarta "Read the Release ›", iconos, etc.
            if not src.get("enrich"):
                continue
            title = urlparse(link).path.rstrip("/").split("/")[-1].replace("-", " ").title()
        seen.add(link)
        img = a.find("img")
        src_img = (img.get("src") or img.get("data-src") or "") if img else ""
        out.append(item(src, title, link, image=urljoin(src["url"], src_img) if src_img else "", date=date or ""))
    return filter_age(src, out)


def fetch_page(src):
    texts = []
    if src.get("render"):
        try:
            texts.append(render_page(src))
        except Exception as ex:
            print(f"  ! {src['name']}: sin navegador ({str(ex)[:90]}); se usa la página sin cargar JavaScript")
    texts.append(get(src["url"]).content.decode("utf-8", errors="replace"))       # la versión sin JavaScript siempre se suma
    out, seen = [], set()
    for t in texts:
        for i in parse_page_html(src, t):
            if i["link"] not in seen:
                seen.add(i["link"])
                out.append(i)
    if not out:
        raise ValueError("0 enlaces encontrados (¿página cargada por JavaScript o bloqueada?)")
    if src.get("enrich") == "article":
        seen_links, kept, opened = src.get("_seen", {}), [], 0
        for i in out:
            if i["link"] in seen_links:
                kept.append(i)
                continue
            if opened >= src.get("enrich_limit", 40):
                continue
            opened += 1
            try:
                title, date, img = article_info(i["link"])
            except Exception:
                continue
            if not date:        # sin fecha de publicación no es un artículo (o no se puede ordenar): se descarta
                continue
            i.update(date=date, date_real=True, title=title or i["title"], image=img or i["image"])
            kept.append(i)
        out = filter_age(src, kept)
        if not out:
            raise ValueError("ningún artículo con fecha válida en el último mes (¿cambió la web?)")
    return sorted(out, key=lambda i: i["date"], reverse=True)


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
    """Quita repeticiones: se queda con la versión que no es Short, de fuente más prioritaria y título más completo."""
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
        for it in sorted(groups[cat], key=lambda i: (i["date"], -i["priority"]), reverse=True):
            parts.append(f'<li><a href="{e(it["link"])}">{e(it["title"])}</a> '
                         f'<small style="color:#777">— {e(it["source"])} · {e(it["date"])}</small></li>')
        parts.append("</ul>")
    if errors:
        parts.append("<hr><p><b>Fuentes con problemas</b> (revisar):</p><ul>" +
                     "".join(f"<li>{e(n)}: {e(m)}</li>" for n, m in errors) + "</ul>")
    return "<html><body style='font-family:sans-serif;max-width:720px'>" + "".join(parts) + "</body></html>"


# ───────────── Muro web ─────────────
WALL_CSS = """
:root{--bg:#fff;--fg:#111;--card:#f5f5f5;--mut:#6b6b6b;--acc:#111;--hot:#FA5400;--line:#d9d9d9}
@media (prefers-color-scheme:dark){:root{--bg:#000;--fg:#fff;--card:#151515;--mut:#9a9a9a;--acc:#fff;--line:#2a2a2a}}
*{box-sizing:border-box}body{margin:0;font-family:"Helvetica Neue",Helvetica,Arial,sans-serif;background:var(--bg);color:var(--fg)}
h1,h2,.wm,.store,.bar button,.cat{font-family:"Futura Condensed ExtraBold","Futura Condensed Medium","Helvetica Neue Condensed Bold","Arial Narrow Bold","Roboto Condensed",Impact,"Arial Narrow",sans-serif;text-transform:uppercase;letter-spacing:.04em}
.top{background:#000;color:#fff;border-bottom:4px solid var(--hot)}
.top .wrap{max-width:1200px;margin:auto;padding:18px 20px 10px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.top img.logo{height:34px;width:auto;display:block}.top img.logo.inv{filter:invert(1) brightness(2)}.wm{font-size:2rem;font-weight:900;font-style:italic;line-height:1}
.top h1{margin:0;font-size:1.7rem;font-weight:900;line-height:1}
.store{background:var(--hot);color:#000;font-weight:900;padding:5px 10px;font-size:1rem;line-height:1;margin-left:auto}
.sub{max-width:1200px;margin:auto;padding:0 20px 14px;color:#bdbdbd;font-size:.85rem}
.bar{display:flex;flex-wrap:wrap;gap:8px;padding:10px 20px 0;max-width:1200px;margin:auto}
.bar button{border:2px solid var(--fg);background:none;color:var(--fg);padding:6px 14px;border-radius:0;cursor:pointer;font-size:.9rem}
.bar button:hover{border-color:var(--hot);color:var(--hot)}.bar button.on{background:var(--acc);color:var(--bg);border-color:var(--acc)}
section{max-width:1200px;margin:16px auto 0;padding:0 20px}section h2{font-size:1.5rem;margin:16px 0 4px;font-weight:900}
section h2 small{font-family:"Helvetica Neue",Arial,sans-serif;text-transform:none;letter-spacing:0;color:var(--mut);font-weight:400;font-size:.85rem;margin-left:8px}
#recent{border:3px solid var(--hot);padding:4px 18px 8px;margin:18px 20px 0;max-width:1160px}#recent h2{color:var(--hot)}
@media (min-width:1240px){#recent{margin:18px auto 0}}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px;padding:12px 0 24px}
.card{background:var(--card);border-radius:0;overflow:hidden;text-decoration:none;color:inherit;display:flex;flex-direction:column;border-bottom:3px solid transparent;transition:border-color .15s,transform .15s}
.card:hover{border-bottom-color:var(--hot);transform:translateY(-2px)}
.card img{width:100%;aspect-ratio:16/9;object-fit:cover;background:#888}.card .t{padding:12px 14px 4px;font-weight:700;line-height:1.3}
.card .m{padding:0 14px 12px;color:var(--mut);font-size:.8rem;margin-top:auto}.card.noimg .t{padding-top:16px;font-size:1.05rem}
.cat{color:var(--hot);font-weight:900;font-size:.85rem}.card .dt.new{color:var(--fg);font-weight:700}
.empty{color:var(--mut);padding:8px 0 16px}.hide{display:none!important}
"""
WALL_JS = """
const iso=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
const now=new Date(), today=iso(now), yest=iso(new Date(now.getTime()-864e5));
const f={p:'',c:''}, gR=document.getElementById('g-recent'), gO=document.getElementById('g-older');
[...gO.querySelectorAll('.card')].forEach(el=>{      // lo de hoy y ayer sube a la zona destacada (mismo orden)
 const s=el.querySelector('.dt');
 if(el.dataset.d===today){s.textContent='Hoy';s.classList.add('new')}
 else if(el.dataset.d===yest){s.textContent='Ayer';s.classList.add('new')}
 if(el.dataset.d>=yest)gR.appendChild(el)});
function apply(){document.querySelectorAll('.card').forEach(el=>{
 el.classList.toggle('hide',!((!f.p||el.dataset.p===f.p)&&(!f.c||el.dataset.c===f.c)))});
 const vis=g=>g.querySelectorAll('.card:not(.hide)').length;
 document.getElementById('none-recent').classList.toggle('hide',vis(gR)>0);
 document.getElementById('older').classList.toggle('hide',vis(gO)===0)}
document.querySelectorAll('.bar button').forEach(b=>b.onclick=()=>{
 const k=b.dataset.k;f[k]=b.dataset.v;
 document.querySelectorAll('.bar button[data-k="'+k+'"]').forEach(x=>x.classList.toggle('on',x===b));apply()});
apply();
"""


def render_wall(items, today, days=30, brand=None, logo=None, icon=None):
    e = html.escape
    brand = brand or {}
    title = brand.get("title", "Novedades oficiales")
    store = brand.get("store", "")
    accent = str(brand.get("accent", ""))
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
            f'data-p="{e(i["platform"])}" data-c="{e(i["category"])}" data-d="{e(i["date"])}">{img}'
            f'<div class="t">{e(i["title"])}</div>'
            f'<div class="m"><span class="cat">{e(i["category"])}</span> · {e(i["source"])} · '
            f'<span class="dt">{e(i["date"])}</span></div></a>')
    inv = " inv" if brand.get("logo_invert") else ""
    mark = (f'<img class="logo{inv}" src="{e(logo)}" alt="Nike">' if logo else '<span class="wm">Nike</span>')
    store_tag = f'<span class="store">{e(store)}</span>' if store else ""
    accent_css = f"<style>:root{{--hot:{accent}}}</style>" if re.fullmatch(r"#[0-9a-fA-F]{3,8}", accent) else ""
    icon = icon or logo
    icon = f'<link rel="icon" href="{e(icon)}">' if icon else ""
    return ("<!doctype html><html lang='es'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{e(title)}{' · ' + e(store) if store else ''}</title>{icon}<style>{WALL_CSS}</style>{accent_css}</head><body>"
            f"<header class='top'><div class='wrap'>{mark}<h1>{e(title)}</h1>{store_tag}</div>"
            f"<div class='sub'>Más recientes arriba · últimos {days} días · actualizado el {today}</div></header>"
            f"<div class='bar'>{btns('p', plats)}</div><div class='bar'>{btns('c', cats)}</div>"
            "<section id='recent'><h2>● Lo más reciente <small>hoy y ayer</small></h2>"
            "<div class='grid' id='g-recent'></div>"
            "<p class='empty hide' id='none-recent'>Sin novedades de hoy ni de ayer.</p></section>"
            f"<section id='older'><h2>Anteriores <small>de más reciente a más antigua · hasta {days} días</small></h2>"
            f"<div class='grid' id='g-older'>{''.join(cards)}</div></section>"
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
    hist_path = state_path.with_name("items.json")
    history = json.loads(hist_path.read_text()) if hist_path.exists() else []
    by_link = {i["link"]: i for i in history}
    today = dt.date.today()
    cap_default = cfg.get("max_items_per_source", 10)
    wall_days = cfg.get("wall_days", 30)
    age_limit = (today - dt.timedelta(days=wall_days)).isoformat()

    groups, errors, new_ids = defaultdict(list), [], {}
    for src in sorted(cfg["sources"], key=lambda s: s.get("priority", 9)):
        if not enabled(src):
            continue
        src.setdefault("include_shorts", cfg.get("include_shorts", False))
        src["_seen"] = state
        try:
            items = FETCHERS[src["type"]](src)
        except Exception as ex:  # una fuente rota no tumba las demás
            errors.append((src["name"], str(ex)[:160]))
            print(f"✗ {src['name']}: {ex}")
            continue
        items = [i for i in items if i["date"] >= age_limit]       # nada con más de wall_days días
        fixed = 0   # corrige fecha/título de lo ya guardado si ahora tenemos la fecha real
        for i in items:
            old = by_link.get(i["link"])
            if old and old.get("platform") != i["platform"]:
                old["platform"] = i["platform"]
            if old and i["date_real"] and (old.get("date") != i["date"] or old.get("title") != i["title"]):
                old["date"], old["title"] = i["date"], i["title"]
                fixed += 1
        fresh = [i for i in items if i["link"] not in state and i["link"] not in new_ids]
        fresh.sort(key=lambda i: i["date"], reverse=True)
        fresh = fresh[:src.get("max_items", cap_default)]
        print(f"✓ {src['name']}: {len(items)} leídos, {len(fresh)} nuevos" + (f", {fixed} corregidos" if fixed else ""))
        if src["type"] == "page":       # diagnóstico: qué ha encontrado en la web
            for i in items[:6]:
                print(f"     {i['date']}  {i['title'][:75]}")
        for i in fresh:
            new_ids[i["link"]] = today.isoformat()
            groups[categorize(i, cfg["categories"])].append(i)

    out = Path(args.out); (out / "site").mkdir(parents=True, exist_ok=True)
    for cat, lst in groups.items():
        for i in lst:
            history.append({**i, "category": cat})
    history = dedupe([i for i in history if i["date"] >= age_limit])
    history.sort(key=lambda i: (i["date"], -i["priority"]), reverse=True)
    history = history[:cfg.get("wall_items", 300)]
    logo_file = next((p for ext in ("svg", "png", "webp", "jpg", "jpeg") for p in [ROOT / f"logo.{ext}"] if p.exists()), None)
    if logo_file:
        shutil.copy(logo_file, out / "site" / logo_file.name)
    icon_file = next((p for ext in ("png", "svg", "ico") for p in [ROOT / f"favicon.{ext}"] if p.exists()), None)
    if icon_file:
        shutil.copy(icon_file, out / "site" / icon_file.name)
    (out / "site" / "index.html").write_text(
        render_wall(history, today.isoformat(), wall_days, cfg.get("branding"), logo_file.name if logo_file else None,
                    icon_file.name if icon_file else None),
        encoding="utf-8")
    print(f"→ {out/'site'/'index.html'} ({len(history)} tarjetas)")
    if not args.dry_run:
        hist_path.write_text(json.dumps(history, ensure_ascii=False, indent=1))

    if groups or errors:
        page = render(groups, errors, [c["name"] for c in cfg["categories"]], today.isoformat())
        (out / "digest.html").write_text(page, encoding="utf-8")
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
