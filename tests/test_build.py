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
from build import day_facts, person_days, summary_days, payload_for, seal_for_codes, event_kind, fetch_icloud, fetch_feishu, fetch_google, main, private_rules, SyncError

# Tests use their own fixed settings, so whatever you save from settings.html into
# config.json (rules, levels, projects, time windows...) can never break them.
CFG = {
    'owner': 'Jan', 'timeZone': 'Asia/Shanghai', 'calendarMode': 'icloud', 'calendarSources': ['icloud'],
    'schedule': {'mode': 'weekly', 'workdaysIso': [1, 2, 3, 4, 5], 'holidaysOverride': True},
    'windows': {'workday': [['18:30', '19:00']], 'restday': [['12:00', '16:00']],
                'convention': [['12:00', '16:00']]},
    'stepMinutes': 30, 'leadHours': {'workday': 4, 'restday': 24, 'convention': 24},
    'cosLeadDays': 30, 'daysAhead': 90, 'bufferMinutes': 60, 'weeklyMax': 3,
}
HOLIDAYS = json.loads((ROOT / 'holidays-cn.json').read_text())
TZ = ZoneInfo('Asia/Shanghai')
NOW = datetime(2026, 9, 28, 8, tzinfo=TZ)


def facts(events, private=None, now=None, **settings):
    return day_facts(dict(CFG, **settings), HOLIDAYS, private or {}, events, now or NOW)


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
    def setUp(self):
        # main() loads rules / levels / projects from a config; start every test from the defaults.
        import build
        build.RULES, build.ACTIVITIES = build.DEFAULT_RULES, build.DEFAULT_ACTIVITIES
        build.SUMMARIES, build.TEXTS = build.DEFAULT_SUMMARIES, build.DEFAULT_TEXTS
        self._root = patch('build.ROOT', self._project_copy())
        self._root.start()

    def tearDown(self):
        self._root.stop()
        self._tmp.cleanup()

    def _project_copy(self):
        """A temporary project folder whose config.json is the fixed test CFG."""
        self._tmp = tempfile.TemporaryDirectory()
        folder = Path(self._tmp.name)
        (folder/'site').mkdir()
        (folder/'site'/'index.html').write_text('<!doctype html>')
        (folder/'config.json').write_text(json.dumps(CFG))
        (folder/'holidays-cn.json').write_text(json.dumps(HOLIDAYS))
        return folder

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
    # The people from the requirements walk-through.
    A = {'code': 'a-0000000001', 'label': 'A', 'city': '老家', 'see': ['漫展', '回老家'], 'activities': ['吃饭', '问问我']}
    C = {'code': 'c-0000000001', 'label': 'C', 'see': ['漫展'], 'activities': ['吃饭', '吃饭（工作日）', '逛街']}
    Dd = {'code': 'd-0000000001', 'label': 'D', 'see': ['漫展'], 'activities': ['拍照', 'cos（已有衣服）', 'cos（新衣服）']}
    CLIENT = {'code': 'k-0000000001', 'label': '客户', 'activities': ['拍照']}
    PARENTS = {'code': 'p-0000000001', 'label': '爸妈', 'summary': True}
    SAT = datetime(2026, 10, 17, tzinfo=TZ)      # a plain rest day
    MON = datetime(2026, 10, 12, tzinfo=TZ)      # a plain workday

    def view(self, who, events=(), now=None, **settings):
        return person_days(who, facts(list(events), now=now, **settings), now or NOW)[0]

    def test_plain_rest_day(self):
        key = '2026-10-17'
        self.assertEqual(self.view(self.C)[key]['offers'], {'吃饭': [['14:00', '18:00', 24]], '逛街': [['14:00', '18:00', 24]]})
        self.assertEqual(self.view(self.Dd)[key]['offers']['拍照'], [['14:00', '18:00', 24]])
        self.assertEqual(self.view(self.Dd)[key]['offers']['cos（已有衣服）'], 'ask')
        self.assertNotIn('cos（新衣服）', self.view(self.Dd)['2026-10-03']['offers'])   # 5 days away: needs 14
        self.assertIn('cos（新衣服）', self.view(self.Dd)[key]['offers'])               # 19 days away
        self.assertEqual(self.view(self.A)[key]['offers'], {'问问我': 'ask'})       # A lives elsewhere: 💬 only
        self.assertEqual(self.view(self.CLIENT)[key]['offers'], {'拍照': [['14:00', '18:00', 24]]})

    def test_plain_workday(self):
        key = '2026-10-12'
        self.assertEqual(self.view(self.C)[key]['offers'], {'吃饭（工作日）': [['18:30', '20:00', 2]]})
        self.assertNotIn('offers', self.view(self.Dd)[key])                        # —
        self.assertNotIn('offers', self.view(self.CLIENT)[key])
        self.assertEqual(self.view(self.A)[key]['offers'], {'问问我': 'ask'})

    def test_convention_day(self):
        con = [item(self.SAT, self.SAT+timedelta(days=1), 'convention', 'ijoy', True),
               item(self.SAT+timedelta(hours=13), self.SAT+timedelta(hours=16), 'lock')]    # a shoot at the convention
        for who in (self.A, self.C, self.Dd):
            self.assertEqual(self.view(who, con)['2026-10-17']['tag'], 'ijoy')
        self.assertEqual(self.view(self.Dd, con)['2026-10-17']['offers']['拍照'], [['12:00', '13:00', 24]])
        self.assertEqual(self.view(self.CLIENT, con)['2026-10-17'], {'dayType': 'restday'})   # —, no hint
        client_fan = dict(self.CLIENT, see=['漫展'])                                            # 同城二次元客户
        self.assertEqual(self.view(client_fan, con)['2026-10-17']['tag'], 'ijoy')

    def test_home_visit(self):
        home = [item(self.SAT, self.SAT+timedelta(days=2), 'convention', '回老家', True)]
        a = self.view(self.A, home)['2026-10-17']
        self.assertEqual(a['tag'], '回老家')
        self.assertEqual(a['offers']['吃饭'], [['14:00', '18:00', 24]])           # same city that day
        c = self.view(self.C, home)['2026-10-17']
        self.assertNotIn('tag', c)
        self.assertNotIn('offers', c)                                           # I am not in C's city
        self.assertEqual(summary_days(self.PARENTS, facts(home))['2026-10-17'], {'kind': 'mark', 'label': '在家'})

    def test_parents_see_only_summaries(self):
        f = facts([item(self.SAT, self.SAT+timedelta(days=1), 'convention', 'ijoy', True),
                   item(self.SAT+timedelta(days=1, hours=13), self.SAT+timedelta(days=1, hours=16), 'lock')])
        f['days']['2026-10-18'].setdefault('marks', ['拍照'])
        days = summary_days(self.PARENTS, f)
        self.assertEqual(days['2026-10-12'], {'kind': 'work', 'label': '上班'})
        self.assertEqual(days['2026-10-17'], {'kind': 'mark', 'label': '出去玩'})
        self.assertEqual(days['2026-10-24'], {'kind': 'free', 'label': '空闲'})
        payload = payload_for(self.PARENTS, f, NOW)[0]
        self.assertTrue(payload['simple'])
        self.assertNotIn('activities', payload)

    def test_big_plans_booked_and_rest_stop_new_invitations_but_keep_shared_items(self):
        con = item(self.SAT, self.SAT+timedelta(days=1), 'convention', 'ijoy', True)
        for blocker in (item(self.SAT, self.SAT+timedelta(days=1), 'lock', all_day=True),
                        item(self.SAT+timedelta(hours=19), self.SAT+timedelta(hours=20), 'booked'),
                        item(self.SAT, self.SAT+timedelta(days=1), 'energy', all_day=True)):
            with self.subTest(kind=blocker['kind']):
                day = self.view(self.Dd, [con, blocker])['2026-10-17']
                self.assertEqual(day['tag'], 'ijoy')                               # still shared
                self.assertNotIn('offers', day)                                    # nothing new
        busy = self.view(self.C, [item(self.SAT+timedelta(hours=10), self.SAT+timedelta(hours=12), 'lock')])['2026-10-17']
        self.assertNotIn('offers', busy)                                            # a timed big plan closes a plain day

    def test_ordinary_events_take_their_time_plus_buffer(self):
        e = [item(self.SAT+timedelta(hours=15), self.SAT+timedelta(hours=16))]
        self.assertEqual(self.view(self.C, e)['2026-10-17']['offers']['吃饭'], [['17:00', '18:00', 24]])
        self.assertEqual(self.view(self.C, e, bufferMinutes=0)['2026-10-17']['offers']['吃饭'],
                         [['14:00', '15:00', 24], ['16:00', '18:00', 24]])

    def test_week_limit_and_lead_time(self):
        booked = [item(datetime(2026, 10, d, 19, tzinfo=TZ), datetime(2026, 10, d, 20, tzinfo=TZ), 'booked') for d in (12, 13, 14)]
        self.assertNotIn('offers', self.view(self.C, booked)['2026-10-17'])
        self.assertIn('offers', self.view(self.C, booked)['2026-10-24'])
        self.assertIn('offers', self.view(self.C, booked, weeklyMax=0)['2026-10-17'])
        now = datetime(2026, 10, 12, 17, tzinfo=TZ)
        self.assertEqual(self.view(self.C, now=now)['2026-10-12']['offers']['吃饭（工作日）'], [['19:00', '20:00', 2]])

    def test_reserved_day(self):
        r = [item(self.SAT, self.SAT+timedelta(days=1), 'reserve', 'C', True)]
        mine = self.view(dict(self.C, reserve='C'), r)['2026-10-17']
        self.assertTrue(mine['forYou'])
        self.assertIn('offers', mine)
        self.assertNotIn('offers', self.view(self.Dd, r)['2026-10-17'])

    def test_schedule_and_my_own_day_off(self):
        cfg = dict(CFG, schedule={'mode': 'cycle', 'anchor': '2026-09-28', 'workDays': 2, 'restDays': 2,
                                  'holidaysOverride': False})
        f = day_facts(cfg, HOLIDAYS, {'dateTypes': {'2026-09-30': 'workday'}}, [], NOW)['days']
        self.assertEqual([f[k]['dayType'] for k in ('2026-09-28', '2026-09-30', '2026-10-01')], ['workday', 'workday', 'restday'])
        self.assertEqual(facts([])['days']['2026-10-10']['dayType'], 'workday')              # 补班
        off = [item(self.MON, self.MON+timedelta(days=1), 'as_rest', all_day=True)]
        self.assertEqual(facts(off)['days']['2026-10-12']['dayType'], 'restday')
        self.assertTrue(facts([], private={'lockedDates': ['2026-10-17']})['days']['2026-10-17']['closed'])

    def test_old_level_codes_still_work(self):
        from build import person
        self.assertEqual(person({'code': 'x', 'level': 'friend'})['activities'], ['吃饭', '逛街'])
        self.assertIn('漫展', person({'code': 'x', 'level': 'con'})['see'])
        self.assertTrue(person({'code': 'x', 'level': 'elder'})['summary'])
        self.assertIn('吃饭（工作日）', person({'code': 'x', 'tags': ['住得近']})['activities'])
        self.assertIn('回老家', person({'code': 'x', 'home': True})['see'])

    def test_owner_view_holds_everyone_and_needs_a_long_code(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        except ImportError:
            self.skipTest('cryptography is not installed')
        f = facts([])
        codes = [self.A, self.C, self.PARENTS]
        public, active, _ = seal_for_codes(f, codes, '2026-09-28', now=NOW, owner_code='owner-code-1234567890')
        self.assertEqual((active, len(public['sealed'])), (3, 4))
        owner = open_sealed(public, 'owner-code-1234567890')
        self.assertTrue(owner['ownerView'])
        self.assertEqual([p['label'] for p in owner['people']], ['A', 'C', '爸妈'])
        self.assertNotIn('a-0000000001', json.dumps(owner))                    # no codes inside
        short, _, _ = seal_for_codes(f, codes, '2026-09-28', now=NOW, owner_code='short-owner')
        self.assertEqual(len(short['sealed']), 3)

    def test_whole_page_is_sealed_per_code(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        except ImportError:
            self.skipTest('cryptography is not installed')
        events = [item(d(30), d(30)+timedelta(days=1), 'convention', 'ijoy', True)]
        codes = [dict(self.Dd, see=['ijoy'], name='江江'),                  # only this one convention
                 {'code': 'coworker-1180', 'activities': ['吃饭']},
                 {'code': 'old-code-1', 'until': '2026-09-01'},
                 {'code': 'short'}]
        public, active, skipped = seal_for_codes(facts(events), codes, '2026-09-28', now=NOW)
        self.assertEqual((active, skipped), (2, 2))
        self.assertEqual(set(public), {'schema', 'iterations', 'sealed'})
        self.assertTrue(all(set(box) == {'s', 'n', 'd'} for box in public['sealed']))
        for secret in ('江江', '2026-09'):
            self.assertNotIn(secret, json.dumps(public, ensure_ascii=False))
        star = open_sealed(public, 'd-0000000001')
        self.assertEqual((star['owner'], star['until']), ('江江', '2026-09-30'))   # ends after that convention
        self.assertEqual(star['days']['2026-09-30']['tag'], 'ijoy')
        friend = open_sealed(public, 'coworker-1180')
        self.assertNotIn('tag', friend['days']['2026-09-30'])
        for private_field in ('"busy":', '"items":', '"reserve":', '"closed":', '"marks":'):
            self.assertNotIn(private_field, json.dumps(star) + json.dumps(friend))

    def test_share_code_typing_is_forgiven(self):
        from build import normalize_code
        for typed in ('星星-936265', '星星－936265', '星星—936265', ' 星星 - 936265 ', '星星-９３６２６５'):
            self.assertEqual(normalize_code(typed), '星星-936265', typed)
        self.assertEqual(normalize_code('Apai-1'), 'apai-1')

    def test_custom_markers(self):
        from build import classify, check_rules
        rules = check_rules({'busy': ['加班'], 'booked': ['OK'], 'rest': ['摆烂'], 'reserve': ['留给'],
                             'semi': [{'word': '出差', 'location': '上海'}, {'word': '展会'}]})
        self.assertEqual(classify('加班到很晚', rules=rules), ('lock', '', '加班'))
        self.assertEqual(classify('聚会', rules=rules)[0], 'private')
        self.assertEqual(classify('OK-阿拍', rules=rules)[0], 'booked')
        self.assertEqual(classify('今天摆烂', True, rules)[0], 'energy')
        self.assertEqual(classify('留给-星星', True, rules)[:2], ('reserve', '星星'))
        self.assertEqual(classify('展会-CP30', rules=rules), ('convention', 'CP30', '展会'))
        with self.assertRaises(ValueError):
            check_rules({'semi': [{'word': ''}]})

    def test_settings_checks(self):
        from build import parse_range, config_problems, check_activities
        self.assertEqual(parse_range('20:00-00:00'), (1200, 1440))
        self.assertEqual(config_problems(dict(CFG, windows={'convention': [['18:00', '00:00']]})), [])
        self.assertIn('windows.convention', config_problems(dict(CFG, windows={'convention': [['18:00', '12:00']]})))
        with self.assertRaises(ValueError):
            check_activities([{'name': '吃饭', 'rest': ['14点-18点']}])
        with self.assertRaises(ValueError):
            check_activities([{'name': '吃饭'}])                              # no days at all
        for value in (None, '', 'abc', -1, 0, '3'):
            with self.subTest(value=value):
                facts([], weeklyMax=value, bufferMinutes=value)

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
        self.assertTrue(all('offers' not in v and 'tag' not in v for v in result['days'].values()))
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
            self.assertTrue(all('offers' not in v and 'tag' not in v for v in result['days'].values()))
            self.assertNotIn('工作事业', log.getvalue())


if __name__ == '__main__':
    unittest.main()
