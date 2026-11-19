"""Parcel quote HTTP sample. The worker supplies RECHECK_PORT."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from urllib.parse import parse_qs, urlsplit


class ParcelAPI(BaseHTTPRequestHandler):
    def reply(self, status, payload):
        body = json.dumps(payload, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            self.send_response(204)
            self.end_headers()
        else:
            self.reply(404, {'error': 'route'})

    def do_POST(self):
        route = urlsplit(self.path)
        if route.path != '/quote' or parse_qs(route.query).get('unit') != ['kg']:
            self.reply(404, {'error': 'route'})
            return
        try:
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            mass = request['weight']
            if not isinstance(mass, str) or not re.fullmatch(r'[0-9]{1,3}', mass):
                raise ValueError
            kilos = int(mass)
            if kilos > 100:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            self.reply(422, {'error': 'weight'})
            return
        self.reply(200, {'amount': 100 + 15 * kilos})

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    with ThreadingHTTPServer(('127.0.0.1', int(os.environ['RECHECK_PORT'])), ParcelAPI) as server:
        server.serve_forever()
