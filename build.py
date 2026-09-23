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
    # All-day events that change the day type: my own day off / extra working day.
    'restday': ['调休'],
    'workday': ['补班'],
    # 半开放日: "漫展-xx" publishes only "xx"; a bare word publishes the word.
    # seenBy: levels that see the name. others: what everyone else sees
    # ("event" = 有安排、可以约吃饭, "busy" = 不可用).
    'semi': [
        {'word': '漫展', 'seenBy': ['con', 'close'], 'others': 'event'},
        {'word': '回老家', 'seenBy': ['elder', 'close'], 'others': 'busy'},
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
    for key in ('busy', 'booked', 'reserve', 'restday', 'workday'):
        if not isinstance(rules[key], list) or not all(isinstance(w, str) and w.strip() for w in rules[key]):
            raise ValueError('invalid rules')
    for rule in rules['semi']:
        if not (isinstance(rule, dict) and isinstance(rule.get('word'), str) and rule['word'].strip()
                and isinstance(rule.get('seenBy', []), list) and rule.get('others', 'event') in ('event', 'busy')):
            raise ValueError('invalid rules')
    return rules


def clean_label(text):
    return re.sub(r'[\x00-\x1f\x7f<>]', ' ', text).strip()[:36]


def classify(summary, all_day=False, rules=None):
    """Return (kind, public label, semi word). kind: booked | reserve | lock | convention | private."""
    rules = rules or RULES
    summary = summary.strip()
    if any(re.search(r'^\s*【?' + word_pattern(w), summary, re.I) for w in rules['booked']):
        return 'booked', '', ''
    if all_day and any(re.search(word_pattern(w), summary, re.I) for w in rules['restday']):
        return 'as_rest', '', ''
    if all_day and any(re.search(word_pattern(w), summary, re.I) for w in rules['workday']):
        return 'as_work', '', ''
    for w in rules['reserve']:
        found = re.search(word_pattern(w) + NAME_AFTER, summary, re.I)
        if found:
            return 'reserve', clean_label(found.group(1)), ''
    if any(re.search(word_pattern(w), summary, re.I) for w in rules['busy']) or (all_day and '休息' in summary):
        return 'lock', '', ''
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


def windows_for(day, cfg, kind, convention):
    origin = datetime.combine(day, time.min, tzinfo=ZoneInfo(cfg['timeZone']))
    step = cfg['stepMinutes']
    name = 'convention' if convention else kind
    result = []
    for start_text, end_text in cfg['windows'][name]:
        a, b = minutes(start_text), end_minutes(end_text)
        if a >= b:
            raise ValueError('invalid window')
        start, end = origin + timedelta(minutes=a), origin + timedelta(minutes=b)
        if convention and not convention['allDay']:
            start, end = max(start, convention['start']), min(end, convention['end'])
        # Keep dropdown times on the same fixed grid as the server check.
        if start < end:
            a_minutes = (start - origin).total_seconds() / 60
            b_minutes = (end - origin).total_seconds() / 60
            start = origin + timedelta(minutes=int(-(-a_minutes // step)) * step)
            end = origin + timedelta(minutes=int(b_minutes // step) * step)
        if start < end:
            result.append((start, end))
    return result


def subtract_busy(windows, busy, origin, step):
    """Remove timed busy events from windows, keeping edges on the step grid."""
    for event in busy:
        pieces = []
        for start, end in windows:
            if event['end'] <= start or event['start'] >= end:
                pieces.append((start, end))
                continue
            if event['start'] > start:
                cut = (event['start'] - origin).total_seconds() / 60
                pieces.append((start, origin + timedelta(minutes=int(cut // step) * step)))
            if event['end'] < end:
                cut = (event['end'] - origin).total_seconds() / 60
                pieces.append((origin + timedelta(minutes=int(-(-cut // step)) * step), end))
        windows = [(a, b) for a, b in pieces if a < b]
    return windows


def merge_windows(windows):
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def has_allowed_start(day, windows, now, notice, step):
    if not windows:
        return False
    origin = datetime.combine(day, time.min, tzinfo=windows[0][0].tzinfo)
    earliest = now.astimezone(origin.tzinfo) + timedelta(hours=notice)
    return any(start <= origin + timedelta(minutes=n) and
               origin + timedelta(minutes=n+step) <= end and
               origin + timedelta(minutes=n) >= earliest
               for start, end in windows for n in range(0, 1440, step))


def build_data(cfg, holiday_data, private, events, now, safe=False):
    tz = ZoneInfo(cfg['timeZone'])
    holidays, makeup, labels = holiday_rules(holiday_data)
    step, lead = cfg['stepMinutes'], cfg['leadHours']
    if not isinstance(step, int) or not 1 <= step <= 60 or 60 % step:
        raise ValueError('invalid time step')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 2160 for v in lead.values()):
        raise ValueError('invalid lead time')
    by_date = {}
    for event in events:
        for day in touched_days(event):
            by_date.setdefault(day, []).append(event)
    buffer = timedelta(minutes=energy_setting(cfg, 'bufferMinutes', 60, 0, 720))
    weekly_max = energy_setting(cfg, 'weeklyMax', 3, 0, 100)
    if weekly_max == 0:
        weekly_max = float('inf')      # 0 = no weekly limit
    booked_weeks = {}
    for event in events:
        if event['kind'] == 'booked':
            monday = event['start'].astimezone(tz).date()
            monday -= timedelta(days=monday.weekday())
            booked_weeks[monday] = booked_weeks.get(monday, 0) + 1
    locked = set(private.get('lockedDates', [])) | set(private.get('lowEnergyDates', [])) | set(private.get('bookedDates', []))
    output = {}
    first = now.astimezone(tz).date()
    for index in range(cfg['daysAhead']):
        day = first + timedelta(days=index)
        key = day.isoformat()
        kind = day_type(day, cfg, private, holidays, makeup)
        marks = {v['kind'] for v in by_date.get(key, [])}
        if 'as_rest' in marks and key not in private.get('dateTypes', {}):
            kind = 'restday'
        elif 'as_work' in marks and key not in private.get('dateTypes', {}):
            kind = 'workday'
        entry = {'status': 'locked', 'dayType': kind}
        if key in labels:
            entry['festival'] = labels[key]
        active = by_date.get(key, [])
        # One meet-up a day: a confirmed 已约 closes the rest of that day.
        if safe or key in locked or any(v['kind'] in ('lock', 'booked') for v in active):
            if not safe:
                entry['busy'] = True   # an activity, not just "no slot left" (only 长辈 view uses it)
            output[key] = entry
            continue
        reserves = [v for v in active if v['kind'] == 'reserve']
        conventions = reserves or [v for v in active if v['kind'] == 'convention']
        convention = conventions[0] if conventions else None
        beginning = datetime.combine(day, time.min, tzinfo=tz)
        ending = beginning + timedelta(days=1)
        # All-day ordinary events (回老家, travel...) keep the day open; only the
        # place changes. Only timed ordinary events occupy time.
        busy = [v for v in active if v['kind'] == 'private' and not v['allDay']]
        # Busy minutes of this day (with the rest-day buffer) for per-project time slots.
        pad = buffer if kind == 'restday' else timedelta(0)
        entry['busyTimes'] = sorted(
            [max(0, int((v['start'] - pad - beginning).total_seconds() // 60)),
             min(1440, int(-(-(v['end'] + pad - beginning).total_seconds() // 60)))]
            for v in busy if v['end'] + pad > beginning and v['start'] - pad < ending)
        if not convention and kind == 'restday' and any(
            (min(v['end'], ending) - max(v['start'], beginning)).total_seconds() > 3*3600
            for v in busy):
            entry['busy'] = True
            output[key] = entry
            continue
        if convention:
            # Half-available: the marked event's own time is bookable, whatever else
            # is on that day. All-day marks use the configured convention window.
            windows = []
            for v in conventions:
                if v['allDay']:
                    # 预留 days use the normal day's hours; 漫展 days the convention hours.
                    windows += windows_for(day, cfg, kind, None if v['kind'] == 'reserve' else v)
                else:
                    a = (max(v['start'], beginning) - beginning).total_seconds() / 60
                    b = min((min(v['end'], ending) - beginning).total_seconds() / 60, 1439)  # keep HH:MM within the day
                    start = beginning + timedelta(minutes=int(-(-a // step)) * step)
                    end = beginning + timedelta(minutes=int(b // step) * step)
                    if start < end:
                        windows.append((start, end))
            windows = merge_windows(windows)
        else:
            # Travel/rest buffer on rest days. Workday windows already sit after the shift.
            pad = buffer if kind == 'restday' else timedelta(0)
            padded = [dict(v, start=v['start'] - pad, end=v['end'] + pad) for v in busy]
            windows = subtract_busy(windows_for(day, cfg, kind, None), padded, beginning, step)
        # Private facts for the per-person views; view_for / project views strip them.
        monday = day - timedelta(days=day.weekday())
        entry['_full'] = booked_weeks.get(monday, 0) >= weekly_max
        if reserves:
            entry['_reserve'] = sorted({v['title'] for v in reserves})
        elif convention:
            entry['_semi'] = {
                'title': ' / '.join(dict.fromkeys(v['title'] for v in conventions))[:40],
                'group': conventions[0].get('group') or ('回老家' if conventions[0]['title'] == '回老家' else '漫展'),
                'windows': [[int((a - beginning).total_seconds() // 60), int((b - beginning).total_seconds() // 60)]
                            for a, b in windows]}
        if not has_allowed_start(day, windows, now, lead[kind], step):
            output[key] = entry
            continue
        entry['status'] = 'reserved' if reserves else 'event' if convention else 'open'
        entry['windows'] = [[a.strftime('%H:%M'), '24:00' if b.date() > day else b.strftime('%H:%M')] for a, b in windows]
        if reserves:
            entry['for'] = sorted({v['title'] for v in reserves})
        elif convention:
            entry['title'] = ' / '.join(dict.fromkeys(v['title'] for v in conventions))[:40]
            first_mark = conventions[0]
            entry['group'] = first_mark.get('group') or ('回老家' if first_mark['title'] == '回老家' else '漫展')
        if entry['_full']:
            entry = {k: v for k, v in entry.items() if k in ('dayType', 'festival') or k.startswith('_')}
            entry['status'] = 'full'
        output[key] = entry
    return {'schema': 2, 'owner': cfg['owner'], 'timeZone': cfg['timeZone'],
            'stepMinutes': step, 'leadHours': lead, 'cosLeadDays': cfg['cosLeadDays'],
            'updated': now.astimezone(tz).isoformat(timespec='minutes'), 'days': output}


SHARE_ITERATIONS = 150000


def normalize_code(code):
    """Forgive typing differences: full-width letters, other dashes, spaces, upper case.

    site/index.html applies exactly the same steps before decrypting.
    """
    code = unicodedata.normalize('NFKC', code)
    code = re.sub('[\u2010-\u2015\u2212\uff0d\u30fc]', '-', code)
    return re.sub(r'\s+', '', code).lower()


def share_codes():
    """Read MYSLOT_SHARE_CODES.

    [{"code": "...", "name"?: "...", "see"?: [漫展 names] | "all", "reserve"?: "xx", "until"?: "YYYY-MM-DD"}]
    """
    raw = json.loads(os.environ.get('MYSLOT_SHARE_CODES', '').strip() or '[]')
    if not isinstance(raw, list):
        raise ValueError('invalid share codes')
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get('code'), str):
            raise ValueError('invalid share codes')
        if entry.get('level', 'friend') not in LEVELS:
            raise ValueError('invalid share codes')
        if 'ordinary' in entry and not isinstance(entry['ordinary'], bool):
            raise ValueError('invalid share codes')
        see = entry.get('see', [])
        if not (isinstance(see, list) and all(isinstance(v, str) for v in see)):
            raise ValueError('invalid share codes')
        if not (isinstance(entry.get('also', []), list) and all(isinstance(v, str) for v in entry.get('also', []))):
            raise ValueError('invalid share codes')
        if entry.get('reserve') is not None and not isinstance(entry['reserve'], str):
            raise ValueError('invalid share codes')
        if entry.get('until') is not None:
            date.fromisoformat(entry['until'])
    return raw


LEVELS = {'elder', 'friend', 'con', 'close'}
# Per level: display name, whether days without a matching project show 可以商量, and the simple
# 空闲/上班/有安排 view. Editable in config "levels" (settings.html).
DEFAULT_LEVELS = {
    'friend': {'name': '朋友', 'ordinary': True, 'simple': False},
    'elder': {'name': '长辈', 'ordinary': True, 'simple': True},
    'con': {'name': '圈内好友', 'ordinary': True, 'simple': False},
    'close': {'name': '密友', 'ordinary': True, 'simple': False},
}
LEVEL_SETTINGS = DEFAULT_LEVELS


def check_levels(levels):
    merged = {k: dict(v) for k, v in DEFAULT_LEVELS.items()}
    for key, value in (levels or {}).items():
        if key not in merged or not isinstance(value, dict):
            raise ValueError('invalid levels')
        for field in ('ordinary', 'simple'):
            if field in value and not isinstance(value[field], bool):
                raise ValueError('invalid levels')
        merged[key].update({k: v for k, v in value.items() if k in ('name', 'ordinary', 'simple')})
    return merged


def semi_visibility(entry, group, title, rules=None):
    """(can this person see the name?, what others see: 'event' | 'busy')."""
    rules = rules or RULES
    rule = {r['word']: r for r in rules['semi']}.get(group, {'seenBy': ['close'], 'others': 'event'})
    also = set(entry.get('also', [])) | ({'回老家'} if entry.get('home') is True else set())
    shown = (entry.get('level', 'friend') in rule.get('seenBy', []) or group in also
             or any(n in title.split(' / ') for n in entry.get('see', [])))
    return shown, rule.get('others', 'event')


def view_for(entry, result, rules=None):
    """What one share code may see. The page never says that others see more or less.

    level: friend (朋友) / elder (长辈) / con (圈内) / close (密友). Each 半开放 rule in
    config "rules" says which levels see its name and what everyone else sees.
    also: extra 半开放 words for this person (home: true is short for also 回老家).
    see: extra single names (the xx) for this person; the code then ends after the last one.
    reserve: the xx in 预留-xx. Anyone can be given one.
    elder: only 空闲 / 上班 / 有安排 (and names of 半开放 days they may see).
    """
    rules = rules or RULES
    level = entry.get('level', 'friend')
    settings = LEVEL_SETTINGS.get(level, DEFAULT_LEVELS['friend'])
    simple = settings['simple']
    # ordinary: can this person book plain open days? A per-person value wins.
    ordinary = entry['ordinary'] if isinstance(entry.get('ordinary'), bool) else settings['ordinary']
    see, mine = entry.get('see', []), entry.get('reserve')
    also = set(entry.get('also', [])) | ({'回老家'} if entry.get('home') is True else set())
    semi = {r['word']: r for r in rules['semi']}
    days, marked = {}, []
    for key, value in result['days'].items():
        day = {k: v for k, v in value.items() if not k.startswith('_') and k != 'busyTimes'}
        had_plans = (day.pop('busy', False) or day['status'] in ('reserved', 'event')
                     or '_semi' in value or '_reserve' in value)
        closed = {k: v for k, v in day.items() if k in ('dayType', 'festival')}
        if day['status'] == 'reserved':
            if mine and mine in day.pop('for'):
                day['status'], day['forYou'] = 'open', True
                marked.append(key)
            else:
                day = dict(closed, status='locked')
        elif day['status'] == 'event':
            group = day.pop('group', '漫展')
            rule = semi.get(group, {'seenBy': ['close'], 'others': 'event'})
            by_level = level in rule.get('seenBy', []) or group in also
            by_name = any(n in day['title'].split(' / ') for n in see)
            if by_name and not by_level:
                marked.append(key)
            if not (by_level or by_name):
                if rule.get('others', 'event') == 'busy':
                    day = dict(closed, status='locked')   # e.g. away from town
                else:
                    del day['title']
        if not simple and not ordinary and (day['status'] == 'full' or
                                            (day['status'] == 'open' and not day.get('forYou'))):
            day = dict(closed, status='off')   # not offered to this person; shown neutrally
        if simple:
            if day['status'] == 'event' and 'title' in day:
                day = dict(closed, status='mark', title=day['title'])
            elif had_plans:
                day = dict(closed, status='busy')        # shown as 有安排, never 忙
            elif day.get('dayType') == 'workday':
                day = dict(closed, status='work')        # 搬砖日
            else:
                day = dict(closed, status='free')
            day.pop('dayType', None)
        days[key] = day
    return days, (max(marked) if marked and see else None)


# What people can ask for, and when. Editable in config "projects" (settings.html).
# rest / work: time ranges offered on rest days / workdays ("14:00-18:00"); leave the key
# out to not offer that day type. slot false = no time picking, just a date to discuss.
DEFAULT_PROJECTS = [
    {'project': '吃饭', 'levels': ['friend'], 'rest': ['14:00-18:00'], 'leadHours': 24},
    {'project': '吃饭', 'tags': ['住得近'], 'work': ['18:30-20:00'], 'leadHours': 2},
    {'project': '逛街', 'levels': ['friend'], 'rest': ['14:00-18:00'], 'leadHours': 24},
    {'project': '拍照', 'levels': ['con'], 'rest': ['14:00-18:00'], 'leadHours': 24},
    {'project': 'cos', 'levels': ['con', 'close'], 'rest': [], 'work': [], 'slot': False, 'leadHours': 720},
    {'project': '旅行', 'levels': ['close'], 'rest': [], 'slot': False, 'leadHours': 336},
    {'project': '吃饭', 'levels': ['close'], 'rest': ['10:00-21:00'], 'work': ['18:30-21:00'], 'leadHours': 2},
    {'project': '逛街', 'levels': ['close'], 'rest': ['10:00-21:00'], 'work': ['18:30-21:00'], 'leadHours': 2},
    {'project': '拍照', 'levels': ['close'], 'rest': ['10:00-21:00'], 'work': ['18:30-21:00'], 'leadHours': 2},
]
PROJECTS = DEFAULT_PROJECTS
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


def check_projects(projects):
    projects = DEFAULT_PROJECTS if projects is None else projects
    if not isinstance(projects, list):
        raise ValueError('invalid projects')
    for row in projects:
        if not (isinstance(row, dict) and isinstance(row.get('project'), str) and row['project'].strip()):
            raise ValueError('invalid projects')
        if any(v not in LEVELS for v in row.get('levels', [])) or not all(isinstance(t, str) for t in row.get('tags', [])):
            raise ValueError('invalid projects')
        lead = row.get('leadHours', 24)
        if isinstance(lead, bool) or not isinstance(lead, (int, float)) or not 0 <= lead <= 24 * 365:
            raise ValueError('invalid projects')
        for field in ('rest', 'work'):
            for text in row.get(field, []):
                parse_range(text)
    return projects


def rows_for(entry, projects):
    level, tags = entry.get('level', 'friend'), set(entry.get('tags', []))
    return [r for r in projects if level in r.get('levels', []) or tags & set(r.get('tags', []))]


def cut(windows, busy):
    for a, b in busy:
        windows = [piece for s, e in windows
                   for piece in ((s, min(e, a)), (max(s, b), e)) if piece[0] < piece[1]]
    return windows


def project_days(entry, result, now, rules=None, step=30):
    """Per-person calendar by project. Only real plans close a day; the rest is 可以商量."""
    rules = rules or RULES
    tz = ZoneInfo(result['timeZone'])
    rows = rows_for(entry, PROJECTS)
    order = list(dict.fromkeys(r['project'] for r in PROJECTS))   # same tab order for everyone
    names = sorted(dict.fromkeys(r['project'] for r in rows), key=order.index)
    level = entry.get('level', 'friend')
    settings = LEVEL_SETTINGS.get(level, DEFAULT_LEVELS['friend'])
    talk = entry['ordinary'] if isinstance(entry.get('ordinary'), bool) else settings['ordinary']
    local_now = now.astimezone(tz)
    days = {}
    for key, raw in result['days'].items():
        day = {k: v for k, v in raw.items() if k in ('dayType', 'festival')}
        day_start = datetime.combine(date.fromisoformat(key), time.min, tzinfo=tz)
        offers = {}
        if raw.get('busy') or 'busyTimes' not in raw:     # real plans, or no data (sync failed)
            base = 'locked'
        elif raw.get('_reserve'):
            mine = entry.get('reserve') in raw['_reserve']
            base = 'ok' if mine else 'locked'
            if mine:
                day['forYou'] = True
        elif raw.get('_semi'):
            semi = raw['_semi']
            shown, others = semi_visibility(entry, semi['group'], semi['title'], rules)
            if shown:
                base, day['title'] = 'semi', semi['title']
            else:
                base = 'locked' if others == 'busy' else 'plans'
        elif raw.get('_full') or raw['status'] == 'full':
            base = 'full'
        else:
            base = 'ok'
        if base == 'semi':
            # Meet inside the marked time only, for any project that picks a time.
            for name in names:
                picks = [r for r in rows if r['project'] == name and r.get('slot', True)]
                if picks:
                    lead = min(r.get('leadHours', 24) for r in picks)
                    offers[name] = [w + [lead] for w in raw['_semi']['windows']]
        elif base == 'ok':
            field = 'rest' if raw['dayType'] == 'restday' else 'work'
            for r in rows:
                if field not in r:
                    continue
                lead = r.get('leadHours', 24)
                if not r.get('slot', True):
                    first_day = (local_now + timedelta(hours=lead)).date().isoformat()
                    if key >= first_day and not isinstance(offers.get(r['project']), list):
                        offers[r['project']] = 'ask'
                    continue
                earliest = (local_now + timedelta(hours=lead) - day_start).total_seconds() / 60
                for a, b in cut([parse_range(t) for t in r[field]], raw.get('busyTimes', [])):
                    a = int(-(-max(a, earliest) // step) * step)
                    b = int(b // step * step)
                    if b - a >= step:
                        current = offers.get(r['project'])
                        offers[r['project']] = (current if isinstance(current, list) else []) + [[a, b, lead]]
            if not offers and not talk:
                base = 'off'
        day['base'] = base
        if offers:
            day['p'] = {k: (v if v == 'ask' else [[f'{a // 60:02d}:{a % 60:02d}', f'{b // 60:02d}:{b % 60:02d}', lead]
                                                  for a, b, lead in v]) for k, v in offers.items()}
        days[key] = day
    return days, names


def seal_for_codes(result, codes, today, rules=None, now=None):
    """Replace the whole public output with one encrypted copy per share code.

    The published file holds only ciphertext; codes live in a GitHub Secret.
    A code without "until" ends after its last 预留/漫展 day, or never if it has none.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    sealed, skipped = [], 0
    for entry in codes:
        code = normalize_code(entry['code'])
        days, last_marked = view_for(entry, result, rules)
        # forever: never expires, even if the entry also lists extra 漫展 names.
        until = None if entry.get('forever') is True else entry.get('until') or last_marked
        if len(code) < 6 or (until and until < today):
            skipped += 1
            continue
        if until:
            days = {k: v for k, v in days.items() if k <= until}
        payload = {k: v for k, v in result.items() if k not in ('days', 'owner')}
        payload.update(owner=entry.get('name') or result['owner'], until=until, days=days)
        for field in ('leadHours', 'cosLeadDays', 'syncFailed'):
            payload.pop(field, None)
        payload['syncFailed'] = bool(result.get('syncFailed'))
        if LEVEL_SETTINGS.get(entry.get('level', 'friend'), {}).get('simple'):
            payload['simple'] = True
            payload.pop('stepMinutes', None)
        else:
            pdays, names = project_days(entry, result, now or datetime.now(timezone.utc), rules,
                                        result.get('stepMinutes', 30))
            if until:
                pdays = {k: v for k, v in pdays.items() if k <= until}
            slot_of = {}
            for r in rows_for(entry, PROJECTS):
                slot_of[r['project']] = slot_of.get(r['project'], False) or r.get('slot', True)
            payload.update(days=pdays, projects=[{'name': n, 'slot': slot_of[n]} for n in names])
        salt, nonce = os.urandom(16), os.urandom(12)
        key = PBKDF2HMAC(SHA256(), 32, salt, SHARE_ITERATIONS).derive(code.encode('utf-8'))
        data = AESGCM(key).encrypt(nonce, json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'), None)
        sealed.append({'s': base64.b64encode(salt).decode(), 'n': base64.b64encode(nonce).decode(),
                       'd': base64.b64encode(data).decode()})
    sealed.sort(key=lambda v: v['d'])  # order must not reveal which entry is which
    return {'schema': 3, 'iterations': SHARE_ITERATIONS, 'sealed': sealed}, len(sealed), skipped


REQUIRED_CONFIG = ('owner', 'timeZone', 'schedule', 'windows', 'stepMinutes', 'leadHours', 'cosLeadDays', 'daysAhead')


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
    for name in ('workday', 'restday', 'convention'):
        try:
            for start, end in windows[name]:
                if minutes(start) >= end_minutes(end):
                    raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            problems.append('windows.' + name)
    step = cfg.get('stepMinutes')
    if not (isinstance(step, int) and 1 <= step <= 60 and 60 % step == 0):
        problems.append('stepMinutes')
    lead = cfg.get('leadHours') if isinstance(cfg.get('leadHours'), dict) else None
    if lead is None or any(not number(lead.get(k), 0, 2160) for k in ('workday', 'restday', 'convention')):
        problems.append('leadHours')
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
    global RULES, LEVEL_SETTINGS, PROJECTS
    rules_broken = False
    try:
        RULES = check_rules(cfg.get('rules'))
        LEVEL_SETTINGS = check_levels(cfg.get('levels'))
        PROJECTS = check_projects(cfg.get('projects'))
    except (ValueError, TypeError):
        rules_broken = True  # never guess: a broken rule list closes every day
    now = datetime.now(timezone.utc)
    tz = ZoneInfo(cfg['timeZone'])
    first = datetime.combine(now.astimezone(tz).date(), time.min, tzinfo=tz)
    last = first + timedelta(days=cfg['daysAhead']+1)
    first -= timedelta(days=first.weekday())  # read from Monday: earlier 已约 count toward this week
    events, private, safe, codes = [], {}, False, []
    if args.fixture:
        codes = [{'code': 'demo-close', 'level': 'close', 'reserve': '星星', 'name': '示例CN'},
                 {'code': 'demo-friend'}, {'code': 'demo-near', 'tags': ['住得近']},
                 {'code': 'demo-circle', 'level': 'con'}, {'code': 'demo-elder', 'level': 'elder'}]
        today = datetime.combine(now.astimezone(tz).date(), time.min, tzinfo=tz)
        show = today + timedelta(days=3)
        events = [
            {'start': show, 'end': show+timedelta(days=1), 'kind': 'convention', 'title': '星光漫展', 'allDay': True},
            {'start': show+timedelta(days=2), 'end': show+timedelta(days=3), 'kind': 'lock', 'title': '', 'allDay': True},
            {'start': show+timedelta(days=4), 'end': show+timedelta(days=5), 'kind': 'reserve', 'title': '星星', 'allDay': True},
            {'start': show+timedelta(days=6), 'end': show+timedelta(days=8), 'kind': 'convention', 'title': '回老家', 'allDay': True},
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
            kinds = {k: sum(1 for v in events if v['kind'] == k) for k in ('private', 'lock', 'convention', 'reserve', 'booked')}
            print(f"Read {len(events)} events in the next {cfg['daysAhead']} days: "
                  f"{kinds['private']} ordinary, {kinds['lock']} busy all day, {kinds['convention']} half-available (漫展/回老家), {kinds['reserve']} reserved, {kinds['booked']} booked.")
        except Exception as exc:
            code = str(exc) if isinstance(exc, SyncError) else 'private_input_or_unexpected_error'
            print(f'Calendar sync failed ({code}); publishing closed days.')
            events, private, safe, codes = [], {}, True, []
    try:
        result = build_data(cfg, holiday_data, private, events, now, safe=safe)
    except Exception as exc:
        # A bad value in the settings (time window, lead time...) must not stop the page.
        names = {'windows.workday': '上班日时段', 'windows.restday': '休息日时段',
                 'windows.convention': '全天半开放日的见面时段', 'schedule.mode': '排班方式',
                 'schedule.workdays': '上班的星期', 'schedule.anchor': '轮班起点',
                 'schedule.workDays': '轮班上几天', 'schedule.restDays': '轮班休几天',
                 'stepMinutes': '时间间隔', 'leadHours': '提前时间', 'daysAhead': '显示天数',
                 'bufferMinutes': '活动前后留出', 'weeklyMax': '每周最多约几次'}
        where = '、'.join(names.get(k, k) for k in config_problems(cfg)) or type(exc).__name__
        print(f'Calendar sync failed (config_values_invalid: {where}); publishing closed days.')
        cfg = json.loads((ROOT/'config.json').read_text(encoding='utf-8'))
        safe, private = True, {}
        result = build_data(cfg, holiday_data, private, [], now, safe=True)
    result['demo'] = args.fixture
    if safe:
        result['syncFailed'] = True
    if not args.fixture:
        try:
            codes = share_codes()
        except Exception:
            # A typo here only disables codes; the public page still updates.
            print('Share codes ignored (share_codes_invalid).')
            codes = []
    result, active, skipped = seal_for_codes(result, codes, now.astimezone(tz).date().isoformat(), now=now)
    result['demo'] = args.fixture
    print(f'Share codes: {active} active, {skipped} expired or too short.' +
          (' Nobody can open the page until a code is added.' if not active else ''))
    args.output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT/'site'/'index.html', args.output/'index.html')
    (args.output/'availability.json').write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    print('Published day-level states only.' + (' Fixture mode.' if args.fixture else ''))


if __name__ == '__main__':
    main()
