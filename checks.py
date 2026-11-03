#!/usr/bin/env python3
"""Чек-лист. Одна проверка — одна функция, добавленная в реестр декоратором.

Две оси у каждой находки, и обе важнее формулировки:

  cat — SEO (попасть в индекс и в выдачу), AEO (чтобы ответ можно было собрать
        и процитировать), GEO (чтобы сущность узнавали между страницами),
        SEM (семантика и доступность), TECH (техника).
  sure — True: факт разметки, спорить не о чем. False: эвристика, может
        соврать, человек смотрит глазами. Смешивать их в одну цифру нельзя:
        отчёт, где «рыба в тексте» лежит рядом с «нет h1», перестают читать.

Порог в коде — не истина, а принятое решение; все пороги собраны в LIM и
переопределяются конфигом, чтобы не спорить с кодом из-за одного сайта.
"""
import re, urllib.parse

LIM = dict(
    title_max=65, title_min=25,          # ~65 знаков — предел сниппета в выдаче
    desc_max=170, desc_min=70,
    text_min=1200,                       # ниже — страница тонкая для своей темы
    html_max=160 * 1024,
    ms_slow=1500,
    head_scripts=2,
    empty_li=2,
)
PLACEHOLDER = ['рыба', 'lorem ipsum', 'заполнител', 'текст текст', 'placeholder',
               'название блока', 'заголовок h', 'тестовый текст', 'описание услуги',
               'todo', 'tbd', 'ваш текст', 'сюда текст']
GENERIC_ANCHOR = ['тут', 'здесь', 'подробнее', 'подробне', 'читать', 'читать далее', 'далее',
                  'ссылка', 'перейти', 'click here', 'link', 'подробности', 'смотреть']
INTERACTIVE = ('a', 'button', 'input', 'select', 'textarea', 'summary', 'label', 'option')
NUMWORD = re.compile(r'\b(од(ин|на)|дв(а|е)|три|четыре|пять|шесть|семь|восемь|девять|десять|\d+)\b', re.I)

PAGE, SITE = [], []
page_check = lambda fn: (PAGE.append(fn), fn)[1]
site_check = lambda fn: (SITE.append(fn), fn)[1]


def F(code, cat, sev, msg, fix='', url='', sure=True):
    return dict(code=code, cat=cat, sev=sev, msg=msg, fix=fix, url=url, sure=sure)


def plural(n, one, few, many):
    """Отчёт читают люди, «3 ссылок» в нём быть не должно."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return '%d %s' % (n, one)
    if n10 in (2, 3, 4) and n100 not in (12, 13, 14):
        return '%d %s' % (n, few)
    return '%d %s' % (n, many)


def lim(ctx, k):
    return ctx.cfg.get('limits', {}).get(k, LIM[k])


# ─────────────────────────── SEO: ответ, мета, заголовки ───────────────────────────

@page_check
def c_status(p, ctx):
    if p.err:
        yield F('TECH-ERR', 'TECH', 'crit', 'страница не загрузилась: %s' % p.err,
                'проверить доступность и сертификат', p.url)
    elif not (200 <= p.status < 300):
        yield F('SEO-STATUS', 'SEO', 'crit', 'ответ %d, а на страницу ведут ссылки' % p.status,
                'вернуть 200 или убрать ссылки на этот адрес', p.url)
    if len(p.chain) > 1:
        yield F('TECH-CHAIN', 'TECH', 'warn', 'цепочка редиректов из %d шагов' % len(p.chain),
                'вести сразу на конечный адрес: каждый шаг теряет часть веса и времени', p.url)
    if p.ms > lim(ctx, 'ms_slow') and not ctx.cached:
        yield F('TECH-SLOW', 'TECH', 'note', 'ответ сервера %d мс (без картинок и стилей)' % p.ms,
                'это время до первого байта, не скорость страницы; смотреть кэш и бэкенд', p.url)
    if p.html and len(p.html) > lim(ctx, 'html_max'):
        yield F('TECH-SIZE', 'TECH', 'note', 'HTML %d КБ' % (len(p.html) // 1024),
                'тяжёлая разметка замедляет разбор; смотреть инлайн-стили и повторы', p.url)
    if p.html and not p.headers.get('content-encoding') and not ctx.local and not ctx.cached:
        yield F('TECH-GZIP', 'TECH', 'warn', 'HTML отдаётся без сжатия (нет Content-Encoding)',
                'включить gzip/brotli на сервере: это самая дешёвая правка скорости', p.url)


@page_check
def c_title(p, ctx):
    t = p.title
    if not t:
        yield F('SEO-TITLE-MISSING', 'SEO', 'crit', 'нет title',
                'title — главный текст страницы для выдачи', p.url)
        return
    n = len(t)
    if n > lim(ctx, 'title_max'):
        yield F('SEO-TITLE-LONG', 'SEO', 'warn', 'title %d знаков — хвост обрежется в выдаче: «%s»' % (n, t),
                'уложить смысл в первые %d знаков, значимое вперёд' % lim(ctx, 'title_max'), p.url)
    elif n < lim(ctx, 'title_min'):
        yield F('SEO-TITLE-SHORT', 'SEO', 'note', 'title %d знаков: «%s»' % (n, t),
                'короткий title отдаёт поиску меньше слов, чем можно', p.url)
    if p.h1 and p.h1[0].strip().lower() == t.strip().lower():
        yield F('SEO-H1-EQ-TITLE', 'SEO', 'note', 'h1 дословно равен title',
                'не ошибка, но заголовок — шанс взять второй набор слов, а он потрачен на копию', p.url)


@page_check
def c_desc(p, ctx):
    d = p.meta.get('description', '').strip()
    if not d:
        yield F('SEO-DESC-MISSING', 'SEO', 'warn', 'нет meta description',
                'поиск соберёт сниппет сам — из случайного куска текста', p.url)
        return
    if len(d) > lim(ctx, 'desc_max'):
        yield F('SEO-DESC-LONG', 'SEO', 'note', 'description %d знаков — обрежется' % len(d), '', p.url)
    elif len(d) < lim(ctx, 'desc_min'):
        yield F('SEO-DESC-SHORT', 'SEO', 'note', 'description %d знаков' % len(d), '', p.url)


@page_check
def c_headings(p, ctx):
    if not p.h1:
        yield F('SEO-H1-MISSING', 'SEO', 'crit', 'нет h1',
                'ровно один h1 на страницу; в этой библиотеке уровень задаёт heading_tag блока', p.url)
    elif len(p.h1) > 1:
        yield F('SEO-H1-MANY', 'SEO', 'crit', 'h1 на странице %d: %s' % (len(p.h1), ' / '.join(p.h1[:3])),
                'h1 должен быть один — его назначает тот, кто собирает страницу', p.url)
    lvls = [l for l, _ in p.headings]
    if lvls and 2 not in lvls and len(p.text) > lim(ctx, 'text_min'):
        yield F('SEO-H2-NONE', 'SEO', 'warn', 'после h1 нет ни одного h2 — структура плоская',
                'разбить текст подзаголовками: по ним собирают быстрые ответы', p.url)
    prev = 0
    for l, t in p.headings:
        if prev and l > prev + 1:
            yield F('SEO-HEAD-SKIP', 'SEO', 'note', 'уровень h%d после h%d: «%s»' % (l, prev, t[:60]),
                    'уровни не пропускать — иерархия читается буквально', p.url)
            break
        prev = l
    for l, t in p.headings:
        if not t:
            yield F('SEM-H-EMPTY', 'SEM', 'warn', 'пустой заголовок h%d' % l,
                    'заголовок без текста — обычно остаток разметки', p.url)
            break


@page_check
def c_head_basics(p, ctx):
    if not p.lang:
        yield F('SEO-LANG-MISSING', 'SEO', 'warn', 'нет lang у html',
                'lang="ru" — язык страницы для поиска, скринридера и перевода', p.url)
    if not p.meta.get('viewport'):
        yield F('SEO-VIEWPORT-MISSING', 'SEO', 'crit', 'нет meta viewport',
                'без него мобильная версия не считается мобильной', p.url)
    if not p.charset and 'charset' not in (p.headers.get('content-type') or ''):
        yield F('SEO-CHARSET-MISSING', 'SEO', 'warn', 'не объявлена кодировка', '', p.url)
    rob = (p.meta.get('robots') or '').lower()
    if 'noindex' in rob:
        yield F('SEO-NOINDEX', 'SEO', 'crit', 'на странице noindex',
                'если это не черновик — страницы нет в поиске вообще', p.url)
    c = p.canonical()
    if not c:
        yield F('SEO-CANON-MISSING', 'SEO', 'warn', 'нет canonical',
                'без него адреса с параметрами и слешем считаются разными страницами', p.url)
    else:
        cu = urllib.parse.urlsplit(p.abs(c))
        if not cu.netloc:
            yield F('SEO-CANON-REL', 'SEO', 'warn', 'canonical относительный: %s' % c,
                    'canonical пишется абсолютным адресом', p.url)
        elif cu.netloc != urllib.parse.urlsplit(p.url).netloc:
            yield F('SEO-CANON-HOST', 'SEO', 'crit', 'canonical ведёт на другой хост: %s' % c,
                    'страница сама отдаёт вес чужому адресу', p.url)
    sh = 0
    for tag, a in p.doc.els:
        if tag == 'body':
            break
        if tag == 'script' and a.get('src') and 'defer' not in a and 'async' not in a:
            sh += 1
    if sh > lim(ctx, 'head_scripts'):
        yield F('TECH-BLOCKING', 'TECH', 'note', 'в head %d блокирующих скриптов' % sh,
                'defer/async: пока они грузятся, страница не рисуется', p.url)


# ─────────────────────────── SEO: содержимое ───────────────────────────

@page_check
def c_images(p, ctx):
    imgs = [a for t, a in p.doc.els if t == 'img']
    no_alt = [a for a in imgs if 'alt' not in a]
    no_dim = [a for a in imgs if not (a.get('width') and a.get('height')) and not a.get('style')]
    if no_alt:
        yield F('SEO-IMG-ALT', 'SEO', 'warn', '%d из %d img без атрибута alt (%s)' % (
            len(no_alt), len(imgs), ', '.join(x.get('src', '?').rsplit('/', 1)[-1][:34] for x in no_alt[:3])),
                'alt="" для декора, текст — для смысловых: это весь вход в Картинки', p.url)
    if no_dim:
        yield F('TECH-IMG-DIM', 'TECH', 'note', '%d img без width/height' % len(no_dim),
                'по заявленным размерам считается CLS; ставить реальные размеры файла', p.url)
    if imgs and not any('lazy' in (a.get('loading') or '') for a in imgs) and len(imgs) > 4:
        yield F('TECH-IMG-LAZY', 'TECH', 'note', '%d картинок, ни одной с loading="lazy"' % len(imgs),
                'первый экран — eager, остальное lazy', p.url)
    if not imgs and len(p.text) > lim(ctx, 'text_min'):
        yield F('SEO-NO-IMG', 'SEO', 'note', 'на странице нет ни одной картинки в разметке',
                'LCP считается по тексту, Картинки не получают ничего', p.url)


@page_check
def c_links(p, ctx):
    empty = [(h, t) for u, t, a, h in p.links if (h or '').strip() in ('#', '', 'javascript:;', 'javascript:void(0)')]
    if empty:
        yield F('SEO-LINK-EMPTY', 'SEO', 'crit', '%s в никуда: href="%s"%s' % (
            plural(len(empty), 'ссылка', 'ссылки', 'ссылок'), empty[0][0], ' («%s»)' % empty[0][1][:40] if empty[0][1] else ''),
                'в проде href="#" — обычно незаполненный слот контента', p.url)
    gen = [t for u, t, a, h in p.links if t and t.strip(' .,:»«—-').lower() in GENERIC_ANCHOR]
    if gen:
        yield F('SEO-LINK-ANCHOR', 'SEO', 'note', '%s с анкором без смысла: %s' % (
            plural(len(gen), 'ссылка', 'ссылки', 'ссылок'), ', '.join('«%s»' % g for g in gen[:3])),
                'анкор — это тема той страницы, а «подробнее» не тема', p.url, sure=False)
    nof = [u for u, t, a, h in p.links if u and 'nofollow' in (a.get('rel') or '').lower() and ctx.internal(u)]
    if nof:
        yield F('SEO-LINK-NOFOLLOW', 'SEO', 'warn', '%d внутренних ссылок с rel=nofollow' % len(nof),
                'внутри сайта nofollow только мешает: вес не доходит', p.url)
    if p.url.startswith('https://'):
        http = set()
        for tag, a in p.doc.els:
            for k in ('src', 'href'):
                v = a.get(k) or ''
                if v.startswith('http://') and tag in ('img', 'script', 'link', 'iframe', 'source', 'video'):
                    http.add(v)
        if http:
            yield F('TECH-MIXED', 'TECH', 'crit', '%d ресурсов по http на https-странице' % len(http),
                    'браузер их заблокирует: %s' % list(http)[0][:70], p.url)


@page_check
def c_social(p, ctx):
    have = lambda k: bool((p.prop.get(k) or p.meta.get(k) or '').strip())
    miss = [k for k in ('og:title', 'og:description', 'og:image', 'og:url') if not have(k)]
    if miss:
        yield F('SEO-OG-MISSING', 'SEO', 'warn', 'нет %s' % ', '.join(miss),
                'без них превью в мессенджерах собирается случайно', p.url)
    if not p.rels.get('icon') and not p.rels.get('shortcut'):
        yield F('SEO-ICON-MISSING', 'SEO', 'note', 'не объявлен favicon', '', p.url)
    if not p.rels.get('apple-touch-icon'):
        yield F('SEO-ICON-APPLE', 'SEO', 'note', 'нет apple-touch-icon', '', p.url)
    t = (p.prop.get('og:type') or '').lower()
    kinds = p.ld_types()
    if t in ('', 'website') and ({'Person'} & kinds):
        yield F('SEO-OG-TYPE', 'SEO', 'warn', 'og:type=%s, хотя страница про Person' % (t or 'не задан'),
                'на странице человека og:type=profile, у статьи article', p.url)
    if t in ('', 'website') and ({'Article', 'BlogPosting', 'NewsArticle'} & kinds):
        yield F('SEO-OG-TYPE', 'SEO', 'warn', 'og:type=%s на странице статьи' % (t or 'не задан'),
                'og:type=article', p.url)


@page_check
def c_content(p, ctx):
    n = len(p.text)
    if n < lim(ctx, 'text_min'):
        yield F('SEO-THIN', 'SEO', 'warn', 'текста %d знаков' % n,
                'нечего оценивать на релевантность и нечего цитировать; порог условный — смотреть глазами',
                p.url, sure=False)
    low = p.text.lower()
    words = list(PLACEHOLDER) + list(ctx.cfg.get('placeholders', []))
    for w in words:
        i = low.find(w)
        if i >= 0:
            yield F('SEO-PLACEHOLDER', 'SEO', 'crit', 'в тексте «%s»: …%s…' % (w, p.text[max(0, i - 30):i + 50]),
                    'похоже на заполнитель, уехавший в прод; если слово по теме сайта — добавить в ignore',
                    p.url, sure=False)
            break
    empty_li = sum(1 for t in p.doc.list_items if not t)
    if empty_li >= lim(ctx, 'empty_li'):
        yield F('SEO-EMPTY-LI', 'SEO', 'warn', '%s без содержимого' % plural(empty_li, 'пункт li', 'пункта li', 'пунктов li'),
                'пустые слоты списка — незаполненный контент, а не разметка', p.url)
    if p.html and n < 400 and p.tags.get('script', 0) >= 3:
        yield F('GEO-JS-ONLY', 'GEO', 'crit', 'в HTML почти нет текста (%d знаков) при %d скриптах' % (
            n, p.tags.get('script', 0)),
                'контент рисуется JS: краулеры поиска это переживут, боты языковых моделей — нет', p.url)
    for tag, a in p.doc.els:
        cls = (a.get('class') or '') + ' ' + (a.get('id') or '')
        if re.search(r'preloader|preload-curtain|loading-screen', cls, re.I):
            yield F('GEO-CURTAIN', 'GEO', 'note', 'на странице занавес-прелоадер (%s)' % cls.strip()[:40],
                    'скриншотеры и превью могут поймать его вместо первого экрана', p.url, sure=False)
            break


# ─────────────────────────── AEO и GEO: разметка сущностей ───────────────────────────

ENTITY = ('Organization', 'LocalBusiness', 'Person', 'WebSite', 'WebPage', 'Article',
          'BlogPosting', 'NewsArticle', 'Service', 'Product', 'MedicalClinic')


@page_check
def c_ld_basics(p, ctx):
    for e in p.ld_errors:
        yield F('AEO-LD-BROKEN', 'AEO', 'crit', 'JSON-LD не разбирается (%s)' % e,
                'битая разметка не читается никем — проверить запятые и кавычки', p.url)
    if not p.jsonld:
        yield F('AEO-NO-SCHEMA', 'AEO', 'crit', 'на странице нет ни одного JSON-LD',
                'без разметки модель разбирает прозу и ошибается в фактах', p.url)
        return
    for n in p.jsonld:
        types = [v for v in n.get('@type', []) if isinstance(v, str)] if isinstance(n.get('@type'), list) else [n.get('@type')]
        for t in types:
            if t in ENTITY and not n.get('@id'):
                yield F('GEO-NO-ID', 'GEO', 'crit', '%s без @id (name: %s)' % (t, str(n.get('name'))[:40]),
                        'нужен URI вида %s#%s: без него узлы с разных страниц — не одна сущность, '
                        'а независимые обрывки' % (ctx.root, t.lower()), p.url)
    for key in ('worksFor', 'publisher', 'author', 'parentOrganization', 'provider'):
        for n in p.jsonld:
            v = n.get(key)
            if isinstance(v, dict) and v.get('name') and not v.get('@id'):
                yield F('GEO-REF-BY-NAME', 'GEO', 'warn', '%s задан объектом с name, без @id' % key,
                        'ссылаться на сущность: {"@id": "%s#organization"} — иначе на каждой странице '
                        'рождается новая' % ctx.root, p.url)
                break
    if not (p.ld_of('WebPage', 'WebSite', 'CollectionPage', 'AboutPage', 'ProfilePage', 'ItemPage')):
        yield F('GEO-NO-WEBPAGE', 'GEO', 'warn', 'нет узла WebPage/WebSite',
                'страница не заявляет, о чём она: нужен WebPage с mainEntity на главную сущность', p.url)
    else:
        if not any(n.get('mainEntity') or n.get('mainEntityOfPage') for n in p.jsonld):
            yield F('GEO-NO-MAINENTITY', 'GEO', 'warn', 'WebPage есть, mainEntity нет',
                    'mainEntity → узел, о котором страница; иначе поиск догадывается по h1', p.url)


@page_check
def c_ld_person(p, ctx):
    for n in p.ld_of('Person'):
        who = str(n.get('name') or '')[:40]
        for key, why in (('description', 'биография обычно уже написана в тексте — положить её в поле; '
                                         'модель получит готовую формулировку вместо разбора абзаца'),
                         ('image', 'портрет сущности'),
                         ('sameAs', 'соцсети и профили: так узел склеивается с внешними источниками'),
                         ('jobTitle', 'кем человек работает, одной строкой')):
            if not n.get(key) and not (key == 'jobTitle' and n.get('hasOccupation')):
                yield F('AEO-PERSON-%s' % key.upper(), 'AEO', 'warn' if key in ('description', 'sameAs') else 'note',
                        'Person «%s» без %s' % (who, key), why, p.url)


@page_check
def c_ld_org(p, ctx):
    for n in p.ld_of('Organization', 'LocalBusiness', 'MedicalClinic'):
        who = str(n.get('name') or '')[:40]
        if not (n.get('telephone') or n.get('email') or n.get('contactPoint')):
            yield F('AEO-ORG-CONTACT', 'AEO', 'crit', 'Organization «%s» без контактов' % who,
                    'на вопрос «как связаться» ответ собрать нечем: telephone, email или contactPoint', p.url)
        for key in ('url', 'logo', 'sameAs', 'address'):
            if not n.get(key):
                yield F('AEO-ORG-%s' % key.upper(), 'AEO', 'note', 'Organization «%s» без %s' % (who, key), '', p.url)
    for n in p.jsonld:
        for a in (n.get('award') or []) if isinstance(n.get('award'), list) else ([n['award']] if n.get('award') else []):
            if isinstance(a, str) and (len(a) > 60 or NUMWORD.search(a)):
                yield F('AEO-AWARD-PROSE', 'AEO', 'note', 'награда записана прозой: «%s»' % a[:70],
                        'модель прочитает, парсер нет: числа и названия отдельными строками', p.url, sure=False)
                break


@page_check
def c_ld_article(p, ctx):
    for n in p.ld_of('Article', 'BlogPosting', 'NewsArticle'):
        miss = [k for k in ('author', 'datePublished', 'dateModified', 'headline') if not n.get(k)]
        if miss:
            yield F('AEO-ARTICLE-META', 'AEO', 'warn', 'Article без %s' % ', '.join(miss),
                    'без автора и даты статья не проходит как источник', p.url)


@page_check
def c_breadcrumbs(p, ctx):
    if ctx.is_home(p.url):
        return
    if p.ld_of('BreadcrumbList'):
        return
    visual = any(re.search(r'breadcrumb|крошк', (a.get('class') or '') + (a.get('aria-label') or ''), re.I)
                 for t, a in p.doc.els)
    yield F('SEO-NO-BREADCRUMBS', 'SEO', 'warn',
            'нет хлебных крошек%s' % ('' if not visual else ' в разметке BreadcrumbList (визуальные есть)'),
            'крошки дают сайтлинки в выдаче и объясняют, у кого страница в родителях', p.url)


@page_check
def c_quotable(p, ctx):
    """AEO: ответ собирают из списков, таблиц и коротких абзацев, а не из полотна."""
    if len(p.text) < lim(ctx, 'text_min'):
        return
    if not (p.tags.get('ul', 0) or p.tags.get('ol', 0) or p.tags.get('table', 0) or p.tags.get('dl', 0)):
        yield F('AEO-NO-LISTS', 'AEO', 'note', 'на длинной странице ни одного списка и ни одной таблицы',
                'факты списком цитируются, полотно прозы — нет', p.url, sure=False)


# ─────────────────────────── Семантика и доступность ───────────────────────────

@page_check
def c_semantics(p, ctx):
    seen, dup = set(), []
    for i in p.ids:
        (dup.append(i) if i in seen else seen.add(i))
    if dup:
        yield F('SEM-DUP-ID', 'SEM', 'crit', 'повторяющиеся id: %s' % ', '.join(sorted(set(dup))[:5]),
                'дубль id — невалидный HTML; в этой библиотеке id в блоках не ставятся вообще, '
                'потому что блок может повторяться на странице', p.url)
    for tag, a in p.doc.els:
        if 'aria-expanded' in a and not a.get('aria-controls'):
            yield F('SEM-ARIA-EXPANDED', 'SEM', 'warn', '<%s aria-expanded> без aria-controls' % tag,
                    'разметка обещает раскрытие: нужен aria-controls и обработчик, иначе атрибут убрать', p.url)
            break
    clickable = [t for t, a in p.doc.els
                 if a.get('onclick') and t not in INTERACTIVE and not a.get('role') and 'tabindex' not in a]
    if clickable:
        yield F('SEM-CLICKABLE-DIV', 'SEM', 'warn', 'кликабельных <%s> без role и tabindex: %d' % (
            clickable[0], len(clickable)),
                'с клавиатуры недостижимо; либо button, либо role+tabindex', p.url)
    noname = [h for u, t, a, h in p.links
              if not t and not (a.get('aria-label') or a.get('title')) and h and not h.startswith('#')]
    if noname:
        yield F('SEM-LINK-NONAME', 'SEM', 'warn', '%s без текста и aria-label (%s)' % (
            plural(len(noname), 'ссылка', 'ссылки', 'ссылок'), noname[0][:40]), 'ссылка-иконка нуждается в aria-label', p.url)
    btn = [a for t, a in p.doc.els if t == 'button:text']
    nb = [a for a in btn if not a.get('text') and not (a.get('aria-label') or a.get('title'))]
    if nb:
        yield F('SEM-BTN-NONAME', 'SEM', 'warn', '%s без доступного имени' % plural(
            len(nb), 'кнопка', 'кнопки', 'кнопок'), 'aria-label или текст внутри', p.url)
    fors = {a.get('for') for t, a in p.doc.els if t == 'label' and a.get('for')}
    bad = [a for t, a in p.doc.els if t in ('input', 'select', 'textarea')
           and (a.get('type') or 'text') not in ('hidden', 'submit', 'button', 'image')
           and not (a.get('aria-label') or a.get('aria-labelledby') or a.get('title') or a.get('id') in fors)]
    if bad:
        yield F('SEM-INPUT-LABEL', 'SEM', 'warn', '%s без label/aria-label' % plural(len(bad), 'поле формы', 'поля формы', 'полей формы'),
                'поле без подписи не читается скринридером и хуже заполняется', p.url)
    if any(t == 'iframe' and not a.get('title') for t, a in p.doc.els):
        yield F('SEM-IFRAME-TITLE', 'SEM', 'note', 'iframe без title', '', p.url)
    if any(t == 'svg' and 'viewbox' not in a for t, a in p.doc.els):
        yield F('SEM-SVG-VIEWBOX', 'SEM', 'note', 'svg без viewBox',
                'размеры берутся из CSS; вне CSS даст нулевую картинку', p.url)
    pos = [a.get('tabindex') for t, a in p.doc.els if (a.get('tabindex') or '0').lstrip('+').isdigit()
           and int(a.get('tabindex') or 0) > 0]
    if pos:
        yield F('SEM-TABINDEX', 'SEM', 'note', 'tabindex больше нуля (%d шт.)' % len(pos),
                'ломает естественный порядок обхода', p.url)
    for t in ('main', 'header', 'nav'):
        if not p.tags.get(t) and p.html:
            yield F('SEM-NO-%s' % t.upper(), 'SEM', 'note', 'нет <%s>' % t,
                    'ориентиры страницы: по ним ходят скринридеры и парсеры контента', p.url)


# ─────────────────────────── Сайт целиком ───────────────────────────
# Самые дорогие дефекты видны только на уровне сайта: страница-сирота сама про себя
# не знает, что на неё никто не ссылается, а дубль title видно лишь при сравнении.

@site_check
def s_duplicates(ctx):
    for field, label, code, sev in (('title', 'title', 'SEO-TITLE-DUP', 'warn'),
                                    ('desc', 'description', 'SEO-DESC-DUP', 'note'),
                                    ('h1first', 'h1', 'SEO-H1-DUP', 'note')):
        seen = {}
        for p in ctx.ok_pages():
            v = (getattr(p, field, '') or '').strip().lower()
            if v:
                seen.setdefault(v, []).append(p.url)
        for v, urls in seen.items():
            if len(urls) > 1:
                yield F(code, 'SEO', sev, '%s совпадает у %d страниц: «%s»' % (label, len(urls), v[:60]),
                        'страницы конкурируют между собой: %s' % ', '.join(u for u in urls[:4]), urls[0])


@site_check
def s_broken_links(ctx):
    bad = [(u, st) for u, st in ctx.link_status.items() if st and not (200 <= st < 300)]
    for u, st in sorted(bad)[:40]:
        src = sorted(ctx.link_src.get(u, []))[:3]
        yield F('SEO-LINK-404', 'SEO', 'crit', '%s → %d (ведёт с %s)' % (u, st, ', '.join(src) or '?'),
                'краулер тратит бюджет на несуществующее и считает сайт недоделанным',
                src[0] if src else u)
    if len(bad) > 40:
        yield F('SEO-LINK-404', 'SEO', 'crit', 'ещё %d битых внутренних ссылок (список обрезан)' % (len(bad) - 40),
                'смотреть report.json', ctx.start)


@site_check
def s_orphans(ctx):
    linked = set(ctx.link_src.keys())
    # Нет ни одной связи между страницами — это не сайт, а папка с фрагментами
    # (локальный прогон по demo/ или по блокам клиента). Говорить про сиротство нечего.
    known = {p.url for p in ctx.pages}
    if not any(s - {u} for u, s in ctx.link_src.items() if u in known):
        return
    for p in ctx.ok_pages():
        if ctx.is_home(p.url):
            continue
        srcs = {s for s in ctx.link_src.get(p.url, set()) if s != p.url}
        if not srcs:
            where = 'есть только в sitemap' if p.url in ctx.sitemap.get('urls', []) else 'нашлась только по прямому адресу'
            yield F('SEO-ORPHAN', 'SEO', 'crit', 'на страницу не ссылается ни одна другая — %s' % where,
                    'без входящей внутренней ссылки вес не передаётся, а переобход редкий — '
                    'это дороже любой правки мета-тегов', p.url)
    for u in ctx.sitemap.get('urls', []):
        if u not in {p.url for p in ctx.pages} and u not in linked:
            yield F('SEO-SITEMAP-ORPHAN', 'SEO', 'warn', 'адрес из sitemap не встречается в ссылках: %s' % u,
                    'sitemap — не замена внутренней перелинковке', u)


@site_check
def s_robots(ctx):
    r = ctx.robots
    if r.get('status') != 200:
        yield F('SEO-ROBOTS-TXT', 'SEO', 'warn', 'robots.txt отвечает %s' % r.get('status'),
                'нужен хотя бы пустой с директивой Sitemap', ctx.root)
        return
    txt = r.get('text') or ''
    if not re.search(r'(?mi)^\s*sitemap\s*:', txt):
        yield F('SEO-ROBOTS-SITEMAP', 'SEO', 'warn', 'в robots.txt нет директивы Sitemap', '', ctx.root)
    if re.search(r'(?mi)^\s*disallow\s*:\s*/\s*$', txt):
        yield F('SEO-ROBOTS-ALL', 'SEO', 'crit', 'в robots.txt есть Disallow: / — сайт закрыт целиком',
                'если это не тестовый контур, поиска у сайта нет', ctx.root)


@site_check
def s_sitemap(ctx):
    sm = ctx.sitemap
    if sm.get('status') != 200:
        yield F('SEO-SITEMAP-MISSING', 'SEO', 'warn', 'sitemap.xml отвечает %s' % sm.get('status'),
                'карта ускоряет переобход, особенно новым страницам', ctx.root)
        return
    if sm.get('err'):
        yield F('SEO-SITEMAP-BROKEN', 'SEO', 'warn', 'sitemap.xml не разбирается: %s' % sm['err'], '', ctx.root)
    urls = set(sm.get('urls', []))
    crawled = {p.url for p in ctx.ok_pages()}
    missing = sorted(crawled - urls)
    if missing:
        yield F('SEO-SITEMAP-GAP', 'SEO', 'note', '%d найденных страниц нет в sitemap: %s' % (
            len(missing), ', '.join(missing[:3])), '', ctx.root)


@site_check
def s_probes(ctx):
    pr = ctx.probe
    if pr.get('soft404') and 200 <= pr['soft404'] < 300:
        yield F('SEO-SOFT404', 'SEO', 'crit', 'несуществующий адрес отвечает %d вместо 404' % pr['soft404'],
                'поиск наберёт бесконечно много пустых страниц-дублей', ctx.root)
    if pr.get('http_final') and not pr['http_final'].startswith('https://'):
        yield F('TECH-HTTPS', 'TECH', 'crit', 'http-версия не уводит на https (ушла на %s)' % pr['http_final'],
                'редирект 301 на https, иначе две копии сайта', ctx.root)
    if pr.get('llms') and pr['llms'] != 200:
        yield F('GEO-LLMS-TXT', 'GEO', 'note', 'нет /llms.txt',
                'официально его не читает ни один из крупных вендоров и подтверждённого влияния нет; '
                'стоит двадцати строк и делается, когда на сайте есть что описывать', ctx.root, sure=False)


@site_check
def s_entities(ctx):
    orgs, persons, faq, services = [], [], [], []
    for p in ctx.ok_pages():
        for n in p.jsonld:
            ts = {v for v in (n.get('@type') if isinstance(n.get('@type'), list) else [n.get('@type')]) if isinstance(v, str)}
            if ts & {'Organization', 'LocalBusiness', 'MedicalClinic'}:
                orgs.append((p.url, n.get('@id'), str(n.get('name') or '')))
            if 'Person' in ts:
                persons.append((p.url, n.get('@id'), str(n.get('name') or '')))
            if 'FAQPage' in ts or 'Question' in ts:
                faq.append(p.url)
            if ts & {'Service', 'OfferCatalog', 'Offer'} or n.get('hasOfferCatalog'):
                services.append(p.url)
    if not orgs:
        yield F('GEO-NO-ORG', 'GEO', 'crit', 'Organization не отдаётся ни на одной странице',
                'сайт не заявляет, чья это студия: узел Organization с @id и ссылками на него со страниц', ctx.root)
    for group, kind in ((orgs, 'Organization'), (persons, 'Person')):
        by_name = {}
        for url, nid, name in group:
            by_name.setdefault(name.strip().lower(), set()).add(nid)
        for name, ids in by_name.items():
            if len(ids) > 1 or (len(ids) == 1 and None in ids and len(group) > 1):
                yield F('GEO-ENTITY-SPLIT', 'GEO', 'crit',
                        '%s «%s» описан на нескольких страницах с разными @id (%s)' % (
                            kind, name[:40], ', '.join(str(i) for i in sorted(ids, key=str))[:120]),
                        'из нескольких страниц собирается граф одной сущности только через один и тот же URI', ctx.root)
    if not faq:
        yield F('AEO-NO-FAQ', 'AEO', 'note', 'на сайте нет ни одного FAQPage',
                'вопросы-ответы (сроки, бюджет, этапы) — самый прямой путь в AI Overviews и в ответы моделей',
                ctx.root, sure=False)
    if orgs and not services:
        yield F('AEO-NO-SERVICES', 'AEO', 'warn', 'услуги нигде не структурированы',
                'для ответа «что делает студия» прозы хватает, для «какие услуги и сколько стоит» — '
                'нужен Service или OfferCatalog', ctx.root)


# ─────────────────────────── названия проверок ───────────────────────────
# Это и есть чек-лист в человеческом виде: `audit.py --list` печатает его целиком.
# Сообщение находки говорит про конкретную страницу, название — про саму проверку,
# поэтому в отчёте двадцать одинаковых находок схлопываются в один пункт.

TITLES = {
    'SEO-STATUS': 'страница отвечает не 200, а на неё ведут ссылки',
    'SEO-TITLE-MISSING': 'нет title', 'SEO-TITLE-LONG': 'title длиннее сниппета',
    'SEO-TITLE-SHORT': 'короткий title', 'SEO-TITLE-DUP': 'одинаковый title у разных страниц',
    'SEO-DESC-MISSING': 'нет meta description', 'SEO-DESC-LONG': 'description обрежется',
    'SEO-DESC-SHORT': 'короткий description', 'SEO-DESC-DUP': 'одинаковый description',
    'SEO-H1-MISSING': 'нет h1', 'SEO-H1-MANY': 'больше одного h1', 'SEO-H1-DUP': 'одинаковый h1',
    'SEO-H1-EQ-TITLE': 'h1 дословно равен title', 'SEO-H2-NONE': 'нет ни одного h2',
    'SEO-HEAD-SKIP': 'пропущен уровень заголовка',
    'SEO-LANG-MISSING': 'нет lang у html', 'SEO-VIEWPORT-MISSING': 'нет meta viewport',
    'SEO-CHARSET-MISSING': 'не объявлена кодировка', 'SEO-NOINDEX': 'страница закрыта noindex',
    'SEO-CANON-MISSING': 'нет canonical', 'SEO-CANON-REL': 'canonical относительный',
    'SEO-CANON-HOST': 'canonical ведёт на чужой хост',
    'SEO-IMG-ALT': 'картинки без alt', 'SEO-NO-IMG': 'на странице нет картинок',
    'SEO-LINK-EMPTY': 'ссылки в никуда (href="#")', 'SEO-LINK-ANCHOR': 'анкор без смысла',
    'SEO-LINK-NOFOLLOW': 'nofollow на внутренней ссылке', 'SEO-LINK-404': 'битая внутренняя ссылка',
    'SEO-ORPHAN': 'страница-сирота: на неё никто не ссылается',
    'SEO-THIN': 'мало текста для самостоятельной страницы',
    'SEO-PLACEHOLDER': 'похоже на заполнитель в проде', 'SEO-EMPTY-LI': 'пустые пункты списка',
    'SEO-OG-MISSING': 'неполная разметка Open Graph', 'SEO-OG-TYPE': 'og:type не тому типу страницы',
    'SEO-OG-ASSET-404': 'объявленная картинка или иконка не отдаётся',
    'SEO-ICON-MISSING': 'не объявлен favicon', 'SEO-ICON-APPLE': 'нет apple-touch-icon',
    'SEO-NO-BREADCRUMBS': 'нет хлебных крошек',
    'SEO-ROBOTS-TXT': 'нет robots.txt', 'SEO-ROBOTS-SITEMAP': 'в robots.txt нет Sitemap',
    'SEO-ROBOTS-ALL': 'robots.txt закрывает сайт целиком',
    'SEO-SITEMAP-MISSING': 'нет sitemap.xml', 'SEO-SITEMAP-BROKEN': 'sitemap.xml не разбирается',
    'SEO-SITEMAP-GAP': 'страницы вне sitemap', 'SEO-SITEMAP-ORPHAN': 'адрес есть только в sitemap',
    'SEO-SOFT404': 'несуществующий адрес отвечает 200',
    'AEO-NO-SCHEMA': 'на странице нет JSON-LD', 'AEO-LD-BROKEN': 'JSON-LD не разбирается',
    'AEO-PERSON-DESCRIPTION': 'Person без description', 'AEO-PERSON-IMAGE': 'Person без image',
    'AEO-PERSON-SAMEAS': 'Person без sameAs', 'AEO-PERSON-JOBTITLE': 'Person без jobTitle',
    'AEO-ORG-CONTACT': 'Organization без контактов', 'AEO-ORG-URL': 'Organization без url',
    'AEO-ORG-LOGO': 'Organization без logo', 'AEO-ORG-SAMEAS': 'Organization без sameAs',
    'AEO-ORG-ADDRESS': 'Organization без address',
    'AEO-AWARD-PROSE': 'награды записаны прозой', 'AEO-ARTICLE-META': 'Article без автора и дат',
    'AEO-NO-FAQ': 'на сайте нет вопросов-ответов', 'AEO-NO-SERVICES': 'услуги не структурированы',
    'AEO-NO-LISTS': 'длинная страница без списков и таблиц',
    'GEO-NO-ID': 'узел разметки без @id', 'GEO-REF-BY-NAME': 'ссылка на сущность по name, а не по @id',
    'GEO-ENTITY-SPLIT': 'одна сущность разошлась на несколько узлов',
    'GEO-NO-WEBPAGE': 'нет узла WebPage/WebSite', 'GEO-NO-MAINENTITY': 'WebPage без mainEntity',
    'GEO-NO-ORG': 'Organization не отдаётся нигде', 'GEO-JS-ONLY': 'контент рисуется только JS',
    'GEO-CURTAIN': 'занавес-прелоадер на первом экране', 'GEO-LLMS-TXT': 'нет /llms.txt',
    'SEM-DUP-ID': 'повторяющиеся id', 'SEM-ARIA-EXPANDED': 'aria-expanded без aria-controls',
    'SEM-CLICKABLE-DIV': 'кликабельный div без роли', 'SEM-LINK-NONAME': 'ссылка без имени',
    'SEM-BTN-NONAME': 'кнопка без имени', 'SEM-INPUT-LABEL': 'поле формы без подписи',
    'SEM-IFRAME-TITLE': 'iframe без title', 'SEM-SVG-VIEWBOX': 'svg без viewBox',
    'SEM-TABINDEX': 'положительный tabindex', 'SEM-H-EMPTY': 'пустой заголовок',
    'SEM-NO-MAIN': 'нет <main>', 'SEM-NO-HEADER': 'нет <header>', 'SEM-NO-NAV': 'нет <nav>',
    'TECH-ERR': 'страница не загрузилась', 'TECH-CHAIN': 'цепочка редиректов',
    'TECH-SLOW': 'медленный ответ сервера', 'TECH-SIZE': 'тяжёлый HTML',
    'TECH-GZIP': 'HTML без сжатия', 'TECH-BLOCKING': 'блокирующие скрипты в head',
    'TECH-IMG-DIM': 'картинки без width/height', 'TECH-IMG-LAZY': 'нет loading="lazy"',
    'TECH-MIXED': 'ресурсы по http на https-странице', 'TECH-HTTPS': 'http не уводит на https',
    'TOOL-ERR': 'сбой самой проверки',
}


# Надбавка к приоритету для находок, которые стоят дороже своего количества.
# Страница-сирота на одной странице важнее отсутствия lazy на двадцати: вес
# считается не числом адресов, а тем, что дефект отнимает у сайта.
PRIO = {
    'SEO-ORPHAN': 60, 'SEO-STATUS': 55, 'SEO-NOINDEX': 55, 'SEO-ROBOTS-ALL': 55,
    'SEO-LINK-404': 45, 'SEO-PLACEHOLDER': 45, 'SEO-SOFT404': 40, 'GEO-JS-ONLY': 40,
    'GEO-NO-ID': 38, 'GEO-ENTITY-SPLIT': 38, 'AEO-NO-SCHEMA': 35, 'GEO-NO-ORG': 35,
    'TECH-MIXED': 32, 'SEO-CANON-HOST': 30, 'AEO-ORG-CONTACT': 28, 'SEO-H1-MISSING': 25,
    'AEO-PERSON-DESCRIPTION': 22, 'SEO-LINK-EMPTY': 20, 'TECH-HTTPS': 20,
    'SEO-TITLE-MISSING': 20, 'SEO-VIEWPORT-MISSING': 18, 'SEM-DUP-ID': 15,
    'SEO-OG-ASSET-404': 12, 'SEO-NO-BREADCRUMBS': 10, 'AEO-NO-FAQ': 8,
}
