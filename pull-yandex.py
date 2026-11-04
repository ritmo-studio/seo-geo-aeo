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
LOAD_TIME = ['ym:pv:avgPageLoadTime', 'ym:s:avgPageLoadTime', 'ym:pv:avgDOMContentLoadTime']
SRV_TIME = ['ym:pv:avgServerResponseTime', 'ym:s:avgServerResponseTime']
SOURCE_DIM = ['ym:s:lastsignTrafficSource', 'ym:s:trafficSource']


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
    p = {'ids': counter, 'metrics': metrics, 'accuracy': 'full', 'limit': limit,
         'date1': '%ddaysAgo' % days, 'date2': 'today'}
    if dimensions:
        p['dimensions'] = dimensions
    if sort:
        p['sort'] = sort
    return api(MET + '/stat/v1/data', token, p)


def first_working(token, counter, candidates, dimensions='', days=30):
    """Перебрать кандидатов и вернуть (имя, данные). Пустой ответ — тоже успех:
    отсутствие данных и отсутствие метрики — разные вещи."""
    tried = []
    for name in candidates:
        d, err = stat(token, counter, name, dimensions, days=days, limit=100)
        if not err:
            return name, d, tried
        tried.append('%s — %s' % (name, err.split(':')[0]))
    return '', None, tried


def num(d, i=0):
    rows = (d or {}).get('data') or []
    if not rows:
        return None
    v = rows[0].get('metrics') or []
    return v[i] if i < len(v) else None


def find_counter(token, host):
    d, err = api(MET + '/management/v1/counters', token, {'per_page': 200})
    if err:
        return 0, err
    got = d.get('counters') or []
    for c in got:
        sites = [c.get('site') or ''] + [s.get('site', '') for s in (c.get('site2') or [])]
        if any(host in s for s in sites):
            return c.get('id'), ''
    return 0, 'счётчик для %s не найден среди %d доступных' % (host, len(got))


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

    dim, d, tried = first_working(token, counter, SOURCE_DIM, days=days)
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
    for h in got:
        names = '%s %s' % (h.get('ascii_host_url') or '', h.get('unicode_host_url') or '')
        if host in names:
            return uid, h.get('host_id'), ''
    return uid, '', 'Вебмастер: хост %s не найден (доступно %d)' % (host, len(got))


def webmaster(token, host, probe):
    out = {'ok': False, 'notes': []}
    uid, hid, err = wm_host(token, host)
    if not hid:
        out['notes'].append(err)
        return out
    out['host_id'] = hid

    base = '%s/user/%s/hosts/%s' % (WM, uid, hid)
    d, err = api(base + '/summary/', token)
    if err:
        d, err = api(base + '/summary', token)
    if err:
        out['notes'].append('Вебмастер, сводка: %s' % err)
    else:
        out['sqi'] = d.get('sqi')
        out['summary_raw'] = {k: v for k, v in d.items() if not isinstance(v, (dict, list))}

    # Главные две цифры: сколько страниц робот загрузил и сколько из них в поиске.
    for path, params in (('/search-urls/in-search/history/', {}),
                         ('/indexing/history/', {'indexing_indicator': 'SEARCHABLE'}),
                         ('/indexing/history/', {'indexing_indicator': 'DOWNLOADED'})):
        d, err = api(base + path, token, params or None)
        if err:
            if probe:
                out['notes'].append('Вебмастер %s %s: %s' % (path, params, err))
            continue
        pts = d.get('history') or d.get('indicators') or []
        if isinstance(pts, dict):
            pts = sum(pts.values(), [])
        if pts:
            last = pts[-1]
            key = params.get('indexing_indicator', 'IN_SEARCH').lower()
            out.setdefault('pages', {})[key] = last.get('value')
            out.setdefault('pages_date', {})[key] = last.get('date')

    d, err = api(base + '/diagnostics/', token)
    if err:
        out['notes'].append('Вебмастер, диагностика: %s' % err)
    else:
        items = d.get('problems') or []
        if isinstance(items, dict):
            items = sum((v if isinstance(v, list) else [v]) for v in items.values())
        out['problems'] = [{'name': p.get('problem_type') or p.get('name'),
                            'severity': p.get('severity') or p.get('state')}
                           for p in items][:20]
    out['ok'] = bool(out.get('pages') or out.get('sqi') is not None)
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
        p = w.get('pages') or {}
        if p.get('downloaded') and p.get('searchable') is not None:
            t.append('Вебмастер: загружено %s страниц, в поиске %s — из индекса выпало %s.' % (
                p['downloaded'], p['searchable'], p['downloaded'] - p['searchable']))
        elif p:
            t.append('Вебмастер, страницы: ' + ', '.join('%s %s' % (k, v) for k, v in p.items()) + '.')
        if w.get('sqi') is not None:
            t.append('ИКС %s.' % w['sqi'])
        pr = w.get('problems') or []
        if pr:
            t.append('Диагностика: ' + ', '.join(str(x['name']) for x in pr[:8]) + '.')
    notes = (m.get('notes') or []) + (w.get('notes') or [])
    if notes:
        t.append('Не выгрузилось: ' + ' | '.join(notes))
    return '\n'.join(t) or 'Выгрузить не удалось ничего — смотреть notes в json.'


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
    m = metrika(token, host, a.days, a.probe)
    print('  Метрика: %s' % ('счётчик %s' % m.get('counter') if m.get('ok') else 'нет данных'))
    w = webmaster(token, host, a.probe)
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
