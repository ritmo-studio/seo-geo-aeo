#!/usr/bin/env python3
"""Вежливый HTTP для аудита.

Скрипт ходит по живому сайту, поэтому вежливость здесь не стиль, а условие работы:
- запросы строго по одному, с паузой между ними; пула потоков нет вообще;
- robots.txt читается и соблюдается, Crawl-delay перебивает --delay, если он больше;
- User-Agent честный, с адресом: по логам видно, кто пришёл и зачем;
- 429/503 — не повод повторить сразу: ждём Retry-After, три отказа подряд — выходим;
- общий бюджет запросов ограничен: оборваться самому дешевле, чем попасть в бан;
- тела больше MAX_BYTES не читаем, аудит смотрит разметку, а не медиа.
"""
import gzip, hashlib, io, json, pathlib, re, ssl, time, zlib
import urllib.error, urllib.parse, urllib.request

UA = 'RitmoAudit/1.0 (+https://ritmo.studio/; SEO/AEO/GEO self-audit; 1 req/s)'
MAX_BYTES = 3 * 1024 * 1024
BLOCK_CODES = (429, 503)


class Stop(Exception):
    """Сервер попросил перестать или кончился бюджет — дальше не идём."""


class Resp:
    def __init__(self, url, status=0, final=None, headers=None, body=None, ms=0, err=None, chain=()):
        self.url, self.status, self.final = url, status, final or url
        self.headers = headers or {}
        self.body, self.ms, self.err, self.chain = body, ms, err, list(chain)

    @property
    def ctype(self):
        return (self.headers.get('content-type') or '').lower()

    @property
    def is_html(self):
        return 'html' in self.ctype

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def bytes_len(self):
        return int(self.headers.get('content-length') or (len(self.body.encode('utf-8', 'replace')) if self.body else 0))


class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.chain = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append((code, newurl))
        return urllib.request.HTTPRedirectHandler.redirect_request(self, req, fp, code, msg, headers, newurl)


class Robots:
    """Разбор robots.txt своими руками, а не стандартной библиотекой.

    `urllib.robotparser` берёт у правила только path и выбрасывает query:
    `Disallow: /?` превращается у него в `Disallow: /`, то есть закрывает сайт
    целиком. Эта строка стоит в типовом robots.txt WordPress — на spbshield.ru
    из-за неё обход по ссылкам остановился на первой же странице, и мы приняли
    это за поломку сайта. Та же библиотека не понимает `*` и `$` внутри пути,
    поэтому `Disallow: *?s=` у неё не срабатывает вообще: мы одновременно
    закрывали лишнее и ходили по запрещённому.

    Поддерживается то, что поддерживают Яндекс и Google: группы по User-agent,
    `*` как любая последовательность, `$` как конец адреса, самое длинное
    правило выигрывает, при равной длине выигрывает Allow. Сравнение идёт по
    пути вместе с query — иначе `*?s=` снова станет бессмысленным.
    """

    def __init__(self, text=''):
        self.groups = {}        # агент -> {'rules': [(rx, длина, allow)], 'delay': float|None}
        agents, seen_rule = [], False
        for raw in (text or '').splitlines():
            line = raw.split('#', 1)[0].strip()
            if not line or ':' not in line:
                continue
            field, _, value = line.partition(':')
            field, value = field.strip().lower(), value.strip()
            if field == 'user-agent':
                # новая группа начинается, только если до этого были правила
                if seen_rule:
                    agents, seen_rule = [], False
                agents.append(value.lower())
                self.groups.setdefault(value.lower(), {'rules': [], 'delay': None})
            elif field in ('allow', 'disallow') and agents:
                seen_rule = True
                if not value:
                    continue            # пустой Disallow — «можно всё», не правило
                if not (value.startswith('/') or value.startswith('*')):
                    continue            # не путь, а мусорная строка
                rule = (self._rx(value), len(value), field == 'allow')
                for a in agents:
                    self.groups[a]['rules'].append(rule)
            elif field == 'crawl-delay' and agents:
                seen_rule = True
                try:
                    delay = float(value.replace(',', '.'))
                except ValueError:
                    continue
                for a in agents:
                    self.groups[a]['delay'] = delay

    @staticmethod
    def _rx(pattern):
        anchored = pattern.endswith('$')
        body = pattern[:-1] if anchored else pattern
        rx = '.*'.join(re.escape(part) for part in body.split('*'))
        return re.compile('^' + rx + ('$' if anchored else ''))

    def _group(self, ua):
        """Группа по самому длинному совпавшему имени агента, иначе звёздочка."""
        low = (ua or '').lower()
        best = None
        for agent in self.groups:
            if agent and agent != '*' and agent in low:
                if best is None or len(agent) > len(best):
                    best = agent
        return self.groups.get(best or '*')

    def can_fetch(self, ua, url):
        g = self._group(ua)
        if not g or not g['rules']:
            return True
        parts = urllib.parse.urlsplit(url)
        target = parts.path or '/'
        if parts.query:
            target += '?' + parts.query
        best = None                      # (длина правила, allow)
        for rx, length, allow in g['rules']:
            if rx.match(target) and (best is None or (length, allow) > best):
                best = (length, allow)
        return best[1] if best else True

    def crawl_delay(self, ua):
        g = self._group(ua)
        return g['delay'] if g else None


class Fetcher:
    def __init__(self, delay=1.0, timeout=15, budget=300, cache=0, cache_dir=None,
                 obey_robots=True, insecure=False, log=print):
        self.delay, self.timeout, self.budget = delay, timeout, budget
        self.cache_ttl = cache
        self.cache_dir = pathlib.Path(cache_dir or '.audit-cache')
        self.obey_robots, self.log = obey_robots, log
        self.used = 0            # сколько сетевых запросов потрачено из бюджета
        self.blocked = 0         # сколько отказов подряд
        self._last = 0.0
        self._rp = {}            # host -> (Robots, текст robots.txt)
        self.ctx = ssl._create_unverified_context() if insecure else None

    # — бюджет и пауза —

    def _wait(self, host):
        pause = self.delay
        rp = self._rp.get(host)
        if rp and rp[0]:
            try:
                cd = rp[0].crawl_delay(UA)
                if cd:
                    pause = max(pause, float(cd))
            except Exception:
                pass
        gap = time.time() - self._last
        if gap < pause:
            time.sleep(pause - gap)

    # — robots.txt —

    def robots(self, base):
        """Забирает robots.txt один раз на хост. Возвращает текст (или '')."""
        host = urllib.parse.urlsplit(base).netloc
        if host in self._rp:
            return self._rp[host][1]
        self._rp[host] = (None, '')          # заглушка, чтобы не уйти в рекурсию
        r = self.get(urllib.parse.urljoin(base, '/robots.txt'), kind='robots')
        txt = r.body if (r.ok and r.body) else ''
        self._rp[host] = (Robots(txt), txt)
        return txt

    def allowed(self, url):
        if not self.obey_robots:
            return True
        host = urllib.parse.urlsplit(url).netloc
        if host not in self._rp:
            # Хост мог появиться по редиректу (spbshield.ru → www.spbshield.ru):
            # раньше для него robots.txt не читался вообще, и мы ходили по
            # чужому хосту без спроса. Вежливость не должна зависеть от того,
            # с какого адреса начали обход.
            self.robots('https://%s/' % host)
        rp = self._rp.get(host)
        if not rp or not rp[0]:
            return True
        try:
            return rp[0].can_fetch(UA, url)
        except Exception:
            return True

    # — кэш на диске —

    def _cpath(self, method, url):
        return self.cache_dir / (hashlib.sha1(('%s %s' % (method, url)).encode()).hexdigest() + '.json')

    def _cread(self, method, url):
        if not self.cache_ttl:
            return None
        p = self._cpath(method, url)
        if not p.exists() or time.time() - p.stat().st_mtime > self.cache_ttl:
            return None
        try:
            d = json.loads(p.read_text(encoding='utf-8'))
            return Resp(url, d['status'], d['final'], d['headers'], d['body'], d['ms'], None, d['chain'])
        except Exception:
            return None

    def _cwrite(self, method, url, r):
        if not self.cache_ttl:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._cpath(method, url).write_text(json.dumps(dict(
                status=r.status, final=r.final, headers=r.headers, body=r.body, ms=r.ms, chain=r.chain),
                ensure_ascii=False), encoding='utf-8')
        except Exception:
            pass

    # — сам запрос —

    def get(self, url, method='GET', kind='page', tries=2):
        cached = self._cread(method, url)
        if cached:
            return cached
        if self.used >= self.budget:
            raise Stop('бюджет запросов исчерпан (%d)' % self.budget)
        host = urllib.parse.urlsplit(url).netloc
        self._wait(host)
        rh = _Redirects()
        req = urllib.request.Request(url, method=method, headers={
            'User-Agent': UA,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'close',
        })
        t0 = time.time()
        self.used += 1
        try:
            opener = urllib.request.build_opener(*([rh] + ([urllib.request.HTTPSHandler(context=self.ctx)] if self.ctx else [])))
            with opener.open(req, timeout=self.timeout) as f:
                raw = f.read(MAX_BYTES + 1) if method == 'GET' else b''
                r = self._mk(url, f.getcode(), f.geturl(), f.headers, raw, t0, rh.chain)
                self._cwrite(method, url, r)
        except urllib.error.HTTPError as e:
            raw = b''
            try:
                raw = e.read(MAX_BYTES + 1)
            except Exception:
                pass
            r = self._mk(url, e.code, e.url or url, e.headers, raw, t0, rh.chain)
            if e.code not in BLOCK_CODES:
                self._cwrite(method, url, r)
            if e.code in BLOCK_CODES and tries > 0:
                wait = self._retry_after(e.headers)
                self.log('  %s %s — ждём %ds и пробуем ещё раз' % (e.code, url, wait))
                time.sleep(wait)
                self.blocked += 1
                self._stop_if_banned()
                return self.get(url, method, kind, tries - 1)
            if e.code in BLOCK_CODES or e.code == 403:
                self.blocked += 1
                self._stop_if_banned()
            return r
        except Exception as e:
            self._last = time.time()
            return Resp(url, 0, url, {}, None, int((time.time() - t0) * 1000), '%s: %s' % (type(e).__name__, e))
        self.blocked = 0
        return r

    def head(self, url):
        """HEAD дешевле GET. Сервер его не умеет — один раз добираем GET'ом."""
        r = self.get(url, method='HEAD', kind='asset')
        if r.status in (400, 405, 501) or r.status == 0:
            r = self.get(url, method='GET', kind='asset')
        return r

    def _retry_after(self, headers):
        ra = (headers or {}).get('retry-after') or (headers or {}).get('Retry-After')
        try:
            return max(1, min(60, int(str(ra).strip())))
        except Exception:
            return 10

    def _stop_if_banned(self):
        if self.blocked >= 3:
            raise Stop('три отказа подряд (429/503/403) — сервер просит перестать')

    def _mk(self, url, status, final, headers, raw, t0, chain):
        self._last = time.time()
        h = {k.lower(): v for k, v in dict(headers or {}).items()}
        body = None
        if raw:
            enc = (h.get('content-encoding') or '').lower()
            try:
                if 'gzip' in enc:
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read(MAX_BYTES + 1)
                elif 'deflate' in enc:
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            except Exception:
                pass
            cs = 'utf-8'
            m = re.search(r'charset=([\w-]+)', h.get('content-type', ''), re.I)
            if m:
                cs = m.group(1)
            else:
                m = re.search(br'charset=["\']?([\w-]+)', raw[:2048], re.I)
                if m:
                    cs = m.group(1).decode('ascii', 'replace')
            try:
                body = raw.decode(cs, 'replace')
            except LookupError:
                body = raw.decode('utf-8', 'replace')
        return Resp(url, status, final, h, body, int((time.time() - t0) * 1000), None, chain)
