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
import resolve_input

REPOSITORY = 'malin-bg/bgzona-win'
BASE = 'https://bgzona.win/'


def read_lock(path):
    return validate_lock(json.loads(path.read_bytes().decode('utf-8-sig','strict')))


def validate_lock(lock):
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
    """Bounded public reads; the existing PROD token never leaves fixed API origins."""
    from urllib.error import HTTPError
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    u=urllib.parse.urlsplit(url)
    require(u.scheme=='https' and not u.username and not u.password and u.port in (None,443), 'PROD_READ_ORIGIN')
    api=(u.hostname=='api.github.com' and any(u.path.startswith('/repos/'+r+'/') or u.path=='/repos/'+r
         for r in (resolve_input.CONTROL,resolve_input.ASSETS,REPOSITORY)))
    browser=(u.hostname=='github.com' and u.path.startswith('/'+resolve_input.CONTROL+'/releases/download/'))
    version=(u.hostname=='dev.bgzona.win' and u.path=='/media-version.json' and re.fullmatch('cid=[0-9a-f]{64}',u.query))
    ledger_archive=(u.hostname=='codeload.github.com' and not u.query and not u.fragment and
                    re.fullmatch('/'+resolve_input.CONTROL+'/zip/[0-9a-f]{40}',u.path) is not None)
    require(api or browser or version or ledger_archive,'PROD_READ_SCOPE')
    headers={'User-Agent':'bgzona-prod-input/1','Accept':'application/vnd.github+json' if api else 'application/octet-stream'}
    token=os.environ.get('GH_TOKEN')
    if api and os.environ.get('GITHUB_ACTIONS')=='true':
        require(bool(token),'PROD_READ_AUTH_REQUIRED')
        require(os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('GITHUB_REPOSITORY')==REPOSITORY and
                os.environ.get('GITHUB_REPOSITORY_ID')=='1128312231' and os.environ.get('GITHUB_REF')=='refs/heads/main' and
                os.environ.get('GITHUB_WORKFLOW_REF')==REPOSITORY+'/.github/workflows/hugo.yml@refs/heads/main' and
                os.environ.get('GITHUB_ACTOR_ID') in ('252877943','353092585') and os.environ.get('GITHUB_RUN_ATTEMPT')=='1' and
                re.fullmatch('[0-9a-f]{40}',os.environ.get('GITHUB_WORKFLOW_SHA','')), 'PROD_READ_CONTEXT')
        headers['Authorization']='Bearer '+token
    opener=urllib.request.build_opener(NoRedirect)
    try:
        response=opener.open(urllib.request.Request(url,headers=headers),timeout=30)
    except HTTPError as exc:
        target=exc.headers.get('Location','');v=urllib.parse.urlsplit(target)
        require(browser and exc.code in (301,302,303,307,308) and v.scheme=='https' and
                v.netloc in ('release-assets.githubusercontent.com','objects.githubusercontent.com'), 'PROD_READ_HTTP_OR_REDIRECT')
        # Never forward API credentials or print a signed target.
        try:response=opener.open(urllib.request.Request(target),timeout=30)
        except Exception:raise RuntimeError('PROD_ASSET_READ_FAILED') from None
    except Exception:raise RuntimeError('PROD_API_READ_FAILED') from None
    with response:
        require(response.status==200,'PROD_READ_STATUS');raw=response.read(limit+1)
    require(len(raw)<=limit,'PROVIDER_SIZE_LIMIT')
    return raw


def obtain(lock, local_archive=None, *, provider_repository=REPOSITORY):
    if local_archive:
        require(os.environ.get('GITHUB_ACTIONS') != 'true', 'LOCAL_OVERRIDE_FORBIDDEN_IN_CI')
        raw = Path(local_archive).read_bytes()
    else:
        provider = lock['provider']
        require(isinstance(provider, dict) and set(provider) == {'release_id', 'asset_id', 'tag'},
                'PROVIDER_NOT_ACTIVATED: verified Release asset is required; no fallback')
        require(all(type(provider[k]) is int and provider[k] > 0 for k in ('release_id', 'asset_id'))
                and isinstance(provider['tag'], str) and bool(provider['tag']), 'PROVIDER_INVALID')
        api = f'https://api.github.com/repos/{provider_repository}/releases/{provider["release_id"]}'
        release = json.loads(get(api, 2_000_000))
        require(release['id'] == provider['release_id'] and release['tag_name'] == provider['tag']
                and release['draft'] is False and release['prerelease'] is False, 'PROVIDER_RELEASE')
        if provider_repository == resolve_input.CONTROL:
            require(release.get('immutable') is True, 'PROVIDER_RELEASE_IMMUTABLE')
        matches = [a for a in release['assets'] if a['id'] == provider['asset_id']]
        require(len(matches) == 1, 'PROVIDER_ASSET')
        asset = matches[0]
        url = (f'https://github.com/{provider_repository}/releases/download/' +
               urllib.parse.quote(provider['tag'], safe='') + '/' + lock['archive_name'])
        require(asset['name'] == lock['archive_name'] and asset['size'] == lock['bytes'] and
                asset['state'] == 'uploaded' and asset['browser_download_url'] == url, 'PROVIDER_IDENTITY')
        raw = get(url, lock['bytes'])
    require(len(raw) == lock['bytes'] and digest(raw) == lock['archive_sha256'], 'ARCHIVE_SHA_OR_SIZE')
    return raw


def unpack(raw, lock, destination, *, accepted=False):
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
            allowed = ('expected-files.json',) if accepted else ('expected-files.json', 'provenance.json')
            require(info.filename.startswith('artifact/') or info.filename in allowed, 'ARCHIVE_PRIVATE_OR_EXTRA')
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
    parser.add_argument('--accepted-cid', required=True)
    parser.add_argument('--standard-trust', type=Path, required=True)
    args = parser.parse_args()
    site = checked_absolute(args.site)
    parent = checked_absolute(args.work.parent)
    work = parent / args.work.name
    require(not work.exists() and not work.is_symlink() and
            not work.is_relative_to(site) and not site.is_relative_to(work) and
            not any((p / '.git').exists() for p in (parent, *parent.parents)), 'NEW_EXTERNAL_WORK_REQUIRED')
    require(args.local_archive is None, 'PROD_UNVERIFIED_LOCAL_OVERRIDE')
    require(args.standard_trust.is_file() and not args.standard_trust.is_symlink() and
            args.standard_trust.stat().st_size<=8192, 'PROD_TRUST_FILE')
    wire=args.standard_trust.read_bytes();config=json.loads(wire)
    known=json.loads((site/'tools/media/input-trust.json').read_bytes())
    require(data(config)==wire and all(config[k]==known[k] for k in
            ('schema','control_id','assets_id','ledger_genesis')), 'PROD_TRUST_ROOT')
    accepted,registry=resolve_input.resolve(config,args.accepted_cid,get)
    lock=validate_lock(accepted['input'])
    raw=obtain(lock,provider_repository=resolve_input.CONTROL)
    work.mkdir()
    manifest, states = unpack(raw, lock, work / 'input', accepted=True)
    resolve_input.verify_projection(work/'input',registry)
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
    resolve_input.check_heads(get,accepted)
    version=candidate/'public/media-version.json'
    require(not version.exists() and not version.is_symlink(),'PROD_VERSION_PATH')
    atomic(version,data(dict(cid=args.accepted_cid,channel='prod',registry_sha256=accepted['registry_sha256'])))
    tree = seal(candidate / 'public', work / 'site-seal.json')
    verify(candidate / 'public', work / 'site-seal.json', tree)
    atomic(work / 'site-tree.sha256', (tree + '\n').encode('ascii'))
    receipt = {'status': 'ACCEPTED_SEALED_UNDEPLOYED', 'archive_sha256': lock['archive_sha256'],
               'tree_sha256': tree, 'snapshot': lock['snapshot'],
               'mode': 'LOCAL_INPUT_TEST' if args.local_archive else 'RELEASE_PROVIDER', 'deployment': False}
    receipt.update(cid=args.accepted_cid,mode='INDEPENDENT_D3_CID')
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
