"""Isolated browser fixture. Never opens the production instance or Azure.

Run from the backend root; serves the workspace on 8765 and a temporary CMS on
8766. Stop with Ctrl+C. Used by the frontend's translation-browser.test.cjs.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from waitress import serve
import translation_workflow
from server import create_app

os.environ.update(MCSA_ADMIN_PASSWORD='browser-translation-test', MCSA_HTTPS='0',
                  MCSA_FRONTEND_URL='http://127.0.0.1:8765/MCSA-offical-website/',
                  MCSA_PUBLIC_URL='http://127.0.0.1:8766', AZURE_TRANSLATOR_KEY='fixture-only')
translation_workflow.translate_text = lambda source, terms: 'Machine translation ' + ' '.join(re.findall(r'\{\w+\}', source))

if __name__ == '__main__':
    with TemporaryDirectory(prefix='mcsa-browser-') as instance:
        app = create_app(instance)
        workflow = app.extensions['translations']
        static = ThreadingHTTPServer(('127.0.0.1', 8765), partial(SimpleHTTPRequestHandler,
            directory=str(Path(__file__).resolve().parents[2])))
        threading.Thread(target=static.serve_forever, daemon=True).start()
        workflow.start()
        print('Isolated fixture ready: frontend 8765, CMS 8766', flush=True)
        try:
            serve(app, host='127.0.0.1', port=8766)
        finally:
            workflow.stop_event.set()
            workflow.thread.join(timeout=30)
            static.shutdown()
