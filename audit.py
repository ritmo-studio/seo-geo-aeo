#!/usr/bin/env python3
"""Мини-аудит сайта по чек-листу SEO / AEO / GEO.

    python3 audit.py https://ritmo.studio/
    python3 audit.py --local ./snapshot/ --out reports

Скрипт собирает факты (fetch.py + parse.py), прогоняет чек-лист (checks.py) и
кладёт два файла: читаемый .md и машинный .json. Json нужен, чтобы следующий
прогон сравнивался с предыдущим: «стало на три крита меньше» — единственная
честная метрика работы, остальное ощущения.

Чего скрипт не делает и не будет: не рендерит JS, не измеряет Core Web Vitals,
не оценивает качество текста и не знает, по каким запросам страница должна
находиться. Он ловит дефекты разметки и связей — то, что иначе ищут глазами
и пропускают.
"""
import argparse, datetime, json, os, pathlib, re, sys, urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fetch, parse, checks

SKIP_EXT = ('.jpg', '.jpeg', '.png', '.webp', '.avif', '.gif', '.ico', '.css', '.js',
            '.woff', '.woff2', '.ttf', '.mp4', '.webm', '.mp3', '.svg')
LOCAL_ONLY_SKIP = ('s_robots', 's_sitemap', 's_probes')
SEV = {'crit': 0, 'warn': 1, 'note': 2}
SEVNAME = {'crit': 'критично', 'warn': 'важно', 'note': 'мелочь'}
CATNAME = {'SEO': 'SEO — индексация и выдача', 'AEO': 'AEO — чтобы ответ можно было собрать и процитировать',
           'GEO': 'GEO — узнаваемость сущности', 'SEM': 'Семантика и доступность', 'TECH': 'Техника'}


class Ctx:
    def __init__(self, start, cfg, local=False, cached=False):
        self.start, self.cfg, self.local, self.cached = start, cfg, local, cached
        sp = urllib.parse.urlsplit(start)
        self.host = sp.netloc
        self.root = '%s://%s/' % (sp.scheme or 'file', sp.netloc) if sp.netloc else start
        self.pages, self.link_status, self.link_src = [], {}, {}
        self.asset_status = {}       # og:image и иконки: проверяем, но это не перелинковка
        self.sitemap, self.robots, self.probe = {}, {}, {}
        # Чем прогон накрыл сайт. Без этого отчёт по выборке из сорока страниц
        # читается как отчёт по всему сайту — и «сирот» в нём будет три тысячи.
        self.extern = {}             # сводки соседних инструментов: Яндекс, GEO-трекер
        self.cover = {'mode': 'links', 'cap': 0, 'per': 0, 'sitemap': 0,
                      'templates': 0, 'picked': 0, 'full_graph': True, 'link_skipped': 0,
                      'link_noresp': 0}

    def internal(self, url):
        if self.local:
            return url.startswith(self.start)
        return urllib.parse.urlsplit(url).netloc.lower().lstrip('www.') == self.host.lower().lstrip('www.')

    def is_home(self, url):
        return url.rstrip('/') in (self.start.rstrip('/'), self.root.rstrip('/'))

    def short(self, url):
        return url[len(self.start):] or '/' if url.startswith(self.start) else url

    def ignored(self, f):
        """Правило конфига: 'SEO-THIN' — код целиком, 'SEO-THIN@/blog/' — код на адресах с куском пути.
        Без этого любой аудит через неделю перестают читать: десять известных и принятых
        находок прячут одну новую."""
        for rule in self.cfg.get('ignore', []):
            code, _, pat = str(rule).partition('@')
            if code in ('*', f['code']) and (not pat or pat in f['url']):
                return True
        return False

    def ok_pages(self):
        return [p for p in self.pages if not p.err and 200 <= p.status < 300]


def norm_url(u):
    u = urllib.parse.urldefrag((u or '').strip())[0]
    sp = urllib.parse.urlsplit(u)
    if not sp.scheme:
        return ''
    q = '&'.join(x for x in sp.query.split('&') if x and not x.lower().startswith(('utm_', 'yclid', 'gclid', 'fbclid')))
    return urllib.parse.urlunsplit((sp.scheme.lower(), sp.netloc.lower(), sp.path or '/', q, ''))


def crawlable(u):
    return bool(u) and not u.lower().split('?')[0].endswith(SKIP_EXT)


# ──────────────────── выборка страниц на большом сайте ────────────────────

def shape(url):
    """Форма пути: /cases/rabachev/ → /cases/*, /blog/2026/01/post/ → /blog/N/N/*.

    На сайте с тремя тысячами страниц дефекты сидят не в страницах, а в шаблонах:
    десяток форм пути описывает весь сайт, и по три страницы каждой формы находят
    почти всё, что нашёл бы полный обход, — за минуты вместо часов."""
    segs = [x for x in (urllib.parse.urlsplit(url).path or '/').split('/') if x]
    if not segs:
        return '/'
    segs = ['N' if x.isdigit() else x for x in segs]
    return '/%s/' % segs[0] if len(segs) == 1 else '/%s/*' % '/'.join(segs[:-1])


def sample(urls, per, cap):
    """По `per` адресов на форму пути, но не больше `cap` всего. Сначала по одному
    с каждой формы: охват шаблонов важнее глубины внутри одного шаблона."""
    per, cap = max(1, per), max(1, cap)
    groups = {}
    for u in urls:
        groups.setdefault(shape(u), []).append(u)
    if len(groups) > cap:
        # Плоские адреса (/post-slug/ у WordPress) дают тысячу форм по одному адресу.
        # Это одна форма, а не тысяча.
        flat = {}
        for k, g in groups.items():
            flat.setdefault('/*' if len(g) == 1 else k, []).extend(g)
        groups = flat
    size = {k: len(g) for k, g in groups.items()}
    for k, g in groups.items():
        g = sorted(g)
        step = max(1, len(g) // per)          # разброс по списку, а не первые N подряд
        groups[k] = (g[::step] or g)[:per]
    order = sorted(groups, key=lambda k: (-size[k], k))
    picked = []
    for i in range(per):
        for k in order:
            if i < len(groups[k]) and len(picked) < cap:
                picked.append(groups[k][i])
    return picked, size


# ─────────────────────────── сбор страниц ───────────────────────────

def crawl(ctx, f, max_pages, max_depth, with_query, log, seeds=None):
    seeds = list(seeds or [ctx.start])
    queue, seen = [(u, 0) for u in seeds], set(seeds)
    while queue and len(ctx.pages) < max_pages:
        url, depth = queue.pop(0)
        if not f.allowed(url):
            log('  robots.txt запрещает %s — пропускаем' % url)
            continue
        r = f.get(url)
        p = parse.Page(url, r.status, r.body if r.is_html else '', r.headers, r.ms, r.chain, r.err)
        ctx.pages.append(p)
        ctx.link_status[url] = r.status
        log('  [%d] %s %s%s' % (len(ctx.pages), r.status or 'ERR', url, ' — %s' % r.err if r.err else ''))
        if not r.is_html or not r.ok:
            check_page(p, ctx)
            continue
        for u, text, a, href in p.links:
            if not u:
                continue
            u = norm_url(u)
            if not u or not ctx.internal(u):
                continue
            ctx.link_src.setdefault(u, set()).add(url)
            if u in seen or not crawlable(u):
                continue
            if urllib.parse.urlsplit(u).query and not with_query:
                continue
            seen.add(u)
            if depth + 1 <= max_depth and len(seen) <= max_pages * 3:
                queue.append((u, depth + 1))
        check_page(p, ctx)


def check_rest(ctx, f, log, cap=0):
    """Ссылки, до которых обход не дошёл, и картинки шеринга — HEAD'ом, по одной.

    На большом сайте сорок страниц ссылаются на восемьсот адресов, и проверка ссылок
    съедает весь бюджет запросов раньше, чем доходит до проб. Поэтому потолок и
    порядок: сначала то, на что ссылаются с большего числа страниц."""
    todo = [u for u in ctx.link_src if u not in ctx.link_status]
    todo.sort(key=lambda u: (-len(ctx.link_src[u]), u))
    if cap and len(todo) > cap:
        ctx.cover['link_skipped'] = len(todo) - cap
        log('ссылок на проверку %d — проверим %d, на которые ссылаются чаще всего' % (len(todo), cap))
        todo = todo[:cap]
    assets = sorted({a for p in ctx.ok_pages() for a in p.assets() if ctx.internal(a)})
    for u in todo:
        ctx.link_status[u] = 200 if (ctx.local and local_exists(ctx, u)) else (404 if ctx.local else f.head(u).status)
        st = ctx.link_status[u]
        # Нет ответа вообще (таймаут, обрыв) — это не 404: ссылка осталась
        # непроверенной. Называть её битой нельзя, по ней пойдут править сайт.
        if not st:
            ctx.cover['link_noresp'] += 1
            log('  не ответила: %s' % u)
        elif not 200 <= st < 300:
            log('  битая ссылка: %s → %d' % (u, st))
    for u in assets:
        if u in ctx.link_status:
            ctx.asset_status[u] = ctx.link_status[u]
            continue
        ctx.asset_status[u] = 200 if (ctx.local and local_exists(ctx, u)) else (404 if ctx.local else f.head(u).status)
    for p in ctx.ok_pages():
        for a in sorted(set(p.assets())):
            st = ctx.asset_status.get(a)
            if st and not (200 <= st < 300):
                p._findings.append(checks.F(
                    'SEO-OG-ASSET-404', 'SEO', 'warn', '%d: %s' % (st, a),
                    'превью в мессенджерах и во вкладке будет пустым; относительный путь в og:image '
                    'считается от адреса страницы, а не от корня — это самая частая причина', p.url))


def probes(ctx, f, log):
    ctx.robots = {'status': 0, 'text': ''}
    r = f.get(urllib.parse.urljoin(ctx.root, '/robots.txt'))
    ctx.robots = {'status': r.status, 'text': r.body or ''}
    sm_url = ''
    m = re.search(r'(?mi)^\s*sitemap\s*:\s*(\S+)', ctx.robots['text'])
    sm_url = m.group(1) if m else urllib.parse.urljoin(ctx.root, '/sitemap.xml')
    ctx.sitemap = read_sitemap(f, sm_url, log)
    rnd = urllib.parse.urljoin(ctx.root, '/zzz-audit-404-probe-%s/' % datetime.date.today().isoformat())
    ctx.probe['soft404'] = f.get(rnd).status
    ctx.probe['llms'] = f.get(urllib.parse.urljoin(ctx.root, '/llms.txt')).status
    if ctx.root.startswith('https://'):
        r = f.get('http://' + ctx.host + '/')
        ctx.probe['http_final'] = r.final


def read_sitemap(f, url, log, depth=0):
    r = f.get(url)
    out = {'status': r.status, 'urls': [], 'err': ''}
    if not r.ok or not r.body:
        return out
    locs = re.findall(r'<loc>\s*([^<\s]+)\s*</loc>', r.body)
    if '<sitemapindex' in r.body and depth < 1:
        for child in locs[:5]:
            out['urls'] += read_sitemap(f, child, log, depth + 1)['urls']
    else:
        out['urls'] = [norm_url(u) for u in locs]
    if not locs:
        out['err'] = 'нет ни одного <loc>'
    return out


# ─────────────────────────── локальный режим ───────────────────────────

def local_path(ctx, url):
    rel = url[len(ctx.start):].lstrip('/')
    base = pathlib.Path(ctx.cfg['_dir'])
    for cand in (base / rel, base / rel / 'index.html', base / (rel + '.html'), base / (rel.rstrip('/') + '.html')):
        if cand.is_file():
            return cand
    return None


def local_exists(ctx, url):
    return local_path(ctx, url) is not None


def crawl_local(ctx, log):
    base = pathlib.Path(ctx.cfg['_dir'])
    files = sorted(p for p in base.rglob('*.htm*') if p.is_file())
    for fp in files:
        rel = fp.relative_to(base).as_posix()
        url = ctx.start + re.sub(r'index\.html?$', '', rel)
        p = parse.Page(url, 200, fp.read_text(encoding='utf-8', errors='replace'), {'content-type': 'text/html'},
                       0, (), None, src=str(fp))
        ctx.pages.append(p)
        ctx.link_status[url] = 200
        log('  [%d] %s' % (len(ctx.pages), rel))
        for u, t, a, href in p.links:
            u = norm_url(u)
            if u and ctx.internal(u):
                ctx.link_src.setdefault(u, set()).add(p.url)
        check_page(p, ctx)


# ─────────────────────────── отчёт ───────────────────────────

def check_page(p, ctx):
    """Прогнать проверки одной страницы и отпустить её разметку. Вызывается сразу
    после разбора: держать тысячу страниц в памяти до конца обхода незачем."""
    # Страница не открылась — разбирать в ней нечего. Пустой ответ иначе даёт «нет
    # title», «нет h1», «нет viewport» и ещё десяток мнимых находок на каждую
    # упавшую страницу: на 220boom.ru тринадцать таймаутов дали 195 замечаний из
    # 499. Говорим один раз, что страница не отвечает, — остальное после ответа.
    live = not p.err and 200 <= p.status < 300
    for fn in checks.PAGE:
        if not live and not getattr(fn, 'on_dead', False):
            continue
        try:
            p._findings += [x for x in fn(p, ctx) if x]
        except Exception as e:
            p._findings.append(checks.F('TOOL-ERR', 'TECH', 'note',
                                        'проверка %s упала: %s' % (fn.__name__, e), '', p.url))
    p._checked = True
    p.release()


def run_checks(ctx):
    for p in ctx.pages:
        if not getattr(p, '_checked', False):
            check_page(p, ctx)


def run_site_checks(ctx):
    out = []
    for fn in checks.SITE:
        if ctx.local and fn.__name__ in LOCAL_ONLY_SKIP:
            continue
        try:
            out += [x for x in fn(ctx) if x]
        except Exception as e:
            out.append(checks.F('TOOL-ERR', 'TECH', 'note', 'проверка %s упала: %s' % (fn.__name__, e), '', ctx.start))
    return out


def key(fnd):
    return (SEV[fnd['sev']], fnd['code'], fnd['url'])


def group(found):
    """Одна и та же находка на двадцати страницах — это один пункт со списком адресов,
    а не двадцать пунктов. Иначе отчёт читают до середины первой категории."""
    by = {}
    for f in found:
        by.setdefault(f['code'], []).append(f)
    out = []
    for code, g in by.items():
        g.sort(key=key)
        title = checks.TITLES.get(code) or min((f['msg'] for f in g), key=len)
        out.append(dict(code=code, sev=g[0]['sev'], cat=g[0]['cat'], fix=g[0]['fix'],
                        title=title, items=g))
    out.sort(key=lambda x: (SEV[x['sev']], x['code']))
    return out


def rank(g):
    """Порядок работ: сначала уровень, потом собственный вес дефекта, потом охват.
    Охват добавляет немного — двадцать страниц без lazy не обгонят одну сироту."""
    return -((3 - SEV[g['sev']]) * 100 + checks.PRIO.get(g['code'], 0) + min(len(g['items']), 20))


def cover_note(ctx):
    """Чем прогон накрыл сайт. Фраза обязательная: «замечаний 69» без охвата читается
    как «на сайте 69 проблем», а это 69 на выборке из сорока страниц."""
    cv, out = ctx.cover, []
    if cv['mode'] == 'sample':
        out.append('Это выборка: %s из %d в sitemap, по %d на каждую из %s пути.' % (
            checks.plural(cv['picked'], 'страница', 'страницы', 'страниц'),
            cv['sitemap'], cv['per'],
            checks.plural(cv['templates'], 'формы', 'форм', 'форм')))
    elif not cv['full_graph'] and len(ctx.pages) >= cv['cap'] and cv['cap']:
        out.append('Обход остановился на потолке %d страниц — на сайте их больше.' % cv['cap'])
    if not cv['full_graph']:
        out.append('Сироты и перелинковка целиком не проверялись: для них нужен прогон '
                   '`--crawl links` с потолком выше числа страниц сайта.')
    if cv['link_skipped']:
        out.append('%s осталось без проверки на 404 — не хватило потолка.' % checks.plural(
            cv['link_skipped'], 'ссылка', 'ссылки', 'ссылок'))
    if cv['link_noresp']:
        out.append('%s не ответило совсем (таймаут) — это не 404, проверить вручную.'
                   % checks.plural(cv['link_noresp'], 'ссылка', 'ссылки', 'ссылок'))
    return out


def read_extern(out):
    """Сводки соседних инструментов из папки клиента. Чтение односторонее и
    необязательное: нет файла — отчёт прежний. Так аудит остаётся без ключей и
    зависимостей, а данные всё равно встречаются в одном месте."""
    got = {}
    for name in ('yandex', 'geo'):
        f = out / ('%s.json' % name)
        if not f.exists():
            continue
        try:
            got[name] = json.loads(f.read_text(encoding='utf-8'))
        except Exception as e:
            got[name] = {'summary': 'файл %s не разобрался: %s' % (f.name, e)}
    return got


def cross(ctx, findings):
    """Сверки, которые не может сделать ни один инструмент в одиночку. Здесь и
    только здесь аудит имеет право говорить о том, чего сам не измерял, — потому
    что цифра рядом посчитана кем-то другим."""
    y = ctx.extern.get('yandex') or {}
    g = ctx.extern.get('geo') or {}
    out = []
    wm = (y.get('webmaster') or {})
    in_search = wm.get('in_search')
    live = len(ctx.ok_pages())
    if isinstance(in_search, int) and live:
        if in_search > live:
            out.append('Краулер дошёл до %d страниц, в поиске Яндекса %d — %d адресов индекс '
                       'знает, а ссылки на них не ведут: сироты или старые копии.' % (
                           live, in_search, in_search - live))
        elif live > in_search:
            out.append('Краулер дошёл до %d страниц, в поиске Яндекса %d — в индекс не попало %d.'
                       % (live, in_search, live - in_search))
    codes = wm.get('codes') or {}
    bad = {k: v for k, v in codes.items() if not k.endswith('2XX') and v}
    if bad:
        out.append('Робот Яндекса видел не только 200: %s. Свой замер можно списать на '
                   'вежливость краулера, эти коды посчитал Яндекс.' % ', '.join(
                       '%s %s' % (k.replace('HTTP_', ''), v) for k, v in sorted(bad.items())))
    m = (y.get('metrika') or {})
    # Только свои адреса: счётчик ловит и посторонние входы (mc.yandex.ru и прочий
    # шум), а «начинать с них» было бы советом чинить чужой сайт.
    ent = [e for e in (m.get('entries') or [])
           if e.get('visits') and ctx.host in (e.get('url') or '')][:3]
    if ent:
        out.append('Зарабатывают входом: %s — с них и начинать.' % ', '.join(
            '%s (%s)' % (e['url'], int(e['visits'])) for e in ent))
    if g.get('share') is not None:
        line = 'GEO-замер: бренд назван в %.1f%% ответов' % g['share']
        if g.get('batch'):
            line += ' (%s)' % g['batch']
        out.append(line + '.')
        geo_blind = [f for f in findings if f['code'] in ('GEO-NO-ORG', 'AEO-NO-SCHEMA')]
        if geo_blind and g.get('cited') and g['cited'] > (g.get('mentioned') or 0):
            out.append('И сходится с разметкой: сайт попал в источники %d раз, а назван %d — '
                       'читать его модели есть что, а понять, чья это компания, нечего.' % (
                           g['cited'], g['mentioned']))
        fnd = [x for x in (g.get('finders') or []) if x.get('with_mention')][:3]
        if fnd:
            out.append('Находят через %s — не через сайт.' % ', '.join(
                x['domain'] for x in fnd))
    return out


def extern_md(ctx):
    """Раздел отчёта с чужими сводками — целиком, как их написали соседние скрипты.
    Пересказывать их своими словами нельзя: сверка держится на том, что цифру
    посчитал другой инструмент, и подменять её нашей формулировкой — терять смысл."""
    t = []
    for name, title in (('yandex', 'Метрика и Вебмастер'), ('geo', 'GEO-трекер')):
        d = ctx.extern.get(name)
        if not d:
            continue
        t += ['### %s' % title, '']
        when = d.get('when') or ''
        if when:
            t += ['_замер %s_' % when, '']
        t += [d.get('summary') or '(без сводки)', '']
    return t


def summary(ctx, findings, stat, base_diff):
    """Вывод в начале отчёта. Собирается из тех же находок, без фантазий: отдельным
    полем уезжает в json, чтобы читать (и скармливать модели) можно было его одного,
    а не сто двадцать пунктов."""
    pages = len(ctx.pages)
    t = ['Проверено %s, замечаний %d: критично %d, важно %d, мелочь %d.' % (
        checks.plural(pages, 'страница', 'страницы', 'страниц'), len(findings),
        stat['crit'], stat['warn'], stat['note'])]
    if base_diff:
        t.append('С прошлого прогона исправлено %d, появилось %d.' % (base_diff['fixed'], base_diff['new']))
    t += cover_note(ctx)
    groups = [g for g in group([f for f in findings if f['sure']])]
    groups.sort(key=rank)
    crit_cats = sorted({g['cat'] for g in groups if g['sev'] == 'crit'})
    if crit_cats:
        t.append('Критичное — в %s.' % ', '.join(CATNAME[c].split(' —')[0] for c in crit_cats))
    clean = [CATNAME[c].split(' —')[0] for c in ('SEO', 'AEO', 'GEO', 'SEM', 'TECH')
             if not any(g['cat'] == c and g['sev'] in ('crit', 'warn') for g in groups)]
    if clean:
        t.append('Без замечаний: %s.' % ', '.join(clean))
    dead = [p for p in ctx.pages if p.err or not 200 <= p.status < 300]
    if dead:
        t.append('%s не отвечает кодом 200.' % checks.plural(len(dead), 'страница', 'страницы', 'страниц'))
    out = [' '.join(t), '']
    # Сверки — своим абзацем: это единственное место, где цифру посчитал не аудит,
    # и слипшись с его собственными выводами она читается как его же.
    cr = cross(ctx, findings)
    if cr:
        out += [' '.join(cr), '']
    top = groups[:6]
    if top:
        out.append('Что делать в этом порядке:')
        out.append('')
        for i, g in enumerate(top, 1):
            n = len(g['items'])
            where = '' if n == 1 else ' — %s' % checks.plural(n, 'страница', 'страницы', 'страниц')
            why = g['fix'].split('; ')[0].strip()
            if len(why) > 150:
                why = why[:150].rsplit(' ', 1)[0] + '…'
            why = (why[:1].upper() + why[1:]).rstrip('.,') + '.' if why else ''
            out.append('%d. **%s**%s. %s' % (i, g['title'], where, why))
        if len(groups) > len(top):
            out.append('')
            out.append('Ниже — ещё %s замечаний по категориям.' % checks.plural(
                len(groups) - len(top), 'вид', 'вида', 'видов'))
    return '\n'.join(out)


def report_md(ctx, findings, stat, base_diff, verdict=''):
    """Отчёт. Первым разделом — `verdict.md` из папки сайта, если он там лежит:
    человеческий вывод живёт дольше одного прогона и пишется руками, скрипт его
    только подклеивает и никогда не перезаписывает."""
    t = ['# Аудит %s' % ctx.start,
         '',
         '_%s · %s_' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), ctx.start),
         '']
    if verdict:
        t += ['## Вердикт', '', verdict, '',
              '_написано руками в `verdict.md`, прогоны его не трогают_', '']
    t += ['## Коротко',
         '',
         summary(ctx, findings, stat, base_diff),
         '']
    ext = extern_md(ctx)
    if ext:
        t += ['## Чужими глазами', '',
              '_сводки соседних инструментов: цифры посчитаны ими, не аудитом_', ''] + ext
    unsure = [f for f in findings if not f['sure']]
    for cat in ('SEO', 'AEO', 'GEO', 'SEM', 'TECH'):
        got = [f for f in findings if f['cat'] == cat and f['sure']]
        if not got:
            continue
        t += ['## %s' % CATNAME[cat], '']
        for g in group(got):
            n = len(g['items'])
            t.append('- **%s** — %s%s' % (SEVNAME[g['sev']], g['title'],
                                          '' if n == 1 else '  _(%d шт.)_' % n))
            if g['fix']:
                t.append('  → %s' % g['fix'])
            for f in g['items'][:8]:
                extra = '' if f['msg'] == g['title'] else ' — %s' % f['msg']
                t.append('  - `%s`%s' % (ctx.short(f['url']), extra))
            if n > 8:
                t.append('  - …и ещё %d (полный список в json)' % (n - 8))
        t.append('')
    if unsure:
        t += ['## Нужен глаз — эвристики, не факты', '',
              'Эти пункты скрипт угадывает по признакам и может ошибаться. Проверять руками, '
              'лишнее — в `ignore` конфига.', '']
        for g in group(unsure):
            t.append('- [%s] %s%s' % (g['cat'], g['title'], '' if len(g['items']) == 1 else '  _(%d шт.)_' % len(g['items'])))
            for f in g['items'][:6]:
                extra = '' if f['msg'] == g['title'] else ' — %s' % f['msg']
                t.append('  - `%s`%s' % (ctx.short(f['url']), extra))
        t.append('')
    t += ['## Страницы', '', '| адрес | код | title | h1 | знаков | находок |', '|---|---|---|---|---|---|']
    for p in ctx.pages:
        t.append('| %s | %s | %d | %d | %d | %d |' % (
            ctx.short(p.url), p.err or p.status, len(p.title), len(p.h1), p.text_len, len(getattr(p, '_findings', []))))
    t += ['', '## Чего здесь нет', '',
          'Скрипт не рендерит JS, не меряет Core Web Vitals (это поле, а не разметка), не судит о качестве '
          'текста и не знает, по каким запросам страница должна находиться. Он проверяет разметку и связи. '
          'Всё остальное по-прежнему смотрят глазами.', '']
    return '\n'.join(t)


def main():
    ap = argparse.ArgumentParser(description='Мини-аудит SEO/AEO/GEO по чек-листу')
    ap.add_argument('url', nargs='?', help='стартовый адрес, например https://ritmo.studio/')
    ap.add_argument('--local', help='папка с готовыми .html вместо живого сайта')
    ap.add_argument('--out', default='audit-reports', help='куда класть отчёты')
    ap.add_argument('--max-pages', type=int, default=50)
    ap.add_argument('--max-depth', type=int, default=5)
    ap.add_argument('--crawl', choices=['auto', 'links', 'sample'], default='auto',
                    help='как набрать страницы: по ссылкам от главной, выборкой по формам пути '
                         'из sitemap или auto (выборка, если в sitemap адресов больше потолка)')
    ap.add_argument('--per-template', type=int, default=3,
                    help='сколько страниц брать на каждую форму пути в режиме выборки')
    ap.add_argument('--max-link-checks', type=int, default=0,
                    help='потолок HEAD-проверок ссылок, до которых не дошёл обход '
                         '(по умолчанию max(40, страниц))')
    ap.add_argument('--delay', type=float, default=1.0, help='пауза между запросами, с')
    ap.add_argument('--timeout', type=int, default=15)
    ap.add_argument('--budget', type=int, default=0, help='потолок запросов за прогон (по умолчанию 2×страниц+40)')
    ap.add_argument('--cache', type=int, default=0, help='секунд держать ответы в .audit-cache')
    ap.add_argument('--with-query', action='store_true', help='обходить адреса с параметрами')
    ap.add_argument('--ignore-robots', action='store_true', help='только для своего сайта')
    ap.add_argument('--insecure', action='store_true', help='не проверять сертификат')
    ap.add_argument('--config', help='json с порогами, ignore и словами-заполнителями')
    ap.add_argument('--baseline', help='json прошлого прогона для сравнения')
    ap.add_argument('--fail-on', choices=['crit', 'warn', 'none'], default='none',
                    help='вернуть код 2, если есть находки такого уровня (для cron и CI)')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--list', action='store_true', help='напечатать чек-лист и выйти')
    a = ap.parse_args()
    if a.list:
        for cat in ('SEO', 'AEO', 'GEO', 'SEM', 'TECH'):
            print('\n%s' % CATNAME[cat])
            for code, t in sorted(checks.TITLES.items()):
                if code.split('-')[0] == cat:
                    print('  %-22s %s' % (code, t))
        return 0
    log = (lambda *x: None) if a.quiet else (lambda *x: print(*x, flush=True))

    cfg = {}
    if a.config:
        cfg = json.loads(pathlib.Path(a.config).read_text(encoding='utf-8'))
    if a.local:
        d = pathlib.Path(a.local).resolve()
        start = 'file://%s/' % d
        cfg['_dir'] = str(d)
    elif a.url:
        start = norm_url(a.url if '://' in a.url else 'https://' + a.url)
    else:
        ap.error('нужен адрес сайта или --local')

    # Своя папка на каждый сайт: в общей куче двадцать прогонов трёх клиентов
    # перестают читаться к концу месяца, а `verdict.md` лежит рядом со своими
    # отчётами, а не угадывается по имени файла.
    tag = re.sub(r'\W+', '-', urllib.parse.urlsplit(start).netloc or pathlib.Path(start).name).strip('-') or 'local'
    out = pathlib.Path(a.out) / tag
    out.mkdir(parents=True, exist_ok=True)
    lock = out / 'run.lock'
    if lock.exists() and (datetime.datetime.now().timestamp() - lock.stat().st_mtime) < 7200:
        print('уже идёт прогон (%s). Если это не так — удалить файл.' % lock)
        return 1
    lock.write_text(str(os.getpid()))
    ctx = Ctx(start, cfg, local=bool(a.local), cached=bool(a.cache))
    ctx.extern = read_extern(out)
    try:
        if a.local:
            log('Локальный аудит: %s' % cfg['_dir'])
            crawl_local(ctx, log)
            check_rest(ctx, None, log)
        else:
            f = fetch.Fetcher(delay=a.delay, timeout=a.timeout,
                              budget=a.budget or (a.max_pages * 2 + 40),
                              cache=a.cache, obey_robots=not a.ignore_robots, insecure=a.insecure, log=log)
            log('Аудит %s — пауза %.1f с, потолок %d страниц' % (start, a.delay, a.max_pages))
            f.robots(start)
            # robots.txt и sitemap читаем до обхода: на большом сайте именно sitemap
            # решает, какие страницы брать.
            probes(ctx, f, log)
            sm = ctx.sitemap.get('urls') or []
            per = max(1, a.per_template)
            ctx.cover.update(sitemap=len(sm), cap=a.max_pages, per=per)
            mode = a.crawl
            if mode == 'auto':
                mode = 'sample' if len(sm) > a.max_pages else 'links'
            if mode == 'sample' and not sm:
                log('выборку делать не из чего: в sitemap нет адресов — идём по ссылкам')
                mode = 'links'
            ctx.cover['mode'] = mode
            if mode == 'sample':
                picked, sizes = sample(sm, per, a.max_pages)
                # Главная первой: если она не отвечает, остальное обсуждать нечего.
                picked = [start] + [u for u in picked if u != start][:a.max_pages - 1]
                ctx.cover.update(templates=len(sizes), picked=len(picked))
                log('В sitemap %d адресов, форм пути %d — берём %s по %d на форму'
                    % (len(sm), len(sizes),
                       checks.plural(len(picked), 'страницу', 'страницы', 'страниц'), per))
                crawl(ctx, f, a.max_pages, 0, a.with_query, log, seeds=picked)
            else:
                crawl(ctx, f, a.max_pages, a.max_depth, a.with_query, log)
            check_rest(ctx, f, log, cap=a.max_link_checks or max(40, a.max_pages))
            log('запросов потрачено: %d' % f.used)
    except fetch.Stop as e:
        log('ОСТАНОВ: %s. Отчёт соберём по тому, что успели.' % e)
    except KeyboardInterrupt:
        log('прервано вручную')
    finally:
        try:
            lock.unlink()
        except OSError:
            pass

    if not ctx.local:
        crawled = {p.url for p in ctx.pages}
        unseen = [u for u in (ctx.sitemap.get('urls') or []) if u not in crawled and u not in ctx.link_src]
        # Про сирот и перелинковку можно говорить только когда граф пройден целиком:
        # обход по ссылкам, не упёрся в потолок и в sitemap нет адресов, которых мы
        # вообще не видели. Иначе любая невзятая страница выглядит сиротой.
        ctx.cover['full_graph'] = (ctx.cover['mode'] == 'links'
                                   and len(ctx.pages) < ctx.cover['cap']
                                   and not unseen)

    run_checks(ctx)
    findings = [f for p in ctx.pages for f in getattr(p, '_findings', [])] + run_site_checks(ctx)
    skipped = [f for f in findings if ctx.ignored(f)]
    findings = [f for f in findings if not ctx.ignored(f)]
    if skipped:
        log('снято правилами ignore: %d' % len(skipped))
    stat = {k: sum(1 for f in findings if f['sev'] == k) for k in SEV}
    base_diff = None
    if a.baseline and pathlib.Path(a.baseline).exists():
        old = json.loads(pathlib.Path(a.baseline).read_text(encoding='utf-8'))
        was = {(f['code'], f['url']) for f in old.get('findings', [])}
        now = {(f['code'], f['url']) for f in findings}
        base_diff = {'fixed': len(was - now), 'new': len(now - was)}

    stamp = datetime.datetime.now().strftime('%Y-%m-%d-%H%M')
    brief = summary(ctx, findings, stat, base_diff)
    data = dict(start=start, when=stamp, summary=brief, pages=[dict(url=p.url, status=p.err or p.status, title=p.title,
                                                     h1=p.h1, text=p.text_len) for p in ctx.pages],
                stat=stat, diff=base_diff, findings=sorted(findings, key=key))
    verdict = ''
    vf = out / 'verdict.md'
    if vf.exists():
        verdict = vf.read_text(encoding='utf-8').strip()
    md_path = out / ('%s.md' % stamp)
    md_path.write_text(report_md(ctx, findings, stat, base_diff, verdict), encoding='utf-8')
    (out / ('%s.json' % stamp)).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    (out / 'latest.json').write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
    print('\n' + brief)
    print('\nотчёт: %s' % md_path)
    if a.fail_on == 'crit' and stat['crit']:
        return 2
    if a.fail_on == 'warn' and (stat['crit'] or stat['warn']):
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
