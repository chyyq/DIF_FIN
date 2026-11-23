"""Public-data publishing checks, with only temporary local Git remotes."""
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.publish_data import PublishError, check_data_sizes, compact_audit, publish, push_error


def audit_bytes(data):
    manifest = data / 'audit-archive/manifest.json'
    segments = json.loads(manifest.read_text(encoding='utf-8'))['segments'] if manifest.exists() else []
    return b''.join(gzip.decompress((manifest.parent / s['file']).read_bytes()) for s in segments) + (data / 'audit.jsonl').read_bytes()


def run(root, *args):
    result = subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class AuditRotationTests(unittest.TestCase):
    def test_archive_is_lossless_ordered_and_safe_to_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            original = b''.join((json.dumps({'id': i, 'text': 'research-only'}) + '\n').encode() for i in range(50))
            (data / 'audit.jsonl').write_bytes(original)
            stats = compact_audit(data, segment_bytes=120)
            self.assertGreater(stats['archivedSegments'], 0)
            self.assertLessEqual(stats['activeBytes'], 120)
            self.assertEqual(audit_bytes(data), original)
            manifest = json.loads((data / 'audit-archive/manifest.json').read_text())
            for segment in manifest['segments']:
                restored = gzip.decompress((data / 'audit-archive' / segment['file']).read_bytes())
                self.assertEqual(hashlib.sha256(restored).hexdigest(), segment['sha256'])
            before = (data / 'audit-archive/manifest.json').read_bytes()
            self.assertEqual(compact_audit(data, 120)['archivedSegments'], 0)
            self.assertEqual((data / 'audit-archive/manifest.json').read_bytes(), before)
            additional = b'new recommendation\n' * 40
            with (data / 'audit.jsonl').open('ab') as stream:
                stream.write(additional)
            compact_audit(data, 120)
            self.assertEqual(audit_bytes(data), original + additional)

    def test_a_single_overlarge_record_retains_original_for_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            original = b'first\n' + b'x' * 200 + b'\n'
            (data / 'audit.jsonl').write_bytes(original)
            with self.assertRaises(PublishError) as context:
                compact_audit(data, 100)
            self.assertEqual(context.exception.code, 'AUDIT_RECORD_TOO_LARGE')
            self.assertEqual((data / 'audit.jsonl').read_bytes(), original)

    def test_all_other_public_files_are_checked_before_git(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / 'history-cache.json').write_bytes(b'x' * 101)
            with self.assertRaises(PublishError) as context:
                check_data_sizes(data, maximum=100)
            self.assertEqual(context.exception.code, 'GIT_FILE_TOO_LARGE')
            self.assertIn('history-cache.json', str(context.exception))


class PublishingTests(unittest.TestCase):
    def setup_remote(self, temporary):
        temporary = Path(temporary)
        remote, checkout = temporary / 'remote.git', temporary / 'checkout'
        remote.mkdir(); checkout.mkdir()
        run(remote, 'init', '--bare')
        run(checkout, 'init', '-b', 'main')
        run(checkout, 'config', 'user.name', 'Local test')
        run(checkout, 'config', 'user.email', 'local@example.invalid')
        run(checkout, 'remote', 'add', 'origin', str(remote))
        (checkout / 'app.js').write_text('baseline', encoding='utf-8')
        (checkout / 'data').mkdir()
        (checkout / 'data/latest.json').write_text('{"day":1}', encoding='utf-8')
        run(checkout, 'add', '.')
        run(checkout, 'commit', '-m', 'Baseline')
        run(checkout, 'push', '-u', 'origin', 'main')
        return remote, checkout

    def test_local_bare_push_updates_public_data_but_not_unstaged_source(self):
        with tempfile.TemporaryDirectory() as directory:
            remote, checkout = self.setup_remote(directory)
            (checkout / 'data/latest.json').write_text('{"day":2}', encoding='utf-8')
            (checkout / 'app.js').write_text('private local work', encoding='utf-8')
            result = publish(checkout, push=True)
            self.assertEqual(result['status'], 'PUSHED')
            self.assertEqual(run(remote, 'show', 'main:data/latest.json'), '{"day":2}')
            self.assertEqual(run(remote, 'show', 'main:app.js'), 'baseline')
            self.assertEqual(publish(checkout, push=True)['status'], 'NO_CHANGES')

    def test_large_audit_is_compacted_before_git_size_guard_and_push(self):
        with tempfile.TemporaryDirectory() as directory:
            remote, checkout = self.setup_remote(directory)
            # This uncompressed input exceeds the publisher's 90 MiB guard.
            # It is entirely synthetic public research, not a private ledger.
            line = b'{"research":"' + b'x' * 1017 + b'"}\n'
            data = checkout / 'data'
            digest = hashlib.sha256()
            with (data / 'audit.jsonl').open('wb') as stream:
                for _ in range(93000):
                    stream.write(line)
                    digest.update(line)
            self.assertGreater((data / 'audit.jsonl').stat().st_size, 90 * 1024 * 1024)
            result = publish(checkout, push=True)
            self.assertEqual(result['status'], 'PUSHED')
            self.assertGreater(result['archivedSegments'], 0)
            self.assertLessEqual((data / 'audit.jsonl').stat().st_size, 8 * 1024 * 1024)
            restored = hashlib.sha256()
            manifest = json.loads((data / 'audit-archive/manifest.json').read_text())
            for segment in manifest['segments']:
                restored.update(gzip.decompress((data / 'audit-archive' / segment['file']).read_bytes()))
            restored.update((data / 'audit.jsonl').read_bytes())
            self.assertEqual(restored.hexdigest(), digest.hexdigest())
            self.assertIn('manifest.json', run(remote, 'ls-tree', '-r', '--name-only', 'main'))

    def test_staged_source_is_refused_before_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            remote, checkout = self.setup_remote(directory)
            original = run(checkout, 'rev-parse', 'HEAD')
            (checkout / 'app.js').write_text('unrelated source', encoding='utf-8')
            run(checkout, 'add', 'app.js')
            with self.assertRaises(PublishError) as context:
                publish(checkout, push=True)
            self.assertEqual(context.exception.code, 'UNRELATED_STAGED_FILES')
            self.assertEqual(run(checkout, 'rev-parse', 'HEAD'), original)

    def test_competing_remote_data_is_never_force_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            remote, checkout = self.setup_remote(directory)
            other = Path(directory) / 'other'
            run(Path(directory), 'clone', '--branch', 'main', str(remote), str(other))
            run(other, 'config', 'user.name', 'Concurrent test')
            run(other, 'config', 'user.email', 'other@example.invalid')
            (other / 'data/latest.json').write_text('{"day":99}', encoding='utf-8')
            run(other, 'add', 'data')
            run(other, 'commit', '-m', 'Concurrent newer data')
            run(other, 'push')
            newest = run(remote, 'rev-parse', 'main')
            (checkout / 'data/latest.json').write_text('{"day":2}', encoding='utf-8')
            with self.assertRaises(PublishError) as context:
                publish(checkout, push=True)
            self.assertEqual(context.exception.code, 'REMOTE_ADVANCED_RERUN')
            self.assertEqual(run(remote, 'rev-parse', 'main'), newest)
            self.assertEqual(run(remote, 'show', 'main:data/latest.json'), '{"day":99}')

    def test_specific_remote_failures_have_visible_reason_codes(self):
        self.assertEqual(push_error('remote: error: GH001: Large files detected'), 'GIT_FILE_TOO_LARGE')
        self.assertEqual(push_error('remote: Write access to repository not granted. 403'), 'GIT_WRITE_PERMISSION')
        self.assertEqual(push_error('remote: error: GH013: Repository rule violations'), 'BRANCH_RULE_REJECTED')


if __name__ == '__main__':
    unittest.main()
