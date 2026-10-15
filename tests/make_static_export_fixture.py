"""Create a disposable public bundle for the provisional export integration test.

Usage: python tests/make_static_export_fixture.py <provisional-root> <new-output>
Never opens the production instance and never calls Azure.
"""
from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def main():
    source, destination = (Path(arg).resolve() for arg in sys.argv[1:])
    destination.mkdir()  # Refuse to overwrite any existing fixture.
    text = (source / 'data/site.js').read_text(encoding='utf-8').strip()
    data = json.loads(text.split('=', 1)[1].rstrip(';'))

    def clean(value):
        if isinstance(value, dict):
            return {key: clean(child) for key, child in value.items() if key != '_translationStatus'}
        if isinstance(value, list):
            return [clean(child) for child in value]
        return value

    data = clean(data)
    removed = data['posts'][-1]['id']
    data['posts'][-1]['published'] = False
    data['posts'].reverse()
    data['departments'].reverse()
    with TemporaryDirectory() as instance, patch.dict('os.environ', {
        'MCSA_ADMIN_PASSWORD': 'fixture-export-password', 'AZURE_TRANSLATOR_KEY': '',
        'MCSA_HTTPS': '0', 'MCSA_FRONTEND_URL': 'http://127.0.0.1:8080', 'MCSA_PUBLIC_URL': 'http://127.0.0.1:8000'
    }), patch.object(server, 'SEED', data):
        app = server.create_app(instance)
        shutil.copytree(source / 'media', Path(instance) / 'media', dirs_exist_ok=True)
        workflow = app.extensions['translations']
        with closing(workflow.connect()) as connection, connection:
            key, encoded = connection.execute('SELECT id,data FROM translation_fields LIMIT 1').fetchone()
            record = json.loads(encoded)
            record.update(draft='NEVER EXPORT THIS HUMAN DRAFT', reviewed_by='PRIVATE REVIEWER')
            connection.execute('UPDATE translation_fields SET data=? WHERE id=?', (json.dumps(record), key))
        client = app.test_client()
        client.post('/api/login', json={'username':'admin', 'password':'fixture-export-password'})
        response = client.get('/api/admin/static-export')
        if response.status_code != 200:
            raise RuntimeError(response.json)
        with zipfile.ZipFile(BytesIO(response.data)) as archive:
            # Only these explicit public names are written to the new fixture directory.
            for name in archive.namelist():
                target = (destination / name).resolve()
                if not target.is_relative_to(destination):
                    raise ValueError('Unsafe bundle path')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
        public = (destination / 'content.json').read_text(encoding='utf-8')
        assert 'NEVER EXPORT' not in public and 'PRIVATE REVIEWER' not in public and '_translationId' not in public
    print(f'Fixture ready; unpublished article must be absent: {removed}')


if __name__ == '__main__':
    main()
