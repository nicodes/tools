#!/usr/bin/env python3
"""Loopback-only static preview with file-based app route fallback."""
import argparse
import urllib.request
import urllib.error
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

def validate_api(value):
    if not value:
        return value
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
            or parsed.port is None or not 1 <= parsed.port <= 65535):
        raise ValueError("Static fixtures may proxy only an explicit loopback API port")
    return value


class Handler(SimpleHTTPRequestHandler):
    def proxy(self):
        if not self.api or not self.path.startswith('/api/'): return False
        size=int(self.headers.get('Content-Length','0'))
        if size>1048576:self.send_error(413);return True
        body=self.rfile.read(size) if size else None
        headers={k:v for k,v in self.headers.items() if k.lower() in ['authorization','content-type','origin']}
        request=urllib.request.Request(self.api+self.path[4:],data=body,headers=headers,method=self.command)
        try:response=urllib.request.urlopen(request,timeout=20)
        except urllib.error.HTTPError as error:response=error
        except OSError:self.send_error(503);return True
        with response:
            data=response.read(1048577)
            self.send_response(response.status)
            for key in ['Content-Type','Cache-Control','X-App-Version']:
                if response.headers.get(key):self.send_header(key,response.headers[key])
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        return True
    def do_PUT(self):
        if not self.proxy():self.send_error(405)
    def do_PATCH(self):
        if not self.proxy():self.send_error(405)
    def do_DELETE(self):
        if not self.proxy():self.send_error(405)
    def do_GET(self):
        if self.proxy():return
        path = Path(self.translate_path(urlsplit(self.path).path))
        if not path.exists() and not Path(urlsplit(self.path).path).suffix:
            self.path = '/index.html'
        super().do_GET()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8088)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--api',default='')
    args = parser.parse_args()
    Handler.api=validate_api(args.api)
    root = args.root.resolve(strict=True)
    if not (root / 'index.html').is_file():
        raise SystemExit('Run mise run build first.')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), partial(Handler, directory=str(root)))
    print(f'Local preview: http://127.0.0.1:{args.port}/', flush=True)
    server.serve_forever()
