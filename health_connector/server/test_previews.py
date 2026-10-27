import os
import tempfile
import unittest
from unittest import mock

import hub
import previews
from hub import HealthError


class FakeHub:
    """Just enough of the Health Hub: one day of drinks, and a food log."""

    def __init__(self):
        self.drinks = []
        self.food = []
        self.writes = 0

    def status(self, drinks):
        std = round(sum(d['std'] for d in drinks), 1)
        return {'day': {'date': '2026-12-05', 'weekday': 'Sat', 'std': std, 'drinks': drinks,
                        'broken': ['over_limit'] if std > 2 else []},
                'week': {'std': std, 'drink_days': 1, 'dry_days': 0, 'unknown_days': 0, 'broken': []},
                'reset': {'active': False}}

    def get(self, path, **query):
        assert path == '/alcohol' and query == {'date': '2026-12-05'}
        return self.status(list(self.drinks))

    def post(self, path, body):
        if path == '/alcohol/drink':
            drink = {'id': body.get('id', 'dryrun00'), 'time': body.get('time', '19:00'),
                     'kind': body['kind'], 'std': body.get('std', 1.5)}
            if body.get('dry_run'):
                return {**self.status(self.drinks + [drink]), 'drink': drink}
            if any(d['id'] == drink['id'] for d in self.drinks):
                return {**self.status(self.drinks), 'already_applied': True}
            self.writes += 1
            self.drinks.append(drink)
            return self.status(self.drinks)
        if path == '/food/parse':
            return {'items': [{'name': 'Salmon (200g)', 'cal': 460, 'protein': 50, 'cups': 0, 'group': 'none'},
                              {'name': 'GF bread', 'cal': 150, 'wahls_ok': False, 'why': 'processed'}]}
        if path == '/food/items':
            if any(i.get('entry') == body['entry_id'] for i in self.food):
                return {'already_applied': True, 'totals': {}}
            self.writes += 1
            self.food += [{**i, 'entry': body['entry_id']} for i in body['items']]
            return {'totals': {'cal': 610}, 'wahls': {'cups': {}, 'cups_total': 0, 'off_plan': 1}}
        raise AssertionError(path)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['HEALTH_RUNTIME'] = self.tmp.name
        self.hub = FakeHub()
        self.patches = [mock.patch.object(hub, 'get', self.hub.get), mock.patch.object(hub, 'post', self.hub.post)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()
        os.environ.pop('HEALTH_RUNTIME', None)

    def test_drink_preview_saves_nothing_then_applies_once(self):
        p = previews.preview_drink('19:00')
        self.assertEqual((p['status'], p['would_log']['date'], p['day_after']['std']), ('preview', '2026-12-05', 1.5))
        self.assertEqual(self.hub.writes, 0)
        a = previews.apply(p['preview_id'], 'req-000001')
        self.assertEqual(a['status'], 'applied')
        again = previews.apply(p['preview_id'], 'req-000001')       # retry after an uncertain response
        self.assertEqual(again['status'], 'already_applied')
        self.assertEqual(self.hub.writes, 1)

    def test_stale_drink_preview_is_refused(self):
        p = previews.preview_drink('19:00')
        self.hub.drinks.append({'id': 'other001', 'time': '18:00', 'kind': 'wine', 'std': 1.5})
        with self.assertRaises(HealthError):
            previews.apply(p['preview_id'], 'req-000002')
        self.assertEqual(self.hub.writes, 0)

    def test_request_id_cannot_be_reused_for_another_preview(self):
        p1 = previews.preview_drink('19:00')
        previews.apply(p1['preview_id'], 'req-000003')
        p2 = previews.preview_drink('19:30')
        with self.assertRaises(HealthError):
            previews.apply(p2['preview_id'], 'req-000003')

    def test_meal_preview_shows_off_plan_then_applies_once(self):
        p = previews.preview_meal('salmon, GF bread', 'dinner', '2026-12-05')
        self.assertEqual(p['off_plan'], ['GF bread (processed)'])
        self.assertEqual(p['totals']['cal'], 610)
        self.assertEqual(self.hub.writes, 0)
        self.assertEqual(previews.apply(p['preview_id'], 'meal-000001')['status'], 'applied')
        self.assertEqual(previews.apply(p['preview_id'], 'meal-000001')['status'], 'already_applied')
        self.assertEqual(self.hub.writes, 1)

    def test_bad_ids_and_meal(self):
        with self.assertRaises(HealthError):
            previews.apply('nope', 'req-000001')
        with self.assertRaises(HealthError):
            previews.apply('0' * 32, 'req-000001')                # unknown preview
        with self.assertRaises(HealthError):
            previews.preview_meal('salmon', 'brunch')


class HubClientTests(unittest.TestCase):
    def test_only_fixed_paths(self):
        with self.assertRaises(HealthError):
            hub.get('/state')
        with self.assertRaises(HealthError):
            hub.post('/checkin', {})


if __name__ == '__main__':
    unittest.main()
