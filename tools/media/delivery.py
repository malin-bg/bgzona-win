"""Single production build/accept/seal entry point. Never deploys or writes source files."""
import argparse
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import urllib.parse
import urllib.request
import zipfile

from artifact_seal import checked_absolute, require, safe_path, seal, verify, inventory
from bridge import derive
from media_support import atomic, data, digest, fhash
from acceptance import validate

REPOSITORY = 'malin-bg/bgzona-win'
BASE = 'https://bgzona.win/'


def read_lock(path):
    lock = json.loads(path.read_bytes().decode('utf-8-sig', 'strict'))
    require(set(lock) == {'schema', 'repository', 'archive_name', 'archive_sha256', 'bytes',
                         'members', 'source_catalog_sha256', 'expected_files_sha256',
                         'snapshot', 'provider'}, 'LOCK_SCHEMA')
    require(lock['schema'] == 1 and lock['repository'] == REPOSITORY, 'LOCK_REPOSITORY')
    safe_path(lock['archive_name'])
    require('/' not in lock['archive_name'] and lock['archive_name'].endswith('.zip'), 'LOCK_NAME')
    for key in ('archive_sha256', 'source_catalog_sha256', 'expected_files_sha256'):
        require(isinstance(lock[key], str) and re.fullmatch('[0-9a-f]{64}', lock[key]), 'LOCK_SHA')
    require(type(lock['bytes']) is int and 0 < lock['bytes'] <= 500_000_000 and
            type(lock['members']) is int and 0 < lock['members'] <= 10000, 'LOCK_LIMITS')
    require(isinstance(lock['snapshot'], dict) and set(lock['snapshot']) ==
            {'published', 'kolaji', 'poslaniya', 'slides'} and
            all(type(v) is int and v >= 0 for v in lock['snapshot'].values()), 'LOCK_COUNTS')
    return lock


def get(url, limit):
    request = urllib.request.Request(url, headers={'User-Agent': 'bgzona-dev-input/1',
                        'Accept': 'application/vnd.github+json' if url.startswith('https://api.github.com/')
                        else 'application/octet-stream'})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read(limit + 1)
    require(len(raw) <= limit, 'PROVIDER_SIZE_LIMIT')
    return raw


def obtain(lock, local_archive=None):
    if local_archive:
        require(os.environ.get('GITHUB_ACTIONS') != 'true', 'LOCAL_OVERRIDE_FORBIDDEN_IN_CI')
        raw = Path(local_archive).read_bytes()
    else:
        provider = lock['provider']
        require(isinstance(provider, dict) and set(provider) == {'repository', 'release_id', 'asset_id', 'tag'},
                'PROVIDER_NOT_ACTIVATED: verified Release asset is required; no fallback')
        require(all(type(provider[k]) is int and provider[k] > 0 for k in ('release_id', 'asset_id'))
                and isinstance(provider['tag'], str) and bool(provider['tag']), 'PROVIDER_INVALID')
        require(isinstance(provider['repository'], str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', provider['repository']), 'PROVIDER_REPOSITORY')
        source_repository = provider['repository']
        api = f'https://api.github.com/repos/{source_repository}/releases/{provider["release_id"]}'
        release = json.loads(get(api, 2_000_000))
        require(release['id'] == provider['release_id'] and release['tag_name'] == provider['tag']
                and release['draft'] is False and release['prerelease'] is False, 'PROVIDER_RELEASE')
        matches = [a for a in release['assets'] if a['id'] == provider['asset_id']]
        require(len(matches) == 1, 'PROVIDER_ASSET')
        asset = matches[0]
        url = (f'https://github.com/{source_repository}/releases/download/' +
               urllib.parse.quote(provider['tag'], safe='') + '/' + lock['archive_name'])
        require(asset['name'] == lock['archive_name'] and asset['size'] == lock['bytes'] and
                asset['state'] == 'uploaded' and asset['browser_download_url'] == url, 'PROVIDER_IDENTITY')
        raw = get(url, lock['bytes'])
    require(len(raw) == lock['bytes'] and digest(raw) == lock['archive_sha256'], 'ARCHIVE_SHA_OR_SIZE')
    return raw


def unpack(raw, lock, destination):
    require(not destination.exists(), 'INPUT_DEST_EXISTS')
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        members = archive.infolist()
        require(len(members) == lock['members'] and
                sum(i.file_size for i in members) <= 500_000_000, 'ARCHIVE_MEMBERS_OR_SIZE')
        names = set()
        buffers = {}
        for info in members:
            safe_path(info.filename)
            require(not info.is_dir() and not info.flag_bits & 1 and
                    not stat.S_ISLNK(info.external_attr >> 16), 'ARCHIVE_MEMBER_TYPE')
            require(info.filename.casefold() not in names, 'ARCHIVE_DUPLICATE')
            names.add(info.filename.casefold())
            require(info.filename.startswith('artifact/') or info.filename in
                    ('expected-files.json', 'provenance.json'), 'ARCHIVE_PRIVATE_OR_EXTRA')
            buffers[info.filename] = archive.read(info)
    require(digest(buffers['expected-files.json']) == lock['expected_files_sha256'] and
            digest(buffers['artifact/catalog.json']) == lock['source_catalog_sha256'], 'INPUT_METADATA_SHA')
    expected = json.loads(buffers['expected-files.json'].decode('utf-8-sig', 'strict'))
    require(isinstance(expected, dict) and {n for n in buffers if n.startswith('artifact/')} ==
            {'artifact/' + n for n in expected}, 'INPUT_EXACT_SET')
    for rel, sha in expected.items():
        safe_path(rel)
        require(digest(buffers['artifact/' + rel]) == sha, 'INPUT_FILE_SHA')
    destination.mkdir(parents=True)
    for rel, content in buffers.items():
        atomic(destination / rel, content)
    # Existing bridge validates catalog, identity, formats, revisions and references.
    manifest, states, catalog = derive(destination / 'artifact', expected)
    rows = manifest['items']
    counts = {'published': len(rows),
              'kolaji': sum(r['collection'] == 'kolaji' for r in rows),
              'poslaniya': sum(r['collection'] == 'poslaniya' for r in rows),
              'slides': sum(r['collection'] == 'kolaji' and r['slideshow'] for r in rows)}
    require(counts == lock['snapshot'], 'SNAPSHOT_COUNTS')
    return manifest, states


def run(command, log):
    q = subprocess.run(command, capture_output=True, text=True, encoding='utf8', errors='replace', timeout=180)
    atomic(log, (json.dumps(command, ensure_ascii=False) + '\n' + q.stdout + q.stderr).encode('utf8'))
    require(q.returncode == 0, 'BUILD_FAILED: ' + log.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--hugo', default='hugo')
    parser.add_argument('--local-archive', type=Path, help='Isolated local verification ONLY; rejected in Actions')
    args = parser.parse_args()
    site = checked_absolute(args.site)
    parent = checked_absolute(args.work.parent)
    work = parent / args.work.name
    require(not work.exists() and not work.is_symlink() and
            not work.is_relative_to(site) and not site.is_relative_to(work) and
            not any((p / '.git').exists() for p in (parent, *parent.parents)), 'NEW_EXTERNAL_WORK_REQUIRED')
    lock = read_lock(site / 'tools/media/input-lock.json')
    raw = obtain(lock, args.local_archive)  # Missing/invalid provider fails before any build.
    work.mkdir()
    manifest, states = unpack(raw, lock, work / 'input')
    # Baseline is rebuilt from CURRENT content, not a historical article/file allowlist.
    atomic(work / 'baseline.toml', ('resourceDir = ' + json.dumps((work / 'baseline-resources').as_posix()) + '\n').encode())
    run([args.hugo, '--source', str(site), '--config', str(site / 'hugo.toml') + ',' + str(work / 'baseline.toml'),
         '--destination', str(work / 'baseline'), '--cacheDir', str(work / 'baseline-cache'),
         '--baseURL', BASE, '--environment', 'production', '--noBuildLock'], work / 'baseline.log')
    candidate = work / 'candidate'
    run([sys.executable, '-B', str(site / 'tools/media/build.py'), '--site', str(site),
         '--artifact', str(work / 'input/artifact'), '--expected-files', str(work / 'input/expected-files.json'),
         '--output-root', str(candidate), '--hugo', args.hugo], work / 'candidate.log')
    # Acceptance and seal are in one trusted caller; arbitrary PASS text is not consulted.
    validate(candidate, work / 'baseline', work / 'acceptance.json')
    require(json.loads((candidate / 'validated-manifest.json').read_bytes()) == manifest, 'MANIFEST_CHANGED')
    tree = seal(candidate / 'public', work / 'site-seal.json')
    verify(candidate / 'public', work / 'site-seal.json', tree)
    atomic(work / 'site-tree.sha256', (tree + '\n').encode('ascii'))
    receipt = {'status': 'ACCEPTED_SEALED_UNDEPLOYED', 'archive_sha256': lock['archive_sha256'],
               'tree_sha256': tree, 'snapshot': lock['snapshot'],
               'mode': 'LOCAL_INPUT_TEST' if args.local_archive else 'RELEASE_PROVIDER', 'deployment': False}
    atomic(work / 'delivery-receipt.json', data(receipt))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf8') as stream:
            stream.write(f'### Production acceptance PASS\n\n{lock["snapshot"]["published"]} media pages; '
                         f'{lock["snapshot"]["slides"]} slides. Site SHA-256: `{tree}`. '
                         'Current articles checked against this source build. No deployment yet.\n')
    print(json.dumps(receipt))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print('DELIVERY_REJECT: ' + str(exc), file=sys.stderr)
        sys.exit(1)
