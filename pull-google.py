#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выгрузка Search Console, GA4 и PageSpeed в сводку рядом с отчётом аудита.

Парный скрипт к pull-yandex.py и по тем же причинам отдельный от audit.py: аудит
ходит по сайту без ключей и без зависимостей, его можно скопировать папкой на любой
сервер. Понадобился бы ему сервисный аккаунт — он перестал бы быть таким.

Считает здесь, локально. В json уезжает пара десятков чисел и текстовая сводка,
а не выгрузка на сорок тысяч строк: арифметика — работа машины, выводы — человека.

    python3 pull-google.py ritmo.studio --out reports
    python3 pull-google.py ritmo.studio --ga4 123456789
    python3 pull-google.py ritmo.studio --psi-only --runs 5

Ключ сервисного аккаунта лежит файлом (по умолчанию ~/.config/ritmo/google-sa.json)
с правами 600. Аргументом командной строки его не передаём никогда: аргументы
видны в `ps`. Ключ PageSpeed — из GOOGLE_PSI_KEY или ~/.config/ritmo/psi.key;
без него API тоже работает, просто лимит жёстче.

Что откуда:
  Search Console — наши позиции и показы в выдаче Google, до клика;
  GA4            — что человек делал на сайте, после клика, но без запроса:
                   Google не передаёт его с 2011 года, и склеивать их можно
                   только по странице входа;
  PageSpeed      — лабораторный прогон Lighthouse на машине Google плюс поле CrUX
                   у живых пользователей Chrome. Это разные вещи, и в сводке они
                   подписаны раздельно.
"""
import argparse
import base64
import datetime
import json
import os
import pathlib
import stat as statmod
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = 'https://oauth2.googleapis.com/token'
GSC = 'https://searchconsole.googleapis.com/webmasters/v3'
GA4 = 'https://analyticsdata.googleapis.com/v1beta'
PSI = 'https://www.googleapis.com/pagespeedonline/v5/runPagespeed'
SCOPES = ('https://www.googleapis.com/auth/webmasters.readonly '
          'https://www.googleapis.com/auth/analytics.readonly')
UA = 'ritmo-audit/1.0 (+https://ritmo.studio/)'

# GA4 переименовал conversions в keyEvents, и старое имя на новых свойствах даёт
# ошибку, а новое — на старых. Как с именами метрик у Метрики: берём первое,
# которое ответило, и пишем в json, какое сработало — иначе через месяц
# непонятно, что означает число.
GOAL_METRIC = ['keyEvents', 'conversions']


def api(url, token=None, body=None, params=None, timeout=90):
    """Один запрос. Возвращает (данные, ошибка) и не поднимает исключений:
    сорванный отчёт не должен уносить с собой всю остальную выгрузку."""
    if params:
        url += '?' + urllib.parse.urlencode(params)
    data = json.dumps(body).encode('utf-8') if body is not None else None
    h = {'User-Agent': UA, 'Accept': 'application/json'}
    if token:
        h['Authorization'] = 'Bearer %s' % token
    if data:
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=h)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8')), ''
        except urllib.error.HTTPError as e:
            raw = (e.read() or b'')[:400].decode('utf-8', 'replace')
            try:
                raw = json.loads(raw)['error']['message']
            except Exception:
                pass
            if e.code in (429, 500, 503) and attempt < 2:
                time.sleep(int(e.headers.get('Retry-After') or 5))
                continue
            return None, 'HTTP %d: %s' % (e.code, raw)
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
                continue
            return None, '%s' % e
    return None, 'не ответил'


def mode_warn(p):
    m = statmod.S_IMODE(p.stat().st_mode)
    if m & 0o077:
        print('  внимание: %s читают посторонние (права %o). chmod 600.' % (p, m))


# ──────────────────────────── доступ по сервисному аккаунту ────────────────────

def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=')


def access_token(sa_path):
    """Обмен сервисного аккаунта на токен доступа.

    Google требует подписать JWT алгоритмом RS256, а RSA в стандартной библиотеке
    нет. Выбор был между pip-зависимостью (google-auth) и openssl, который есть и
    на macOS, и на любом сервере. Взял openssl: этот скрипт должен уезжать к
    клиенту копированием, как и остальной аудит.

    openssl умеет подписывать только ключом из файла, поэтому PEM один раз
    выкладывается рядом с json — в том же защищённом каталоге и с теми же правами
    600, а не в /tmp, где его прочитает кто угодно."""
    p = pathlib.Path(sa_path).expanduser()
    if not p.exists():
        return '', ('нет файла сервисного аккаунта: %s\n'
                    '  console.cloud.google.com → IAM → сервисные аккаунты → ключ JSON,\n'
                    '  положить файлом и chmod 600' % p)
    mode_warn(p)
    try:
        sa = json.loads(p.read_text(encoding='utf-8'))
        email, key = sa['client_email'], sa['private_key']
    except Exception as e:
        return '', 'файл сервисного аккаунта не разобрался: %s' % e

    pem = p.with_suffix('.pem')
    if not pem.exists() or pem.read_text(encoding='utf-8') != key:
        pem.write_text(key, encoding='utf-8')
        pem.chmod(0o600)

    now = int(time.time())
    head = b64u(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
    claim = b64u(json.dumps({'iss': email, 'scope': SCOPES, 'aud': TOKEN_URL,
                             'iat': now, 'exp': now + 3600}).encode())
    signing_input = head + b'.' + claim
    try:
        sig = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(pem)],
                             input=signing_input, capture_output=True, check=True).stdout
    except FileNotFoundError:
        return '', 'нет openssl — подписать JWT нечем'
    except subprocess.CalledProcessError as e:
        return '', 'openssl не подписал: %s' % (e.stderr or b'')[:200].decode('utf-8', 'replace')

    jwt = (signing_input + b'.' + b64u(sig)).decode()
    body = urllib.parse.urlencode({
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': jwt}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode('utf-8'))['access_token'], ''
    except urllib.error.HTTPError as e:
        raw = (e.read() or b'')[:300].decode('utf-8', 'replace')
        return '', 'токен не выдан (HTTP %d): %s' % (e.code, raw)
    except Exception as e:
        return '', 'токен не выдан: %s' % e


# ───────────────────────────────── Search Console ──────────────────────────────

def gsc_query(token, site, days, dims=None, limit=1000, extra=None):
    end = datetime.date.today() - datetime.timedelta(days=2)   # два последних дня неполные
    body = {'startDate': str(end - datetime.timedelta(days=days)), 'endDate': str(end),
            'rowLimit': limit}
    if dims:
        body['dimensions'] = dims
    if extra:
        body.update(extra)
    url = '%s/sites/%s/searchAnalytics/query' % (GSC, urllib.parse.quote(site, safe=''))
    return api(url, token, body=body)


def rows_of(d):
    return (d or {}).get('rows') or []


def one(d):
    r = rows_of(d)
    if not r:
        return {}
    x = r[0]
    return {'clicks': x.get('clicks'), 'impressions': x.get('impressions'),
            'ctr': round((x.get('ctr') or 0) * 100, 2), 'position': round(x.get('position') or 0, 1)}


def flat(d, n=25):
    out = []
    for x in rows_of(d)[:n]:
        out.append({'key': ' / '.join(x.get('keys') or []),
                    'clicks': x.get('clicks'), 'impressions': x.get('impressions'),
                    'ctr': round((x.get('ctr') or 0) * 100, 2),
                    'position': round(x.get('position') or 0, 1)})
    return out


def search_console(token, host, site, days, probe):
    out = {'ok': False, 'site': site, 'days': days, 'notes': []}
    d, err = gsc_query(token, site, days, limit=1)
    if err:
        out['notes'].append('итоги: %s' % err)
        if 'HTTP 403' in err:
            out['notes'].append('403 обычно значит, что e-mail сервисного аккаунта '
                                'не добавлен в пользователи ресурса в Search Console')
        if 'HTTP 404' in err:
            out['notes'].append('404 значит, что ресурс %s не подтверждён; '
                                'для ресурса-домена нужен префикс sc-domain:' % site)
        return out
    out['ok'] = True
    out['total'] = one(d)

    d, err = gsc_query(token, site, days, ['query'], limit=1000)
    if err:
        out['notes'].append('запросы: %s' % err)
    else:
        rs = rows_of(d)
        out['queries_total'] = len(rs)
        out['queries'] = flat(d, 25)
        # Ножницы: показов много, позиция в пределах двух экранов, кликов нет.
        # Это и есть кандидаты на переписанный Title и Description — место уже
        # есть, не цепляет сниппет. Вывод односторонний: обратное (клики без
        # показов) невозможно, поэтому проверять второе направление не нужно.
        miss = [x for x in rs if (x.get('impressions') or 0) >= 20
                and not x.get('clicks') and (x.get('position') or 99) <= 20]
        miss.sort(key=lambda x: -(x.get('impressions') or 0))
        out['no_clicks'] = [{'key': ' '.join(x['keys']), 'impressions': x['impressions'],
                             'position': round(x['position'], 1)} for x in miss[:15]]

    d, err = gsc_query(token, site, days, ['page'], limit=200)
    out['pages'] = flat(d, 15) if not err else []
    if err:
        out['notes'].append('страницы: %s' % err)

    d, err = gsc_query(token, site, days, ['device'], limit=10)
    out['devices'] = flat(d, 5) if not err else []
    if err and probe:
        out['notes'].append('устройства: %s' % err)

    # Половина к половине: ровно так же, как считается динамика в отчётах трекера.
    # Нужны два равных окна, иначе сравнение показывает длину периода, а не рост.
    half = max(days // 2, 7)
    end = datetime.date.today() - datetime.timedelta(days=2)
    late, e1 = gsc_query(token, site, half, limit=1)
    early, e2 = gsc_query(token, site, half, limit=1, extra={
        'startDate': str(end - datetime.timedelta(days=half * 2)),
        'endDate': str(end - datetime.timedelta(days=half + 1))})
    if not e1 and not e2:
        out['halves'] = {'days': half, 'late': one(late), 'early': one(early)}
    elif probe:
        out['notes'].append('динамика: %s' % (e1 or e2))
    return out


# ───────────────────────────────────── GA4 ─────────────────────────────────────

def ga4_report(token, prop, days, metrics, dims=None, limit=25, filt=None, order=None):
    body = {'dateRanges': [{'startDate': '%ddaysAgo' % days, 'endDate': 'today'}],
            'metrics': [{'name': m} for m in metrics], 'limit': limit}
    if dims:
        body['dimensions'] = [{'name': d} for d in dims]
    if filt:
        body['dimensionFilter'] = filt
    if order:
        body['orderBys'] = [{'desc': True, 'metric': {'metricName': order}}]
    return api('%s/properties/%s:runReport' % (GA4, prop), token, body=body)


def ga4_rows(d):
    out = []
    for r in (d or {}).get('rows') or []:
        keys = ' / '.join(v.get('value', '') for v in r.get('dimensionValues') or [])
        vals = [v.get('value') for v in r.get('metricValues') or []]
        out.append({'key': keys, 'values': vals})
    return out


def analytics(token, prop, days, probe):
    out = {'ok': False, 'property': prop, 'days': days, 'notes': []}
    base = ['sessions', 'totalUsers', 'engagedSessions', 'averageSessionDuration']
    goal = ''
    d = None
    for name in GOAL_METRIC:
        d, err = ga4_report(token, prop, days, base + [name], limit=1)
        if not err:
            goal = name
            break
        if probe:
            out['notes'].append('метрика цели %s — %s' % (name, err[:160]))
    if not goal:
        d, err = ga4_report(token, prop, days, base, limit=1)
        if err:
            out['notes'].append('итоги: %s' % err)
            if 'HTTP 403' in err:
                out['notes'].append('403 обычно значит, что e-mail сервисного аккаунта '
                                    'не добавлен Читателем в свойство GA4')
            return out
    out['ok'] = True
    out['goal_metric'] = goal or 'нет'
    r = ga4_rows(d)
    out['total'] = r[0]['values'] if r else []
    out['total_names'] = base + ([goal] if goal else [])

    d, err = ga4_report(token, prop, days, ['sessions'], ['sessionDefaultChannelGroup'],
                        limit=12, order='sessions')
    out['channels'] = ga4_rows(d) if not err else []
    if err:
        out['notes'].append('каналы: %s' % err)

    # Страницы входа именно из поиска: без фильтра в верх списка всегда вылезает
    # главная со всего трафика, и органику по ней не видно.
    filt = {'filter': {'fieldName': 'sessionDefaultChannelGroup',
                       'stringFilter': {'value': 'Organic Search'}}}
    d, err = ga4_report(token, prop, days, ['sessions'], ['landingPagePlusQueryString'],
                        limit=15, filt=filt, order='sessions')
    out['organic_landings'] = ga4_rows(d) if not err else []
    if err:
        out['notes'].append('страницы входа из поиска: %s' % err)
    return out


# ──────────────────────────────────── PageSpeed ────────────────────────────────

LAB = {'largest-contentful-paint': 'LCP', 'cumulative-layout-shift': 'CLS',
       'total-blocking-time': 'TBT', 'first-contentful-paint': 'FCP',
       'speed-index': 'SI'}


def psi_once(url, strategy, key):
    p = {'url': url, 'strategy': strategy, 'category': 'performance'}
    if key:
        p['key'] = key
    return api(PSI, params=p, timeout=120)


def field(d):
    """Поле CrUX из ответа. Его может не быть вовсе: у молодого сайта не хватает
    живых пользователей Chrome, и это не ошибка выгрузки, а отсутствие данных."""
    out = {}
    for src, label in (('loadingExperience', 'url'), ('originLoadingExperience', 'origin')):
        block = (d or {}).get(src) or {}
        m = block.get('metrics') or {}
        if not m:
            continue
        out[label] = {k: {'p75': v.get('percentile'), 'category': v.get('category')}
                      for k, v in m.items()}
    return out


def pagespeed(url, strategy, key, runs):
    """Медиана нескольких прогонов, а не один.

    Лаборатория Lighthouse даёт разброс в несколько пунктов на одном и том же
    URL: это синтетический прогон на чужой машине с эмуляцией медленного 4G.
    Одиночный прогон не вердикт, поэтому берём медиану и записываем все прогоны
    — по ним видно, стабильна ли цифра или скачет на двадцать пунктов."""
    out = {'ok': False, 'url': url, 'strategy': strategy, 'runs': runs, 'notes': []}
    scores, labs, fld, ver = [], {}, {}, ''
    for i in range(runs):
        d, err = psi_once(url, strategy, key)
        if err:
            out['notes'].append('прогон %d: %s' % (i + 1, err))
            continue
        lh = d.get('lighthouseResult') or {}
        ver = ver or lh.get('lighthouseVersion') or ''
        sc = ((lh.get('categories') or {}).get('performance') or {}).get('score')
        if sc is not None:
            scores.append(round(sc * 100))
        for aid, label in LAB.items():
            v = ((lh.get('audits') or {}).get(aid) or {}).get('numericValue')
            if v is not None:
                labs.setdefault(label, []).append(v)
        fld = fld or field(d)
        if i + 1 < runs:
            time.sleep(2)
    if not scores:
        return out
    out['ok'] = True
    out['lighthouse'] = ver
    out['score_median'] = int(statistics.median(scores))
    out['score_runs'] = scores
    out['spread'] = max(scores) - min(scores)
    out['lab_median'] = {k: round(statistics.median(v)) if k != 'CLS'
                         else round(statistics.median(v), 3) for k, v in labs.items()}
    out['field'] = fld
    return out


# ───────────────────────────────────── сводка ──────────────────────────────────

def brief(g, a, ps, host, days):
    t = ['Google по %s за %d дней.' % (host, days), '']
    if g.get('ok'):
        x = g.get('total') or {}
        t.append('Search Console (%s): %s клика при %s показах, CTR %s%%, '
                 'средняя позиция %s.' % (g.get('site'), x.get('clicks'), x.get('impressions'),
                                          x.get('ctr'), x.get('position')))
        h = g.get('halves') or {}
        if h.get('late') and h.get('early'):
            t.append('Половина к половине (по %d дней): клики %s → %s, показы %s → %s, '
                     'позиция %s → %s.' % (h['days'], h['early'].get('clicks'),
                                           h['late'].get('clicks'), h['early'].get('impressions'),
                                           h['late'].get('impressions'),
                                           h['early'].get('position'), h['late'].get('position')))
        q = g.get('queries') or []
        if q:
            t.append('Запросов с показами: %s. Верх списка: %s.' % (
                g.get('queries_total'),
                ', '.join('%s (поз. %s, кликов %s)' % (x['key'], x['position'], x['clicks'])
                          for x in q[:5])))
        nc = g.get('no_clicks') or []
        if nc:
            t.append('Показы без кликов в пределах двадцатки — переписать сниппет: ' +
                     ', '.join('%s (поз. %s, %s показов)' % (x['key'], x['position'],
                                                             x['impressions']) for x in nc[:5]) + '.')
    else:
        t.append('Search Console: данных нет.')
    if a.get('ok'):
        names, vals = a.get('total_names') or [], a.get('total') or []
        t.append('GA4 (%s): %s.' % (a.get('property'),
                                    ', '.join('%s %s' % (n, v) for n, v in zip(names, vals))))
        ch = a.get('channels') or []
        if ch:
            t.append('Каналы: ' + ', '.join('%s %s' % (c['key'], c['values'][0])
                                            for c in ch[:6]) + '.')
        t.append('Запрос GA4 не показывает — Google его не передаёт. '
                 'Запросы смотреть в Search Console, склейка по странице входа.')
    else:
        t.append('GA4: данных нет.')
    for p in ps:
        if not p.get('ok'):
            continue
        f = p.get('field') or {}
        t.append('PageSpeed, %s: %s баллов (медиана %d прогонов, разброс %s, Lighthouse %s). '
                 'Лаборатория: %s.' % (
                     p['strategy'], p['score_median'], p['runs'], p['spread'],
                     p.get('lighthouse'),
                     ', '.join('%s %s' % (k, v) for k, v in sorted(p['lab_median'].items()))))
        if f:
            key = 'url' if 'url' in f else 'origin'
            t.append('  Поле CrUX (%s, живые пользователи Chrome, 28 дней): %s.' % (
                key, ', '.join('%s %s (%s)' % (k, v['p75'], v['category'])
                               for k, v in sorted(f[key].items()))))
        else:
            t.append('  Поля CrUX нет: трафика Chrome не хватает для публикации. '
                     'Это отсутствие данных, а не ноль.')
    notes = (g.get('notes') or []) + (a.get('notes') or []) + sum(
        [p.get('notes') or [] for p in ps], [])
    if notes:
        # В сводке — короткая строка: полный текст ошибки лежит в json, а в отчёте
        # сырое тело ответа Google занимает полэкрана и прячет остальное.
        t.append('Не выгрузилось: ' + ' | '.join(
            (n if len(n) < 120 else n[:117].rstrip() + '…') for n in notes))
    return '\n'.join(t)


def safe(fn, label, *args):
    """Разбор чужого json — главный источник сюрпризов: поле приходит не того вида,
    и трейсбек уносит с собой уже выгруженное. Ловим и пишем в notes."""
    try:
        return fn(*args)
    except Exception as e:
        return {'ok': False, 'notes': ['%s: разбор ответа упал — %s: %s' % (
            label, type(e).__name__, e)]}


def psi_key(path):
    v = os.environ.get('GOOGLE_PSI_KEY', '')
    if v:
        return v
    p = pathlib.Path(path).expanduser()
    if p.exists():
        mode_warn(p)
        return p.read_text(encoding='utf-8').strip()
    return ''


def main():
    ap = argparse.ArgumentParser(description='Search Console, GA4 и PageSpeed в сводку рядом с отчётом')
    ap.add_argument('host', help='домен без схемы, например ritmo.studio')
    ap.add_argument('--out', default='reports', help='куда класть (появится <out>/<хост>/google.json)')
    ap.add_argument('--site', help='ресурс в Search Console; по умолчанию sc-domain:<хост>, '
                                   'для ресурса-префикса указать https://<хост>/')
    ap.add_argument('--ga4', help='идентификатор свойства GA4, только цифры')
    ap.add_argument('--sa-file', default='~/.config/ritmo/google-sa.json')
    ap.add_argument('--psi-key-file', default='~/.config/ritmo/psi.key')
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--runs', type=int, default=5, help='прогонов PageSpeed на профиль')
    ap.add_argument('--psi-only', action='store_true', help='только PageSpeed, без ключей')
    ap.add_argument('--no-psi', action='store_true', help='без PageSpeed')
    ap.add_argument('--probe', action='store_true', help='писать в notes каждую неудачу')
    a = ap.parse_args()

    host = a.host.replace('https://', '').replace('http://', '').strip('/')
    site = a.site or 'sc-domain:%s' % host
    print('Выгрузка Google по %s за %d дней' % (host, a.days))

    g = {'ok': False, 'notes': []}
    an = {'ok': False, 'notes': []}
    if not a.psi_only:
        token, err = access_token(a.sa_file)
        if err:
            print('  ' + err.replace('\n', '\n  '))
            g['notes'].append(err.splitlines()[0])
        else:
            g = safe(search_console, 'Search Console', token, host, site, a.days, a.probe)
            print('  Search Console: %s' % ('ресурс %s' % site if g.get('ok') else 'нет данных'))
            if a.ga4:
                an = safe(analytics, 'GA4', token, a.ga4, a.days, a.probe)
                print('  GA4: %s' % ('свойство %s' % a.ga4 if an.get('ok') else 'нет данных'))
            else:
                an['notes'].append('свойство GA4 не указано — ключ --ga4')
                print('  GA4: пропущен, нет --ga4')

    ps = []
    if not a.no_psi:
        key = psi_key(a.psi_key_file)
        print('  PageSpeed: ключ %s, по %d прогона на профиль' % (
            'есть' if key else 'нет (лимит жёстче)', a.runs))
        for strategy in ('mobile', 'desktop'):
            p = safe(pagespeed, 'PageSpeed', 'https://%s/' % host, strategy, key, a.runs)
            print('    %s: %s' % (strategy, p.get('score_median', 'нет данных')))
            ps.append(p)

    text = brief(g, an, ps, host, a.days)
    out = pathlib.Path(a.out) / host.replace('.', '-')
    out.mkdir(parents=True, exist_ok=True)
    data = {'host': host, 'when': datetime.datetime.now().strftime('%Y-%m-%d-%H%M'),
            'days': a.days, 'summary': text,
            'search_console': g, 'ga4': an, 'pagespeed': ps}
    (out / 'google.json').write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                     encoding='utf-8')
    print('\n' + text)
    print('\nсводка: %s' % (out / 'google.json'))
    return 0 if (g.get('ok') or an.get('ok') or any(p.get('ok') for p in ps)) else 2


if __name__ == '__main__':
    sys.exit(main())
