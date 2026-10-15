"""Public static bundles must never behave like private CMS backups."""
from contextlib import closing
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from server import create_app


class StaticExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict('os.environ', {'MCSA_ADMIN_PASSWORD': 'static-export-test-password',
            'MCSA_HTTPS': '0', 'AZURE_TRANSLATOR_KEY': '', 'MCSA_FRONTEND_URL': 'http://127.0.0.1:8080',
            'MCSA_PUBLIC_URL': 'http://127.0.0.1:8000'})
        env.start()
        self.addCleanup(env.stop)
        self.app = create_app(self.directory.name)
        self.client = self.app.test_client()
        result = self.client.post('/api/login', json={'username': 'admin', 'password': 'static-export-test-password'})
        self.headers = {'X-CSRF-Token': result.json['csrf']}

    def test_public_projection_and_only_referenced_media(self):
        current = self.client.get('/api/admin/site').json
        public = deepcopy(current['data']['posts'][0])
        public.update(id='export-public', published=True, image='media/'+'a'*32+'.png')
        public['title'] = {'zh': '公开标题', 'en': 'UNAPPROVED INPUT', 'hant': ''}
        public['text'] = {'zh': '', 'en': '', 'hant': ''}
        draft = deepcopy(public)
        draft.update(id='export-draft', published=False, image='media/'+'b'*32+'.png')
        draft['title'] = {'zh': 'PRIVATE CHINESE DRAFT', 'en': '', 'hant': ''}
        current['data']['posts'] = [public, draft]
        result = self.client.put('/api/admin/site', json={'revision':current['revision'],'data':current['data']}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        for name in ['a','b']:
            (Path(self.directory.name)/'media'/(name*32+'.png')).write_bytes(b'original image bytes')
        identifier = result.json['data']['posts'][0]['title']['_translationId']
        record = result.json['translations']['fields'][identifier]
        with closing(self.app.extensions['translations'].connect()) as connection, connection:
            # Model an existing public machine candidate plus a private human draft.
            record.update(candidate='PUBLIC MACHINE', candidate_source='公开标题', draft='PRIVATE ENGLISH DRAFT', reviewed_by='PRIVATE REVIEWER')
            connection.execute('UPDATE translation_fields SET data=? WHERE id=?',(json.dumps(record),identifier))
        response = self.client.get('/api/admin/static-export')
        self.assertEqual(response.status_code,200)
        with zipfile.ZipFile(BytesIO(response.data)) as archive:
            self.assertEqual(sorted(archive.namelist()),['content.json','media/'+'a'*32+'.png'])
            bundle = json.loads(archive.read('content.json'))
            self.assertEqual(bundle['format'],'mcsa-public-v1')
            self.assertEqual(bundle['data']['posts'][0]['title']['en'],'PUBLIC MACHINE')
            self.assertEqual(bundle['data']['posts'][0]['image'],'media/'+'a'*32+'.png')
            self.assertNotIn('PRIVATE',json.dumps(bundle))
            self.assertNotIn('_translationId',json.dumps(bundle))
            self.assertNotIn('UNAPPROVED',json.dumps(bundle))
            self.assertNotIn('translations',bundle)
        current = self.client.get('/api/admin/site').json
        current['data']['posts'][0]['published'] = False
        self.client.put('/api/admin/site',json={'revision':current['revision'],'data':current['data']},headers=self.headers)
        with zipfile.ZipFile(BytesIO(self.client.get('/api/admin/static-export').data)) as archive:
            self.assertEqual(archive.namelist(),['content.json'])
            self.assertEqual(json.loads(archive.read('content.json'))['data']['posts'],[])

    def test_missing_media_stops_export(self):
        current = self.client.get('/api/admin/site').json
        current['data']['posts'][0].update(published=True,image='media/'+'c'*32+'.png')
        self.client.put('/api/admin/site',json={'revision':current['revision'],'data':current['data']},headers=self.headers)
        result = self.client.get('/api/admin/static-export')
        self.assertEqual(result.status_code,409)
        self.assertEqual(result.json['error'],'export_missing_media')

    def test_login_and_site_read_permission_required(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get('/api/admin/static-export').status_code,401)
        with self.client.session_transaction() as session:
            user_id=session['user_id']
        with closing(self.app.extensions['translations'].connect()) as connection, connection:
            connection.execute("UPDATE users SET role='admin',permissions='[]' WHERE id=?",(user_id,))
        self.assertEqual(self.client.get('/api/admin/static-export').status_code,403)


if __name__ == '__main__':
    unittest.main()
