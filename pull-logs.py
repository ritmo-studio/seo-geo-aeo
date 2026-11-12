#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Logs API Метрики: сырые визиты и просмотры, разбор воронки оформления.

Третий скрипт рядом с pull-yandex.py и pull-google.py, и отдельный от них по двум
причинам, а не из любви к файлам.

Первая — класс данных. pull-yandex ходит в Reporting API: это агрегаты, пара
десятков чисел, их можно класть в отчёт и показывать заказчику. Logs API отдаёт
построчно поведение людей: clientID, адреса, время. Такие файлы в папку отчёта
и в репозиторий не попадают никогда — они живут в кэше и стираются. Смешать это
в одном скрипте значит однажды закоммитить выгрузку посетителей.

Вторая — время. Reporting API отвечает сразу, Logs API работает заявками:
создать, дождаться обработки, скачать части, убрать за собой. Заявки занимают
квоту (10 ГБ на счётчик), поэтому `clean` вызывается даже когда разбор упал.

    python3 pull-logs.py spbshield.ru --out reports          # 3 дня просмотров
    python3 pull-logs.py spbshield.ru --days 7 --source visits
    python3 pull-logs.py spbshield.ru --keep                 # не стирать сырое
    python3 pull-logs.py spbshield.ru --list                 # что висит в заявках

Токен — в файле (по умолчанию ~/.config/ritmo/yandex.token) с правами 600.
Аргументом командной строки его не передаём никогда: аргументы видны в `ps`.

Вспомогательные функции (api, read_token, find_counter) повторяют pull-yandex.py
намеренно. Правило этого репозитория — каждый скрипт копируется наружу папкой
и работает сам; общий модуль это правило ломает. Расхождение ловится глазами:
функции короткие и рядом.

Почему воронка считается по людям, а не по визитам. Возврат из платёжной системы
Метрика считает новым визитом: у «Питерского Щита» 227 из 463 «успешных» визитов
начинались прямо со страницы «заказ принят» с реферером widget.cloudpayments.ru
или пустым. Любая воронка по визитам в такой схеме врёт в обе стороны сразу,
поэтому шаги считаются по уникальному clientID.
"""
import argparse
import collections
import csv
import datetime
import json
import os
import pathlib
import re
import stat as statmod
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MET = 'https://api-metrika.yandex.net'
UA = 'ritmo-audit/1.0 (+https://ritmo.studio/)'

HITS_FIELDS = ('ym:pv:dateTime,ym:pv:clientID,ym:pv:URL,ym:pv:title,'
               'ym:pv:referer,ym:pv:deviceCategory,ym:pv:lastTrafficSource')
VISITS_FIELDS = ('ym:s:visitID,ym:s:dateTime,ym:s:startURL,ym:s:pageViews,'
                 'ym:s:visitDuration,ym:s:deviceCategory,ym:s:lastTrafficSource,'
                 'ym:s:goalsID,ym:s:clientID,ym:s:purchaseRevenue,ym:s:isNewUser,'
                 'ym:s:regionCity,ym:s:bounce')

# Шаги корзины отличаются от сайта к сайту; по умолчанию WooCommerce.
STEPS = {'cart': r'^/cart', 'checkout': r'^/checkout(?!/order-received)',
         'success': r'^/checkout/order-received/(\d+)',
         'catalog': r'^/(product|shop|catalog|collections)', 'account': r'^/my-account'}

DEVICE = {'1': 'компьютеры', '2': 'телефоны', '3': 'планшеты', '4': 'телевизоры'}


def api(url, token, params=None, method='GET', timeout=120):
    """Один запрос. Возвращает (данные, ошибка) и не поднимает исключений."""
    if params:
        url += '?' + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        url, data=b'' if method == 'POST' else None, method=method,
        headers={'Authorization': 'OAuth %s' % token, 'User-Agent': UA,
                 'Accept': 'application/json'})
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
    """Токен из файла. Строки с # — комментарий, не часть токена."""
    p = pathlib.Path(path).expanduser()
    if not p.exists():
        return '', 'нет файла с токеном: %s' % p
    mode = statmod.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        print('  внимание: %s доступен не только владельцу (chmod 600)' % p)
    for line in p.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            return line, ''
    return '', 'файл с токеном пуст: %s' % p


def find_counter(token, host):
    """Счётчик по домену. Сверяем и site, и зеркала: www и без www — один сайт."""
    d, err = api(MET + '/management/v1/counters', token, {'per_page': 200})
    if not d:
        return None, err
    want = host.replace('www.', '').lower()
    for c in d.get('counters') or []:
        names = [c.get('site') or '']
        # Зеркала Метрика отдаёт то строками, то объектами {site: ...} — зависит
        # от того, как счётчик заводили. Приводим к строкам, не угадывая форму.
        for s in (c.get('mirrors2') or c.get('site2') or []):
            names.append(s.get('site', '') if isinstance(s, dict) else str(s))
        for n in names:
            if n and n.replace('www.', '').lower().strip('/') == want:
                return str(c['id']), ''
    have = ', '.join((c.get('site') or '?') for c in (d.get('counters') or [])[:10])
    return None, 'счётчик для %s не найден. Доступны: %s' % (host, have)


# ─────────────────────────────────── заявки ────────────────────────────────────

def evaluate(token, counter, d1, d2, source, fields):
    d, err = api('%s/management/v1/counter/%s/logrequests/evaluate' % (MET, counter), token,
                 {'date1': d1, 'date2': d2, 'source': source, 'fields': fields})
    if not d:
        return None, err
    e = d.get('log_request_evaluation') or {}
    if not e.get('possible'):
        return None, 'Метрика отказала в заявке: %s' % json.dumps(e, ensure_ascii=False)
    return e, ''


def reuse(token, counter, d1, d2, source):
    """Висящая заявка на тот же период переиспользуется.

    Иначе каждый повторный запуск плодит заявки и съедает квоту: прошлый разбор
    «Питерского Щита» оставил две по 44 МБ, и это ещё скромно."""
    d, _ = api('%s/management/v1/counter/%s/logrequests' % (MET, counter), token)
    for r in (d or {}).get('requests', []):
        if (r.get('source') == source and r.get('date1') == d1 and r.get('date2') == d2
                and r.get('status') in ('created', 'processed')):
            return r
    return None


def create(token, counter, d1, d2, source, fields):
    d, err = api('%s/management/v1/counter/%s/logrequests' % (MET, counter), token,
                 {'date1': d1, 'date2': d2, 'source': source, 'fields': fields},
                 method='POST')
    if not d:
        return None, err
    return d.get('log_request') or {}, ''


def wait(token, counter, rid, tries=120, pause=15):
    for _ in range(tries):
        d, err = api('%s/management/v1/counter/%s/logrequest/%s' % (MET, counter, rid), token)
        st = (d or {}).get('log_request') or {}
        status = st.get('status')
        if status == 'processed':
            return st, ''
        if status in ('canceled', 'processing_failed'):
            return None, 'заявка %s: %s' % (rid, status)
        time.sleep(pause)
    return None, 'заявка %s не обработалась за %d минут' % (rid, tries * pause // 60)


def download(token, counter, rid, part, dest):
    """Часть заявки в файл. Качаем потоком: части бывают по десятки мегабайт,
    и обрыв на середине — обычное дело, поэтому попыток несколько."""
    url = '%s/management/v1/counter/%s/logrequest/%s/part/%d/download' % (MET, counter, rid, part)
    for attempt in range(1, 6):
        try:
            req = urllib.request.Request(url, headers={'Authorization': 'OAuth %s' % token,
                                                       'User-Agent': UA})
            tmp = dest.with_suffix('.part')
            with urllib.request.urlopen(req, timeout=1800) as r, tmp.open('wb') as w:
                while True:
                    chunk = r.read(1 << 18)
                    if not chunk:
                        break
                    w.write(chunk)
            tmp.replace(dest)
            return dest.stat().st_size, ''
        except Exception as e:
            if attempt == 5:
                return 0, 'часть %d не скачалась: %s' % (part, e)
            time.sleep(5)
    return 0, 'часть %d не скачалась' % part


def clean(token, counter, rid):
    api('%s/management/v1/counter/%s/logrequest/%s/clean' % (MET, counter, rid), token,
        method='POST')


def fetch(token, counter, d1, d2, source, fields, raw_dir, keep, refresh=False):
    """Заявка от начала до конца: оценка, создание, ожидание, скачивание, уборка.

    Уже скачанное за тот же период берём с диска. Разбор правится чаще, чем
    меняются данные: без этого каждая правка воронки стоит заявки, десятков
    мегабайт и нескольких минут ожидания."""
    have = sorted(raw_dir.glob('%s-%s-%s-*.tsv' % (source, d1, d2)))
    if have and not refresh:
        print('  %s %s..%s: беру из кэша, %d файл(ов), %.1f МБ'
              % (source, d1, d2, len(have), sum(p.stat().st_size for p in have) / 2 ** 20))
        return have, ''
    ev, err = evaluate(token, counter, d1, d2, source, fields)
    if err:
        return [], err
    print('  %s %s..%s: ожидаемый размер %.1f МБ' % (source, d1, d2, ev['expected_size'] / 2 ** 20))
    r = reuse(token, counter, d1, d2, source)
    if r:
        print('    беру висящую заявку %s (%s)' % (r['request_id'], r['status']))
    else:
        r, err = create(token, counter, d1, d2, source, fields)
        if err:
            return [], err
        print('    заявка %s создана' % r['request_id'])
    rid = r['request_id']
    try:
        st, err = wait(token, counter, rid)
        if err:
            return [], err
        files = []
        for p in st.get('parts') or []:
            n = p['part_number']
            dest = raw_dir / ('%s-%s-%s-%d.tsv' % (source, d1, d2, n))
            size, err = download(token, counter, rid, n, dest)
            if err:
                return files, err
            print('    часть %d: %.1f МБ' % (n, size / 2 ** 20))
            files.append(dest)
        return files, ''
    finally:
        if not keep:
            clean(token, counter, rid)


# ─────────────────────────────────── разбор ────────────────────────────────────

def read_tsv(paths):
    for p in paths:
        with p.open(encoding='utf-8') as f:
            rd = csv.reader(f, delimiter='\t', quoting=csv.QUOTE_NONE)
            head = next(rd, None)
            if not head:
                continue
            idx = {n: i for i, n in enumerate(head)}
            for row in rd:
                if len(row) >= len(head):
                    yield idx, row


def path_of(url):
    return re.sub(r'\?.*', '', re.sub(r'https?://[^/]+', '', url))


GAP = datetime.timedelta(minutes=30)


def checkout_span(hits, rx):
    """Сколько человек пробыл с момента открытия оформления до конца этого захода.

    Считать до последнего хита за всё окно выгрузки нельзя: человек вернулся через
    день, и «медиана 689 секунд» превращается в медиану двух суток. Поэтому идём
    вперёд от первого оформления, пока перерыв между хитами не больше получаса —
    это и есть конец захода."""
    ts = [datetime.datetime.strptime(t, '%Y-%m-%d %H:%M:%S') for t, _ in hits]
    start = next((i for i, (_, q) in enumerate(hits) if rx.match(q)), None)
    if start is None:
        return None
    end = start
    while end + 1 < len(ts) and ts[end + 1] - ts[end] <= GAP:
        end += 1
    return (ts[end] - ts[start]).total_seconds()


def funnel(paths, steps):
    """Воронка и портрет ушедших — по людям, не по визитам (почему: см. шапку).

    Взаимодействия с формой Метрика пишет отдельными хитами с адресом form://.
    Это не страница, поэтому в шаги они не идут, но доля коснувшихся формы —
    самый говорящий признак: кто начал заполнять и ушёл, ушёл не из-за товара.
    """
    rx = {k: re.compile(v) for k, v in steps.items()}
    ppl = collections.defaultdict(lambda: collections.Counter())
    seq = collections.defaultdict(list)
    orders = set()
    hits = 0
    for idx, row in paths:
        hits += 1
        cid = row[idx['ym:pv:clientID']]
        url = row[idx['ym:pv:URL']]
        when = row[idx['ym:pv:dateTime']]
        dev = row[idx['ym:pv:deviceCategory']]
        p = ppl[cid]
        if not p.get('device'):
            p['device'] = dev
        if url.startswith('form://'):
            p['form'] += 1
            seq[cid].append((when, 'form://'))
            continue
        q = path_of(url)
        m = rx['success'].match(q)
        if m:
            p['success'] += 1
            orders.add(m.group(1))
        elif rx['checkout'].match(q):
            p['checkout'] += 1
        elif rx['cart'].match(q):
            p['cart'] += 1
        elif rx['catalog'].match(q):
            p['catalog'] += 1
        if rx['account'].match(q):
            p['account'] += 1
        seq[cid].append((when, q))
    for v in seq.values():
        v.sort()

    def share(group, key):
        return sum(1 for v in group if v[key])

    everyone = list(ppl.values())
    f = {'хитов': hits, 'людей': len(ppl), 'заказов по номерам': len(orders)}
    f['шаги'] = [('смотрели товар или категорию', share(everyone, 'catalog')),
                 ('открыли корзину', share(everyone, 'cart')),
                 ('открыли оформление', share(everyone, 'checkout')),
                 ('дошли до подтверждения', share(everyone, 'success'))]
    lost = [c for c, v in ppl.items() if v['checkout'] and not v['success']]
    won = [c for c, v in ppl.items() if v['success']]
    f['профили'] = {}
    for label, group in (('не заказали', lost), ('заказали', won)):
        if not group:
            continue
        g = [ppl[c] for c in group]
        secs = [x for x in (checkout_span(seq[c], rx['checkout']) for c in group) if x is not None]
        f['профили'][label] = {
            'людей': len(g),
            'трогали форму, %': round(share(g, 'form') / len(g) * 100),
            'событий формы, медиана': statistics.median([v['form'] for v in g]),
            'секунд с открытия оформления до конца, медиана': round(statistics.median(secs)) if secs else None,
            'не открывали корзину': sum(1 for v in g if not v['cart']),
            'были в личном кабинете, %': round(share(g, 'account') / len(g) * 100),
            'устройства': collections.Counter(DEVICE.get(v['device'], v['device']) for v in g).most_common(),
        }
    exits = collections.Counter()
    nowhere = 0
    for c in lost:
        s = seq[c]
        for i in range(len(s) - 1):
            # form:// — это касание поля, а не переход; в списке уходов оно
            # оказывается первой строкой и сбивает чтение. Доля коснувшихся
            # формы уже посчитана выше, здесь нужны только настоящие страницы.
            if (rx['checkout'].match(s[i][1]) and not rx['checkout'].match(s[i + 1][1])
                    and s[i + 1][1] != 'form://'):
                exits[s[i + 1][1]] += 1
        if rx['checkout'].match(s[-1][1]):
            nowhere += 1
    f['куда уходят с оформления'] = exits.most_common(12)
    f['ушли, не открыв больше ничего'] = nowhere
    return f


def brief(f, host, d1, d2):
    L = ['Оформление заказа на %s, %s..%s.' % (host, d1, d2),
         'Разобрано %d просмотров, %d человек, %d номеров заказов.'
         % (f['хитов'], f['людей'], f['заказов по номерам']), '', 'Воронка по людям:']
    prev = None
    for name, n in f['шаги']:
        L.append('  %-30s %6d%s' % (name, n, '  %.1f%% от предыдущего' % (n / prev * 100) if prev else ''))
        prev = n or prev
    for label, p in (f.get('профили') or {}).items():
        L += ['', '%s: %d человек.' % (label.capitalize(), p['людей']),
              '  трогали форму %d%%, событий формы медиана %.0f'
              % (p['трогали форму, %'], p['событий формы, медиана']),
              '  от открытия оформления до конца: медиана %s с'
              % p['секунд с открытия оформления до конца, медиана'],
              '  не открывали корзину: %d; были в личном кабинете: %d%%'
              % (p['не открывали корзину'], p['были в личном кабинете, %']),
              '  устройства: %s' % ', '.join('%s %d' % (k, v) for k, v in p['устройства'])]
    L += ['', 'Куда уходят со страницы оформления:']
    for u, n in f['куда уходят с оформления']:
        L.append('  %5d  %s' % (n, u))
    L.append('  %5d  ушли, не открыв больше ничего' % f['ушли, не открыв больше ничего'])
    return '\n'.join(L)


def main():
    ap = argparse.ArgumentParser(description='Logs API Метрики: сырые данные и воронка оформления')
    ap.add_argument('host', help='домен без схемы, например spbshield.ru')
    ap.add_argument('--out', default='reports', help='куда класть сводку (<out>/<хост>/logs.json)')
    ap.add_argument('--raw', default='~/.cache/ritmo/logs',
                    help='куда класть сырые TSV: это данные посетителей, в отчёт и в git они не идут')
    ap.add_argument('--token-file', default='~/.config/ritmo/yandex.token')
    ap.add_argument('--days', type=int, default=3,
                    help='сколько дней до вчерашнего (просмотры тяжёлые, по умолчанию 3)')
    ap.add_argument('--source', default='hits', choices=('hits', 'visits', 'both'))
    ap.add_argument('--keep', action='store_true',
                    help='не стирать заявку и сырые файлы (по умолчанию заявка убирается, файлы остаются в --raw)')
    ap.add_argument('--refresh', action='store_true',
                    help='перекачать, даже если файлы за этот период уже лежат в --raw')
    ap.add_argument('--list', action='store_true', help='показать висящие заявки и выйти')
    ap.add_argument('--counter', default='', help='номер счётчика, если автопоиск по домену не сработал')
    a = ap.parse_args()

    token = os.environ.get('YANDEX_TOKEN', '')
    if not token:
        token, err = read_token(a.token_file)
        if err:
            print(err)
            print('Токен: oauth.yandex.ru → своё приложение → права на Метрику.')
            return 1

    host = a.host.replace('https://', '').replace('http://', '').strip('/')
    counter = a.counter
    if not counter:
        counter, err = find_counter(token, host)
        if err:
            print(err)
            return 2
    print('Logs API %s, счётчик %s' % (host, counter))

    if a.list:
        d, err = api('%s/management/v1/counter/%s/logrequests' % (MET, counter), token)
        if err:
            print(err)
            return 2
        rows = (d or {}).get('requests', [])
        if not rows:
            print('  заявок нет')
        for r in rows:
            print('  %s %s %s..%s %s, частей %d' % (r['request_id'], r['source'], r.get('date1'),
                                                    r.get('date2'), r['status'], len(r.get('parts') or [])))
        return 0

    end = datetime.date.today() - datetime.timedelta(days=1)
    d1, d2 = str(end - datetime.timedelta(days=a.days - 1)), str(end)
    raw = pathlib.Path(a.raw).expanduser() / host.replace('.', '-')
    raw.mkdir(parents=True, exist_ok=True)
    os.chmod(raw, 0o700)

    notes, data = [], {}
    sources = ('hits', 'visits') if a.source == 'both' else (a.source,)
    text = ''
    for src in sources:
        fields = HITS_FIELDS if src == 'hits' else VISITS_FIELDS
        files, err = fetch(token, counter, d1, d2, src, fields, raw, a.keep, a.refresh)
        if err:
            notes.append('%s: %s' % (src, err))
            print('  %s: %s' % (src, err))
            continue
        data[src] = {'файлы': [str(p) for p in files], 'байт': sum(p.stat().st_size for p in files)}
        if src == 'hits':
            f = funnel(read_tsv(files), STEPS)
            data['воронка'] = f
            text = brief(f, host, d1, d2)

    tag = host.replace('.', '-')
    out = pathlib.Path(a.out) / tag
    out.mkdir(parents=True, exist_ok=True)
    payload = {'host': host, 'counter': counter, 'from': d1, 'to': d2,
               'when': datetime.datetime.now().strftime('%Y-%m-%d-%H%M'),
               'summary': text, 'notes': notes, 'raw': str(raw), **data}
    (out / 'logs.json').write_text(json.dumps(payload, ensure_ascii=False, indent=1,
                                              default=str), encoding='utf-8')
    if text:
        print('\n' + text)
    print('\nсводка: %s' % (out / 'logs.json'))
    print('сырые файлы: %s — это данные посетителей, в репозиторий не кладём' % raw)
    return 0 if data else 2


if __name__ == '__main__':
    sys.exit(main())
