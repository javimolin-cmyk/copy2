#!/usr/bin/env python3
"""
Clonador de sitios con navegador real (Playwright/Chromium).

Guarda en copias/<carpeta>/ el sitio tal como se ve al inspeccionarlo:
DOM ya renderizado por JavaScript, todo el trafico de red (HTML, CSS, JS, imagenes,
fuentes, video, JSON de fetch/XHR...) y las subpaginas. Los enlaces se reescriben a
rutas relativas para que funcione servido desde GitHub Pages.

Se configura por variables de entorno (las rellena el workflow) o por argumentos.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from collections import deque
from datetime import datetime, timezone
from posixpath import dirname, relpath
from urllib import robotparser
from urllib.parse import parse_qs, quote, unquote, urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

MAX_FILE = 50 * 1024 * 1024
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")

PAGE_EXT = {"", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".shtml", ".cfm", ".xhtml"}
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "blob:", "sms:", "about:")
URL_ATTRS = {
    "a": ("href",), "area": ("href",), "link": ("href",), "script": ("src",),
    "img": ("src", "data-src", "data-original", "data-lazy-src"),
    "source": ("src", "data-src"), "video": ("src", "poster"), "audio": ("src",),
    "track": ("src",), "embed": ("src",), "iframe": ("src",), "frame": ("src",),
    "input": ("src",), "object": ("data",),
}
SRCSET_ATTRS = ("srcset", "data-srcset")
CT_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/svg+xml": ".svg", "image/avif": ".avif", "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico", "text/css": ".css", "application/javascript": ".js",
    "text/javascript": ".js", "application/json": ".json", "font/woff2": ".woff2",
    "font/woff": ".woff", "font/ttf": ".ttf", "application/pdf": ".pdf",
    "video/mp4": ".mp4", "video/webm": ".webm", "audio/mpeg": ".mp3",
}
# Palabras que marcan una página de precios (se buscan como palabra completa dentro de la ruta)
PRICE_WORDS = {"pricing", "price", "prices", "precio", "precios", "tarifa", "tarifas", "preise", "tarifs", "prix"}
# Códigos de idioma ISO 639-1 (se omiten los que se confunden con palabras de rutas: my, as, or, to, an, be, so, is)
LANGS = set("""af am ar az bg bn bs ca cs cy da de el en es et eu fa fi fr ga gl gu he hi hr hu hy id it ja ka kk km
kn ko lo lt lv mk ml mn mr ms ne nl no nb nn pa pl pt ro ru si sk sl sq sr sv sw ta te th tl tr uk ur uz vi zh""".split())
LANG_QUERY = ("lang", "hl", "locale", "language", "lng")


def lang_code(s):
    """'es', 'pt-br', 'zh_hans' -> código primario si es un idioma conocido."""
    m = re.fullmatch(r"([a-z]{2})(?:[-_]([a-z]{2,4}))?", s.lower())
    return m.group(1) if m and m.group(1) in LANGS else None


CSS_URL = re.compile(r"""url\(\s*(['"]?)([^'")]+?)\1\s*\)""", re.I)
CSS_IMPORT = re.compile(r"""@import\s+(['"])([^'"]+)\1""", re.I)

# Elementos que se pueden pulsar sin cambiar de pagina ni enviar formularios
MARK_CLICK = """
() => {
  const sel = '[aria-expanded="false"], summary, [role="tab"], [data-toggle], [data-bs-toggle], .accordion-button, button[aria-controls]';
  let i = 0;
  document.querySelectorAll(sel).forEach(el => {
    if (i >= 40) return;
    if (el.disabled) return;
    if ((el.tagName === 'BUTTON' || el.tagName === 'INPUT') && el.type === 'submit' && el.form) return;
    if (el.tagName === 'A') { const h = el.getAttribute('href'); if (h && !h.startsWith('#')) return; }
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    el.setAttribute('data-w2r', String(i++));
  });
  return i;
}
"""
MARK_HOVER = """
() => {
  const sel = '[aria-haspopup], nav li, .menu-item, .dropdown';
  let i = 0;
  document.querySelectorAll(sel).forEach(el => {
    if (i >= 25) return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    el.setAttribute('data-w2r-h', String(i++));
  });
  return i;
}
"""

SHIM_JS = """/* Web2Repo: redirige fetch/XHR GET a las respuestas guardadas en esta copia. */
(function () {
  var M = __MAP__;
  var me = document.currentScript && document.currentScript.src || '';
  var ROOT = me.slice(0, me.lastIndexOf('/_w2r/') + 1);
  var meta = document.querySelector('meta[name="w2r-source"]');
  var SRC = meta ? meta.content : location.href;
  function mapUrl(u) {
    try { var a = new URL(u, SRC); a.hash = ''; if (M[a.href]) return ROOT + M[a.href]; } catch (e) {}
    return u;
  }
  var of = window.fetch;
  if (of) window.fetch = function (i, o) {
    try {
      var get = ((o && o.method) || (i && i.method) || 'GET').toUpperCase() === 'GET';
      if (get) {
        if (typeof i === 'string' || i instanceof URL) i = mapUrl(String(i));
        else if (i && i.url) { var m = mapUrl(i.url); if (m !== i.url) i = new Request(m, i); }
      }
    } catch (e) {}
    return of.call(this, i, o);
  };
  var ox = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u) {
    if (String(m).toUpperCase() === 'GET') arguments[1] = mapUrl(String(u));
    return ox.apply(this, arguments);
  };
})();
"""


# ------------------------------------------------------------------ utilidades
def log(msg):
    print(msg, flush=True)


def skippable(v):
    v = v.strip()
    return (not v) or v.startswith("#") or v.lower().startswith(SKIP_SCHEMES)


def norm(url):
    u, _ = urldefrag(url)
    p = urlparse(u)
    path = p.path
    for idx in ("/index.html", "/index.htm"):
        if path.lower().endswith(idx):
            path = path[: -len(idx) + 1]
            break
    return p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower(), path=path).geturl()


def host_key(netloc):
    h = netloc.lower()
    return h[4:] if h.startswith("www.") else h


def safe_seg(s):
    s = re.sub(r'[<>:"|?*\\\x00-\x1f]', "_", s).rstrip(". ")
    if s in ("", ".", ".."):
        s = "_"
    if len(s) > 100:
        root, ext = os.path.splitext(s)
        s = root[:80] + "_" + hashlib.md5(s.encode()).hexdigest()[:8] + ext[:10]
    return s


def slug(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.")
    return s[:80] or "sitio"


def decode_css(data):
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace")


def walk_css(text, base, h):
    def r_url(m):
        q, u = m.group(1), m.group(2).strip()
        if skippable(u):
            return m.group(0)
        n = h(urljoin(base, u), "asset", u)
        return m.group(0) if n is None else f"url({q}{n}{q})"

    def r_imp(m):
        q, u = m.group(1), m.group(2).strip()
        if skippable(u):
            return m.group(0)
        n = h(urljoin(base, u), "css", u)
        return m.group(0) if n is None else f"@import {q}{n}{q}"

    return CSS_IMPORT.sub(r_imp, CSS_URL.sub(r_url, text))


def walk_html(soup, page_url, h):
    b = soup.find("base", href=True)
    base = urljoin(page_url, b["href"]) if b else page_url
    for tag in soup.find_all(True):
        for a in URL_ATTRS.get(tag.name, ()):
            v = tag.get(a)
            if not isinstance(v, str) or skippable(v):
                continue
            if tag.name in ("a", "area", "iframe", "frame"):
                kind = "link"
            elif tag.name == "link":
                rel = " ".join(tag.get("rel") or []).lower()
                if "stylesheet" in rel:
                    kind = "css"
                elif any(x in rel for x in ("icon", "manifest", "preload", "prefetch", "modulepreload")):
                    kind = "asset"
                else:
                    continue
            else:
                kind = "asset"
            n = h(urljoin(base, v.strip()), kind, v)
            if n is not None:
                tag[a] = n
        for a in SRCSET_ATTRS:
            v = tag.get(a)
            if isinstance(v, str) and v.strip():
                parts = []
                for e in re.split(r",\s+", v.strip()):
                    bits = e.split(None, 1)
                    if not bits:
                        continue
                    u = bits[0]
                    if not skippable(u):
                        n = h(urljoin(base, u), "asset", u)
                        if n is not None:
                            u = n
                    parts.append(u + (" " + bits[1] if len(bits) > 1 else ""))
                tag[a] = ", ".join(parts)
        st = tag.get("style")
        if isinstance(st, str) and "url(" in st:
            tag["style"] = walk_css(st, base, h)
    for s in soup.find_all("style"):
        txt = s.string
        if txt and ("url(" in txt or "@import" in txt):
            s.string = walk_css(str(txt), base, h)


class Resp:
    __slots__ = ("url", "ctype", "body", "rtype")

    def __init__(self, url, ctype, body, rtype):
        self.url, self.ctype, self.body, self.rtype = url, ctype, body, rtype


# ---------------------------------------------------------------------- clonador
class Cloner:
    def __init__(self, a):
        self.a = a
        self.start = norm(a.url)
        self.root_key = host_key(urlparse(self.start).netloc)
        self.root_set = False
        self.carpeta = slug(a.carpeta) if a.carpeta else slug(self.root_key)
        self.out = os.path.join("copias", self.carpeta)
        self.t0 = time.time()
        self.store = {}        # url norm -> Resp
        self.pending = set()   # respuestas parciales (206) a volver a pedir
        self.failed = set()
        self.wanted = set()    # recursos vistos en DOM/CSS
        self.css_scanned = set()
        self.rendered = {}     # url final norm -> html renderizado
        self.alias = {}        # url pedida norm -> url final norm
        self.queue = deque()
        self.seen = set()
        self.pages_q = 0
        self.robots = {}
        self.saved = {}
        self.taken = {}
        self.meta = {}         # ruta local -> (tipo, url base, renderizado?)
        self.omitidos = 0
        self.base_lang = None  # idioma de la copia (el de la página inicial)
        self.excluded = set()  # URLs de otros idiomas declaradas con hreflang
        self.skipped = {}      # URL omitida -> motivo

    # --- filtros: precios, otros idiomas, exclusiones propias
    def lang_of(self, url):
        p = urlparse(url)
        h = host_key(p.netloc)
        if h != self.root_key and h.endswith("." + self.root_key):   # fr.ejemplo.com
            c = lang_code(h[: -len(self.root_key) - 1].split(".")[0])
            if c:
                return c
        seg = [s for s in p.path.split("/") if s]
        if seg:
            c = lang_code(seg[0])                                      # /fr/pagina
            if c:
                return c
        q = parse_qs(p.query)
        for k in LANG_QUERY:                                           # ?lang=fr
            for v in q.get(k, []):
                c = lang_code(v)
                if c:
                    return c
        return None

    def skip_reason(self, n):
        omitir = self.a.omitir_set
        p = urlparse(n)
        path = unquote(p.path).lower()
        if "precios" in omitir and any(t in PRICE_WORDS for t in re.split(r"[^a-z0-9]+", path)):
            return "precios"
        if "idiomas" in omitir:
            if n in self.excluded:
                return "idioma"
            l = self.lang_of(n)
            if l and l != self.base_lang:
                return "idioma"
        full = path + ("?" + p.query.lower() if p.query else "")
        for w in self.a.excluir:
            if w in full:
                return "excluido"
        return None

    def read_alternates(self, soup, final):
        """Las <link rel=alternate hreflang> declaran las versiones de la misma página en otros idiomas."""
        alts = []
        for l in soup.find_all("link", hreflang=True):
            if "alternate" in [x.lower() for x in (l.get("rel") or [])] and l.get("href"):
                hl = l["hreflang"].strip().lower()
                if hl != "x-default":
                    alts.append((hl.split("-")[0], norm(urljoin(final, l["href"]))))
        if self.base_lang is None:
            for code, u in alts:
                if u == final:
                    self.base_lang = code
        if "idiomas" in self.a.omitir_set and self.base_lang:
            for code, u in alts:
                if code != self.base_lang:
                    self.excluded.add(u)

    # --- ayudas
    def time_over(self):
        return (time.time() - self.t0) > self.a.max_minutes * 60

    def same_site(self, netloc):
        h = host_key(netloc)
        return h == self.root_key or (self.a.subdomains and h.endswith("." + self.root_key))

    def robots_ok(self, url):
        if not self.a.robots:
            return True
        p = urlparse(url)
        if not self.same_site(p.netloc):
            return True
        base = f"{p.scheme}://{p.netloc}"
        rp = self.robots.get(base)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                r = requests.get(base + "/robots.txt", headers={"User-Agent": UA}, timeout=10)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except Exception:
                rp.parse([])
            self.robots[base] = rp
        return rp.can_fetch(UA, url)

    def classify(self, url, kind):
        u, _ = urldefrag(url)
        p = urlparse(u)
        if p.scheme not in ("http", "https"):
            return None
        same = self.same_site(p.netloc)
        if kind == "link":
            if not same:
                return None
            ext = os.path.splitext(unquote(p.path))[1].lower()
            k = "page" if ext in PAGE_EXT else "asset"
            if k == "asset" and not self.a.files:
                return None
        else:
            k = "asset"
            if not same and not self.a.external:
                return None
        return norm(u), k

    def note(self, url, kind):
        r = self.classify(url, kind)
        if not r:
            return
        n, k = r
        if kind == "link":
            why = self.skip_reason(n)
            if why:
                self.skipped.setdefault(n, why)
                return
        if k == "page":
            if n not in self.seen and self.pages_q < self.a.max_pages:
                self.seen.add(n)
                self.queue.append(n)
                self.pages_q += 1
        else:
            self.wanted.add(n)

    def discover_handler(self, u, k, raw):
        self.note(u, k)
        return None

    # --- captura de red
    def on_response(self, resp):
        try:
            req = resp.request
            if req.method != "GET" or not resp.url.startswith("http"):
                return
            n = norm(resp.url)
            if n in self.store:
                return
            if resp.status == 206:
                self.pending.add(n)
                return
            if resp.status != 200:
                return
            body = resp.body()
            if len(body) > MAX_FILE:
                self.omitidos += 1
                return
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            self.store[n] = Resp(n, ctype, body, req.resource_type)
        except Exception:
            pass

    # --- interaccion con la pagina
    def scroll_all(self, page):
        y = 0
        for _ in range(80):
            h = page.evaluate("document.documentElement.scrollHeight")
            if y >= h:
                break
            y += 700
            page.evaluate(f"window.scrollTo(0,{y})")
            page.wait_for_timeout(250)
        page.wait_for_timeout(500)
        page.evaluate("window.scrollTo(0,0)")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

    def interact(self, page):
        cur = page.url
        try:
            n = page.evaluate(MARK_CLICK)
            for i in range(n):
                if self.time_over():
                    break
                try:
                    page.locator(f'[data-w2r="{i}"]').first.click(timeout=1500, no_wait_after=True)
                    page.wait_for_timeout(250)
                except Exception:
                    pass
                if page.url != cur:
                    try:
                        page.goto(cur, wait_until="load", timeout=20000)
                    except Exception:
                        pass
                    break
            m = page.evaluate(MARK_HOVER)
            for i in range(m):
                try:
                    page.locator(f'[data-w2r-h="{i}"]').first.hover(timeout=1000)
                    page.wait_for_timeout(150)
                except Exception:
                    pass
            page.keyboard.press("Escape")
            for extra in page.context.pages:
                if extra is not page:
                    try:
                        extra.close()
                    except Exception:
                        pass
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
        except Exception as e:
            log(f"   (acciones interrumpidas: {str(e)[:100]})")

    def collect_links(self, page):
        try:
            return page.eval_on_selector_all(
                "a[href], area[href], iframe[src], frame[src]", "els => els.map(e => e.href || e.src)")
        except Exception:
            return []

    # --- rastreo
    def crawl_page(self, ctx, url):
        page = ctx.new_page()
        page.set_default_timeout(20000)
        page.on("dialog", lambda d: d.dismiss())
        try:
            resp = None
            try:
                resp = page.goto(url, wait_until="networkidle", timeout=45000)
            except PWTimeout:
                log("   (tiempo de espera: se usa lo que alcanzó a cargar)")
            except Exception as e:
                if "Download" in str(e):
                    self.wanted.add(norm(url))
                else:
                    log(f"⚠️ {url}: {str(e)[:120]}")
                return
            if resp is not None and resp.status >= 400:
                log(f"⚠️ HTTP {resp.status}: {url}")
                return
            ct = (resp.headers.get("content-type", "") if resp else "").split(";")[0].lower()
            final = norm(page.url)
            if resp is not None and ct and "html" not in ct:
                self.wanted.add(final)
                return
            if not self.root_set:
                self.root_key = host_key(urlparse(final).netloc)
                self.root_set = True
                self.base_lang = self.lang_of(final)
                if not self.base_lang:
                    try:
                        self.base_lang = lang_code(page.evaluate("document.documentElement.lang || ''")
                                                   .split("-")[0].split("_")[0])
                    except Exception:
                        pass
                log(f"Idioma de la copia: {self.base_lang or 'no detectado'}")
            if self.a.acciones:
                self.scroll_all(page)
            html = page.content()
            self.read_alternates(BeautifulSoup(html, "html.parser"), final)
            links = self.collect_links(page)
            if self.a.acciones:
                self.interact(page)
                links += self.collect_links(page)
            for l in links:
                self.note(l, "link")
            self.rendered[final] = html
            self.alias[norm(url)] = final
            self.seen.add(final)
            walk_html(BeautifulSoup(html, "html.parser"), final, self.discover_handler)
            log(f"✓ [{len(self.rendered)}] {final}")
        finally:
            page.close()

    def scan_css(self):
        for n, r in list(self.store.items()):
            if n in self.css_scanned:
                continue
            if r.ctype == "text/css" or urlparse(n).path.endswith(".css"):
                self.css_scanned.add(n)
                walk_css(decode_css(r.body), n, self.discover_handler)

    def fill_missing(self, ctx):
        for _ in range(5):
            self.scan_css()
            todo = [n for n in (self.wanted | self.pending)
                    if n not in self.store and n not in self.failed and n not in self.rendered]
            if not todo or self.time_over():
                break
            log(f"→ Descargando {len(todo)} recurso(s) que faltaban…")
            for n in todo:
                if self.time_over():
                    break
                try:
                    r = ctx.request.get(n, timeout=30000)
                    if r.status == 200:
                        body = r.body()
                        if len(body) <= MAX_FILE:
                            ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
                            self.store[n] = Resp(n, ct, body, "other")
                            continue
                        self.omitidos += 1
                    self.failed.add(n)
                except Exception:
                    self.failed.add(n)
            self.pending -= set(self.store)

    def run_crawl(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, user_agent=UA,
                                      locale="es-ES", service_workers="block",
                                      ignore_https_errors=True, accept_downloads=False)
            ctx.on("response", self.on_response)
            self.seen.add(self.start)
            self.queue.append(self.start)
            self.pages_q = 1
            done = 0
            while self.queue and not self.time_over():
                url = self.queue.popleft()
                if not self.robots_ok(url):
                    log(f"⛔ robots.txt: {url}")
                    continue
                self.crawl_page(ctx, url)
                done += 1
            if self.queue:
                log(f"⏱ Quedaron {len(self.queue)} página(s) sin visitar (límite de tiempo o de páginas).")
            self.fill_missing(ctx)
            browser.close()
        if not self.rendered:
            raise SystemExit("No se pudo cargar ninguna página del enlace indicado.")

    # --- escritura
    def local_for(self, n, ctype, is_html):
        p = urlparse(n)
        segs = [safe_seg(unquote(s)) for s in p.path.split("/") if s not in ("", ".", "..")]
        trailing = p.path.endswith("/") or not segs
        if is_html:
            if trailing:
                segs.append("index.html")
            else:
                ext = os.path.splitext(segs[-1])[1].lower()
                if ext in (".html", ".htm"):
                    pass
                elif ext == "":
                    segs.append("index.html")
                else:
                    segs[-1] += ".html"
        else:
            if trailing:
                segs.append("index" + CT_EXT.get(ctype, ""))
            elif not os.path.splitext(segs[-1])[1] and ctype in CT_EXT:
                segs[-1] += CT_EXT[ctype]
        if p.query:
            root, ext = os.path.splitext(segs[-1])
            segs[-1] = f"{root}_{hashlib.md5(p.query.encode()).hexdigest()[:8]}{ext}"
        if host_key(p.netloc) != self.root_key:
            segs = ["_ext", safe_seg(p.netloc)] + segs
        path = "/".join(segs)
        owner = self.taken.get(path.lower())
        if owner and owner != n:
            root, ext = os.path.splitext(path)
            path = f"{root}_{hashlib.md5(n.encode()).hexdigest()[:6]}{ext}"
        self.taken[path.lower()] = n
        return path

    def relink(self, url, cur):
        u, frag = urldefrag(url)
        if urlparse(u).scheme not in ("http", "https"):
            return None
        local = self.saved.get(norm(u))
        if local:
            rel = relpath(local, dirname(cur) or ".")
            return quote(rel, safe="/") + ("#" + frag if frag else "")
        return url

    def put(self, local, data):
        path = os.path.join(self.out, *local.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def write_all(self):
        shutil.rmtree(self.out, ignore_errors=True)
        os.makedirs(self.out, exist_ok=True)
        max_bytes = self.a.max_mb * 1024 * 1024
        total, count = 0, 0
        for n, html in self.rendered.items():
            local = self.local_for(n, "text/html", True)
            self.saved[n] = local
            data = html.encode("utf-8")
            self.put(local, data)
            self.meta[local] = ("html", n, True)
            total += len(data)
            count += 1
        for req, fin in self.alias.items():
            if fin in self.saved:
                self.saved[req] = self.saved[fin]
        net = []
        for n, r in self.store.items():
            if n in self.rendered:
                continue
            if count >= self.a.max_files or total + len(r.body) > max_bytes:
                self.omitidos += 1
                net.append({"url": n, "tipo": r.rtype, "mime": r.ctype, "bytes": len(r.body), "archivo": None})
                continue
            is_html = r.ctype in ("text/html", "application/xhtml+xml")
            local = self.local_for(n, r.ctype, is_html)
            self.saved[n] = local
            self.put(local, r.body)
            typ = "html" if is_html else "css" if (r.ctype == "text/css" or local.endswith(".css")) else "bin"
            self.meta[local] = (typ, n, False)
            total += len(r.body)
            count += 1
            net.append({"url": n, "tipo": r.rtype, "mime": r.ctype, "bytes": len(r.body), "archivo": local})
        self.total_bytes, self.count = total, count

        interactive = self.a.modo == "interactivo"
        if interactive:
            m = {n: self.saved[n] for n, r in self.store.items()
                 if r.rtype in ("xhr", "fetch") and n in self.saved}
            self.put("_w2r/shim.js", SHIM_JS.replace("__MAP__", json.dumps(m)).encode("utf-8"))
        self.put("_w2r/red.json", json.dumps(net, ensure_ascii=False, indent=1).encode("utf-8"))
        self.put("_w2r/omitidas.json", json.dumps(
            [{"url": u, "motivo": m} for u, m in self.skipped.items()], ensure_ascii=False, indent=1).encode("utf-8"))

        for local, (typ, base, rendered) in list(self.meta.items()):
            try:
                if typ == "html":
                    self.process_html(local, base, rendered, interactive)
                elif typ == "css":
                    path = os.path.join(self.out, *local.split("/"))
                    with open(path, "rb") as f:
                        text = decode_css(f.read())
                    text = re.sub(r'@charset\s+"[^"]*";?', "", text, flags=re.I)
                    text = walk_css(text, base, lambda u, k, raw, c=local: self.relink(u, c))
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
            except Exception as e:
                log(f"⚠️ No se pudieron reescribir enlaces de {local}: {e}")

    def process_html(self, local, base, rendered, interactive):
        path = os.path.join(self.out, *local.split("/"))
        if rendered:
            with open(path, encoding="utf-8") as f:
                soup = BeautifulSoup(f.read(), "html.parser")
        else:
            with open(path, "rb") as f:
                soup = BeautifulSoup(f.read(), "html.parser")
        if not interactive:  # modo instantanea: DOM tal cual, sin scripts
            for t in soup.find_all(["script", "noscript"]):
                t.decompose()
            for l in soup.find_all("link"):
                rel = " ".join(l.get("rel") or []).lower()
                if any(x in rel for x in ("preload", "prefetch", "modulepreload", "dns-prefetch", "preconnect")):
                    l.decompose()
        for m in soup.find_all("meta"):
            if (m.get("http-equiv") or "").lower() == "refresh":
                m.decompose()
        walk_html(soup, base, lambda u, k, raw: self.relink(u, local))
        for b in soup.find_all("base"):
            b.decompose()
        # el sitio original ya no es el destino de los formularios
        for f in soup.find_all("form"):
            f["action"] = "#"
            f["onsubmit"] = "return false;"
        for m in soup.find_all("meta"):
            if m.get("charset") or (m.get("http-equiv") or "").lower() == "content-type":
                m.decompose()
        head = soup.find("head")
        if head is None:
            head = soup.new_tag("head")
            html_tag = soup.find("html")
            (html_tag.insert(0, head) if html_tag else soup.insert(0, head))
        extra = [soup.new_tag("meta", charset="utf-8"),
                 soup.new_tag("meta", attrs={"name": "w2r-source", "content": base})]
        if interactive:
            sc = soup.new_tag("script", src=quote(relpath("_w2r/shim.js", dirname(local) or "."), safe="/"))
            extra.append(sc)
        for i, t in enumerate(extra):
            head.insert(i, t)
        banner = soup.new_tag("a", href=base, target="_blank", rel="noopener", attrs={
            "style": "position:fixed;right:8px;bottom:8px;z-index:2147483647;font:12px/1.3 system-ui,sans-serif;"
                     "background:#111;color:#fff;padding:6px 10px;border-radius:6px;opacity:.85;text-decoration:none"})
        banner.string = f"Copia archivada de {host_key(urlparse(base).netloc)} · {datetime.now():%Y-%m-%d}"
        (soup.body or soup).append(banner)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(soup))

    def write_index(self):
        idx_path = os.path.join("copias", "index.json")
        try:
            with open(idx_path, encoding="utf-8") as f:
                idx = json.load(f)
        except Exception:
            idx = []
        idx = [e for e in idx if e.get("carpeta") != self.carpeta]
        idx.insert(0, {"carpeta": self.carpeta, "url": self.a.url, "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "paginas": len(self.rendered), "archivos": self.count,
                       "mb": round(self.total_bytes / 1048576, 1), "modo": self.a.modo})
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=1)


def truthy(v):
    return str(v).strip().lower() in ("1", "true", "si", "sí", "yes", "on")


def main():
    e = os.environ.get
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=e("W2R_URL", ""))
    ap.add_argument("--carpeta", default=e("W2R_CARPETA", ""))
    ap.add_argument("--modo", default=e("W2R_MODO", "snapshot"), choices=["snapshot", "interactivo"])
    ap.add_argument("--acciones", default=e("W2R_ACCIONES", "true"))
    ap.add_argument("--robots", default=e("W2R_ROBOTS", "true"))
    ap.add_argument("--externo", default=e("W2R_EXTERNO", "true"))
    ap.add_argument("--subdominios", default=e("W2R_SUBDOMINIOS", "false"))
    ap.add_argument("--archivos", default=e("W2R_ARCHIVOS", "true"))
    ap.add_argument("--omitir", default=e("W2R_OMITIR", "precios,idiomas"))
    ap.add_argument("--excluir", default=e("W2R_EXCLUIR", ""))
    ap.add_argument("--max-paginas", type=int, default=int(e("W2R_MAX_PAGINAS", "300") or 300))
    ap.add_argument("--max-archivos", type=int, default=int(e("W2R_MAX_ARCHIVOS", "8000") or 8000))
    ap.add_argument("--max-mb", type=int, default=int(e("W2R_MAX_MB", "800") or 800))
    ap.add_argument("--max-minutos", type=int, default=int(e("W2R_MAX_MIN", "240") or 240))
    a = ap.parse_args()
    a.url = a.url.strip()
    if a.url and not re.match(r"^https?://", a.url, re.I):
        a.url = "https://" + a.url
    if not urlparse(a.url).netloc:
        sys.exit("Falta un enlace válido (W2R_URL).")
    a.acciones, a.robots = truthy(a.acciones), truthy(a.robots)
    a.external, a.subdomains, a.files = truthy(a.externo), truthy(a.subdominios), truthy(a.archivos)
    a.max_pages, a.max_files, a.max_mb, a.max_minutes = a.max_paginas, a.max_archivos, a.max_mb, a.max_minutos
    a.omitir_set = {x.strip().lower() for x in a.omitir.split(",") if x.strip()}
    a.excluir = [x.strip().lower() for x in a.excluir.split(",") if x.strip()]

    c = Cloner(a)
    log(f"Clonando {a.url} en copias/{c.carpeta}/ (modo {a.modo})")
    c.run_crawl()
    log("Guardando y reescribiendo enlaces…")
    c.write_all()
    c.write_index()
    msg = (f"Listo: {len(c.rendered)} páginas, {c.count} archivos, {c.total_bytes/1048576:.1f} MB"
           f"{', ' + str(c.omitidos) + ' omitidos por límites' if c.omitidos else ''}.")
    log(msg)
    if c.skipped:
        por = {}
        for m in c.skipped.values():
            por[m] = por.get(m, 0) + 1
        resumen = ", ".join(f"{v} por {k}" for k, v in por.items())
        log(f"Enlaces omitidos: {resumen} (lista en _w2r/omitidas.json)")
        msg += f" Omitidos: {resumen}."
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a", encoding="utf-8") as f:
            f.write(f"### Copia de {a.url}\n\n{msg}\n\nCarpeta: `copias/{c.carpeta}/`\n")


if __name__ == "__main__":
    main()
