#!/usr/bin/env python3
"""Read selected private calendars and publish minimal day states."""
import argparse
import base64
import json
import os
import re
import shutil
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
# Calendar markers. Everyone can rename these in config.json "rules" (settings.html).
DEFAULT_RULES = {
    # 全私密日: busy all day. Title contains any word (brackets optional).
    'busy': ['聚会', '拍照', 'cos', '【休息】'],
    # 已约: a confirmed meet-up. Closes the rest of that day and counts toward the weekly limit.
    'booked': ['已约'],
    # 私密开放日: "预留-xx" is open only to the person whose code has reserve "xx".
    'reserve': ['预留'],
    # 躺平: no new invitations that day (energy). Already shared items stay visible.
    'rest': ['躺平'],
    # All-day events that change the day type: my own day off / extra working day.
    'restday': ['调休'],
    'workday': ['补班'],
    # 公开项 (half-open): "漫展-xx" publishes only "xx"; a bare word publishes the word.
    # Who sees it is decided per person ("see"). location: where I am that day, so
    # people in that city can meet me (回老家 → 老家).
    'semi': [
        {'word': '漫展'},
        {'word': '回老家', 'location': '老家'},
    ],
}
NAME_AFTER = r'\s*[-－—–_:：]\s*([^\s【】\[\]，,。；;（）()]+)'
RULES = DEFAULT_RULES


def word_pattern(word):
    """Latin words match whole words only (cos is not Costco); others match anywhere."""
    escaped = re.escape(word)
    if re.fullmatch(r'[A-Za-z0-9]+', word):
        return r'(?<![A-Za-z])' + escaped + r'(?![A-Za-z])'
    return escaped


def check_rules(rules):
    rules = dict(DEFAULT_RULES, **(rules or {}))
    for key in ('busy', 'booked', 'reserve', 'rest', 'restday', 'workday'):
        if not isinstance(rules[key], list) or not all(isinstance(w, str) and w.strip() for w in rules[key]):
            raise ValueError('invalid rules')
    for rule in rules['semi']:
        if not (isinstance(rule, dict) and isinstance(rule.get('word'), str) and rule['word'].strip()
                and isinstance(rule.get('location', ''), str)):
            raise ValueError('invalid rules')
    return rules


def clean_label(text):
    return re.sub(r'[\x00-\x1f\x7f<>]', ' ', text).strip()[:36]


def classify(summary, all_day=False, rules=None):
    """Return (kind, public label, marker word).

    kind: booked | energy | as_rest | as_work | reserve | lock | convention | private.
    The marker word lets 长辈 see a hand-written summary (拍照 → 出去拍照).
    """
    rules = rules or RULES
    summary = summary.strip()
    if any(re.search(r'^\s*【?' + word_pattern(w), summary, re.I) for w in rules['booked']):
        return 'booked', '', ''
    if any(re.search(word_pattern(w), summary, re.I) for w in rules['rest']):
        return 'energy', '', ''
    if all_day and any(re.search(word_pattern(w), summary, re.I) for w in rules['restday']):
        return 'as_rest', '', ''
    if all_day and any(re.search(word_pattern(w), summary, re.I) for w in rules['workday']):
        return 'as_work', '', ''
    for w in rules['reserve']:
        found = re.search(word_pattern(w) + NAME_AFTER, summary, re.I)
        if found:
            return 'reserve', clean_label(found.group(1)), ''
    for w in rules['busy']:
        if re.search(word_pattern(w), summary, re.I):
            return 'lock', '', w
    if all_day and '休息' in summary:
        return 'lock', '', '【休息】'
    for rule in rules['semi']:
        found = re.search(word_pattern(rule['word']) + NAME_AFTER, summary, re.I)
        if found and clean_label(found.group(1)):
            return 'convention', clean_label(found.group(1)), rule['word']
    for rule in rules['semi']:
        if re.search(word_pattern(rule['word']), summary, re.I):
            return 'convention', rule['word'], rule['word']
    return 'private', '', ''


def event_kind(summary, all_day=False):
    return classify(summary, all_day)[:2]


class SyncError(Exception):
    """A fixed code with no private details."""


def end_minutes(value):
    """An end time of 00:00 or 24:00 means midnight at the end of the day."""
    return 1440 if value.strip() in ('00:00', '0:00', '24:00') else minutes(value)


def energy_setting(cfg, name, default, low, high):
    """Energy settings never stop the calendar: a blank or odd value falls back to the default."""
    value = cfg.get(name, default)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        return default
    return value


def minutes(value):
    h, m = map(int, value.split(':'))
    if not 0 <= h < 24 or not 0 <= m < 60:
        raise ValueError('invalid time')
    return h * 60 + m


def local_time(value, tz):
    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return datetime.combine(value, time.min, tzinfo=tz)


def fetch_caldav(first, last, tz, *, source, url, user, password, names):
    selected = {v.strip() for v in names.split(',') if v.strip()}
    if not user or not password:
        raise SyncError(source + '_credentials_missing')
    if not selected:
        raise SyncError(source + '_calendars_missing')
    try:
        import caldav
        calendars = caldav.DAVClient(url=url, username=user, password=password).principal().calendars()
    except Exception as exc:
        raise SyncError(source + '_login_or_connection_failed') from exc
    found, records = set(), []
    for calendar in calendars:
        if calendar.name not in selected:
            continue
        if calendar.name in found:
            raise SyncError(source + '_duplicate_calendar_name')
        found.add(calendar.name)
        try:
            # Expand recurring events whose original dates may be years ago.
            items = calendar.search(start=first, end=last, event=True, expand=True)
            for item in items:
                for component in item.icalendar_instance.walk('VEVENT'):
                    if not component.get('DTSTART') or str(component.get('STATUS', '')).upper() == 'CANCELLED':
                        continue
                    raw_start = component.get('DTSTART').dt
                    raw_end = component.get('DTEND').dt if component.get('DTEND') else None
                    begin = local_time(raw_start, tz)
                    all_day = isinstance(raw_start, date) and not isinstance(raw_start, datetime)
                    if raw_end is not None:
                        finish = local_time(raw_end, tz)
                    elif component.get('DURATION'):
                        finish = begin + component.get('DURATION').dt
                    else:
                        finish = begin + (timedelta(days=1) if all_day else timedelta(hours=1))
                    if finish <= begin:
                        finish = begin + timedelta(hours=1)
                    if finish <= first or begin >= last:
                        continue
                    kind, title, group = classify(str(component.get('SUMMARY', '')), all_day)
                    if kind == 'private' and str(component.get('TRANSP', '')).upper() == 'TRANSPARENT':
                        continue
                    records.append({'start': begin, 'end': finish, 'kind': kind,
                                    'title': title, 'allDay': all_day, 'group': group})
        except Exception as exc:
            raise SyncError(source + '_event_read_failed') from exc
    if found != selected:
        raise SyncError(source + '_calendar_not_found')
    if not records:
        raise SyncError(source + '_no_events_returned')
    return records


def fetch_icloud(first, last, tz):
    return fetch_caldav(first, last, tz, source='icloud', url='https://caldav.icloud.com',
                        user=os.environ.get('ICLOUD_USER'), password=os.environ.get('ICLOUD_APP_PASSWORD'),
                        names=os.environ.get('MYSLOT_BUSY_CALENDARS', ''))


def fetch_feishu(first, last, tz):
    url = os.environ.get('FEISHU_CALDAV_URL', '').strip()
    if not url.startswith('https://') or not url.split('/')[2].endswith('.feishu.cn'):
        raise SyncError('feishu_caldav_url_missing_or_invalid')
    return fetch_caldav(first, last, tz, source='feishu', url=url,
                        user=os.environ.get('FEISHU_CALDAV_USER'),
                        password=os.environ.get('FEISHU_CALDAV_PASSWORD'),
                        names=os.environ.get('MYSLOT_FEISHU_CALENDARS', ''))


def fetch_google(first, last, tz):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    if not all(os.environ.get(k) for k in ('GOOGLE_REFRESH_TOKEN', 'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET')):
        raise SyncError('google_credentials_missing')
    credentials = Credentials.from_authorized_user_info({
        'refresh_token': os.environ['GOOGLE_REFRESH_TOKEN'],
        'client_id': os.environ['GOOGLE_CLIENT_ID'],
        'client_secret': os.environ['GOOGLE_CLIENT_SECRET'],
    }, scopes=['https://www.googleapis.com/auth/calendar.readonly'])
    try:
        credentials.refresh(Request())
        api = build('calendar', 'v3', credentials=credentials, cache_discovery=False)
    except Exception as exc:
        raise SyncError('google_login_or_connection_failed') from exc
    selected = {v.strip() for v in os.environ.get('MYSLOT_GOOGLE_CALENDARS', '').split(',') if v.strip()}
    if not selected:
        raise SyncError('google_calendars_missing')
    try:
        return _read_google(api, selected, first, last, tz)
    except SyncError:
        raise
    except Exception as exc:
        raise SyncError('google_event_read_failed') from exc


def _read_google(api, selected, first, last, tz):
    visible, page = {}, None
    while True:
        response = api.calendarList().list(pageToken=page).execute()
        for item in response.get('items', []):
            if item['id'] in selected or item.get('summary') in selected:
                if item.get('summary') in visible and item['id'] != visible[item['summary']]:
                    raise SyncError('google_duplicate_calendar_name_use_id')
                visible[item.get('summary', item['id'])] = item['id']
        page = response.get('nextPageToken')
        if not page:
            break
    ids = {value for name, value in visible.items() if name in selected or value in selected}
    if len(ids) != len(selected):
        raise SyncError('google_calendar_not_found')
    records = []
    for calendar_id in ids:
        page = None
        while True:
            response = api.events().list(calendarId=calendar_id, timeMin=first.isoformat(),
                                         timeMax=last.isoformat(), singleEvents=True,
                                         maxResults=2500, pageToken=page).execute()
            for event in response.get('items', []):
                if event.get('status') == 'cancelled':
                    continue
                start_raw, end_raw = event['start'], event['end']
                all_day = 'date' in start_raw
                begin = local_time(date.fromisoformat(start_raw['date']), tz) if all_day else datetime.fromisoformat(start_raw['dateTime'].replace('Z', '+00:00')).astimezone(tz)
                finish = local_time(date.fromisoformat(end_raw['date']), tz) if all_day else datetime.fromisoformat(end_raw['dateTime'].replace('Z', '+00:00')).astimezone(tz)
                kind, title, group = classify(event.get('summary', ''), all_day)
                if event.get('transparency') == 'transparent' and kind == 'private':
                    continue
                records.append({'start': begin, 'end': finish, 'kind': kind,
                                'title': title, 'allDay': all_day, 'group': group})
            page = response.get('nextPageToken')
            if not page:
                break
    if not records:
        raise SyncError('google_no_events_returned')
    return records


def private_rules():
    raw = json.loads(os.environ.get('MYSLOT_PRIVATE_JSON', '').strip() or '{}')
    if not isinstance(raw, dict) or not isinstance(raw.get('dateTypes', {}), dict):
        raise ValueError('invalid private rules')
    for field in ('lockedDates', 'lowEnergyDates', 'bookedDates'):
        if not isinstance(raw.get(field, []), list):
            raise ValueError('invalid private rules')
        for item in raw.get(field, []):
            date.fromisoformat(item)
    for item, value in raw.get('dateTypes', {}).items():
        date.fromisoformat(item)
        if value not in ('workday', 'restday'):
            raise ValueError('invalid date type')
    return raw


def holiday_rules(data):
    holidays, makeup, labels = set(), set(), {}
    for year in data.values():
        for beginning, ending in year['ranges']:
            day, last = date.fromisoformat(beginning), date.fromisoformat(ending)
            while day <= last:
                holidays.add(day.isoformat())
                day += timedelta(days=1)
        makeup.update(year['makeupWorkdays'])
        labels.update(year['festivalLabels'])
    return holidays, makeup, labels


def day_type(day, cfg, private, holidays, makeup):
    key = day.isoformat()
    override = private.get('dateTypes', {}).get(key)
    if override:
        return override
    schedule = cfg['schedule']
    if schedule['mode'] == 'cycle':
        work, rest = schedule['workDays'], schedule['restDays']
        if not isinstance(work, int) or not isinstance(rest, int) or work < 1 or rest < 1:
            raise ValueError('invalid cycle')
        regular = 'workday' if (day - date.fromisoformat(schedule['anchor'])).days % (work + rest) < work else 'restday'
    elif schedule['mode'] == 'weekly':
        regular = 'workday' if day.isoweekday() in schedule['workdaysIso'] else 'restday'
    else:
        raise ValueError('invalid schedule')
    if schedule.get('holidaysOverride', True):
        if key in makeup:
            return 'workday'
        if key in holidays:
            return 'restday'
    return regular


def touched_days(event):
    day, end = event['start'].date(), (event['end'] - timedelta(microseconds=1)).date()
    while day <= end:
        yield day.isoformat()
        day += timedelta(days=1)


RANGE = re.compile(r'^(\d{1,2}:\d{2})\s*[-–~～至到]\s*(\d{1,2}:\d{2})$')


def parse_range(text):
    found = RANGE.match(text.strip())
    if not found:
        raise ValueError('invalid time range')
    a = minutes(found.group(1))
    b = end_minutes(found.group(2))
    if a >= b:
        raise ValueError('invalid time range')
    return a, b


def cut(windows, busy):
    """Remove busy [a, b] minute ranges from windows."""
    for a, b in busy:
        windows = [piece for s, e in windows
                   for piece in ((s, min(e, a)), (max(s, b), e)) if piece[0] < piece[1]]
    return windows


def snap(windows, step):
    out = []
    for a, b in windows:
        a, b = int(-(-a // step) * step), int(b // step * step)
        if b - a >= step:
            out.append([a, b])
    return out


def clip_minutes(start, end, day_start):
    a = (start - day_start).total_seconds() / 60
    b = (end - day_start).total_seconds() / 60
    return [max(0, a), min(1440, b)]


# ---------------------------------------------------------------------------
# What each day holds (private; never published as is)
# ---------------------------------------------------------------------------

def day_facts(cfg, holiday_data, private, events, now, safe=False):
    """Per day, before deciding anything about a particular person.

    closed - no data (sync failed) or a date I locked: nothing is offered.
    noNew  - no new invitations: a big plan (聚会/cos/拍照/【休息】), 已约, 躺平.
             A timed big plan on a 漫展 day only cuts its own time (fans can still meet me there).
    full   - the week already has weeklyMax 已约.
    busy   - minutes taken by ordinary timed events (rest days: plus the buffer).
    items  - 公开项 on this day: [{word, title, place}] e.g. 漫展 ijoy, 回老家.
    event  - {windows}: the time I am at a 漫展-like event.
    location - where I am (回老家 → 老家); default is my own city.
    marks  - marker words of big plans, for the summaries 长辈 see.
    """
    tz = ZoneInfo(cfg['timeZone'])
    holidays, makeup, labels = holiday_rules(holiday_data)
    step = cfg['stepMinutes']
    if not isinstance(step, int) or not 1 <= step <= 60 or 60 % step:
        raise ValueError('invalid time step')
    buffer = timedelta(minutes=energy_setting(cfg, 'bufferMinutes', 60, 0, 720))
    weekly_max = energy_setting(cfg, 'weeklyMax', 3, 0, 100) or float('inf')   # 0 = no limit
    conv_windows = [(minutes(a), end_minutes(b)) for a, b in cfg['windows']['convention']]
    if any(a >= b for a, b in conv_windows):
        raise ValueError('invalid window')
    places = {r['word']: r.get('location', '') for r in RULES['semi']}
    by_date, booked_weeks = {}, {}
    for event in events:
        for day in touched_days(event):
            by_date.setdefault(day, []).append(event)
        if event['kind'] == 'booked':
            monday = event['start'].astimezone(tz).date()
            monday -= timedelta(days=monday.weekday())
            booked_weeks[monday] = booked_weeks.get(monday, 0) + 1
    locked = set(private.get('lockedDates', [])) | set(private.get('lowEnergyDates', [])) | set(private.get('bookedDates', []))
    first = now.astimezone(tz).date()
    days = {}
    for index in range(cfg['daysAhead']):
        day = first + timedelta(days=index)
        key = day.isoformat()
        active = by_date.get(key, [])
        kind = day_type(day, cfg, private, holidays, makeup)
        marks = {v['kind'] for v in active}
        if key not in private.get('dateTypes', {}):
            kind = 'restday' if 'as_rest' in marks else 'workday' if 'as_work' in marks else kind
        facts = {'dayType': kind}
        if key in labels:
            facts['festival'] = labels[key]
        days[key] = facts
        if safe:
            facts['closed'] = True
            continue
        start = datetime.combine(day, time.min, tzinfo=tz)
        end = start + timedelta(days=1)
        big = [v for v in active if v['kind'] == 'lock']
        semis = [v for v in active if v['kind'] == 'convention']
        group = lambda v: v.get('group') or ('回老家' if v['title'] == '回老家' else '漫展')
        events_here = [v for v in semis if not places.get(group(v))]
        facts['items'] = [{'word': group(v), 'title': v['title'], 'place': bool(places.get(group(v)))} for v in semis]
        facts['marks'] = sorted({v.get('group', '') for v in big if v.get('group')})
        location = next((places[group(v)] for v in semis if places.get(group(v))), '')
        if location:
            facts['location'] = location
        if key in locked:
            facts['closed'] = True
        facts['plans'] = bool(active and any(v['kind'] in ('lock', 'booked', 'energy', 'convention', 'reserve') for v in active))
        if (any(v['kind'] in ('booked', 'energy') for v in active)
                or any(v['allDay'] for v in big) or (big and not events_here)):
            facts['noNew'] = True
        pad = buffer if kind == 'restday' else timedelta(0)
        facts['busy'] = [clip_minutes(v['start'] - pad, v['end'] + pad, start)
                         for v in active if v['kind'] == 'private' and not v['allDay']
                         and v['end'] + pad > start and v['start'] - pad < end]
        reserves = [v for v in active if v['kind'] == 'reserve']
        if reserves:
            facts['reserve'] = sorted({v['title'] for v in reserves})
        if events_here:
            windows = []
            for v in events_here:
                windows += conv_windows if v['allDay'] else [clip_minutes(v['start'], v['end'], start)]
            windows = cut(windows, [clip_minutes(v['start'], v['end'], start) for v in big])
            facts['event'] = {'windows': snap(windows, step)}
        monday = day - timedelta(days=day.weekday())
        if booked_weeks.get(monday, 0) >= weekly_max:
            facts['full'] = True
        for k in ('items', 'marks', 'busy'):
            if not facts[k]:
                del facts[k]
    return {'owner': cfg['owner'], 'timeZone': cfg['timeZone'], 'stepMinutes': step,
            'updated': now.astimezone(tz).isoformat(timespec='minutes'), 'days': days}


# ---------------------------------------------------------------------------
# What each person may see and ask for
# ---------------------------------------------------------------------------

SHARE_ITERATIONS = 150000
OWNER_CODE_MIN = 16


def normalize_code(code):
    """Forgive typing differences: full-width letters, other dashes, spaces, upper case.

    site/index.html applies exactly the same steps before decrypting.
    """
    code = unicodedata.normalize('NFKC', code)
    code = re.sub('[‐-―−－ー]', '-', code)
    return re.sub(r'\s+', '', code).lower()


# Activities: what people can ask for, each with its own times. rest / work: time ranges on
# rest days / workdays (leave out = not offered). slot false: no time picking, the date
# is a starting point to discuss. sameCity: only for people in the city I am in that day.
DEFAULT_ACTIVITIES = [
    {'name': '吃饭', 'rest': ['14:00-18:00'], 'leadHours': 24, 'sameCity': True},
    {'name': '吃饭（工作日）', 'work': ['18:30-20:00'], 'leadHours': 2, 'sameCity': True},
    {'name': '逛街', 'rest': ['14:00-18:00'], 'leadHours': 24, 'sameCity': True},
    {'name': '拍照', 'rest': ['14:00-18:00'], 'leadHours': 24, 'sameCity': True},
    {'name': 'cos（已有衣服）', 'rest': [], 'slot': False, 'leadHours': 72, 'sameCity': True},
    {'name': 'cos（新衣服）', 'rest': [], 'slot': False, 'leadHours': 336, 'sameCity': True},
    {'name': '问问我', 'rest': [], 'work': [], 'slot': False, 'leadHours': 0,
     'message': '{owner}，{date}有空吗？想找你～'},
]
ACTIVITIES = DEFAULT_ACTIVITIES
# What 长辈 see instead of details. Keys are marker words, plus 上班 / 空闲 / 其他.
DEFAULT_SUMMARIES = {'漫展': '出去玩', '回老家': '在家', '拍照': '出去拍照', 'cos': '出去拍照',
                     '聚会': '出去玩', '上班': '上班', '空闲': '空闲', '其他': '有安排'}
SUMMARIES = DEFAULT_SUMMARIES
# Words on the page. Empty = icons only.
DEFAULT_TEXTS = {'open': '', 'ask': '', 'none': '', 'hint': '以我回复确认为准'}
TEXTS = DEFAULT_TEXTS
LEVELS = {'elder', 'friend', 'con', 'close'}


def check_activities(rows):
    rows = DEFAULT_ACTIVITIES if rows is None else rows
    if not isinstance(rows, list):
        raise ValueError('invalid activities')
    names = set()
    for row in rows:
        if not (isinstance(row, dict) and isinstance(row.get('name'), str) and row['name'].strip()):
            raise ValueError('invalid activities')
        if row['name'] in names:
            raise ValueError('invalid activities')
        names.add(row['name'])
        lead = row.get('leadHours', 24)
        if isinstance(lead, bool) or not isinstance(lead, (int, float)) or not 0 <= lead <= 24 * 366:
            raise ValueError('invalid activities')
        if 'rest' not in row and 'work' not in row:
            raise ValueError('invalid activities')
        for field in ('rest', 'work'):
            for text in row.get(field, []):
                parse_range(text)
    return rows


def check_texts(texts, default):
    merged = dict(default)
    for key, value in (texts or {}).items():
        if not isinstance(value, str):
            raise ValueError('invalid texts')
        merged[key] = value.strip()[:40]
    return merged


def person(entry):
    """One person's settings. Old codes (level / tags / home / also) are translated."""
    if 'activities' in entry or 'summary' in entry or 'city' in entry:
        return {'city': entry.get('city') or '本地', 'see': set(entry.get('see', [])),
                'activities': list(entry.get('activities', [])), 'summary': entry.get('summary') is True}
    level = entry.get('level', 'friend')
    acts = {'friend': ['吃饭', '逛街'], 'con': ['拍照', 'cos（已有衣服）', 'cos（新衣服）'],
            'close': [a['name'] for a in ACTIVITIES], 'elder': []}[level]
    if '住得近' in entry.get('tags', []):
        acts = acts + ['吃饭（工作日）']
    see = set(entry.get('see', [])) | set(entry.get('also', []))
    see |= {'回老家'} if entry.get('home') is True else set()
    see |= {'漫展'} if level == 'con' else {r['word'] for r in RULES['semi']} if level == 'close' else set()
    return {'city': '本地', 'see': see, 'activities': acts, 'summary': level == 'elder'}


def hhmm(value):
    value = int(value)
    return f'{value // 60:02d}:{value % 60:02d}'


def person_days(entry, facts, now):
    """The calendar one person sees. Each day: tag (公开项 they may see), offers
    {activity: [[start, end, leadHours]] | 'ask'}, forYou. No offers and no tag = —."""
    tz = ZoneInfo(facts['timeZone'])
    step = facts['stepMinutes']
    local_now = now.astimezone(tz)
    me = person(entry)
    acts = [a for a in ACTIVITIES if a['name'] in me['activities']]
    days, marked = {}, []
    for key, f in facts['days'].items():
        day = {k: v for k, v in f.items() if k in ('dayType', 'festival')}
        days[key] = day
        if f.get('closed'):
            continue
        shown = [i for i in f.get('items', []) if i['word'] in me['see'] or i['title'] in me['see']]
        if shown:
            day['tag'] = ' / '.join(dict.fromkeys(i['title'] for i in shown))
            if any(i['title'] in me['see'] and i['word'] not in me['see'] for i in shown):
                marked.append(key)          # a single named event (e.g. ijoy): the code can end after it
        mine = f.get('reserve') and entry.get('reserve') in f['reserve']
        if mine:
            day['forYou'] = True
        if f.get('noNew') or f.get('full') or (f.get('reserve') and not mine):
            continue
        event = f.get('event')
        sees_event = any(not i['place'] for i in shown)
        if event and not sees_event:
            continue                        # I am at an event they don't know about
        day_start = datetime.combine(date.fromisoformat(key), time.min, tzinfo=tz)
        field = 'rest' if f['dayType'] == 'restday' else 'work'
        offers = {}
        for a in acts:
            if a.get('sameCity') and me['city'] != f.get('location', '本地'):
                continue
            lead = a.get('leadHours', 24)
            if field not in a:
                continue                    # not offered on this kind of day
            if a.get('slot', True) is False:
                if key >= (local_now + timedelta(hours=lead)).date().isoformat():
                    offers[a['name']] = 'ask'
                continue
            # At an event: meet inside its time. Otherwise: the activity's own time, minus busy time.
            windows = event['windows'] if event else cut([parse_range(t) for t in a[field]], f.get('busy', []))
            earliest = (local_now + timedelta(hours=lead) - day_start).total_seconds() / 60
            usable = [[hhmm(s), hhmm(e), lead] for s, e in snap([[max(s, earliest), e] for s, e in windows], step)]
            if usable:
                offers[a['name']] = usable
        if offers:
            day['offers'] = offers
    return days, (max(marked) if marked and entry.get('see') else None)


def summary_days(entry, facts):
    """长辈: only my hand-written summaries — 空闲 / 上班 / 出去玩 / 在家 ..."""
    days = {}
    for key, f in facts['days'].items():
        day = {k: v for k, v in f.items() if k == 'festival'}
        words = [i['word'] for i in f.get('items', [])] + f.get('marks', [])
        text = next((SUMMARIES[w] for w in words if SUMMARIES.get(w)), None)
        if f.get('closed') and 'busy' not in f and not f.get('items'):
            day.update(kind='none', label='')
        elif text:
            day.update(kind='mark', label=text)
        elif f.get('plans') or f.get('closed'):
            day.update(kind='plan', label=SUMMARIES.get('其他', '有安排'))
        elif f['dayType'] == 'workday':
            day.update(kind='work', label=SUMMARIES.get('上班', '上班'))
        else:
            day.update(kind='free', label=SUMMARIES.get('空闲', '空闲'))
        days[key] = day
    return days


def share_codes():
    """Read MYSLOT_SHARE_CODES: one entry per person.

    {"code", "label"?, "city"?, "see"?: [公开项 words or names], "activities"?: [names],
     "summary"?: true, "reserve"?, "name"?, "until"?, "forever"?}  (old level / tags still work)
    """
    raw = json.loads(os.environ.get('MYSLOT_SHARE_CODES', '').strip() or '[]')
    if not isinstance(raw, list):
        raise ValueError('invalid share codes')
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get('code'), str):
            raise ValueError('invalid share codes')
        if entry.get('level', 'friend') not in LEVELS:
            raise ValueError('invalid share codes')
        for field in ('see', 'also', 'tags', 'activities'):
            if not (isinstance(entry.get(field, []), list) and all(isinstance(v, str) for v in entry.get(field, []))):
                raise ValueError('invalid share codes')
        for field in ('reserve', 'city', 'label', 'name'):
            if entry.get(field) is not None and not isinstance(entry[field], str):
                raise ValueError('invalid share codes')
        if entry.get('until') is not None:
            date.fromisoformat(entry['until'])
    return raw


def label_of(entry):
    return entry.get('label') or entry['code'].split('-')[0]


def payload_for(entry, facts, now):
    """Everything one code may see; returns (payload, until)."""
    me = person(entry)
    payload = {k: facts[k] for k in ('timeZone', 'updated')}
    payload['owner'] = entry.get('name') or facts['owner']
    payload['syncFailed'] = bool(facts.get('syncFailed'))
    payload['texts'] = TEXTS
    if me['summary']:
        days, last_marked = summary_days(entry, facts), None
        payload['simple'] = True
    else:
        days, last_marked = person_days(entry, facts, now)
        payload['stepMinutes'] = facts['stepMinutes']
        payload['activities'] = [{'name': a['name'], 'slot': a.get('slot', True), 'message': a.get('message', '')}
                                 for a in ACTIVITIES if a['name'] in me['activities']]
    until = None if entry.get('forever') is True else entry.get('until') or last_marked
    if until:
        days = {k: v for k, v in days.items() if k <= until}
    payload.update(days=days, until=until)
    return payload, until


def _seal(code, payload):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    salt, nonce = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(SHA256(), 32, salt, SHARE_ITERATIONS).derive(code.encode('utf-8'))
    data = AESGCM(key).encrypt(nonce, json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'), None)
    return {'s': base64.b64encode(salt).decode(), 'n': base64.b64encode(nonce).decode(),
            'd': base64.b64encode(data).decode()}


def seal_for_codes(facts, codes, today, now=None, owner_code=None):
    """One encrypted copy per share code, plus my own overview. Only ciphertext is published."""
    now = now or datetime.now(timezone.utc)
    sealed, skipped, people = [], 0, []
    for entry in codes:
        code = normalize_code(entry['code'])
        payload, until = payload_for(entry, facts, now)
        if len(code) < 6 or (until and until < today):
            skipped += 1
            continue
        payload['demo'] = bool(facts.get('demo'))
        sealed.append(_seal(code, payload))
        people.append({'label': label_of(entry), 'view': payload})
    owner = normalize_code(owner_code or '')
    if len(owner) >= OWNER_CODE_MIN:
        sealed.append(_seal(owner, {'ownerView': True, 'owner': facts['owner'], 'timeZone': facts['timeZone'],
                                    'updated': facts['updated'], 'demo': bool(facts.get('demo')),
                                    'syncFailed': bool(facts.get('syncFailed')), 'people': people}))
    sealed.sort(key=lambda v: v['d'])  # order must not reveal which entry is which
    return {'schema': 5, 'iterations': SHARE_ITERATIONS, 'sealed': sealed}, len(people), skipped


REQUIRED_CONFIG = ('owner', 'timeZone', 'schedule', 'windows', 'stepMinutes', 'daysAhead')


def config_problems(cfg):
    """Names of settings that look wrong. Field names only, never values: the log is public."""
    problems = []
    def number(value, low, high):
        return not isinstance(value, bool) and isinstance(value, (int, float)) and low <= value <= high
    schedule = cfg.get('schedule') if isinstance(cfg.get('schedule'), dict) else {}
    if schedule.get('mode') not in ('weekly', 'cycle'):
        problems.append('schedule.mode')
    if schedule.get('mode') == 'weekly' and not (isinstance(schedule.get('workdaysIso'), list)
                                                 and all(number(v, 1, 7) for v in schedule['workdaysIso'])):
        problems.append('schedule.workdays')
    if schedule.get('mode') == 'cycle':
        try:
            date.fromisoformat(schedule.get('anchor', ''))
        except (TypeError, ValueError):
            problems.append('schedule.anchor')
        for field in ('workDays', 'restDays'):
            if not (isinstance(schedule.get(field), int) and schedule[field] >= 1):
                problems.append('schedule.' + field)
    windows = cfg.get('windows') if isinstance(cfg.get('windows'), dict) else {}
    for name in ('convention',):
        try:
            for start, end in windows[name]:
                if minutes(start) >= end_minutes(end):
                    raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            problems.append('windows.' + name)
    step = cfg.get('stepMinutes')
    if not (isinstance(step, int) and 1 <= step <= 60 and 60 % step == 0):
        problems.append('stepMinutes')
    if not number(cfg.get('daysAhead'), 1, 366) or not isinstance(cfg.get('daysAhead'), int):
        problems.append('daysAhead')
    return problems


def load_config():
    """MYSLOT_CONFIG_JSON (a Secret) keeps the schedule out of the public repository.

    A broken Secret must not crash the run: fall back to config.json, close every day
    and say so in the log.
    """
    public = json.loads((ROOT/'config.json').read_text(encoding='utf-8'))
    raw = os.environ.get('MYSLOT_CONFIG_JSON', '').strip()
    if not raw:
        return public, None
    try:
        cfg = json.loads(raw)
    except ValueError:
        return public, 'config_secret_not_valid_json'
    if not isinstance(cfg, dict) or any(k not in cfg for k in REQUIRED_CONFIG):
        return public, 'config_secret_missing_fields'
    return cfg, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT/'dist')
    parser.add_argument('--fixture', action='store_true')
    args = parser.parse_args()
    cfg, config_problem = load_config()
    for name, label, default, low, high in (('weeklyMax', '每周最多约几次', 3, 0, 100), ('bufferMinutes', '活动前后留出', 60, 0, 720)):
        if name in cfg and energy_setting(cfg, name, default, low, high) != cfg[name]:
            print(f'Setting「{label}」looks blank or odd; using {default}.')
    holiday_data = json.loads((ROOT/'holidays-cn.json').read_text(encoding='utf-8'))
    global RULES, ACTIVITIES, SUMMARIES, TEXTS
    rules_broken = False
    try:
        RULES = check_rules(cfg.get('rules'))
        acts = cfg.get('activities')
        # Older settings listed activities without their own times: fall back to the defaults.
        new_style = isinstance(acts, list) and acts and all(isinstance(r, dict) and ('rest' in r or 'work' in r) for r in acts)
        ACTIVITIES = check_activities(acts if new_style else None)
        SUMMARIES = check_texts(cfg.get('summaries'), DEFAULT_SUMMARIES)
        TEXTS = check_texts(cfg.get('texts'), DEFAULT_TEXTS)
    except (ValueError, TypeError):
        rules_broken = True  # never guess: a broken rule list closes every day
    now = datetime.now(timezone.utc)
    tz = ZoneInfo(cfg['timeZone'])
    first = datetime.combine(now.astimezone(tz).date(), time.min, tzinfo=tz)
    last = first + timedelta(days=cfg['daysAhead']+1)
    first -= timedelta(days=first.weekday())  # read from Monday: earlier 已约 count toward this week
    events, private, safe, codes = [], {}, False, []
    owner_code = os.environ.get('MYSLOT_OWNER_CODE', '')
    if args.fixture:
        codes = [{'code': 'demo-a', 'label': 'A', 'city': '老家', 'see': ['漫展', '回老家'], 'activities': ['吃饭', '问问我']},
                 {'code': 'demo-c', 'label': 'C', 'see': ['漫展'], 'activities': ['吃饭', '吃饭（工作日）', '逛街']},
                 {'code': 'demo-d', 'label': 'D', 'see': ['漫展'], 'activities': ['拍照', 'cos（已有衣服）', 'cos（新衣服）'],
                  'reserve': 'D', 'name': '示例CN'},
                 {'code': 'demo-client', 'label': '拍照客户', 'activities': ['拍照']},
                 {'code': 'demo-parents', 'label': '爸妈', 'summary': True}]
        owner_code = 'demo-owner-0000000'
        today = datetime.combine(now.astimezone(tz).date(), time.min, tzinfo=tz)
        show = today + timedelta(days=3)
        events = [
            {'start': show, 'end': show+timedelta(days=1), 'kind': 'convention', 'title': '星光漫展', 'allDay': True, 'group': '漫展'},
            {'start': show+timedelta(hours=14), 'end': show+timedelta(hours=15), 'kind': 'lock', 'title': '', 'allDay': False, 'group': '拍照'},
            {'start': show+timedelta(days=2), 'end': show+timedelta(days=3), 'kind': 'lock', 'title': '', 'allDay': True, 'group': '聚会'},
            {'start': show+timedelta(days=4), 'end': show+timedelta(days=5), 'kind': 'reserve', 'title': 'D', 'allDay': True},
            {'start': show+timedelta(days=6), 'end': show+timedelta(days=8), 'kind': 'convention', 'title': '回老家', 'allDay': True, 'group': '回老家'},
            {'start': show+timedelta(days=1, hours=13), 'end': show+timedelta(days=1, hours=15), 'kind': 'private', 'title': '', 'allDay': False},
        ]
    else:
        try:
            private = private_rules()
            if config_problem:
                raise SyncError(config_problem)
            if rules_broken:
                raise SyncError('calendar_rules_invalid')
            sources = cfg.get('calendarSources', ['icloud'])
            if not isinstance(sources, list) or not sources or any(s not in ('icloud', 'google', 'feishu') for s in sources):
                raise SyncError('calendar_source_not_configured')
            if 'icloud' in sources:
                events.extend(fetch_icloud(first, last, tz))
            if 'google' in sources:
                events.extend(fetch_google(first, last, tz))
            if 'feishu' in sources:
                events.extend(fetch_feishu(first, last, tz))
            if not events:
                raise SyncError('no_events_returned_check_calendar_selection')
            # Counts only: no titles, calendar names or dates reach the public log.
            kinds = {k: sum(1 for v in events if v['kind'] == k) for k in ('private', 'lock', 'convention', 'reserve', 'booked', 'energy')}
            print(f"Read {len(events)} events in the next {cfg['daysAhead']} days: "
                  f"{kinds['private']} ordinary, {kinds['lock']} big plans, {kinds['convention']} shared items (漫展/回老家), "
                  f"{kinds['reserve']} reserved, {kinds['booked']} booked, {kinds['energy']} rest days.")
        except Exception as exc:
            code = str(exc) if isinstance(exc, SyncError) else 'private_input_or_unexpected_error'
            print(f'Calendar sync failed ({code}); publishing closed days.')
            events, private, safe, codes = [], {}, True, []
    try:
        facts = day_facts(cfg, holiday_data, private, events, now, safe=safe)
    except Exception as exc:
        # A bad value in the settings must not stop the page.
        names = {'windows.convention': '全天半开放日的见面时段', 'schedule.mode': '排班方式',
                 'schedule.workdays': '上班的星期', 'schedule.anchor': '轮班起点',
                 'schedule.workDays': '轮班上几天', 'schedule.restDays': '轮班休几天',
                 'stepMinutes': '时间间隔', 'daysAhead': '显示天数'}
        where = '、'.join(names.get(k, k) for k in config_problems(cfg)) or type(exc).__name__
        print(f'Calendar sync failed (config_values_invalid: {where}); publishing closed days.')
        cfg = json.loads((ROOT/'config.json').read_text(encoding='utf-8'))
        safe, private = True, {}
        facts = day_facts(cfg, holiday_data, private, [], now, safe=True)
    facts['demo'] = args.fixture
    facts['syncFailed'] = safe
    if not args.fixture:
        try:
            codes = share_codes()
        except Exception:
            # A typo here only disables codes; the public page still updates.
            print('Share codes ignored (share_codes_invalid).')
            codes = []
    result, active, skipped = seal_for_codes(facts, codes, now.astimezone(tz).date().isoformat(), now=now,
                                             owner_code=owner_code)
    owner_ok = len(normalize_code(owner_code)) >= OWNER_CODE_MIN
    print(f'Share codes: {active} active, {skipped} expired or too short. '
          + ('Owner view: on.' if owner_ok else f'Owner view: off (MYSLOT_OWNER_CODE needs {OWNER_CODE_MIN}+ characters).'))
    args.output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT/'site'/'index.html', args.output/'index.html')
    (args.output/'availability.json').write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print('Published day-level states only.' + (' Fixture mode.' if args.fixture else ''))


if __name__ == '__main__':
    main()
