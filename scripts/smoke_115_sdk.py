"""Exercise the real ARM64 SDK against a disposable loopback OSS fixture."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
from pathlib import Path
import platform
import tempfile
import threading


def main():
    for name in ('oss2', 'cryptography', 'OpenSSL', 'requests', 'urllib3',
                 'crcmod', 'Crypto', 'aliyunsdkcore', 'aliyunsdkkms', 'cffi'):
        importlib.import_module(name)
    import oss2
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            data = self.rfile.read(int(self.headers['Content-Length']))
            received.append((self.path, data))
            self.send_response(200)
            self.send_header('ETag', hashlib.md5(data).hexdigest())
            self.send_header('x-oss-request-id', 'local-fixture')
            self.send_header('Content-Length', '0')
            self.end_headers()

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = 'http://127.0.0.1:%d' % server.server_port
            bucket = oss2.Bucket(oss2.StsAuth('fixture-id', 'fixture-secret', 'fixture-token'),
                                endpoint, 'fixture-bucket', connect_timeout=3)
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'payload.bin'
                payload = bytes(range(256)) * 4096
                source.write_bytes(payload)
                result = bucket.put_object_from_file('probe.bin', str(source))
                if result.status != 200 or received != [('/fixture-bucket/probe.bin', payload)]:
                    raise RuntimeError('SDK upload fixture mismatch')
        finally:
            server.shutdown()
            thread.join(timeout=5)
    print(json.dumps({'imports': 'passed', 'oss_upload': 'passed', 'bytes': len(payload),
                      'architecture': platform.machine(), 'python': platform.python_version()}))


if __name__ == '__main__':
    main()
