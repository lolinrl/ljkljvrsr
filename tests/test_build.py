import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build import view_for, seal_for_codes, build_data, event_kind, fetch_icloud, fetch_feishu, fetch_google, main, private_rules, SyncError

# Tests use fixed windows so editing config.json in settings.html does not break them.
CFG = json.loads((ROOT / 'config.json').read_text())
CFG.update(schedule={'mode': 'weekly', 'workdaysIso': [1, 2, 3, 4, 5], 'holidaysOverride': True},
           windows={'workday': [['18:30', '19:00']], 'restday': [['12:00', '16:00']],
                    'convention': [['12:00', '16:00']]},
           stepMinutes=30, leadHours={'workday': 4, 'restday': 24, 'convention': 24}, daysAhead=90)
HOLIDAYS = json.loads((ROOT / 'holidays-cn.json').read_text())
TZ = ZoneInfo('Asia/Shanghai')
NOW = datetime(2026, 9, 28, 8, tzinfo=TZ)


def item(start, end, kind='private', title='', all_day=False):
    return {'start': start, 'end': end, 'kind': kind, 'title': title, 'allDay': all_day}


def d(day, hour=0, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)



def open_sealed(public, code):
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    for box in public['sealed']:
        key = PBKDF2HMAC(SHA256(), 32, base64.b64decode(box['s']), public['iterations']).derive(code.encode())
        try:
            return json.loads(AESGCM(key).decrypt(base64.b64decode(box['n']), base64.b64decode(box['d']), None))
        except Exception:
            pass

class CalendarTests(unittest.TestCase):
    def test_three_kinds_of_days_from_titles(self):
        # Busy all day
        for title in ('【休息】', '朋友聚会', '【cos】', 'COS 返图', '约好的拍照', '【拍照】外景'):
            self.assertEqual(event_kind(title), ('lock', ''), title)
        self.assertEqual(event_kind('休息', all_day=True), ('lock', ''))
        # Half-available: a meal is fine
        self.assertEqual(event_kind('【漫展-ijoy】我的私密备忘'), ('convention', 'ijoy'))
        self.assertEqual(event_kind('ijoy漫展'), ('convention', '漫展'))
        for title in ('漫展-ijoy', '漫展-ijoy 和朋友逛', '【漫展-ijoy】', '漫展－ijoy', '漫展：ijoy', '漫展 - ijoy'):
            self.assertEqual(event_kind(title), ('convention', 'ijoy'), title)
        self.assertEqual(event_kind('漫展-ijoy', all_day=True), ('convention', 'ijoy'))
        self.assertEqual(event_kind('回老家', all_day=True), ('convention', '回老家'))
        # Ordinary
        self.assertEqual(event_kind('休息一下'), ('private', ''))
        self.assertEqual(event_kind('Costco 采购'), ('private', ''))
        self.assertEqual(event_kind('开会'), ('private', ''))
        self.assertEqual(event_kind('预留-星星'), ('reserve', '星星'))
        self.assertEqual(event_kind('预留-cos组'), ('reserve', 'cos组'))
    def test_private_day_has_priority_over_convention(self):
        events = [item(d(30), d(30)+timedelta(days=1), 'convention', 'ijoy', True),
                  item(d(30, 13), d(30, 14), 'lock')]
        output = build_data(CFG, HOLIDAYS, {}, events, NOW)['days']['2026-09-30']
        self.assertEqual(output['status'], 'locked')
        self.assertNotIn('ijoy', json.dumps(output))

    def test_restday_event_over_three_hours_locks_day(self):
        for hours, expected in [(3, 'open'), (3.5, 'locked')]:
            with self.subTest(hours=hours):
                events = [item(d(27, 10), d(27, 10)+timedelta(hours=hours))]
                output = build_data(CFG, HOLIDAYS, {}, events, d(26, 8))
                self.assertEqual(output['days']['2026-09-27']['status'], expected)

    def test_work_shift_does_not_block_dinner_but_conflict_does(self):
        shift = item(d(28, 9), d(28, 18))
        free = build_data(CFG, HOLIDAYS, {}, [shift], NOW)['days']['2026-09-28']
        busy = build_data(CFG, HOLIDAYS, {}, [shift, item(d(28, 18, 45), d(28, 19))], NOW)['days']['2026-09-28']
        self.assertEqual(free['status'], 'open')
        self.assertEqual(free['windows'], [['18:30', '19:00']])
        self.assertEqual(busy['status'], 'locked')

    def test_half_available_day_uses_normal_windows(self):
        events = [item(d(27), d(27)+timedelta(days=1), 'convention', '回老家', True)]
        result = build_data(CFG, HOLIDAYS, {}, events, d(26, 8))['days']['2026-09-27']
        self.assertEqual(result['status'], 'event')
        self.assertEqual(result['title'], '回老家')
        self.assertEqual(result['windows'], [['12:00', '16:00']])
    def test_timed_event_removes_busy_time_on_restday(self):
        # 2026-09-25 is a public holiday (restday); a 3-hour event is not long enough to close it.
        cfg = dict(CFG, windows=dict(CFG['windows'], restday=[['12:00', '18:00']]))
        events = [item(d(25, 13, 15), d(25, 16, 15))]
        result = build_data(cfg, HOLIDAYS, {}, events, d(23, 17))['days']['2026-09-25']
        self.assertEqual(result['status'], 'open')
        self.assertEqual(result['windows'], [['17:30', '18:00']])  # 1 h buffer on both sides
        no_buffer = build_data(dict(cfg, bufferMinutes=0), HOLIDAYS, {}, events, d(23, 17))['days']['2026-09-25']
        self.assertEqual(no_buffer['windows'], [['12:00', '13:00'], ['16:30', '18:00']])

    def test_all_day_ordinary_event_keeps_day_open(self):
        for day in (27, 29):  # restday and workday
            with self.subTest(day=day):
                events = [item(d(day), d(day)+timedelta(days=1), all_day=True)]
                result = build_data(CFG, HOLIDAYS, {}, events, d(26, 8))['days'][f'2026-09-{day}']
                self.assertEqual(result['status'], 'open')
    def test_all_day_rest_marker_locks_day(self):
        events = [item(d(29), d(29)+timedelta(days=1), 'lock', all_day=True)]
        result = build_data(CFG, HOLIDAYS, {}, events, d(26, 8))['days']['2026-09-29']
        self.assertEqual(result['status'], 'locked')

    def test_convention_time_is_bookable_despite_other_events(self):
        # Timed 漫展 10:15-17:00 on a rest day, plus a long ordinary event.
        events = [item(d(27, 10, 15), d(27, 17), 'convention', 'ijoy'),
                  item(d(27, 9), d(27, 15))]
        result = build_data(CFG, HOLIDAYS, {}, events, d(25, 8))['days']['2026-09-27']
        self.assertEqual(result['status'], 'event')
        self.assertEqual(result['title'], 'ijoy')
        self.assertEqual(result['windows'], [['10:30', '17:00']])

    def test_all_day_convention_uses_convention_window(self):
        events = [item(d(30), d(30)+timedelta(days=1), 'convention', 'ijoy', True),
                  item(d(30, 18, 30), d(30, 19))]
        result = build_data(CFG, HOLIDAYS, {}, events, NOW)['days']['2026-09-30']
        self.assertEqual(result['status'], 'event')
        self.assertEqual(result['windows'], [['12:00', '16:00']])
    def test_notice_and_restday_cutoff(self):
        result = build_data(CFG, HOLIDAYS, {}, [], d(27, 9))['days']['2026-09-27']
        self.assertEqual(result['status'], 'locked')
        future = build_data(CFG, HOLIDAYS, {}, [], d(26, 8))['days']['2026-09-27']
        self.assertEqual(future['windows'], [['12:00', '16:00']])
        self.assertTrue(all(int(a[:2]) >= 12 for day in future['windows'] for a in day[:1]))

    def test_cycle_with_manual_day_type_and_holidays_switch(self):
        cfg = json.loads(json.dumps(CFG))
        cfg['schedule'] = {'mode': 'cycle', 'anchor': '2026-09-28', 'workDays': 2,
                           'restDays': 2, 'holidaysOverride': False}
        result = build_data(cfg, HOLIDAYS, {}, [], NOW)
        self.assertEqual(result['days']['2026-09-28']['dayType'], 'workday')
        self.assertEqual(result['days']['2026-09-30']['dayType'], 'restday')
        self.assertEqual(result['days']['2026-10-01']['dayType'], 'restday')
        overridden = build_data(cfg, HOLIDAYS, {'dateTypes': {'2026-09-30': 'workday'}}, [], NOW)
        self.assertEqual(overridden['days']['2026-09-30']['dayType'], 'workday')

    def test_whole_page_is_sealed_per_code(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            from cryptography.hazmat.primitives.hashes import SHA256
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        except ImportError:
            self.skipTest('cryptography is not installed')
        import base64
        events = [item(d(30), d(30)+timedelta(days=1), 'convention', 'ijoy', True),
                  item(d(29), d(29)+timedelta(days=1), 'reserve', '星星', True)]
        result = build_data(CFG, HOLIDAYS, {}, events, NOW)
        self.assertEqual(result['days']['2026-09-29']['status'], 'reserved')
        codes = [{'code': 'xingxing-7342', 'see': ['ijoy'], 'reserve': '星星', 'name': '江江'},
                 {'code': 'coworker-1180'},                                       # plain friend
                 {'code': 'old-code-1', 'level': 'close', 'until': '2026-09-01'},     # expired
                 {'code': 'short'}]                                                # too short
        public, active, skipped = seal_for_codes(result, codes, '2026-09-28', now=NOW)
        self.assertEqual((active, skipped), (2, 2))
        text = json.dumps(public, ensure_ascii=False)
        for secret in ('ijoy', '江江', '星星', 'workday', 'windows', '2026-09'):
            self.assertNotIn(secret, text)

        def open_box(code):
            for box in public['sealed']:
                key = PBKDF2HMAC(SHA256(), 32, base64.b64decode(box['s']), public['iterations']).derive(code.encode())
                try:
                    return json.loads(AESGCM(key).decrypt(base64.b64decode(box['n']), base64.b64decode(box['d']), None))
                except Exception:
                    pass
        star = open_box('xingxing-7342')
        self.assertEqual(star['owner'], '江江')
        self.assertEqual(star['until'], '2026-09-30')          # ends after her last marked day
        self.assertEqual(star['days']['2026-09-30']['title'], 'ijoy')
        self.assertEqual(star['days']['2026-09-30']['base'], 'semi')
        self.assertEqual(star['days']['2026-09-29']['base'], 'ok')
        self.assertTrue(star['days']['2026-09-29']['forYou'])
        self.assertNotIn('p', star['days']['2026-09-29'])      # a friend has no workday project: 可以商量
        friend = open_box('coworker-1180')
        self.assertIsNone(friend['until'])
        self.assertEqual(friend['owner'], CFG['owner'])
        self.assertEqual(friend['days']['2026-09-29'], {'base': 'locked', 'dayType': 'workday'})
        self.assertEqual(friend['days']['2026-09-30'], {'base': 'plans', 'dayType': 'workday'})
        self.assertEqual([p['name'] for p in friend['projects']], ['吃饭', '逛街'])
        for secret in ('busyTimes', '_semi', '_reserve', '_full'):
            self.assertNotIn(secret, json.dumps(star) + json.dumps(friend))
        self.assertIsNone(open_box('old-code-1'))
        forever = [{'code': 'forever-111111', 'see': ['ijoy'], 'until': '2026-09-01', 'forever': True}]
        public, active, _ = seal_for_codes(result, forever, '2026-09-28')
        self.assertEqual(active, 1)
    def test_levels_decide_what_each_person_sees(self):
        events = [item(d(30), d(30)+timedelta(days=1), 'convention', 'ijoy', True),
                  item(d(29), d(29)+timedelta(days=1), 'convention', '回老家', True)]
        result = build_data(CFG, HOLIDAYS, {}, events, NOW)
        def seen(entry):
            days, _ = view_for(entry, result)
            return days['2026-09-29'], days['2026-09-30']
        home, con = seen({'level': 'friend'})
        self.assertEqual(home['status'], 'locked')
        self.assertEqual((con['status'], 'title' in con), ('event', False))
        home, con = seen({'level': 'friend', 'home': True})
        self.assertEqual(home['title'], '回老家')
        home, con = seen({'level': 'elder'})
        self.assertEqual((home, con), ({'status': 'mark', 'title': '回老家'}, {'status': 'busy'}))
        elder_days = view_for({'level': 'elder'}, result)[0]
        self.assertEqual(elder_days['2026-09-28'], {'status': 'work'})             # workday
        self.assertTrue(all('busy' not in v for v in view_for({'level': 'close'}, result)[0].values()))
        # A rest day that is closed only because it is too soon is still 空闲 for parents.
        soon = view_for({'level': 'elder'}, build_data(CFG, HOLIDAYS, {}, [], d(27, 9)))[0]
        self.assertEqual(soon['2026-09-27'], {'status': 'free'})
        cos = build_data(CFG, HOLIDAYS, {}, [item(d(27), d(28), 'lock', all_day=True)], d(25, 9))
        self.assertEqual(view_for({'level': 'elder'}, cos)[0]['2026-09-27'], {'status': 'busy'})
        self.assertTrue(all(set(v) <= {'status', 'festival', 'title'} for v in elder_days.values()))
        home, con = seen({'level': 'con'})
        self.assertEqual((home['status'], con['title']), ('locked', 'ijoy'))
        home, con = seen({'level': 'close'})
        self.assertEqual((home['title'], con['title']), ('回老家', 'ijoy'))
        self.assertTrue(all('group' not in v for v in view_for({'level': 'close'}, result)[0].values()))

    def test_booked_closes_day_and_weekly_limit(self):
        self.assertEqual(event_kind('已约-星星 吃饭'), ('booked', ''))
        self.assertEqual(event_kind('【已约】阿拍'), ('booked', ''))
        cfg = dict(CFG, weeklyMax=3)
        # Week of Mon 2026-10-12: three confirmed meet-ups.
        booked = [item(datetime(2026, 10, day, 18, 30, tzinfo=TZ), datetime(2026, 10, day, 19, tzinfo=TZ), 'booked')
                  for day in (12, 13, 14)]
        days = build_data(cfg, HOLIDAYS, {}, booked, NOW)['days']
        self.assertEqual(days['2026-10-12']['status'], 'locked')
        self.assertEqual(days['2026-10-15']['status'], 'full')
        self.assertNotIn('windows', days['2026-10-15'])
        self.assertEqual(days['2026-10-20']['status'], 'open')   # next week is fresh
        two = build_data(cfg, HOLIDAYS, {}, booked[:2], NOW)['days']
        self.assertEqual(two['2026-10-15']['status'], 'open')

    def test_share_code_typing_is_forgiven(self):
        from build import normalize_code
        for typed in ('星星-936265', '星星－936265', '星星—936265', ' 星星 - 936265 ', '星星-９３６２６５'):
            self.assertEqual(normalize_code(typed), '星星-936265', typed)
        self.assertEqual(normalize_code('Apai-1'), 'apai-1')

    def test_custom_rules_from_config(self):
        from build import classify, check_rules
        rules = check_rules({'busy': ['加班'], 'booked': ['OK'], 'reserve': ['留给'],
                             'semi': [{'word': '出差', 'seenBy': ['close'], 'others': 'busy'},
                                      {'word': '展会', 'seenBy': ['con', 'close'], 'others': 'event'}]})
        self.assertEqual(classify('加班到很晚', rules=rules)[0], 'lock')
        self.assertEqual(classify('聚会', rules=rules)[0], 'private')        # not in this person's list
        self.assertEqual(classify('OK-阿拍', rules=rules)[0], 'booked')
        self.assertEqual(classify('留给-星星', True, rules)[:2], ('reserve', '星星'))
        self.assertEqual(classify('展会-CP30', rules=rules), ('convention', 'CP30', '展会'))
        self.assertEqual(classify('去上海出差', True, rules), ('convention', '出差', '出差'))
        with self.assertRaises(ValueError):
            check_rules({'semi': [{'word': '', 'others': 'maybe'}]})
        events = [item(d(29), d(29)+timedelta(days=1), 'convention', '出差', True),
                  item(d(30), d(30)+timedelta(days=1), 'convention', 'CP30', True)]
        events[0]['group'], events[1]['group'] = '出差', '展会'
        result = build_data(CFG, HOLIDAYS, {}, events, NOW)
        friend = view_for({'level': 'friend', 'also': ['展会']}, result, rules)[0]
        self.assertEqual(friend['2026-09-29']['status'], 'locked')
        self.assertEqual(friend['2026-09-30']['title'], 'CP30')

    def test_projects_light_up_different_days_per_person(self):
        from build import project_days, check_levels
        # Sat 10-03 is a holiday rest day; Mon 10-12 a workday with dinner 19:00-20:00 booked privately.
        events = [item(datetime(2026, 10, 12, 19, tzinfo=TZ), datetime(2026, 10, 12, 20, tzinfo=TZ)),
                  item(d(30), d(30)+timedelta(days=1), 'convention', 'ijoy', True)]
        result = build_data(CFG, HOLIDAYS, {}, events, NOW)
        def days(entry):
            return project_days(entry, result, NOW)
        friend, names = days({'level': 'friend'})
        self.assertEqual(names, ['吃饭', '逛街'])
        self.assertEqual(friend['2026-10-03']['p']['吃饭'], [['14:00', '18:00', 24]])
        self.assertEqual(friend['2026-10-12'], {'dayType': 'workday', 'base': 'ok'})   # 可以商量
        self.assertEqual(friend['2026-09-30']['base'], 'plans')
        near, _ = days({'level': 'friend', 'tags': ['住得近']})
        self.assertEqual(near['2026-10-13']['p']['吃饭'], [['18:30', '20:00', 2]])
        self.assertEqual(near['2026-10-12']['p']['吃饭'], [['18:30', '19:00', 2]])   # dinner 19:00-20:00 is taken
        circle, names = days({'level': 'con'})
        self.assertEqual(names, ['拍照', 'cos'])
        self.assertEqual(circle['2026-10-03']['p']['拍照'], [['14:00', '18:00', 24]])
        self.assertEqual(circle['2026-09-30']['title'], 'ijoy')
        self.assertEqual(circle['2026-10-01'].get('p', {}).get('cos'), None)        # sooner than 30 days
        self.assertEqual(circle['2026-10-30']['p']['cos'], 'ask')
        close, names = days({'level': 'close'})
        self.assertEqual(names, ['吃饭', '逛街', '拍照', 'cos', '旅行'])
        self.assertEqual(close['2026-10-03']['p']['吃饭'], [['10:00', '21:00', 2]])
        self.assertEqual(close['2026-10-13']['p']['逛街'], [['18:30', '21:00', 2]])
        from build import LEVEL_SETTINGS
        import build
        old = build.LEVEL_SETTINGS
        try:
            build.LEVEL_SETTINGS = check_levels({'con': {'ordinary': False}})
            strict, _ = project_days({'level': 'con'}, result, NOW)
            self.assertEqual(strict['2026-10-13']['base'], 'off')
        finally:
            build.LEVEL_SETTINGS = old

    def test_my_own_day_off_and_extra_workday(self):
        from build import classify
        self.assertEqual(classify('调休', True)[0], 'as_rest')
        self.assertEqual(classify('补班', True)[0], 'as_work')
        events = [item(datetime(2026, 10, 13, tzinfo=TZ), datetime(2026, 10, 14, tzinfo=TZ), 'as_rest', all_day=True)]
        self.assertEqual(build_data(CFG, HOLIDAYS, {}, events, NOW)['days']['2026-10-13']['dayType'], 'restday')

    def test_private_secret_empty_is_valid(self):
        with patch.dict(os.environ, {'MYSLOT_PRIVATE_JSON': ''}):
            self.assertEqual(private_rules(), {})

    def test_selected_calendar_search_expands_recurring_events(self):
        class Value:
            def __init__(self, value): self.dt = value

        class Component(dict):
            pass

        component = Component(DTSTART=Value(d(28, 9)), DTEND=Value(d(28, 18)), SUMMARY='上班')
        item_object = SimpleNamespace(icalendar_instance=SimpleNamespace(walk=lambda kind: [component]))
        searches = []
        calendar = SimpleNamespace(name='工作事业', search=lambda **kw: searches.append(kw) or [item_object])
        fake = SimpleNamespace(DAVClient=lambda **kw: SimpleNamespace(
            principal=lambda: SimpleNamespace(calendars=lambda: [calendar])))
        env = {'ICLOUD_USER': 'test@example.com', 'ICLOUD_APP_PASSWORD': 'fake', 'MYSLOT_BUSY_CALENDARS': '工作事业'}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {'caldav': fake}):
            records = fetch_icloud(d(28), d(30), TZ)
        self.assertTrue(searches[0]['expand'])
        self.assertEqual(len(records), 1)

    def test_feishu_uses_independent_caldav_credentials(self):
        with patch.dict(os.environ, {'FEISHU_CALDAV_URL': 'https://caldav.feishu.cn',
                                     'FEISHU_CALDAV_USER': 'feishu-user',
                                     'FEISHU_CALDAV_PASSWORD': 'feishu-secret',
                                     'MYSLOT_FEISHU_CALENDARS': '工作'}, clear=True), \
             patch('build.fetch_caldav', return_value=[item(d(28), d(29))]) as fetch:
            fetch_feishu(d(28), d(30), TZ)
        self.assertEqual(fetch.call_args.kwargs['source'], 'feishu')
        self.assertEqual(fetch.call_args.kwargs['names'], '工作')

    def test_google_reads_selected_nonprimary_calendar_and_markers(self):
        try:
            import google.oauth2.credentials
            import googleapiclient.discovery
        except ImportError:
            self.skipTest('optional Google packages are not installed')
        api = MagicMock()
        api.calendarList.return_value.list.return_value.execute.return_value = {
            'items': [{'id': 'other@example.com', 'summary': 'Meet-up'}]}
        api.events.return_value.list.return_value.execute.return_value = {
            'items': [{'status': 'confirmed', 'summary': '【漫展-ijoy】私人备注',
                       'start': {'date': '2026-09-28'}, 'end': {'date': '2026-09-29'}}]}
        creds = MagicMock()
        env = {'GOOGLE_CLIENT_ID': 'example', 'GOOGLE_CLIENT_SECRET': 'example',
               'GOOGLE_REFRESH_TOKEN': 'example', 'MYSLOT_GOOGLE_CALENDARS': 'Meet-up'}
        with patch.dict(os.environ, env, clear=True), \
             patch('google.oauth2.credentials.Credentials.from_authorized_user_info', return_value=creds), \
             patch('googleapiclient.discovery.build', return_value=api):
            records = fetch_google(d(28), d(30), TZ)
        self.assertEqual(records[0]['kind'], 'convention')
        self.assertEqual(records[0]['title'], 'ijoy')
        self.assertEqual(api.events.return_value.list.call_args.kwargs['calendarId'], 'other@example.com')

    def test_missing_enabled_feishu_source_closes_every_day(self):
        from unittest.mock import patch as mock_patch
        with tempfile.TemporaryDirectory() as folder, \
             mock_patch.dict(os.environ, {'MYSLOT_SHARE_CODES': '[{"code": "friend-0001"}]'}, clear=True), \
             mock_patch('build.fetch_icloud', return_value=[item(d(28), d(29))]), \
             mock_patch('build.fetch_feishu', side_effect=SyncError('feishu_credentials_missing')), \
             mock_patch('build.ROOT', Path(folder)), \
             mock_patch.object(sys, 'argv', ['build.py', '--output', str(Path(folder)/'dist')]), \
             redirect_stdout(io.StringIO()) as log:
            # Root points to a temporary project with its own source files.
            (Path(folder)/'site').mkdir()
            (Path(folder)/'site'/'index.html').write_text('<!doctype html>')
            config = dict(CFG, calendarSources=['icloud', 'feishu'])
            (Path(folder)/'config.json').write_text(json.dumps(config))
            (Path(folder)/'holidays-cn.json').write_text(json.dumps(HOLIDAYS))
            main()
            result = open_sealed(json.loads((Path(folder)/'dist'/'availability.json').read_text()), 'friend-0001')
        self.assertTrue(result['syncFailed'])
        self.assertTrue(all(v['base']=='locked' and 'p' not in v for v in result['days'].values()))
        self.assertIn('feishu_credentials_missing', log.getvalue())

    def test_caldav_duration_without_dtend(self):
        class Value:
            def __init__(self, value): self.dt = value
        component = {'DTSTART': Value(d(28, 18)), 'DURATION': Value(timedelta(hours=3)), 'SUMMARY': 'x'}
        obj = SimpleNamespace(icalendar_instance=SimpleNamespace(walk=lambda kind: [component]))
        calendar = SimpleNamespace(name='A', search=lambda **kw: [obj])
        fake = SimpleNamespace(DAVClient=lambda **kw: SimpleNamespace(
            principal=lambda: SimpleNamespace(calendars=lambda: [calendar])))
        env = {'ICLOUD_USER': 'u', 'ICLOUD_APP_PASSWORD': 'p', 'MYSLOT_BUSY_CALENDARS': 'A'}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {'caldav': fake}):
            records = fetch_icloud(d(28), d(30), TZ)
        self.assertEqual(records[0]['end'], d(28, 21))

    def test_zero_events_and_unknown_calendar_fail_closed(self):
        fake = SimpleNamespace(DAVClient=lambda **kw: SimpleNamespace(principal=lambda: SimpleNamespace(
            calendars=lambda: [SimpleNamespace(name='工作事业', search=lambda **kw: [])])))
        env = {'ICLOUD_USER': 'test@example.com', 'ICLOUD_APP_PASSWORD': 'fake', 'MYSLOT_BUSY_CALENDARS': '工作事业',
               'MYSLOT_SHARE_CODES': '[{"code": "friend-0001"}]'}
        with patch.dict(os.environ, env, clear=True), patch.dict(sys.modules, {'caldav': fake}):
            with self.assertRaisesRegex(SyncError, 'icloud_no_events_returned'):
                fetch_icloud(d(28), d(30), TZ)
            with tempfile.TemporaryDirectory() as folder, patch.object(sys, 'argv', ['build.py', '--output', folder]), redirect_stdout(io.StringIO()) as log:
                main()
                result = open_sealed(json.loads((Path(folder)/'availability.json').read_text()), 'friend-0001')
            self.assertTrue(all(v['base']=='locked' and 'p' not in v for v in result['days'].values()))
            self.assertNotIn('工作事业', log.getvalue())


if __name__ == '__main__':
    unittest.main()
