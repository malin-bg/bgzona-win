"""Resolve one owner-pinned PROD CID against GitHub's independent D3 ledger."""
from base64 import b64decode
from hashlib import sha1
import json
import re

from artifact_seal import require
from media_support import data, digest

CONTROL = 'malin-bg/bgzona-github-lab'
ASSETS = 'malin-bg/bgzona-assets'
LEDGER = 'heads/automation-ledger'
WITNESS = 'bgzona/witness'
LIMIT = 2_000_000


def hash_string(value, width):
    return type(value) is str and re.fullmatch(f'[0-9a-f]{{{width}}}', value) is not None


def provider(get, repo, path):
    suffix = '/' + path if path else ''
    return json.loads(get(f'https://api.github.com/repos/{repo}{suffix}', LIMIT))


def check_repo(get, name, expected_id):
    row = provider(get, name, '')
    require(type(expected_id) is int and expected_id > 0 and
            (row['full_name'], row['id'], row['private'], row['archived']) ==
            (name, expected_id, False, False), 'CID_REPOSITORY')


def head(get, repo, ref):
    row = provider(get, repo, 'git/ref/' + ref)
    oid = row['object']['sha']
    require(row['ref'] == 'refs/' + ref and row['object']['type'] == 'commit' and
            hash_string(oid, 40), 'CID_REF')
    return oid


def blob_at(get, repo, path, commit):
    row = provider(get, repo, f'contents/{path}?ref={commit}')
    require((row['path'], row['type'], row['encoding']) == (path, 'file', 'base64'), 'CID_BLOB')
    raw = b64decode(''.join(row['content'].splitlines()), validate=True)
    oid = sha1(f'blob {len(raw)}\0'.encode() + raw).hexdigest()
    require(row['sha'] == oid and row['size'] == len(raw), 'CID_BLOB')
    return raw


def check_record(record, raw, index, previous):
    require(set(record) == {'schema', 'index', 'previous', 'library_id', 'registry_seq', 'counters',
            'registry_sha256', 'primary_main', 'primary_witness', 'primary_base',
            'phase', 'operation', 'backup'} and record['schema'] == 1 and
            type(record['index']) is int and record['index'] == index and
            record['previous'] == previous and data(record) == raw, 'CID_LEDGER_RECORD')
    require(type(record['registry_seq']) is int and record['registry_seq'] >= 0 and
            hash_string(record['library_id'], 32) and
            hash_string(record['registry_sha256'], 64) and record['phase'] in ('accepted', 'staged') and
            all(hash_string(record[k], 40) for k in ('primary_main', 'primary_witness', 'primary_base')) and
            set(record['counters']) == {'kolaji', 'poslaniya'} and
            all(type(v) is int and 0 <= v <= 99999 for v in record['counters'].values()), 'CID_LEDGER_FIELDS')
    b = record['backup']
    require(set(b) == {'file', 'sha256', 'bytes', 'full'} and hash_string(b['sha256'], 64) and
            b['file'] == b['sha256'] + '.bundle' and type(b['bytes']) is int and b['bytes'] > 0 and
            type(b['full']) is bool and (index != 0 or b['full']), 'CID_LEDGER_BACKUP')
    require(record['operation'] == digest(data({'library': record['library_id'],
            'seq': record['registry_seq'], 'registry': record['registry_sha256']})), 'CID_LEDGER_OPERATION')


"""Bounded Git tree/object equivalent of the existing D3 history verifier."""
def git_object(kind, raw):
    return sha1(kind.encode() + b' ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def git_tree(rows):
    return git_object('tree', b''.join(mode.encode() + b' ' + name.encode('ascii') + b'\0' +
            bytes.fromhex(oid) for name, mode, oid in rows))


def ledger_objects(get, tip):
    """Read one pinned Git history and bind its archive to every historical tree.

    No acceptance cache. Only immutable identifiers are reused in this call;
    resolve/check_heads retain their independent fresh authority checks.
    """
    from io import BytesIO
    from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED
    import stat
    require(hash_string(tip, 40), 'CID_LEDGER_COMMIT')
    chain = []
    wanted = tip
    for page in range(1, 4):
        rows = provider(get, CONTROL, f'commits?sha={tip}&per_page=100&page={page}')
        require(type(rows) is list and 0 < len(rows) <= 100, 'CID_LEDGER_PAGE')
        for pos, row in enumerate(rows):
            require(len(chain) < 256, 'CID_LEDGER_LIMIT')
            require(row['sha'] == wanted and hash_string(wanted, 40) and
                    all(old['sha'] != wanted for old in chain), 'CID_LEDGER_COMMIT')
            tree = row['commit']['tree']['sha']
            require(hash_string(tree, 40), 'CID_LEDGER_TREE')
            chain.append(dict(sha=wanted, tree=tree))
            parents = row['parents']
            require(type(parents) is list, 'CID_LEDGER_PARENT')
            if not parents:
                require(pos == len(rows)-1, 'CID_LEDGER_PAGE')
                break
            require(len(parents) == 1 and hash_string(parents[0]['sha'], 40), 'CID_LEDGER_PARENT')
            wanted = parents[0]['sha']
        else:
            require(len(rows) == 100, 'CID_LEDGER_GENESIS')
            continue
        break
    else:
        raise RuntimeError('CID_LEDGER_LIMIT')
    count = len(chain)
    root = provider(get, CONTROL, 'git/trees/' + chain[0]['tree'] + '?recursive=1')
    require(root['sha'] == chain[0]['tree'] and root['truncated'] is False and
            type(root['tree']) is list and len(root['tree']) == count+2, 'CID_LEDGER_TREE')
    paths = {'anchor.json', 'records', *(f'records/{i:08d}.json' for i in range(count))}
    meta = {}
    for row in root['tree']:
        path = row['path']
        require(path in paths and path not in meta and hash_string(row['sha'], 40), 'CID_LEDGER_TREE_PATH')
        if path == 'records':
            require((row['mode'], row['type']) == ('040000', 'tree'), 'CID_LEDGER_TREE_MODE')
        else:
            require((row['mode'], row['type']) == ('100644', 'blob') and
                    type(row['size']) is int and 0 < row['size'] <= 32000, 'CID_LEDGER_TREE_MODE')
        meta[path] = row
    require(set(meta) == paths, 'CID_LEDGER_TREE_PATH')
    # Bounded public source transport. Its ZIP bytes are never an authority.
    wire = get('https://codeload.github.com/' + CONTROL + '/zip/' + tip, 16_000_000)
    require(0 < len(wire) <= 16_000_000, 'CID_LEDGER_ARCHIVE_SIZE')
    prefix = CONTROL.split('/')[1] + '-' + tip + '/'
    buffers = {}
    with ZipFile(BytesIO(wire)) as archive:
        infos = archive.infolist()
        require(len(infos) <= count+4 and sum(i.file_size for i in infos) <= (count+1)*32000,
                'CID_LEDGER_ARCHIVE_SIZE')
        seen = set()
        for info in infos:
            require(info.filename.startswith(prefix) and info.filename not in seen and
                    not info.flag_bits & 1 and info.compress_type in (ZIP_STORED, ZIP_DEFLATED),
                    'CID_LEDGER_ARCHIVE_MEMBER')
            seen.add(info.filename)
            name = info.filename[len(prefix):]
            mode = info.external_attr >> 16
            if info.is_dir():
                require(name in ('', 'records/') and stat.S_IFMT(mode) in (0, stat.S_IFDIR),
                        'CID_LEDGER_ARCHIVE_MEMBER')
                continue
            require(name in paths and name != 'records' and
                    stat.S_IFMT(mode) in (0, stat.S_IFREG) and
                    info.file_size == meta[name]['size'] <= 32000, 'CID_LEDGER_ARCHIVE_MEMBER')
            raw = archive.read(info)
            require(len(raw) == info.file_size and git_object('blob', raw) == meta[name]['sha'],
                    'CID_BLOB')
            buffers[name] = raw
    require(set(buffers) == paths-{'records'}, 'CID_LEDGER_ARCHIVE_MEMBERS')
    records = [buffers[f'records/{i:08d}.json'] for i in range(count)]
    require(buffers['anchor.json'] == records[-1], 'CID_LEDGER_HISTORY')
    entries = []
    for i, commit in enumerate(reversed(chain)):
        oid = meta[f'records/{i:08d}.json']['sha']
        entries.append((f'{i:08d}.json', '100644', oid))
        records_tree = git_tree(entries)
        calculated = git_tree([('anchor.json', '100644', oid), ('records', '40000', records_tree)])
        require(calculated == commit['tree'], 'CID_LEDGER_HISTORY')
    require(records_tree == meta['records']['sha'], 'CID_LEDGER_TREE')
    return records


def verify_ledger(get, tip, genesis, expected):
    previous = None
    old = None
    for i, raw in enumerate(ledger_objects(get, tip)):
        row = json.loads(raw)
        check_record(row, raw, i, previous)
        previous = digest(raw)
        if old is None:
            require(previous == genesis, 'CID_LEDGER_GENESIS')
        else:
            require(row['library_id'] == old['library_id'] and row['registry_seq'] > old['registry_seq']
                    and all(row['counters'][k] >= old['counters'][k] for k in row['counters']),
                    'CID_LEDGER_REGRESSION')
        old = row
    require(previous == expected and old['phase'] == 'staged', 'CID_LEDGER_FINAL')
    return old


def check_heads(get, accepted):
    require(head(get, CONTROL, LEDGER) == accepted['ledger_commit'] and
            head(get, ASSETS, 'heads/main') == accepted['assets_tip'] and
            head(get, ASSETS, WITNESS) == accepted['witness_commit'], 'CID_STALE')


def protected_trees(get, commit):
    row = provider(get, ASSETS, 'git/commits/' + commit)
    tree = row['tree']['sha']
    require(row['sha'] == commit and hash_string(tree, 40), 'CID_AUTHORITY_TREE')
    root = provider(get, ASSETS, 'git/trees/' + tree)
    require(root['sha'] == tree and root['truncated'] is False, 'CID_AUTHORITY_TREE')
    paths = {x['path']: x['sha'] for x in root['tree'] if x['path'] in ('automation-state', 'objects')
             and x['type'] == 'tree' and x['mode'] == '040000' and hash_string(x['sha'], 40)}
    require(set(paths) == {'automation-state', 'objects'}, 'CID_AUTHORITY_TREE')
    return paths


def verify_materialized_authority(get, accepted):
    authority, tip = accepted['authority_commit'], accepted['assets_tip']
    if tip == authority:
        return
    comp = provider(get, ASSETS, f'compare/{authority}...{tip}')
    require(comp['status'] == 'ahead' and comp['ahead_by'] > 0 and
            comp['base_commit']['sha'] == comp['merge_base_commit']['sha'] == authority and
            protected_trees(get, authority) == protected_trees(get, tip), 'CID_AUTHORITY_DIVERGED')


def resolve(config, cid, get):
    """Consume an explicitly selected CID from reviewed PROD source only."""
    require(set(config) == {'schema', 'control_id', 'assets_id', 'ledger_genesis', 'cid', 'accepted'} and
            config['schema'] == 1 and hash_string(cid, 64) and cid == config['cid'] and
            hash_string(config['ledger_genesis'], 64), 'CID_TRUST')
    a = config['accepted']
    require(set(a) == {'channel', 'ledger_commit', 'record_sha256', 'authority_commit',
            'witness_commit', 'assets_tip', 'registry_sha256', 'input'} and a['channel'] == 'prod' and
            digest(data(a)) == cid and
            all(hash_string(a[k], 40) for k in ('ledger_commit', 'authority_commit', 'witness_commit', 'assets_tip')) and
            all(hash_string(a[k], 64) for k in ('record_sha256', 'registry_sha256')), 'CID_IDENTITY')
    check_repo(get, CONTROL, config['control_id'])
    check_repo(get, ASSETS, config['assets_id'])
    check_heads(get, a)
    r = verify_ledger(get, a['ledger_commit'], config['ledger_genesis'], a['record_sha256'])
    require((r['primary_main'], r['primary_witness'], r['registry_sha256']) ==
            (a['authority_commit'], a['witness_commit'], a['registry_sha256']), 'CID_AUTHORITY')
    verify_materialized_authority(get, a)
    registry = blob_at(get, ASSETS, 'automation-state/registry.json', a['authority_commit'])
    require(digest(registry) == a['registry_sha256'], 'CID_REGISTRY')
    reg = json.loads(registry)
    require(data(reg) == registry and reg['schema'] == 1 and
            (reg['library_id'], reg['seq'], reg['counters'], reg['transaction_status']) ==
            (r['library_id'], r['registry_seq'], r['counters'], 'staged'), 'CID_REGISTRY')
    w = json.loads(blob_at(get, ASSETS, 'witness.json', a['witness_commit']))
    require(set(w) == {'schema', 'authority', 'library_id', 'seq', 'counters', 'registry_sha256', 'previous'}
            and w['schema'] == 1 and (w['authority'], w['library_id'], w['seq'], w['counters'], w['registry_sha256']) ==
            (a['authority_commit'], r['library_id'], r['registry_seq'], r['counters'], a['registry_sha256']),
            'CID_WITNESS')
    check_heads(get, a)
    verify_dev(get,a)
    return a, reg


def verify_projection(directory, registry):
    """Match the existing Store.project catalog and object set to the D3 registry."""
    rows = []
    expected = {}
    for key, item in sorted(registry['items'].items()):
        require(key == item['id'], 'CID_CATALOG_ID')
        row = {k: item[k] for k in ('id', 'collection', 'page_url', 'state')}
        if item['state'] == 'published':
            rev = item['revisions'][-1]
            require(rev['number'] == item['desired_revision'], 'CID_CATALOG_REVISION')
            row.update(revision=rev['number'], original=rev['public_path'], sha256=rev['sha256'],
                       description=item['description'], slideshow=item['slideshow'])
            expected[rev['public_path'].lstrip('/')] = rev['sha256']
            if item['og']:
                og = item['og']
                path = f'{item["collection"]}/og/{key}-{og["sha256"][:12]}{og["ext"]}'
                row['og'] = '/' + path
                expected[path] = og['sha256']
        rows.append(row)
    cat = data({'schema': 1, 'items': rows})
    expected['catalog.json'] = digest(cat)
    require((directory / 'artifact' / 'catalog.json').read_bytes() == cat and
            json.loads((directory / 'expected-files.json').read_bytes()) == expected, 'CID_CATALOG_PROJECTION')



# A consumer CID is channel-specific. This consumes the same D3 record and
# existing immutable projection; it never appends or filters authority content.
PROD='malin-bg/bgzona-win'
DEV='malin-bg/bgzona-win-dev'


def verify_dev(get,accepted):
    dev=json.loads(data(accepted));dev['channel']='dev';dev['input']['repository']=DEV
    wanted=digest(data(dev))
    version=json.loads(get('https://dev.bgzona.win/media-version.json?cid='+wanted,32000))
    require(version==dict(cid=wanted,channel='dev',registry_sha256=accepted['registry_sha256']), 'PROD_DEV_NOT_ACCEPTED')


def current(config,get):
    """Current accepted media for an ordinary PROD article Push. No old-lock fallback."""
    from io import BytesIO
    from zipfile import ZipFile
    roots={k:config[k] for k in ('schema','control_id','assets_id','ledger_genesis')}
    require(roots['schema']==1 and hash_string(roots['ledger_genesis'],64),'PROD_ROOT')
    check_repo(get,CONTROL,roots['control_id']);check_repo(get,ASSETS,roots['assets_id'])
    tip=head(get,CONTROL,LEDGER);raw=blob_at(get,CONTROL,'anchor.json',tip);r=json.loads(raw)
    key=digest(raw);verify_ledger(get,tip,roots['ledger_genesis'],key)
    release=provider(get,CONTROL,'releases/tags/r18h-dev-'+key)
    require(release['tag_name']=='r18h-dev-'+key and release.get('immutable') is True and
            release['draft'] is False and release['prerelease'] is False and len(release['assets'])==1,'PROD_RELEASE')
    asset,=release['assets'];name='r18h-dev-'+key+'.zip'
    url='https://github.com/'+CONTROL+'/releases/download/'+release['tag_name']+'/'+name
    require(asset['name']==name and asset['state']=='uploaded' and asset['browser_download_url']==url and
            type(asset['size']) is int and 0<asset['size']<=64_000_000 and
            asset['digest'].startswith('sha256:') and hash_string(asset['digest'][7:],64),'PROD_ASSET')
    wire=get(url,asset['size']);require(len(wire)==asset['size'] and digest(wire)==asset['digest'][7:],'PROD_ARCHIVE')
    with ZipFile(BytesIO(wire)) as z:
        infos=z.infolist();require(len(infos)<=10000 and len({i.filename for i in infos})==len(infos),'PROD_ARCHIVE_MEMBERS')
        require(z.getinfo('artifact/catalog.json').file_size<=2_000_000 and z.getinfo('expected-files.json').file_size<=2_000_000,'PROD_ARCHIVE_METADATA')
        catalog=z.read('artifact/catalog.json');expected=z.read('expected-files.json')
    reg=json.loads(blob_at(get,ASSETS,'automation-state/registry.json',r['primary_main']))
    require(digest(data(reg))==r['registry_sha256'],'PROD_REGISTRY')
    rows=[v for v in reg['items'].values() if v['state']=='published']
    counts=dict(published=len(rows),kolaji=sum(v['collection']=='kolaji' for v in rows),
                poslaniya=sum(v['collection']=='poslaniya' for v in rows),
                slides=sum(v['collection']=='kolaji' and v['slideshow'] for v in rows))
    lock=dict(schema=1,repository=PROD,archive_name=name,archive_sha256=digest(wire),bytes=len(wire),members=len(infos),
              source_catalog_sha256=digest(catalog),expected_files_sha256=digest(expected),snapshot=counts,
              provider=dict(release_id=release['id'],asset_id=asset['id'],tag=release['tag_name']))
    a=dict(channel='prod',ledger_commit=tip,record_sha256=key,authority_commit=r['primary_main'],
           witness_commit=r['primary_witness'],assets_tip=r['primary_main'],registry_sha256=r['registry_sha256'],input=lock)
    result=roots|dict(cid=digest(data(a)),accepted=a)
    resolve(result,result['cid'],get)
    return result
