"""Bound and publish public market files; preserve audit bytes in gzip segments.

Only --push commits/pushes, and only data/ is staged. This never rewrites Git
history, forces a push, or reads a private browser ledger.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SEGMENT_BYTES = 8 * 1024 * 1024
MAX_GIT_FILE_BYTES = 90 * 1024 * 1024


class PublishError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def atomic_bytes(path, payload):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def compact_audit(data: Path, segment_bytes=SEGMENT_BYTES):
    """Keep a small final segment; archive every earlier byte without filtering.

    manifest.segments is chronological and may reference an identical content
    digest more than once when the source intentionally repeats identical bytes.
    A reader must respect that order, then read data/audit.jsonl as the tail.
    """
    audit = data / 'audit.jsonl'
    if not audit.is_file() or audit.stat().st_size <= segment_bytes:
        return {'archivedSegments': 0, 'archivedBytes': 0, 'activeBytes': audit.stat().st_size if audit.exists() else 0}
    archive = data / 'audit-archive'
    manifest_path = archive / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {'version': 1, 'segments': []}
    if manifest.get('version') != 1 or not isinstance(manifest.get('segments'), list):
        raise PublishError('AUDIT_MANIFEST_INVALID', 'Cannot extend an unknown audit archive manifest.')
    archive.mkdir(parents=True, exist_ok=True)
    segments = []
    def save_segment(raw):
        digest = hashlib.sha256(raw).hexdigest()
        filename = f'{digest}.jsonl.gz'
        target = archive / filename
        compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        if target.exists():
            if gzip.decompress(target.read_bytes()) != raw:
                raise PublishError('AUDIT_ARCHIVE_COLLISION', f'Existing audit segment does not match its digest: {filename}')
        else:
            atomic_bytes(target, compressed)
        segments.append({'file': filename, 'sha256': digest, 'uncompressedBytes': len(raw), 'compressedBytes': target.stat().st_size})
    buffer = bytearray()
    original_bytes = audit.stat().st_size
    with audit.open('rb') as stream:
        for line in stream:
            if len(line) > segment_bytes:
                raise PublishError('AUDIT_RECORD_TOO_LARGE', f'A single audit record exceeds {segment_bytes} bytes; original audit retained.')
            if buffer and len(buffer) + len(line) > segment_bytes:
                save_segment(bytes(buffer))
                buffer.clear()
            buffer.extend(line)
    # No source replacement occurs until every compressed segment is durable.
    archived_bytes = sum(s['uncompressedBytes'] for s in segments)
    if archived_bytes + len(buffer) != original_bytes:
        raise PublishError('AUDIT_CHANGED_DURING_ROTATION', 'Audit changed while reading; refusing to replace the source.')
    new_manifest = {**manifest, 'segments': manifest['segments'] + segments,
                    'tail': '../audit.jsonl', 'format': 'Ordered gzip JSONL segments, followed by active audit.jsonl; no records discarded'}
    tail_tmp = audit.with_suffix('.jsonl.tmp')
    manifest_tmp = manifest_path.with_suffix('.json.tmp')
    tail_tmp.write_bytes(buffer)
    manifest_tmp.write_text(json.dumps(new_manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # The caller makes one Git commit only after both replacements succeed;
    # an interrupted Actions job therefore never publishes a partial rotation.
    os.replace(tail_tmp, audit)
    os.replace(manifest_tmp, manifest_path)
    return {'archivedSegments': len(segments), 'archivedBytes': archived_bytes, 'activeBytes': len(buffer)}


def check_data_sizes(data, maximum=MAX_GIT_FILE_BYTES):
    for source in data.rglob('*'):
        if source.is_file() and source.stat().st_size > maximum:
            raise PublishError('GIT_FILE_TOO_LARGE', f'{source.relative_to(data.parent).as_posix()} is {source.stat().st_size} bytes; refuses commit before GitHub 100 MiB limit.')


def git(root, *args, timeout=45):
    try:
        return subprocess.run(['git', *args], cwd=root, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PublishError('GIT_TIMEOUT', f'Git {args[0]} exceeded {timeout} seconds.') from exc


def must_git(root, *args):
    result = git(root, *args)
    if result.returncode:
        raise PublishError('GIT_LOCAL_FAILURE', clean_output(result.stderr or result.stdout))
    return result.stdout.strip()


def clean_output(message):
    return re.sub(r'(https?://)[^/@\s]+:[^/@\s]+@', r'\1[redacted]@', message).strip()[:3000]


def push_error(message):
    low = message.lower()
    if any(s in low for s in ('gh001', 'exceeds github', 'file size limit', 'large files detected')):
        return 'GIT_FILE_TOO_LARGE'
    if any(s in low for s in ('permission', '403', 'authentication', 'could not read username', 'write access')):
        return 'GIT_WRITE_PERMISSION'
    if any(s in low for s in ('non-fast-forward', 'fetch first', 'remote contains work')):
        return 'REMOTE_ADVANCED_RERUN'
    if any(s in low for s in ('protected branch', 'gh006', 'repository rule', 'gh013')):
        return 'BRANCH_RULE_REJECTED'
    return 'GIT_PUSH_FAILED'


def publish(root=ROOT, *, push=False, branch='main', remote='origin', attempts=3):
    root = Path(root).resolve()
    data = root / 'data'
    if not data.is_dir():
        raise PublishError('PUBLIC_DATA_MISSING', 'No data directory exists in the selected project.')
    rotation = compact_audit(data)
    check_data_sizes(data)
    print(json.dumps({'phase': 'PUBLIC_DATA_CHECKED', **rotation}), flush=True)
    if not push:
        return {'status': 'CHECKED_NO_PUSH', **rotation}
    git_root = Path(must_git(root, 'rev-parse', '--show-toplevel')).resolve()
    if git_root != root:
        raise PublishError('GIT_ROOT_MISMATCH', 'Selected project must be the Git repository root.')
    staged = must_git(root, 'diff', '--cached', '--name-only', '-z').split('\x00')
    if any(name and not name.startswith('data/') for name in staged):
        raise PublishError('UNRELATED_STAGED_FILES', 'Refusing to commit staged files outside public data/.')
    must_git(root, 'config', 'user.name', 'github-actions[bot]')
    must_git(root, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    must_git(root, 'add', '--', 'data')
    changed = git(root, 'diff', '--cached', '--quiet')
    if changed.returncode == 0:
        return {'status': 'NO_CHANGES', **rotation}
    if changed.returncode != 1:
        raise PublishError('GIT_LOCAL_FAILURE', clean_output(changed.stderr))
    must_git(root, 'commit', '-m', 'Update LBR market data')
    last_message = ''
    for attempt in range(1, attempts + 1):
        print(json.dumps({'phase': 'GIT_PUSH', 'attempt': attempt}), flush=True)
        try:
            result = git(root, 'push', remote, f'HEAD:refs/heads/{branch}')
            last_message = clean_output(result.stderr or result.stdout)
            if result.returncode == 0:
                return {'status': 'PUSHED', **rotation}
            code = push_error(last_message)
        except PublishError as exc:
            code, last_message = exc.code, str(exc)
        # Retrying a permission, file-size or stale-main rejection cannot help.
        # Never force-push or blindly merge a competing day's market snapshot.
        if code not in ('GIT_TIMEOUT', 'GIT_PUSH_FAILED'):
            break
        if attempt < attempts:
            time.sleep(2)
    hints = {'GIT_WRITE_PERMISSION': 'Check Actions workflow write permissions and repository policies.',
             'REMOTE_ADVANCED_RERUN': 'main changed during generation; rerun the workflow from current main.',
             'GIT_FILE_TOO_LARGE': 'A public file crossed the GitHub size limit; inspect the named file.',
             'BRANCH_RULE_REJECTED': 'Repository rules rejected the bot commit; inspect the active branch rule.'}
    raise PublishError(code, f'{hints.get(code, "Push did not complete within the bounded attempts.")} {last_message}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--push', action='store_true', help='Commit public data and push (Actions only; omitted means local check/rotation).')
    args = parser.parse_args()
    try:
        result = publish(args.root, push=args.push)
        print(json.dumps(result), flush=True)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
                stream.write(f'### Public market data\n\nStatus: {result["status"]}\n\nAudit segments archived: {result["archivedSegments"]}; active audit bytes: {result["activeBytes"]}.\n')
    except PublishError as exc:
        message = str(exc).replace('\r', ' ').replace('\n', ' ')
        print(f'::error title=LBR {exc.code}::{message}', file=sys.stderr, flush=True)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
                stream.write(f'### Publish failed: {exc.code}\n\n{message}\n')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
