"""Serve the staged web fixture files on the worker loopback interface."""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os


if __name__ == '__main__':
    with ThreadingHTTPServer(('127.0.0.1', int(os.environ['RECHECK_PORT'])),
                             SimpleHTTPRequestHandler) as server:
        server.serve_forever()
