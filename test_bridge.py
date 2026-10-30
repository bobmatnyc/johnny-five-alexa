import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from bridge import Backends, Router, Store, worker, alexa_response


class BridgeTests(unittest.TestCase):
    def test_media_lookup_is_read_only_and_uses_media_backend(self):
        req=self.request(name='MediaCheckMovieIntent')
        req['request']['intent']['slots']={'title':{'value':'Edge of Tomorrow'}}
        self.speech(req)
        row=self.store.claim();self.assertEqual(row[2],'media')
        self.assertEqual(json.loads(row[3]),{'action':'check','kind':'movie','query':'Edge of Tomorrow'})

    def test_conditional_download_requires_the_spoken_download_clause(self):
        req=self.request(name='MediaCheckMovieIntent')
        req['request']['intent']['slots']={'title':{'value':"Edge of Tomorrow. If we don't, download it and put it in Plex"}}
        self.speech(req)
        data=json.loads(self.store.claim()[3])
        self.assertEqual(data['action'],'request');self.assertEqual(data['query'],'Edge of Tomorrow')

    def test_media_season_is_explicit(self):
        req=self.request(name='MediaSeasonIntent')
        req['request']['intent']['slots']={'selection':{'value':'two of Test Show'}}
        self.speech(req)
        data=json.loads(self.store.claim()[3]);self.assertEqual(data['seasons'],'2')

    def test_launch_keeps_skill_open_for_a_followup(self):
        req=self.request();req['request']={'type':'LaunchRequest','requestId':'launch'}
        result=self.router.route(req)['response']
        self.assertFalse(result['shouldEndSession']);self.assertIn('reprompt',result)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'jobs.sqlite3')
        self.config = {'ALEXA_SKILL_ID': 'skill', 'ALEXA_USER_IDS': 'owner', 'CODEX_THREAD_ID': 'thread'}
        self.router = Router(self.store, self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, name='CodexIntent', owner='owner', ident='request'):
        return {'context': {'System': {'application': {'applicationId': 'skill'}, 'user': {'userId': owner}}}, 'request': {'type': 'IntentRequest', 'requestId': ident, 'intent': {'name': name, 'slots': {'request': {'value': 'summarize pending setup'}}}}}

    def speech(self, envelope):
        return self.router.route(envelope)['response']['outputSpeech']['text']

    def test_briefing_has_fixed_readonly_prompt_and_hermes_route(self):
        self.config.update(HERMES_URL='http://local', HERMES_API_KEY='test')
        req=self.request(name='HouseBriefingIntent')
        req['request']['intent']['slots']={}
        self.speech(req)
        row=self.store.claim()
        self.assertEqual(row[2], 'hermes')
        self.assertIn('sensor.house_briefing', row[3])
        self.assertIn('Read-only', row[3])
        self.assertIn('detailed display report', row[3])
        self.assertIn('snapshot age', row[3])
        self.assertIn('historical', row[3])

    def test_backlog_requests_complete_screen_lists_without_changing_tasks(self):
        self.config.update(HERMES_URL='http://local', HERMES_API_KEY='test')
        self.speech(self.request(name='BacklogIntent'))
        row = self.store.claim()
        self.assertEqual(row[2], 'hermes')
        self.assertIn('full pending household_backlog and home_visit', row[3])
        self.assertIn('Do not change tasks', row[3])

    def test_house_report_uses_the_same_screen_path_as_recipes(self):
        report = 'Maintenance\nFilter needs checking.\nPower and network\nInternet online.\nSampled at 12:00; unavailable pool readings.'
        job = self.store.submit('house', 'owner', 'hermes', 'house briefing')
        self.store.finish(job, 'completed', json.dumps({'title': 'House status', 'speech': 'Check the purifier filter.', 'display': report}))
        req = self.request(name='StatusIntent')
        req['context']['System']['device'] = {'supportedInterfaces': {'Alexa.Presentation.APL': {}}}
        response = self.router.route(req)['response']
        self.assertEqual(response['outputSpeech']['text'], 'Check the purifier filter.')
        self.assertEqual(response['card']['content'], report)
        self.assertIn('unavailable pool readings', response['directives'][0]['datasources']['answer']['text'])

    def test_owner_allowlist(self):
        with self.assertRaises(PermissionError):
            self.router.route(self.request(owner='stranger'))
        self.assertIsNone(self.store.claim())

    def test_wrong_skill(self):
        req = self.request()
        req['context']['System']['application']['applicationId'] = 'wrong'
        with self.assertRaises(PermissionError):
            self.router.route(req)

    def test_duplicate_delivery_runs_once(self):
        self.speech(self.request())
        self.speech(self.request())
        self.assertIsNotNone(self.store.claim())
        self.assertIsNone(self.store.claim())

    def test_cross_owner_request_id(self):
        self.store.submit('same', 'owner', 'codex', 'hello')
        with self.assertRaises(ValueError):
            self.store.submit('same', 'other', 'codex', 'hello')

    def test_restart_does_not_replay_uncertain_job(self):
        job = self.store.submit('test', 'owner', 'codex', 'hello')
        self.store.claim()
        restarted = Store(self.store.path)
        self.assertIsNone(restarted.claim())
        self.assertEqual(restarted.latest('owner')[0], 'uncertain')

    def test_queued_job_survives_restart(self):
        self.store.submit('test', 'owner', 'codex', 'hello')
        self.assertIsNotNone(Store(self.store.path).claim())

    def test_status_does_not_leak_other_owner(self):
        job = self.store.submit('test', 'other', 'codex', 'hello')
        self.store.finish(job, 'completed', 'private answer')
        self.assertIn('no requests', self.speech(self.request(name='StatusIntent')))

    def test_status_returns_actual_result(self):
        job = self.store.submit('test', 'owner', 'codex', 'hello')
        self.store.finish(job, 'completed', 'actual agent answer')
        self.assertEqual(self.speech(self.request(name='StatusIntent')), 'actual agent answer')

    def test_missing_hermes_not_acknowledged_as_queued(self):
        self.assertIn('not connected', self.speech(self.request(name='HermesIntent')))
        self.assertIsNone(self.store.claim())

    def test_default_request_routes_to_hermes(self):
        self.config.update(HERMES_URL='http://private', HERMES_API_KEY='test-key')
        self.speech(self.request(name='RequestIntent'))
        self.assertEqual(self.store.claim()[2], 'hermes')

    def test_bounded_queue_and_prompt(self):
        with self.assertRaises(ValueError):
            self.store.submit('long', 'owner', 'codex', 'a'*2001)
        for i in range(10):
            self.store.submit(str(i), 'owner', 'codex', 'hello')
        with self.assertRaises(ValueError):
            self.store.submit('full', 'owner', 'codex', 'hello')

    def test_failure_not_spoken_with_credentials(self):
        stop = threading.Event()
        class Failing:
            def run(self, *args):
                stop.set()
                raise RuntimeError('secret token')
        self.store.submit('fail', 'owner', 'codex', 'hello')
        worker(self.store, Failing(), stop)
        status, result = self.store.latest('owner')
        self.assertEqual(status, 'uncertain')
        self.assertNotIn('secret', result)

    def test_plaintext_not_ssml(self):
        job = self.store.submit('test', 'owner', 'codex', 'hello')
        self.store.finish(job, 'completed', '<audio src="evil"/>')
        speech = self.router.route(self.request(name='StatusIntent'))['response']['outputSpeech']
        self.assertEqual(speech['type'], 'PlainText')

    def test_recipe_speech_and_complete_card_are_separate(self):
        recipe = 'Ingredients\n' + 'Measured ingredient and numbered procedure.\n'*140
        job = self.store.submit('recipe', 'owner', 'hermes', 'give me a recipe')
        self.store.finish(job, 'completed', json.dumps({'title': 'Chicken dinner', 'speech': 'Your chicken recipe serves six.', 'display': recipe}))
        response = self.router.route(self.request(name='StatusIntent'))['response']
        self.assertEqual(response['outputSpeech']['text'], 'Your chicken recipe serves six.')
        self.assertEqual(response['card']['content'], recipe)
        self.assertNotIn('directives', response)

    def test_echo_show_gets_scrollable_apl_with_recipe(self):
        job = self.store.submit('screen', 'owner', 'hermes', 'recipe')
        self.store.finish(job, 'completed', json.dumps({'title': 'Dinner', 'speech': 'Dinner is ready to view.', 'display': 'Ingredients\n2 lbs chicken\nDirections\n1. Cook.'}))
        request = self.request(name='StatusIntent')
        request['context']['System']['device'] = {'supportedInterfaces': {'Alexa.Presentation.APL': {'runtime': {'maxVersion': '2024.3'}}}}
        response = self.router.route(request)['response']
        directive = response['directives'][0]
        self.assertEqual(directive['type'], 'Alexa.Presentation.APL.RenderDocument')
        items = directive['document']['mainTemplate']['items'][0]['items']
        self.assertEqual(items[1]['type'], 'ScrollView')
        self.assertIn('2 lbs chicken', directive['datasources']['answer']['text'])
        self.assertNotIn('2 lbs chicken', response['outputSpeech']['text'])

    def test_model_content_cannot_supply_directives_or_apl_markup(self):
        result = json.dumps({'speech': 'Summary', 'display': '<b>Unsafe</b> ${danger}', 'directives': [{'type': 'AudioPlayer.Play'}]})
        job = self.store.submit('untrusted', 'owner', 'hermes', 'recipe')
        self.store.finish(job, 'completed', result)
        request = self.request(name='StatusIntent')
        request['context']['System']['device'] = {'supportedInterfaces': {'Alexa.Presentation.APL': {}}}
        response = self.router.route(request)['response']
        self.assertEqual(len(response['directives']), 1)
        self.assertIn('&lt;b&gt;', response['directives'][0]['datasources']['answer']['text'])
        self.assertNotIn('AudioPlayer', json.dumps(response))

    def test_long_unicode_answer_fits_amazon_limits_and_discloses_cutoff(self):
        response = alexa_response('Summary', {'device': {'supportedInterfaces': {'Alexa.Presentation.APL': {}}}}, display='🍲 & <b>\n'*8000)
        card = response['response']['card']
        self.assertLessEqual(len(card['title'])+len(card['content']), 8000)
        self.assertIn('shortened', card['content'])
        self.assertLess(len(json.dumps(response).encode()), 120000)
        if response['response'].get('directives'):
            self.assertLess(len(json.dumps(response).encode()), 24000)

    def test_fenced_json_answer_is_not_read_as_json(self):
        job = self.store.submit('fenced', 'owner', 'hermes', 'recipe')
        self.store.finish(job, 'completed', '```json\n'+json.dumps({'speech': 'Summary', 'display': 'Full recipe'})+'\n```')
        self.assertEqual(self.speech(self.request(name='StatusIntent')), 'Summary')

    def test_cancel_does_not_claim_to_cancel_running_work(self):
        self.assertIn('keep running', self.speech(self.request(name='AMAZON.CancelIntent')))

    def test_sdk_rejects_unsigned_request(self):
        from ask_sdk_webservice_support.verifier import RequestVerifier, VerificationException
        with self.assertRaises(VerificationException):
            RequestVerifier().verify({}, '{}', None)

    def test_sdk_rejects_untrusted_certificate_host_before_fetch(self):
        from ask_sdk_webservice_support.verifier import RequestVerifier, VerificationException
        with self.assertRaisesRegex(VerificationException, 'invalid hostname'):
            RequestVerifier().verify({'SignatureCertChainUrl':'https://attacker.invalid/echo.api/cert.pem','Signature-256':'bad'}, '{}', None)

    def test_sdk_rejects_stale_request(self):
        from ask_sdk_core.serialize import DefaultSerializer
        from ask_sdk_model import RequestEnvelope
        from ask_sdk_webservice_support.verifier import TimestampVerifier, VerificationException
        req = self.request()
        req['request']['timestamp'] = (datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()
        raw = json.dumps(req)
        model = DefaultSerializer().deserialize(raw, RequestEnvelope)
        with self.assertRaises(VerificationException):
            TimestampVerifier().verify({}, raw, model)

    def test_interaction_model_has_required_intents(self):
        model = json.loads(Path(__file__).with_name('interaction-model.json').read_text())
        intents = model['interactionModel']['languageModel']['intents']
        self.assertTrue({'CodexIntent','HermesIntent','StatusIntent'} <= {v['name'] for v in intents})
        for intent in intents:
            for sample in intent.get('samples', []):
                self.assertNotEqual(sample.strip(), '{request}')
                search_slots = [x['name'] for x in intent.get('slots', []) if x['type'] == 'AMAZON.SearchQuery']
                if any('{'+slot+'}' in sample for slot in search_slots):
                    self.assertEqual(sample.count('{'), 1)


if __name__ == '__main__':
    unittest.main()
