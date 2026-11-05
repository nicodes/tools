import importlib.util
from functools import partial
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

spec = importlib.util.spec_from_file_location('static_preview', Path(__file__).resolve().parents[1] / 'helpers/static-preview.py')
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class StaticPreviewControls(unittest.TestCase):
    def test_proxy_refuses_remote_or_credentialed_origins(self):
        for value in ['https://127.0.0.1:8000', 'http://example.invalid:8000', 'http://user:password@127.0.0.1:8000', 'http://127.0.0.1:8000/path']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                preview.validate_api(value)

    def test_route_fallback_and_local_api_transport_keep_their_boundaries(self):
        observed = []
        class Api(BaseHTTPRequestHandler):
            def do_PUT(self):
                observed.append((self.path, dict(self.headers), self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.send_header('X-App-Version', 'fixture-source')
                self.end_headers()
                self.wfile.write(b'accepted local request')
            def log_message(self, *_args):
                pass
        class Handler(preview.Handler):
            def log_message(self, *_args):
                pass
        with tempfile.TemporaryDirectory() as directory, ThreadingHTTPServer(('127.0.0.1', 0), Api) as api:
            Path(directory, 'index.html').write_text('owned static export')
            Handler.api = preview.validate_api('http://127.0.0.1:' + str(api.server_port))
            api_thread = threading.Thread(target=api.serve_forever, daemon=True)
            api_thread.start()
            try:
                with ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=directory)) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                        connection.request('GET', '/nested/app/route')
                        response = connection.getresponse()
                        self.assertEqual((response.status, response.read()), (200, b'owned static export'))
                        connection.request('PUT', '/api/account', b'fixture request', {'Authorization': 'Bearer fixture', 'Cookie': 'must-stay-local'})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.getheader('X-App-Version'), 'fixture-source')
                        self.assertEqual(response.read(), b'accepted local request')
                        connection.close()
                        self.assertEqual(observed[0][0], '/account')
                        self.assertEqual(observed[0][1]['Authorization'], 'Bearer fixture')
                        self.assertNotIn('Cookie', observed[0][1])
                    finally:
                        server.shutdown()
                        thread.join(timeout=3)
            finally:
                api.shutdown()
                api_thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
