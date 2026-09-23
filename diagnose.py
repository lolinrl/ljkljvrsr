#!/usr/bin/env python3
"""Local-only check: show which iCloud calendar holds each event on one day
and whether MySlot would use it. Run on your own computer; nothing is uploaded.

    pip3 install "caldav>=2,<3"
    python3 diagnose.py
"""
import getpass
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import caldav

import build

ROOT = Path(__file__).resolve().parent
cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
holidays = json.loads((ROOT / 'holidays-cn.json').read_text(encoding='utf-8'))
tz = ZoneInfo(cfg['timeZone'])

user = input('Apple ID: ').strip()
password = getpass.getpass('App 专用密码（输入时不显示）: ').strip()
monitored = {v.strip() for v in input('MYSLOT_BUSY_CALENDARS 里填的内容（原样粘贴）: ').split(',') if v.strip()}
day = date.fromisoformat(input('要检查的日期，例如 2026-09-30: ').strip())

start = datetime.combine(day, time.min, tzinfo=tz)
end = start + timedelta(days=1)
calendars = caldav.DAVClient(url='https://caldav.icloud.com', username=user,
                             password=password).principal().calendars()

print('\n账户里的日历：')
for cal in calendars:
    print(f"  {'[监控中]' if cal.name in monitored else '[未监控]'} {cal.name!r}")
missing = monitored - {c.name for c in calendars}
if missing:
    print(f'  ⚠ 这些名称在账户里找不到：{sorted(missing)}')

print(f'\n{day} 的事件：')
used = []
for cal in calendars:
    try:
        items = cal.search(start=start, end=end, event=True, expand=True)
    except Exception as exc:
        print(f'  {cal.name!r}: 读取失败 {type(exc).__name__}')
        continue
    for item in items:
        for c in item.icalendar_instance.walk('VEVENT'):
            if not c.get('DTSTART'):
                continue
            s = c.get('DTSTART').dt
            all_day = isinstance(s, date) and not isinstance(s, datetime)
            b = build.local_time(s, tz)
            if c.get('DTEND'):
                f = build.local_time(c.get('DTEND').dt, tz)
            elif c.get('DURATION'):
                f = b + c.get('DURATION').dt
            else:
                f = b + (timedelta(days=1) if all_day else timedelta(hours=1))
            title = str(c.get('SUMMARY', ''))
            kind, label = build.event_kind(title)
            transp = str(c.get('TRANSP', '')).upper()
            status = str(c.get('STATUS', '')).upper()
            if cal.name not in monitored:
                why = '✗ 不在监控日历里，会被忽略'
            elif status == 'CANCELLED':
                why = '✗ 已取消，会被忽略'
            elif kind == 'private' and transp == 'TRANSPARENT':
                why = '✗ 「显示为：空闲」，会被忽略（全天事件默认就是空闲）'
            else:
                why = f'✓ 会使用（{kind}）'
                used.append({'start': b, 'end': f, 'kind': kind, 'title': label, 'allDay': all_day})
            when = '全天' if all_day else f"{b:%H:%M}–{f:%H:%M}"
            print(f'  [{cal.name}] {when} {title!r}  → {why}')

now = datetime.now(tz)
result = build.build_data(cfg, holidays, {}, used, now)['days'].get(day.isoformat())
print(f'\n按当前 config.json，{day} 的结果：{result}')
if result:
    print('  规则：上班日只看 18:30–19:00 前后是否冲突；休息日普通事件要超过 3 小时才关闭；'
          '法定假日按休息日算；距离现在太近（提前时间不足）也会关闭。')
