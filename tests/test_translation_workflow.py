"""Exercise the real SQLite queue and APIs without contacting Azure."""
from contextlib import closing
from copy import deepcopy
from io import BytesIO
import json
import sqlite3
import time
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from server import create_app
from translations import TranslationError, translate_text


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict('os.environ', {'MCSA_ADMIN_PASSWORD': 'translation-test-password',
            'MCSA_HTTPS': '0', 'AZURE_TRANSLATOR_KEY': '', 'MCSA_FRONTEND_URL': 'http://127.0.0.1:8080',
            'MCSA_PUBLIC_URL': 'http://127.0.0.1:8000'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.app = create_app(self.directory.name)
        self.client = self.app.test_client()
        result = self.client.post('/api/login', json={'username': 'admin', 'password': 'translation-test-password'})
        self.headers = {'X-CSRF-Token': result.json['csrf']}
        self.workflow = self.app.extensions['translations']
        self.refresh()

    def refresh(self):
        self.current = self.client.get('/api/admin/site').json
        return self.current

    def save(self):
        result = self.client.put('/api/admin/site', headers=self.headers,
                                 json={'revision': self.current['revision'], 'data': self.current['data']})
        self.assertEqual(result.status_code, 200, result.json)
        self.current = result.json
        return result

    def new_post(self, source='新活动'):
        post = deepcopy(self.current['data']['posts'][0])
        post.update(id='translation-test', published=True)
        post['title'] = {'zh': source, 'en': 'UNAPPROVED INPUT', 'hant': ''}
        post['text'] = {'zh': '', 'en': '', 'hant': ''}
        self.current['data']['posts'].append(post)
        self.save()
        return self.current['data']['posts'][-1]['title']['_translationId']

    def public_post(self):
        return self.client.get('/api/site').json['data']['posts'][-1]

    def action(self, identifier, action, text='', version=None):
        self.refresh()
        record = self.current['translations']['fields'][identifier]
        return self.client.post('/api/admin/translations', headers=self.headers, json={
            'action': action, 'entries': [{'id': identifier, 'version': record['version'] if version is None else version, 'text': text}]})

    def run_job(self, english='New event'):
        with patch('translation_workflow.translate_text', return_value=english) as call:
            self.assertTrue(self.workflow.run_once())
            self.assertEqual(call.call_count, 1)
        self.refresh()

    def test_machine_draft_and_approval_are_separate(self):
        identifier = self.new_post()
        self.assertEqual(self.public_post()['title']['en'], '')
        self.assertEqual(self.public_post()['title']['_translationStatus'], 'pending')
        self.run_job()
        self.assertEqual(self.public_post()['title']['en'], 'New event')
        self.assertEqual(self.public_post()['title']['_translationStatus'], 'machine')
        self.assertEqual(self.action(identifier, 'draft', 'PRIVATE DRAFT').status_code, 200)
        self.assertNotIn('PRIVATE DRAFT', self.client.get('/api/site').text)
        self.assertEqual(self.action(identifier, 'approve', 'Official event').status_code, 200)
        self.assertEqual(self.public_post()['title']['en'], 'Official event')
        public = self.client.get('/api/site').text
        self.assertNotIn('_translationId', public)
        self.assertNotIn('reviewed_by', public)
        self.assertNotIn('PRIVATE DRAFT', public)

    def test_source_change_keeps_manual_copy_and_generates_candidate(self):
        identifier = self.new_post()
        self.action(identifier, 'approve', 'Reviewed event')
        self.refresh()['data']['posts'][-1]['title']['zh'] = '修改活动'
        self.save()
        self.assertEqual(self.public_post()['title']['en'], 'Reviewed event')
        self.assertEqual(self.public_post()['title']['_translationStatus'], 'stale')
        self.run_job('Updated event')
        self.assertEqual(self.public_post()['title']['en'], 'Reviewed event')
        self.assertEqual(self.current['translations']['fields'][identifier]['candidate'], 'Updated event')

    def test_saving_english_in_content_cannot_bypass_review(self):
        before = self.current['data']['departments'][0]['name']['en']
        self.current['data']['departments'][0]['name']['en'] = 'FORGED'
        self.save()
        self.assertEqual(self.current['data']['departments'][0]['name']['en'], before)
        self.assertFalse(self.workflow.run_once())

    def test_legacy_migration_is_idempotent_and_protected(self):
        original = self.current['data']['departments'][0]['name']
        identifier = original['_translationId']
        self.assertTrue(self.current['translations']['fields'][identifier]['legacy'])
        create_app(self.directory.name)
        self.refresh()
        self.assertEqual(self.current['data']['departments'][0]['name'], original)
        self.current['data']['departments'][0]['name']['zh'] += '新名称'
        self.save()
        self.run_job('New name')
        self.assertEqual(self.current['data']['departments'][0]['name']['en'], original['en'])

    def test_stale_result_cannot_overwrite_new_source_or_approval(self):
        identifier = self.new_post()
        def mutate_while_translating(source, terms):
            self.refresh()['data']['posts'][-1]['title']['zh'] = '更新后的活动'
            self.save()
            return 'OUTDATED RESULT'
        with patch('translation_workflow.translate_text', side_effect=mutate_while_translating):
            self.workflow.run_once()
        self.assertEqual(self.public_post()['title']['en'], '')
        def approve_while_translating(source, terms):
            self.action(identifier, 'approve', 'Approved during request')
            return 'MACHINE TOO LATE'
        with patch('translation_workflow.translate_text', side_effect=approve_while_translating):
            self.workflow.run_once()
        self.assertEqual(self.public_post()['title']['en'], 'Approved during request')

    def test_worker_preserves_draft_written_during_request(self):
        identifier = self.new_post()
        def draft_while_translating(source, terms):
            self.action(identifier, 'draft', 'Keep this draft')
            return 'Machine result'
        with patch('translation_workflow.translate_text', side_effect=draft_while_translating):
            self.workflow.run_once()
        self.refresh()
        self.assertEqual(self.current['translations']['fields'][identifier]['draft'], 'Keep this draft')

    def test_reorder_delete_and_unpublish_never_mismatch_or_resurrect(self):
        identifier = self.new_post()
        self.current['data']['posts'].reverse()
        self.save()
        self.assertEqual(self.current['data']['posts'][0]['title']['_translationId'], identifier)
        self.current['data']['posts'][0]['published'] = False
        self.save()
        self.run_job()
        self.assertNotIn('translation-test', self.client.get('/api/site').text)
        self.action(identifier, 'retry')
        def remove_during_request(source, terms):
            self.refresh()['data']['posts'].pop(0)
            self.save()
            return 'Deleted event'
        with patch('translation_workflow.translate_text', side_effect=remove_during_request):
            self.workflow.run_once()
        self.assertNotIn(identifier, self.refresh()['translations']['fields'])

    def test_missing_key_and_bounded_retry(self):
        identifier = self.new_post()
        self.workflow.run_once()
        self.assertEqual(self.refresh()['translations']['fields'][identifier]['error'], 'not_configured')
        self.action(identifier, 'retry')
        with patch('translation_workflow.translate_text', side_effect=TranslationError('network_error', retryable=True)) as call:
            for _ in range(4):
                with closing(self.workflow.connect()) as c, c:
                    c.execute('UPDATE translation_jobs SET next_at=0')
                self.assertTrue(self.workflow.run_once())
            self.assertFalse(self.workflow.run_once())
            self.assertEqual(call.call_count, 4)
        self.assertEqual(self.refresh()['translations']['fields'][identifier]['status'], 'failed')
        self.assertEqual(self.public_post()['title']['_translationStatus'], 'pending')

    def test_restart_recovers_expired_lease(self):
        self.new_post()
        with closing(self.workflow.connect()) as c, c:
            c.execute("UPDATE translation_jobs SET lease='dead-worker',lease_until=?", (time.time() - 1,))
        reopened = create_app(self.directory.name).extensions['translations']
        with patch('translation_workflow.translate_text', return_value='Recovered'):
            self.assertTrue(reopened.run_once())
        self.assertEqual(self.public_post()['title']['en'], 'Recovered')

    def test_review_conflict_atomic_batch_and_authorization(self):
        identifier = self.new_post()
        old = self.refresh()['translations']['fields'][identifier]['version']
        self.run_job()
        self.assertEqual(self.action(identifier, 'approve', 'Old decision', old).status_code, 409)
        endpoint = '/api/admin/translations'
        self.assertEqual(self.app.test_client().post(endpoint, json={}).status_code, 401)
        self.assertEqual(self.client.post(endpoint, json={}).status_code, 403)
        result = self.client.post(endpoint, headers=self.headers, json={'action':'approve', 'entries':[
            {'id':identifier, 'version':self.current['translations']['fields'][identifier]['version'], 'text':'Do not publish'},
            {'id':'missing', 'version':1, 'text':'Invalid'}]})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.public_post()['title']['en'], 'New event')

    def test_glossary_changes_queue_only_affected_fields(self):
        identifier = self.new_post('校友欢迎活动')
        self.action(identifier, 'approve', 'Alumni Welcome')
        config = self.refresh()['translations']['glossary']
        result = self.client.put('/api/admin/translation-glossary', headers=self.headers,
            json={'version':config['version'], 'terms':[{'zh':'欢迎活动', 'en':'Welcome Event', 'enabled':True}]})
        self.assertEqual(result.status_code, 200)
        with closing(self.workflow.connect()) as c:
            self.assertEqual(c.execute('SELECT id FROM translation_jobs').fetchall(), [(identifier,)])
        self.run_job('Alumni Welcome Event')
        self.assertEqual(self.public_post()['title']['en'], 'Alumni Welcome')

    def test_backup_restore_retains_drafts_and_recovers_work(self):
        identifier = self.new_post()
        self.action(identifier, 'draft', 'Saved draft')
        response = self.client.get('/api/backup')
        payload = json.loads(zipfile.ZipFile(BytesIO(response.data)).read('content.json'))
        response.close()
        with closing(self.workflow.connect()) as c, c:
            c.execute('BEGIN IMMEDIATE')
            self.workflow.restore(c, payload['data'], payload['translations'])
            c.execute('UPDATE site SET data=? WHERE id=1', (json.dumps(payload['data']),))
        self.run_job()
        self.assertEqual(self.current['translations']['fields'][identifier]['draft'], 'Saved draft')

    def test_ids_and_placeholders_are_validated(self):
        data = self.current['data']
        data['departments'][1]['name']['_translationId'] = data['departments'][0]['name']['_translationId']
        self.assertEqual(self.client.put('/api/admin/site', headers=self.headers, json=self.current).status_code, 400)
        self.refresh()
        identifier = self.new_post('查看{name}')
        self.assertEqual(self.action(identifier, 'approve', 'Explore name').status_code, 400)
        self.assertEqual(self.action(identifier, 'approve', 'Explore {name}').status_code, 200)

    def test_invalid_requests_and_read_only_accounts_cannot_review(self):
        identifier = self.new_post()
        for body in ({'action': [], 'entries': []}, {'action': 'retry', 'entries': [{'id': [], 'version':1}]},
                     {'action': 'draft', 'entries': [{'id':identifier, 'version':True, 'text':'x'}]}):
            self.assertEqual(self.client.post('/api/admin/translations', headers=self.headers, json=body).status_code, 400)
        created = self.client.post('/api/admin/accounts', headers=self.headers, json={
            'username':'review-reader', 'password':'read-only-test-password', 'role':'admin', 'active':True, 'permissions':['site.read']})
        self.assertEqual(created.status_code, 201)
        reader = self.app.test_client()
        session = reader.post('/api/login', json={'username':'review-reader', 'password':'read-only-test-password'})
        self.assertEqual(reader.get('/api/admin/translations').status_code, 200)
        headers = {'X-CSRF-Token':session.json['csrf']}
        self.assertEqual(reader.post('/api/admin/translations', headers=headers, json={}).status_code, 403)
        self.assertEqual(reader.put('/api/admin/translation-glossary', headers=headers, json={}).status_code, 403)


class AzureAdapterTests(unittest.TestCase):
    def test_dictionary_escapes_names_prefers_longest_and_keeps_placeholders(self):
        response = BytesIO(json.dumps([{'translations':[{'to':'en','text':'MCSA Event {name} &amp; friends'}]}]).encode())
        with patch.dict('os.environ', {'AZURE_TRANSLATOR_KEY':'test-only'}), patch('translations.urlopen', return_value=response) as call:
            result = translate_text('学生会活动 {name} & friends', [
                {'zh':'学生会','en':'MCSA', 'enabled':True},
                {'zh':'学生会活动','en':'MCSA Event', 'enabled':True}])
        body = json.loads(call.call_args.args[0].data)[0]['Text']
        self.assertIn('translation="MCSA Event">学生会活动</mstrans:dictionary>', body)
        self.assertIn('translation="{name}"', body)
        self.assertIn('&amp; friends', body)
        self.assertEqual(result, 'MCSA Event {name} & friends')

    def test_malformed_response_and_changed_placeholders_fail_closed(self):
        for payload in ([], [{'translations':[{'to':'en', 'text':'Missing placeholder'}]}]):
            with patch.dict('os.environ', {'AZURE_TRANSLATOR_KEY':'test-only'}), patch('translations.urlopen', return_value=BytesIO(json.dumps(payload).encode())):
                with self.assertRaises(TranslationError):
                    translate_text('查看{name}', [])


if __name__ == '__main__':
    unittest.main()
