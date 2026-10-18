"""Python 3.10+ / stdlib. Integrity only, never acceptance or authorization.

Trusted caller: run relevant acceptance on the current quiescent candidate,
then seal it. No PASS flag, receipt or text is interpreted as approval here.
Keep manifest AND expected digest in the trusted pipeline context, outside root.
Verify immediately before handing exactly this artifact to the publisher.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

HEX = re.compile(r'[0-9a-f]{64}\Z')
RESERVED = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
            *(f'LPT{i}' for i in range(1, 10))}


class Rejected(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise Rejected(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf8')


def safe_path(value):
    require(isinstance(value, str) and bool(value), 'UNSAFE_PATH')
    require(not any(ord(c) < 32 or ord(c) == 127 or c in '\\:<>"|?*' for c in value),
            'UNSAFE_PATH: ' + repr(value))
    for part in value.split('/'):
        require(part not in ('', '.', '..') and not part.endswith((' ', '.')) and
                part.split('.')[0].upper() not in RESERVED, 'UNSAFE_PATH: ' + repr(value))
    value.encode('utf8', 'strict')


def no_link(info):
    require(not stat.S_ISLNK(info.st_mode) and not
            (getattr(info, 'st_file_attributes', 0) & 0x400), 'LINK_OR_REPARSE_POINT')


def checked_absolute(path):
    path = Path(os.path.abspath(path))  # Do not resolve away symlink evidence.
    for ancestor in reversed((path, *path.parents)):
        no_link(ancestor.lstat())
    return path


def fingerprint(info):
    # Windows Python may expose different ctime semantics via lstat and fstat.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def inventory(root):
    root = checked_absolute(root)
    require(root.is_dir(), 'ROOT_NOT_DIRECTORY')
    rows, seen = [], set()

    def walk(folder):
        before = folder.lstat()
        no_link(before)
        require(stat.S_ISDIR(before.st_mode), 'NOT_DIRECTORY')
        with os.scandir(folder) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            p = Path(entry.path)
            rel = p.relative_to(root).as_posix()
            safe_path(rel)
            require(rel.casefold() not in seen, 'CASE_COLLISION')
            seen.add(rel.casefold())
            info = p.lstat()
            no_link(info)
            if stat.S_ISDIR(info.st_mode):
                walk(p)
                continue
            require(stat.S_ISREG(info.st_mode), 'NOT_REGULAR_FILE')
            # Compare pathname and open handle, including stability during reading.
            flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            with os.fdopen(os.open(p, flags), 'rb') as stream:
                opened = os.fstat(stream.fileno())
                no_link(opened)
                require(stat.S_ISREG(opened.st_mode) and
                        fingerprint(opened) == fingerprint(info), 'FILE_CHANGED_DURING_READ')
                digest = hashlib.sha256()
                size = 0
                while block := stream.read(1024 * 1024):
                    digest.update(block)
                    size += len(block)
                require(fingerprint(os.fstat(stream.fileno())) == fingerprint(info) ==
                        fingerprint(p.lstat()) and size == info.st_size,
                        'FILE_CHANGED_DURING_READ')
                no_link(p.lstat())
            rows.append({'path': rel, 'bytes': size, 'sha256': digest.hexdigest()})
        require(fingerprint(folder.lstat()) == fingerprint(before), 'DIRECTORY_CHANGED_DURING_READ')
        no_link(folder.lstat())

    walk(root)
    return sorted(rows, key=lambda r: r['path'])


def stable_inventory(root):
    first = inventory(root)
    require(first == inventory(root), 'TREE_CHANGED_DURING_SCAN')
    return first


def payload(rows):
    return {'version': 1, 'algorithm': 'sha256', 'files': rows}


def tree_digest(rows):
    return hashlib.sha256(canonical(payload(rows))).hexdigest()


def external_manifest(root, manifest):
    root = checked_absolute(root)
    dest = Path(os.path.abspath(manifest))
    parent = checked_absolute(dest.parent)
    require(root != parent and root not in parent.parents, 'MANIFEST_INSIDE_OUTPUT')
    if dest.exists() or dest.is_symlink():
        no_link(dest.lstat())
    return dest


def seal(root, manifest):
    """Caller must establish acceptance; this operation makes NO approval claim."""
    dest = external_manifest(root, manifest)
    require(not dest.exists(), 'MANIFEST_ALREADY_EXISTS')
    rows = stable_inventory(root)
    require(bool(rows), 'EMPTY_ARTIFACT')
    result = {**payload(rows), 'tree_sha256': tree_digest(rows)}
    # Exclusive creation preserves earlier seals. A partial write is invalid on verify.
    with dest.open('xb') as stream:
        stream.write(canonical(result) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())
    return result['tree_sha256']


def unique_object(pairs):
    result = {}
    for k, v in pairs:
        require(k not in result, 'DUPLICATE_JSON_KEY')
        result[k] = v
    return result


def verify(root, manifest, expected_digest):
    require(isinstance(expected_digest, str) and bool(HEX.fullmatch(expected_digest)),
            'INVALID_EXPECTED_DIGEST')
    manifest = external_manifest(root, manifest)
    data = json.loads(manifest.read_bytes().decode('utf8', 'strict'), object_pairs_hook=unique_object)
    require(isinstance(data, dict) and set(data) == {'version', 'algorithm', 'files', 'tree_sha256'},
            'INVALID_MANIFEST_SCHEMA')
    require(type(data['version']) is int and data['version'] == 1 and data['algorithm'] == 'sha256',
            'INVALID_MANIFEST_VERSION')
    rows = data['files']
    require(isinstance(rows, list) and bool(rows), 'INVALID_FILE_LIST')
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and set(row) == {'path', 'bytes', 'sha256'}, 'INVALID_FILE_ROW')
        safe_path(row['path'])
        require(row['path'].casefold() not in seen, 'DUPLICATE_FILE_PATH')
        seen.add(row['path'].casefold())
        require(type(row['bytes']) is int and row['bytes'] >= 0 and
                isinstance(row['sha256'], str) and bool(HEX.fullmatch(row['sha256'])), 'INVALID_FILE_ROW')
    require(rows == sorted(rows, key=lambda r: r['path']), 'UNSORTED_FILE_LIST')
    require(tree_digest(rows) == data['tree_sha256'] == expected_digest, 'TREE_DIGEST_MISMATCH')
    actual = stable_inventory(root)
    wanted = {r['path']: r for r in rows}
    found = {r['path']: r for r in actual}
    missing = sorted(wanted.keys() - found.keys())
    added = sorted(found.keys() - wanted.keys())
    changed = sorted(k for k in wanted.keys() & found.keys() if wanted[k] != found[k])
    require(not (missing or added or changed),
            f'ARTIFACT_MISMATCH missing={missing[:5]} added={added[:5]} changed={changed[:5]}')
    require(tree_digest(actual) == expected_digest, 'TREE_DIGEST_MISMATCH')
    return expected_digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    for name in ('seal', 'verify'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--root', required=True)
        cmd.add_argument('--manifest', required=True)
        if name == 'verify':
            cmd.add_argument('--expected-digest', required=True)
    args = parser.parse_args()
    try:
        digest = (seal(args.root, args.manifest) if args.operation == 'seal' else
                  verify(args.root, args.manifest, args.expected_digest))
        print(json.dumps({'operation': args.operation, 'status': 'PASS', 'tree_sha256': digest}))
        return 0
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        print(json.dumps({'operation': args.operation, 'status': 'REJECT', 'reason': str(exc)}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
