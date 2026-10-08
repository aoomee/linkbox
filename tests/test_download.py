"""Regression coverage for interrupted bootstrap downloads."""
import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('download_lb', root / 'linkbox.py')
lb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lb)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.destination = Path(self.tmp.name) / 'core.tar.gz'
        self.sleep = patch.object(lb.time, 'sleep').start()
        self.output = redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)
        self.addCleanup(patch.stopall)

    def simulate(self, codes):
        calls = []
        def execute(args, **kwargs):
            self.assertFalse(self.destination.exists(), 'remove partial file before retry')
            self.assertNotIn('-k', args)
            self.assertEqual(kwargs['timeout'], 190)
            self.destination.write_bytes(b'complete' if codes[len(calls)] == 0 else b'partial')
            calls.append(args)
            return subprocess.CompletedProcess(args, codes[len(calls)-1], '',
                                               'curl: (56) Recv failure: Connection reset by peer')
        return calls, execute

    def test_receive_error_retries_http1_and_replaces_partial_download(self):
        calls, execute = self.simulate([56, 0])
        with patch.object(lb.subprocess, 'run', side_effect=execute):
            lb.download('https://github.com/example', self.destination)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('-4', calls[0])
        self.assertNotIn('-6', calls[0])
        self.assertIn('--http1.1', calls[1])
        self.assertEqual(self.destination.read_bytes(), b'complete')

    def test_ipv6_remains_available_after_ipv4_failure(self):
        calls, execute = self.simulate([56, 56, 7, 0])
        with patch.object(lb.subprocess, 'run', side_effect=execute):
            lb.download('https://github.com/example', self.destination)
        self.assertIn('-4', calls[2])
        self.assertIn('-6', calls[3])

    def test_exhausted_retries_explain_download_error_and_remove_partial(self):
        calls, execute = self.simulate([56] * 4)
        with patch.object(lb.subprocess, 'run', side_effect=execute):
            with self.assertRaises(lb.Error) as caught:
                lb.download('https://github.com/example', self.destination)
        self.assertEqual(len(calls), 4)
        message = str(caught.exception)
        self.assertIn('curl 56', message)
        self.assertIn('Connection reset by peer', message)
        self.assertIn('https://github.com/example', message)
        self.assertNotIn('服务日志', message)
        self.assertFalse(self.destination.exists())

    def test_certificate_failure_stops_without_insecure_retry(self):
        calls, execute = self.simulate([60])
        with patch.object(lb.subprocess, 'run', side_effect=execute):
            with self.assertRaises(lb.Error):
                lb.download('https://github.com/example', self.destination)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.destination.exists())

    def test_process_timeout_retries(self):
        results = [subprocess.TimeoutExpired('curl', 190),
                   subprocess.CompletedProcess(['curl'], 0, '', '')]
        with patch.object(lb.subprocess, 'run', side_effect=results) as run:
            lb.download('https://github.com/example', self.destination)
        self.assertEqual(run.call_count, 2)

    def test_checksum_mismatch_never_extracts_binary(self):
        def wrong_download(_url, target):
            Path(target).write_bytes(b'wrong archive')
        with patch.object(lb, 'download', side_effect=wrong_download):
            with self.assertRaisesRegex(lb.Error, 'SHA-256'):
                lb.fetch_core(self.tmp.name, 'alpine', 'x86_64')
        self.assertFalse(self.destination.exists())
        self.assertFalse((Path(self.tmp.name) / 'sing-box').exists())

    def test_installer_digest_and_branding_match_manager(self):
        source = (root / 'linkbox.py').read_bytes()
        installer = (root / 'install.sh').read_text()
        self.assertIn('MANAGER_SHA=' + hashlib.sha256(source).hexdigest(), installer)
        for path in ('linkbox.py', 'install.sh', 'README.md'):
            content = (root / path).read_text()
            self.assertNotIn('LinkBox', content)
            self.assertIn('LINKBOX', content)


class BootstrapDownloadTests(unittest.TestCase):
    def exercise(self, directory, failures, code):
        installer = (root / 'install.sh').read_text()
        function = installer.split('download_manager() {', 1)[1].split('\ndownload_manager\n', 1)[0]
        script = r'''
set -eu
TASK_TMP=$1
MANAGER_URL=https://raw.githubusercontent.com/example
FAIL_COUNT=$2
FAIL_CODE=$3
count=0
fail() { printf '%s\n' "$*" >&2; exit 1; }
sleep() { :; }
curl() {
    count=$((count + 1))
    printf '%s\n' "$*" >> "$TASK_TMP/calls"
    [ ! -e "$TASK_TMP/linkbox.py" ] || return 99
    printf 'download-%s' "$count" > "$TASK_TMP/linkbox.py"
    if [ "$count" -le "$FAIL_COUNT" ]; then
        printf 'Recv failure: Connection reset by peer\n' >&2
        return "$FAIL_CODE"
    fi
    return 0
}
download_manager() {''' + function + '\ndownload_manager\n'
        return subprocess.run(['sh', '-s', '--', str(directory), str(failures), str(code)],
                              input=script, text=True, capture_output=True, timeout=10)

    def test_bootstrap_receive_error_retries_under_set_e(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.exercise(tmp, 1, 56)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((Path(tmp) / 'linkbox.py').read_text(), 'download-2')
            calls = (Path(tmp) / 'calls').read_text().splitlines()
            self.assertEqual(len(calls), 2)
            self.assertIn('--http1.1', calls[1])

    def test_bootstrap_exhaustion_explains_failure_and_cleans_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.exercise(tmp, 4, 56)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('curl 56', result.stderr)
            self.assertIn('Connection reset by peer', result.stderr)
            self.assertNotIn('服务日志', result.stderr)
            self.assertFalse((Path(tmp) / 'linkbox.py').exists())
            calls = (Path(tmp) / 'calls').read_text().splitlines()
            self.assertEqual(len(calls), 4)
            self.assertIn(' -4 ', calls[2])
            self.assertIn(' -6 ', calls[3])

    def test_bootstrap_certificate_failure_stops_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.exercise(tmp, 4, 60)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(len((Path(tmp) / 'calls').read_text().splitlines()), 1)
            self.assertFalse((Path(tmp) / 'linkbox.py').exists())


if __name__ == '__main__':
    unittest.main()
