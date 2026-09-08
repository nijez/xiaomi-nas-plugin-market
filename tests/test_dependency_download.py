import http.client
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import prepare_115_inputs as download


class DownloadTests(unittest.TestCase):
    def response(self, status=200, body=b'complete', offset=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.url = 'https://files.pythonhosted.org/fixture.whl'
        response.headers = {'Content-Range': offset} if offset else {}
        response.read.return_value = body
        return response

    def test_reject_untrusted_urls_before_network(self):
        for url in ('http://pypi.org/a', 'https://example.org/a',
                    'https://user@pypi.org/a', 'https://pypi.org:8443/a'):
            with self.subTest(url=url), patch.object(download.urllib.request, 'build_opener') as opener:
                with self.assertRaises(ValueError):
                    download.fetch(url)
                opener.assert_not_called()

    def test_reject_redirect_before_following(self):
        with self.assertRaises(ValueError):
            download.OfficialRedirect().redirect_request(None, None, 302, '', {}, 'https://example.org/x')

    def test_resumes_partial_bytes(self):
        first = self.response()
        first.read.side_effect = http.client.IncompleteRead(b'ab', 2)
        second = self.response(206, b'cd', 'bytes 2-3/4')
        opener = MagicMock()
        opener.open.side_effect = [first, second]
        with patch.object(download.urllib.request, 'build_opener', return_value=opener), patch.object(download.time, 'sleep'):
            self.assertEqual(download.fetch(first.url), b'abcd')
        self.assertEqual(opener.open.call_args.args[0].get_header('Range'), 'bytes=2-')

    def test_range_mismatch_rejected(self):
        opener = MagicMock()
        opener.open.return_value = self.response(206, b'x', 'bytes 9-9/10')
        with patch.object(download.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(ValueError, 'offset'):
                download.fetch('https://pypi.org/fixture')

    def test_server_ignoring_range_restarts_download(self):
        first = self.response()
        first.read.side_effect = http.client.IncompleteRead(b'ab', 2)
        opener = MagicMock()
        opener.open.side_effect = [first, self.response(body=b'abcd')]
        with patch.object(download.urllib.request, 'build_opener', return_value=opener), patch.object(download.time, 'sleep'):
            self.assertEqual(download.fetch(first.url), b'abcd')


if __name__ == '__main__':
    unittest.main()
