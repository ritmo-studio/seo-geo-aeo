#!/usr/bin/env python3
"""Разбор HTML в плоскую модель страницы.

Только стандартная библиотека: скрипту предстоит жить на чужом сервере, где
`pip install` может быть недоступен, а лишняя зависимость — лишняя причина не запуститься.
html.parser прощает кривую разметку (незакрытые теги, лишние кавычки) и не падает —
для аудита это важнее академической точности: падать на битой странице нельзя,
битая страница и есть предмет проверки.

Из страницы достаётся ровно то, на чём стоят проверки, и ничего больше: тяжёлые
страницы не держим в памяти целиком, после checks от объекта остаётся сводка (см. audit.py).
"""
import json, re, urllib.parse
from html.parser import HTMLParser

H = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')
NOTEXT = ('script', 'style', 'noscript', 'template', 'svg')
VOID = ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'param', 'source', 'track', 'wbr')


class Doc(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.els = []          # [(tag, attrs)] — для проверок разметки и доступности
        self.headings = []     # [(уровень, текст)]
        self.links = []        # [(href, текст, attrs)]
        self.jsonld = []       # сырые строки <script type=application/ld+json>
        self.ids = []
        self.title = ''
        self.html_attrs = {}
        self.text_parts = []
        self.list_items = []   # текст каждого <li> — чтобы увидеть пустые слоты
        self._cap = []         # стек открытых «захватов» текста
        self._notext = 0
        self._tags = {}        # счётчик тегов

    # — захват текста внутри элемента —

    def _open(self, kind, attrs):
        if len(self._cap) < 16:
            self._cap.append([kind, attrs, []])

    def _close(self, kind):
        for i in range(len(self._cap) - 1, -1, -1):
            if self._cap[i][0] == kind:
                item = self._cap.pop(i)
                del self._cap[i:]          # незакрытые вложенные захваты не копим
                return item
        return None

    def handle_starttag(self, tag, attrs):
        a = {}
        for k, v in attrs:
            a[k.lower()] = v if v is not None else ''
        self.els.append((tag, a))
        self._tags[tag] = self._tags.get(tag, 0) + 1
        if a.get('id'):
            self.ids.append(a['id'])
        if tag == 'html':
            self.html_attrs = a
        if tag in NOTEXT:
            self._notext += 1
            if tag == 'script' and 'json' in (a.get('type') or '').lower():
                self._open('jsonld', a)
            return
        if tag == 'title':
            self._open('title', a)
        elif tag == 'a':
            self._open('a', a)
        elif tag in H:
            self._open(tag, a)
        elif tag == 'li':
            self._open('li', a)
        elif tag == 'button':
            self._open('button', a)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in NOTEXT:
            self._notext = max(0, self._notext - 1)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if tag in NOTEXT:
            self._notext = max(0, self._notext - 1)
            if tag == 'script':
                it = self._close('jsonld')
                if it:
                    self.jsonld.append(''.join(it[2]))
            return
        it = None
        if tag in ('title', 'a', 'li', 'button') or tag in H:
            it = self._close(tag)
        if not it:
            return
        txt = norm(''.join(it[2]))
        if tag == 'title':
            self.title = self.title or txt
        elif tag == 'a':
            self.links.append((it[1].get('href', ''), txt, it[1]))
        elif tag == 'li':
            self.list_items.append(txt)
        elif tag in H:
            self.headings.append((int(tag[1]), txt))
        elif tag == 'button':
            self.els.append(('button:text', {'text': txt, **it[1]}))

    def handle_data(self, d):
        for c in self._cap:
            c[2].append(d)
        if not self._notext:
            self.text_parts.append(d)


def norm(s):
    return re.sub(r'\s+', ' ', (s or '').replace(' ', ' ')).strip()


class Page:
    """Страница как набор фактов. Ни одной оценки здесь нет — оценки в checks.py."""

    def __init__(self, url, status=200, body='', headers=None, ms=0, chain=(), err=None, src=None):
        self.url, self.status, self.ms, self.err = url, status, ms, err
        self._findings = []
        self.src = src or url                     # файл, если аудит локальный
        self.headers = headers or {}
        self.chain = list(chain)
        self.html = body or ''
        self.doc = Doc()
        if self.html:
            try:
                self.doc.feed(self.html)
                self.doc.close()
            except Exception as e:
                self.err = self.err or 'parse: %s' % e
        d = self.doc
        self.base = url
        for tag, a in d.els:
            if tag == 'base' and a.get('href'):
                self.base = urllib.parse.urljoin(url, a['href'])
                break
        self.title = d.title
        self.meta = {}        # name=
        self.prop = {}        # property= (og:, article:)
        self.http_equiv = {}
        self.charset = ''
        for tag, a in d.els:
            if tag != 'meta':
                continue
            if 'charset' in a:
                self.charset = a['charset']
            n, p, c = (a.get('name') or '').lower(), (a.get('property') or '').lower(), a.get('content') or ''
            if n:
                self.meta[n] = c
            if p:
                self.prop[p] = c
            if a.get('http-equiv'):
                self.http_equiv[a['http-equiv'].lower()] = c
        self.rels = {}        # rel -> [href]
        for tag, a in d.els:
            if tag == 'link' and a.get('rel'):
                for r in a['rel'].lower().split():
                    self.rels.setdefault(r, []).append(a.get('href', ''))
        self.lang = (d.html_attrs.get('lang') or '').strip()
        self.desc = (self.meta.get('description') or '').strip()
        self.headings = d.headings
        self.h1 = [t for lvl, t in d.headings if lvl == 1]
        self.h1first = self.h1[0] if self.h1 else ''
        self.text = norm(' '.join(d.text_parts))
        self.ids = d.ids
        self.tags = d._tags
        self.jsonld, self.ld_errors = [], []
        for raw in d.jsonld:
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception as e:
                self.ld_errors.append(str(e).split(':')[0])
                continue
            self.jsonld.extend(flatten_ld(data))
        self.links = []       # [(абсолютный url, анкор, attrs, сырой href)]
        for href, txt, a in d.links:
            self.links.append((self.abs(href), txt, a, href))
        self.text_len = len(self.text)

    def release(self):
        """Отпустить разметку: после проверок самой страницы она больше не нужна —
        сквозным проверкам и отчёту хватает title, description, h1, узлов JSON-LD
        и длины текста.

        Замер на библиотечных демо-страницах: разобранная страница стоит 356 КБ,
        то есть прогон по трём тысячам адресов держал бы в памяти гигабайт. Скрипт
        обещал не мешать серверу, и память сервера — тоже сервер."""
        self.html, self.text, self.links, self.doc = '', '', [], None

    def abs(self, href):
        href = (href or '').strip()
        if not href or href.startswith(('#', 'javascript:', 'mailto:', 'tel:', 'data:')):
            return ''
        try:
            u = urllib.parse.urljoin(self.base, href)
        except Exception:
            return ''
        return urllib.parse.urldefrag(u)[0]

    def canonical(self):
        v = (self.rels.get('canonical') or [''])[0]
        return v.strip()

    def assets(self):
        """Ссылки, которые имеет смысл проверить на 200: картинка шеринга, иконки."""
        out = []
        for k in ('og:image', 'og:image:secure_url', 'twitter:image'):
            if self.prop.get(k) or self.meta.get(k):
                out.append(self.abs(self.prop.get(k) or self.meta.get(k)))
        for r in ('icon', 'shortcut', 'apple-touch-icon', 'manifest'):
            for h in self.rels.get(r, []):
                out.append(self.abs(h))
        return [u for u in out if u]

    def ld_types(self):
        t = set()
        for n in self.jsonld:
            for v in as_list(n.get('@type')):
                if isinstance(v, str):
                    t.add(v)
        return t

    def ld_of(self, *types):
        want = set(types)
        return [n for n in self.jsonld if want & {v for v in as_list(n.get('@type')) if isinstance(v, str)}]


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def flatten_ld(data, out=None):
    """@graph и вложенные объекты разворачиваются в плоский список узлов.
    Проверки GEO смотрят на узлы, а не на форму документа: один и тот же
    Organization может лежать и в @graph, и внутри worksFor."""
    out = [] if out is None else out
    for item in as_list(data):
        if not isinstance(item, dict):
            continue
        if '@graph' in item:
            flatten_ld(item['@graph'], out)
        node = {k: v for k, v in item.items() if k != '@graph'}
        if node.get('@type'):
            out.append(node)
        for v in node.values():
            if isinstance(v, (dict, list)):
                flatten_ld(v, out)
    return out
