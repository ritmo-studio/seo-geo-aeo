#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выгрузка Метрики и Вебмастера в сводку рядом с отчётом аудита.

Отдельный скрипт, а не часть audit.py, и это принципиально: аудит ходит по сайту
без ключей и без зависимостей, его можно скопировать на любой сервер папкой.
Понадобился бы ему OAuth-токен — он перестал бы быть таким.

Считает здесь, локально. В json уезжает полтора десятка чисел и текстовая сводка,
а не сорок тысяч строк выгрузки: арифметика — работа машины, выводы — человека.

    python3 pull-yandex.py 220boom.ru --out reports
    python3 pull-yandex.py 220boom.ru --probe    # что из запросов вообще живо

Токен лежит в файле (по умолчанию ~/.config/ritmo/yandex.token) с правами 600.
Аргументом командной строки его не передаём никогда: аргументы видны в `ps`.
"""
import argparse
import datetime
import json
import re
import os
import pathlib
import stat as statmod
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MET = 'https://api-metrika.yandex.net'
WM = 'https://api.webmaster.yandex.net/v4'
UA = 'ritmo-audit/1.0 (+https://ritmo.studio/)'

# Имена метрик у Метрики менялись, а проверить их без живого токена нельзя.
# Поэтому списком кандидатов: берём первый, который ответил, и записываем в json,
# какой именно сработал — иначе через месяц непонятно, что означает число.
# Время загрузки: 4 ноября 2026 перебрал шестнадцать имён (ym:pv: и ym:s:,
# avgPageLoadTime, avgDOMContentLoadTime, avgServerResponseTime, avgTimeToFirstByte
# и далее) — все дали «Incorrectly specified attribute», и пресета под этот отчёт
# тоже нет. Похоже, отчёт «Время загрузки страниц» из интерфейса через
# Reporting API не отдаётся вовсе. Список оставлен на случай, что имена появятся,
# но полагаться на него нельзя: скорость у живых людей берём из CrUX/PageSpeed,
# он для этого и сделан. Перебирать заново не нужно — вот результат.
LOAD_TIME = ['ym:pv:avgPageLoadTime', 'ym:s:avgPageLoadTime']
SRV_TIME = ['ym:pv:avgServerResponseTime']
SOURCE_DIM = ['ym:s:lastsignTrafficSource', 'ym:s:lastTrafficSource', 'ym:s:trafficSource']


def api(url, token, params=None, timeout=60):
    """Один запрос. Возвращает (данные, ошибка) и не поднимает исключений: сорванный
    отчёт не должен уносить с собой всю остальную выгрузку."""
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        'Authorization': 'OAuth %s' % token,
        'User-Agent': UA,
        'Accept': 'application/json',
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8')), ''
        except urllib.error.HTTPError as e:
            body = (e.read() or b'')[:300].decode('utf-8', 'replace')
            if e.code in (429, 503) and attempt < 2:
                time.sleep(int(e.headers.get('Retry-After') or 5))
                continue
            return None, 'HTTP %d: %s' % (e.code, body)
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
                continue
            return None, '%s' % e
    return None, 'не ответил'


def read_token(path):
    p = pathlib.Path(path).expanduser()
    if not p.exists():
        return '', 'нет файла с токеном: %s' % p
    mode = statmod.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        print('  внимание: %s читают посторонние (права %o). chmod 600.' % (p, mode))
    return p.read_text(encoding='utf-8').strip(), ''


# ───────────────────────────────── Метрика ─────────────────────────────────

def stat(token, counter, metrics, dimensions='', days=30, limit=20, sort=''):
    """Отчёт за последние `days` дней.

    «Query is too complicated» у Метрики — не про размер запроса: 11 ноября
    2026 на spbshield.ru один и тот же запрос за 90 дней проходил и через
    секунду не проходил, с любой точностью, а помесячный ряд за год прошёл
    целиком с full. Это мигающий отказ на их стороне. Поэтому сначала повтор
    той же точности с паузой, и только потом ниже: medium, затем low.
    Сработавшая точность пишется в ответ полем `_accuracy`: low — оценка по
    выборке, не счёт."""
    p = {'ids': counter, 'metrics': metrics, 'limit': limit,
         'date1': '%ddaysAgo' % days, 'date2': 'today'}
    if dimensions:
        p['dimensions'] = dimensions
    if sort:
        p['sort'] = sort
    err = ''
    for acc, pause in (('full', 0), ('full', 4), ('medium', 2), ('low', 2)):
        time.sleep(pause)
        p['accuracy'] = acc
        d, err = api(MET + '/stat/v1/data', token, p)
        if not err:
            d['_accuracy'] = acc
            return d, ''
        if 'too complicated' not in err:
            break
    return None, err


def first_working(token, counter, candidates, as_dim=False, days=30):
    """Перебрать кандидатов и вернуть (имя, данные). Пустой ответ — тоже успех:
    отсутствие данных и отсутствие метрики — разные вещи.

    `as_dim` обязателен: измерение и метрика идут в разные параметры запроса, и
    живое имя, попавшее не в тот параметр, выглядит как несуществующее. На этом
    я и обжёгся — источники «не выгружались», хотя измерение работало."""
    tried = []
    for name in candidates:
        if as_dim:
            d, err = stat(token, counter, 'ym:s:visits', name, days=days, limit=100)
        else:
            d, err = stat(token, counter, name, days=days, limit=100)
        if not err:
            return name, d, tried
        tried.append('%s — %s' % (name, err[:160]))
    return '', None, tried


def num(d, i=0):
    rows = (d or {}).get('data') or []
    if not rows:
        return None
    v = rows[0].get('metrics') or []
    return v[i] if i < len(v) else None


def domains(counter):
    """Домены счётчика. `site2` приходит то списком строк, то списком словарей —
    зависит от возраста счётчика, в документации об этом ни слова. Разбираем оба
    вида и всё незнакомое просто приводим к строке: промахнуться именем поля
    дешевле, чем уронить всю выгрузку на ровном месте."""
    out = [counter.get('site') or '']
    for s in (counter.get('site2') or []):
        out.append(s.get('site', '') if isinstance(s, dict) else '%s' % s)
    return [x for x in out if x]


def find_counter(token, host):
    """Счётчик по хосту: сначала точное совпадение, подстрока — в последнюю очередь.

    Подстрока первой стоила дорого: у spbshield.ru два счётчика, боевой
    `www.spbshield.ru` и тестовый `new.spbshield.ru`, и `host in s` выбирал тот,
    что попался раньше. Выбрался тестовый — 12 тысяч визитов и 94% отказов
    вместо 642 тысяч и 23%, то есть выгрузка соврала в пятьдесят раз и выглядела
    при этом правдоподобно. Поддомен с `new`, `dev`, `test`, `stage`, `staging`
    отодвигается в конец всегда: такой счётчик почти никогда не тот, который
    спрашивали, а если других нет — о выборе сказано в notes.
    """
    d, err = api(MET + '/management/v1/counters', token, {'per_page': 200})
    if err:
        return 0, err
    got = d.get('counters') or []
    bare = host[4:] if host.startswith('www.') else host
    exact, prefixed, loose = [], [], []
    for c in got:
        for s in domains(c):
            s = s.strip().lower().rstrip('/')
            s = s.split('://')[-1]
            if s in (bare, 'www.' + bare):
                exact.append(c)
            elif s.endswith('.' + bare):
                prefixed.append(c)
            elif bare in s:
                loose.append(c)
    def junk(c):
        return any(re.match(r'(new|dev|test|stage|staging|beta|old)\b', s.split('.')[0].lower())
                   for s in domains(c))
    order = sorted(exact, key=junk) + sorted(prefixed, key=junk) + sorted(loose, key=junk)
    if order:
        pick = order[0]
        note = ''
        if len(order) > 1:
            other = ', '.join('%s (%s)' % (c.get('id'), (domains(c) or [''])[0]) for c in order[1:4])
            note = 'счётчиков с таким доменом несколько, взят %s (%s); остальные: %s' % (
                pick.get('id'), (domains(pick) or [''])[0], other)
        return pick.get('id'), note
    names = ', '.join(sorted({s for c in got for s in domains(c)}))[:200]
    return 0, 'счётчик для %s не найден. Доступны: %s' % (host, names or 'ни одного')


def metrika(token, host, days, probe):
    out = {'ok': False, 'notes': []}
    counter, err = find_counter(token, host)
    if not counter:
        out['notes'].append('Метрика: %s' % err)
        return out
    out['counter'] = counter

    d, err = stat(token, counter,
                  'ym:s:visits,ym:s:users,ym:s:bounceRate,ym:s:avgVisitDurationSeconds',
                  days=days)
    if err:
        out['notes'].append('Метрика, сводка: %s' % err)
    else:
        t = (d.get('totals') or [None] * 4)
        out.update(visits=t[0], users=t[1], bounce=t[2], duration=t[3], days=days)
        if d.get('_accuracy') != 'full':
            out['accuracy'] = d['_accuracy']
            out['notes'].append('Метрика: сводка посчитана с accuracy=%s — Яндекс не отдал '
                                'полный счёт; порядок верный, единицы — нет.' % d['_accuracy'])

    dim, d, tried = first_working(token, counter, SOURCE_DIM, as_dim=True, days=days)
    if dim:
        rows = [(r['dimensions'][0].get('name') or '?', r['metrics'][0])
                for r in (d.get('data') or [])]
        total = sum(v for _, v in rows) or 1
        out['sources'] = [{'name': n, 'visits': v, 'share': round(100.0 * v / total, 1)}
                          for n, v in sorted(rows, key=lambda x: -x[1])[:6]]
        out['source_dim'] = dim
    else:
        out['notes'].append('Метрика, источники: не вышло (%s)' % '; '.join(tried))

    name, d, tried = first_working(token, counter, LOAD_TIME, days=days)
    if name:
        out['load_ms'] = num(d)
        out['load_metric'] = name
    else:
        out['notes'].append('Метрика, время загрузки: не вышло (%s)' % '; '.join(tried))

    name, d, tried = first_working(token, counter, SRV_TIME, days=days)
    if name:
        out['server_ms'] = num(d)
        out['server_metric'] = name
    elif probe:
        out['notes'].append('Метрика, ответ сервера: не вышло (%s)' % '; '.join(tried))
    if 'load_ms' not in out:
        out['notes'].append('Скорость у живых людей Метрика через API не отдаёт — '
                            'брать из CrUX/PageSpeed.')

    d, err = stat(token, counter, 'ym:s:visits,ym:s:bounceRate', 'ym:s:startURL',
                  days=days, limit=15, sort='-ym:s:visits')
    if err:
        out['notes'].append('Метрика, страницы входа: %s' % err)
    else:
        out['entries'] = [{'url': r['dimensions'][0].get('name'),
                           'visits': r['metrics'][0], 'bounce': r['metrics'][1]}
                          for r in (d.get('data') or [])]
    out['ok'] = 'visits' in out
    return out


# ──────────────────────────────── Вебмастер ────────────────────────────────

def wm_host(token, host):
    u, err = api(WM + '/user/', token)
    if err:
        return '', '', 'Вебмастер: %s' % err
    uid = u.get('user_id')
    hs, err = api(WM + '/user/%s/hosts/' % uid, token)
    if err:
        return uid, '', 'Вебмастер, список хостов: %s' % err
    got = hs.get('hosts') or []
    # Домен может быть не хостом, а главным зеркалом другого хоста: у ritmo.studio
    # в Вебмастере подтверждён unitbox.one, а ritmo.studio висит его зеркалом.
    # Данные отдаются по подтверждённому host_id, поэтому ищем и среди зеркал,
    # а подмену объявляем вслух — иначе цифры будут про «не тот» сайт.
    for h in got:
        if host in '%s %s' % (h.get('ascii_host_url') or '', h.get('unicode_host_url') or ''):
            return uid, h.get('host_id'), ''
    for h in got:
        mm = h.get('main_mirror') or {}
        if host in '%s %s' % (mm.get('ascii_host_url') or '', mm.get('unicode_host_url') or ''):
            return uid, h.get('host_id'), 'зеркало: %s подтверждён как %s, цифры по нему' % (
                host, h.get('ascii_host_url'))
    names = ', '.join((h.get('ascii_host_url') or '?') for h in got)
    return uid, '', 'Вебмастер: хост %s не найден. Доступны: %s' % (host, names or 'ни одного')


def webmaster(token, host, probe):
    out = {'ok': False, 'notes': []}
    uid, hid, err = wm_host(token, host)
    if not hid:
        out['notes'].append(err)
        return out
    out['host_id'] = hid
    if err:
        out['notes'].append('Вебмастер, %s' % err)

    base = '%s/user/%s/hosts/%s' % (WM, uid, hid)
    d, err = api(base + '/summary/', token)
    if err:
        out['notes'].append('Вебмастер, сводка: %s' % err)
    else:
        out['sqi'] = d.get('sqi')
        out['in_search'] = d.get('searchable_pages_count')
        out['excluded'] = d.get('excluded_pages_count')
        out['problem_count'] = d.get('site_problems') or {}

    # Страницы в поиске в динамике: одна цифра не отличает «так и было»
    # от «вчера выпала половина».
    d, err = api(base + '/search-urls/in-search/history/', token)
    if err:
        out['notes'].append('Вебмастер, история поиска: %s' % err)
    else:
        pts = [(x.get('date', '')[:10], x.get('value')) for x in (d.get('history') or [])]
        if pts:
            out['in_search_history'] = {'from': pts[0], 'to': pts[-1], 'points': len(pts)}

    # Коды ответа глазами робота. Ради этого всё и затевалось: свой замер я могу
    # объяснить вежливостью краулера, а тут таймауты и пятисотки считает Яндекс.
    d, err = api(base + '/indexing/history/', token, {'indexing_indicator': 'DOWNLOADED'})
    if err:
        out['notes'].append('Вебмастер, коды ответа: %s' % err)
    else:
        codes = {}
        for name, pts in (d.get('indicators') or {}).items():
            if pts:
                codes[name] = pts[-1].get('value')
        out['codes'] = codes

    d, err = api(base + '/diagnostics/', token)
    if err:
        out['notes'].append('Вебмастер, диагностика: %s' % err)
    else:
        probs = d.get('problems') or {}
        # ABSENT и NOT_APPLICABLE — это «проверено, всё в порядке». В список
        # проблем они попадать не должны: отчёт из восьми небылиц не читают.
        out['problems'] = [
            {'name': k, 'severity': (v or {}).get('severity'), 'state': (v or {}).get('state')}
            for k, v in sorted(probs.items())
            if isinstance(v, dict) and v.get('state') not in ('ABSENT', 'NOT_APPLICABLE', None)]

    out['ok'] = bool(out.get('in_search') is not None or out.get('codes'))
    return out


# ───────────────────────────────── Сводка ─────────────────────────────────

def brief(m, w, host, days):
    """Текст для чтения человеком и моделью. Только то, что посчитано, без догадок:
    толковать цифры — не работа выгрузки."""
    t = []
    if m.get('ok'):
        line = 'Метрика за %d дней: %s визитов, %s посетителей' % (
            days, m.get('visits'), m.get('users'))
        if m.get('bounce') is not None:
            line += ', отказы %.1f%%' % m['bounce']
        t.append(line + '.')
        src = m.get('sources') or []
        if src:
            t.append('Источники: ' + ', '.join('%s %.1f%%' % (s['name'], s['share'])
                                               for s in src[:4]) + '.')
        if m.get('load_ms') is not None:
            t.append('Время загрузки у живых людей: %.0f мс (%s). Краулер мерил 15 000–31 000 мс '
                     'до первого байта — если здесь цифра втрое меньше, значит людям отдаётся '
                     'кэш, а боту нет.' % (m['load_ms'], m.get('load_metric')))
        if m.get('server_ms') is not None:
            t.append('Ответ сервера по Метрике: %.0f мс.' % m['server_ms'])
        ent = m.get('entries') or []
        if ent:
            t.append('Зарабатывающие входы: ' + '; '.join(
                '%s — %s визитов, отказы %.0f%%' % (e['url'], e['visits'], e['bounce'])
                for e in ent[:5]) + '.')
    if w.get('ok'):
        line = 'Вебмастер: в поиске %s страниц' % w.get('in_search')
        if w.get('excluded'):
            line += ', исключено %s' % w['excluded']
        if w.get('sqi') is not None:
            line += ', ИКС %s' % w['sqi']
        t.append(line + '.')
        h = w.get('in_search_history') or {}
        if h.get('from') and h.get('to') and h['from'][1] != h['to'][1]:
            t.append('Страниц в поиске: %s на %s → %s на %s.' % (
                h['from'][1], h['from'][0], h['to'][1], h['to'][0]))
        c = w.get('codes') or {}
        if c:
            t.append('Коды ответа глазами робота: ' + ', '.join(
                '%s %s' % (k.replace('HTTP_', ''), v) for k, v in sorted(c.items())) + '.')
        pr = w.get('problems') or []
        t.append('Проблемы в диагностике: ' + (', '.join(
            '%s (%s)' % (x['name'], x['severity']) for x in pr[:8]) if pr else 'нет.'))
    notes = (m.get('notes') or []) + (w.get('notes') or [])
    if notes:
        # В сводке — короткая строка: полный текст ошибки лежит в json, а в отчёте
        # сырое тело ответа Яндекса занимает полэкрана и прячет остальное.
        short = [(n if len(n) < 120 else n[:117].rstrip() + '…') for n in notes]
        t.append('Не выгрузилось: ' + ' | '.join(short))
    return '\n'.join(t) or 'Выгрузить не удалось ничего — смотреть notes в json.'


def safe(fn, label, *args):
    """Разбор чужого json — главный источник сюрпризов: поле приходит не того вида,
    и трейсбек уносит с собой уже выгруженное. Ловим и пишем в notes."""
    try:
        return fn(*args)
    except Exception as e:
        return {'ok': False, 'notes': ['%s: разбор ответа упал — %s: %s' % (
            label, type(e).__name__, e)]}


def main():
    ap = argparse.ArgumentParser(description='Метрика и Вебмастер в сводку рядом с отчётом')
    ap.add_argument('host', help='домен без схемы, например 220boom.ru')
    ap.add_argument('--out', default='reports', help='куда класть (появится <out>/<хост>/yandex.json)')
    ap.add_argument('--token-file', default='~/.config/ritmo/yandex.token')
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--probe', action='store_true', help='писать в notes каждую неудачу, не только важные')
    a = ap.parse_args()

    token = os.environ.get('YANDEX_TOKEN', '')
    if not token:
        token, err = read_token(a.token_file)
        if err:
            print(err)
            print('Токен: oauth.yandex.ru → своё приложение → права на Метрику и Вебмастер.')
            return 1

    host = a.host.replace('https://', '').replace('http://', '').strip('/')
    print('Выгрузка %s за %d дней' % (host, a.days))
    m = safe(metrika, 'Метрика', token, host, a.days, a.probe)
    print('  Метрика: %s' % ('счётчик %s' % m.get('counter') if m.get('ok') else 'нет данных'))
    w = safe(webmaster, 'Вебмастер', token, host, a.probe)
    print('  Вебмастер: %s' % ('хост найден' if w.get('ok') else 'нет данных'))

    text = brief(m, w, host, a.days)
    tag = host.replace('.', '-')
    out = pathlib.Path(a.out) / tag
    out.mkdir(parents=True, exist_ok=True)
    data = {'host': host, 'when': datetime.datetime.now().strftime('%Y-%m-%d-%H%M'),
            'days': a.days, 'summary': text, 'metrika': m, 'webmaster': w}
    (out / 'yandex.json').write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                     encoding='utf-8')
    print('\n' + text)
    print('\nсводка: %s' % (out / 'yandex.json'))
    return 0 if (m.get('ok') or w.get('ok')) else 2


if __name__ == '__main__':
    sys.exit(main())
