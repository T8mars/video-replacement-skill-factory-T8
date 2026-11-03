"""Verify this one skill's staged tree and every outgoing Git commit."""
from pathlib import Path, PurePosixPath
import argparse, hashlib, json, os, re, subprocess, sys

EXPECTED_SKILL = 'video-replacement-skill-factory'
EXPECTED_REPO = 'https://github.com/T8mars/video-replacement-skill-factory-T8'
SECRET_PATTERNS = [
    re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{24,})\b'),
    re.compile(rb'''(?i)(?:api[_-]?key|access[_-]?token|secret[_-]?key)\s*["']?\s*[:=]\s*["'][A-Za-z0-9_-]{24,}["']'''),
    re.compile(rb'https?://[^/\s:]+:[^/\s@]+@'),
    re.compile(rb'(?i)[?&](?:q-signature|x-amz-signature|x-cos-signature)=[a-f0-9]{24,}')
]

def git(*args, required=True):
    result = subprocess.run(['git', *args], capture_output=True)
    if required and result.returncode:
        raise ValueError('Git metadata or tree could not be read')
    return result.stdout

def remote_ok(url):
    value = url.strip().removesuffix('.git').rstrip('/')
    return value in (EXPECTED_REPO, 'git@github.com:T8mars/video-replacement-skill-factory-T8')

def forbidden_path(name):
    parts = PurePosixPath(name).parts
    lower = [p.lower() for p in parts]
    if not parts or name.startswith('/') or '..' in parts or '\\' in name:
        return True
    if any(p == 'roadmap.md' or p.startswith(('api_key', 'api-key', 'apikey', '.env')) for p in lower):
        return True
    if any(p.startswith('r2va-video-') or p in ('.git','private','_private','runs','productions','candidates','outputs','refs','guides','__pycache__') for p in lower):
        return True
    if parts[-1].lower() == 'skill.md' and name != 'SKILL.md':
        return True
    return PurePosixPath(name).suffix.lower() in ('.mp4','.mov','.nut','.png','.jpg','.jpeg','.wav','.mp3','.zip','.sqlite','.db','.sqlite-wal','.sqlite-shm','.db-wal','.db-shm','.pem','.key','.pfx','.pyc','.exe','.dll')

def check_payload(name, data, known_keys=()):
    errors = []
    if forbidden_path(name):
        errors.append(name+': forbidden publication path')
    if any(key and key in data for key in known_keys) or any(p.search(data) for p in SECRET_PATTERNS):
        errors.append(name+': possible credential or private signed URL; value suppressed')
    return errors

def check_tree(files, known_keys=()):
    errors=[]
    for name, data in files.items():
        errors.extend(check_payload(name,data,known_keys))
    try:
        policy=json.loads(files['publish-policy.json'])
        if policy['skill'] != EXPECTED_SKILL or policy['repository'] != EXPECTED_REPO:
            errors.append('publish-policy.json: wrong skill or repository')
        allowed=set(policy['runtime_files'])|set(policy['repository_files'])
        for name in sorted(set(files)-allowed):
            errors.append(name+': outside the factory publication whitelist')
        if not re.search(rb'(?m)^name:\s*'+EXPECTED_SKILL.encode()+rb'\s*$',files['SKILL.md']):
            errors.append('SKILL.md: factory identity mismatch')
        manifest=json.loads(files['manifest.json'])
        meta=json.loads(files['meta.json'])
        if manifest['name'] != EXPECTED_SKILL or meta['name'] != EXPECTED_SKILL:
            errors.append('manifest/meta: factory identity mismatch')
        if manifest['version'] != meta['version']:
            errors.append('manifest/meta: version mismatch')
        expected_runtime=set(policy['runtime_files'])-{'manifest.json'}
        if set(manifest['files']) != expected_runtime:
            errors.append('manifest: runtime whitelist coverage is incomplete or contains extra members')
        for name, sha in manifest['files'].items():
            if name not in policy['runtime_files'] or forbidden_path(name):
                errors.append(name+': invalid runtime manifest member')
            elif name not in files or hashlib.sha256(files[name]).hexdigest()!=sha:
                errors.append(name+': manifest hash mismatch')
    except (KeyError,TypeError,ValueError):
        errors.append('Required publication metadata is missing or invalid')
    return sorted(set(errors))

def known_keys(explicit=None):
    configured=git('config','--get','factory.keyFile',required=False).decode().strip()
    home=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    path=Path(explicit or configured) if explicit or configured else home/'private/r2va-character-replacement/api_key.txt'
    return [path.read_bytes().strip()] if path.is_file() else []

def tree(revision=None):
    if revision is None:
        names=git('ls-files','--cached','-z').decode('utf8').split('\0')
        return {name:git('show',':'+name) for name in names if name}
    names=git('ls-tree','-r','--name-only','-z',revision).decode('utf8').split('\0')
    return {name:git('show',revision+':'+name) for name in names if name}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged',action='store_true')
    parser.add_argument('--revision')
    parser.add_argument('--key-file')
    parser.add_argument('--pre-push',nargs=2,metavar=('REMOTE','URL'))
    args=parser.parse_args()
    try:
        origin=git('remote','get-url','origin').decode().strip()
        if not remote_ok(origin):
            raise ValueError('origin is not the dedicated factory repository')
        keys=known_keys(args.key_file)
        revisions=[]
        if args.pre_push:
            remote,url=args.pre_push
            if remote!='origin' or not remote_ok(url):
                raise ValueError('Push target is not the dedicated factory repository')
            for line in sys.stdin.read().splitlines():
                local_ref,local_sha,remote_ref,remote_sha=line.split()
                if set(local_sha)=={'0'}:
                    continue
                if set(remote_sha)=={'0'}:
                    pending=git('rev-list',local_sha,'--not','--remotes=origin')
                else:
                    pending=git('rev-list',remote_sha+'..'+local_sha)
                revisions.extend(pending.decode().splitlines())
            if not revisions:
                revisions=['HEAD']
        elif args.revision:
            revisions=[args.revision]
        else:
            revisions=[None]
        errors=[]
        for revision in dict.fromkeys(revisions):
            errors.extend(check_tree(tree(revision),keys))
        if errors:
            raise ValueError('\n'.join(sorted(set(errors))))
        print('Publication verified: factory-only whitelist, manifest and credential checks passed.')
        return 0
    except (ValueError,OSError) as error:
        print('Publication blocked: '+str(error),file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
