"""Inactive on-demand render handoff. No SDK, credentials or network transport.

Private object generations are immutable inputs; ephemeral workspaces are not a
job ledger. Real storage ACL/IAM and the actual compositor remain separate gates.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
from pathlib import Path
from .elevenlabs_adapter import fingerprint

SHA = re.compile(r'[0-9a-f]{64}\Z')
ID = re.compile(r'[A-Za-z0-9_-]{8,128}\Z')
OBJECT_KEYS = {'bucket', 'object', 'generation', 'sha256', 'bytes', 'content_type'}
MAX_TOTAL_BYTES = 400_000_000


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_target(target):
    require(isinstance(target, dict) and set(target) == {'schema_version', 'status', 'live_release',
        'project_id', 'region', 'job_name', 'bucket', 'container_image', 'cpu', 'memory',
        'task_timeout_seconds', 'task_count', 'parallelism', 'max_retries'}, 'Complete reviewed job target required.')
    require(type(target['schema_version']) is int and target['schema_version'] == 1
            and target['status'] == 'DRAFT' and target['live_release'] is False, 'Only an inactive draft target is supported.')
    require(isinstance(target['project_id'], str) and re.fullmatch(r'[a-z][a-z0-9-]{4,28}[a-z0-9]', target['project_id']), 'Explicit project ID required.')
    require(isinstance(target['region'], str) and re.fullmatch(r'[a-z]+-[a-z]+[0-9]', target['region']), 'Explicit region required.')
    require(isinstance(target['job_name'], str) and re.fullmatch(r'byron-renderer(?:-[a-z0-9-]{1,30})?', target['job_name']), 'Unexpected render job name.')
    require(isinstance(target['bucket'], str) and re.fullmatch(r'[a-z][a-z0-9-]{2,61}[a-z0-9]', target['bucket']), 'Explicit private bucket identifier required.')
    prefix = f"{target['region']}-docker.pkg.dev/{target['project_id']}/"
    require(isinstance(target['container_image'], str) and target['container_image'].startswith(prefix)
            and re.fullmatch(re.escape(prefix) + r'[a-z0-9-]+/[a-z0-9-]+@sha256:[0-9a-f]{64}', target['container_image']), 'Container must be digest-pinned in the chosen project/region.')
    require(target['cpu'] == '2' and target['memory'] == '4Gi', 'This draft has one explicit resource envelope.')
    require(type(target['task_timeout_seconds']) is int and 60 <= target['task_timeout_seconds'] <= 3600, 'Bounded task timeout required.')
    require(all(type(target[x]) is int for x in ('task_count', 'parallelism', 'max_retries'))
            and target['task_count'] == target['parallelism'] == 1 and target['max_retries'] == 0,
            'One task per execution and zero automatic task retries are required.')
    return copy.deepcopy(target)


def validate_object(ref, *, bucket, prefix, content_type, max_bytes):
    require(isinstance(ref, dict) and set(ref) == OBJECT_KEYS, 'Complete immutable object reference required.')
    name = ref['object']
    require(ref['bucket'] == bucket and isinstance(name, str) and name.startswith(prefix)
            and re.fullmatch(r'[A-Za-z0-9/_-]+\.[a-z0-9]+', name) and '..' not in name
            and '//' not in name and '\\' not in name, 'Object escaped its private job namespace.')
    require(isinstance(ref['generation'], str) and re.fullmatch(r'[1-9][0-9]{0,19}', ref['generation']), 'Exact object generation required; latest is forbidden.')
    require(isinstance(ref['sha256'], str) and SHA.fullmatch(ref['sha256']), 'Object fingerprint required.')
    require(type(ref['bytes']) is int and 0 < ref['bytes'] <= max_bytes, 'Object size exceeds its processing limit.')
    require(ref['content_type'] == content_type, 'Unexpected object content type.')
    return copy.deepcopy(ref)


def validate_manifest(manifest, target, *, trusted_job):
    target = validate_target(target)
    require(isinstance(manifest, dict) and set(manifest) == {'schema_version', 'status', 'job_id', 'owner_namespace',
        'source_job_sha256', 'source_order', 'composition', 'narration', 'clips', 'sha256'}, 'Complete render manifest required.')
    require(type(manifest['schema_version']) is int and manifest['schema_version'] == 1
            and manifest['status'] == 'DRAFT_NOT_ACTIVATED', 'Render manifest must remain inactive.')
    for key in ('job_id', 'owner_namespace'):
        require(isinstance(manifest[key], str) and ID.fullmatch(manifest[key]), 'Opaque job/owner identity required.')
    require(isinstance(manifest['source_job_sha256'], str) and SHA.fullmatch(manifest['source_job_sha256']), 'Frozen upstream job fingerprint required.')
    require(isinstance(trusted_job, dict) and {'job_id','owner_id','snapshot_sha256','frozen_job'} <= set(trusted_job)
            and isinstance(trusted_job['owner_id'], str) and re.fullmatch(r'[A-Za-z0-9_-]{8,128}', trusted_job['owner_id'])
            and isinstance(trusted_job['frozen_job'], dict), 'Independently loaded owner-bound job handoff required.')
    frozen = trusted_job['frozen_job']
    require(fingerprint(frozen) == trusted_job['snapshot_sha256'] == manifest['source_job_sha256']
            and manifest['job_id'] == trusted_job['job_id']
            and manifest['owner_namespace'] == hashlib.sha256(trusted_job['owner_id'].encode()).hexdigest(),
            'Manifest differs from the independently loaded job, owner or snapshot.')
    selected = frozen.get('images')
    require(isinstance(selected, list) and frozen.get('input_order') == [x.get('id') for x in selected if isinstance(x, dict)]
            and all(isinstance(x, dict) and type(x.get('position')) is int and x['position'] == i for i, x in enumerate(selected, 1)), 'Trusted frozen image order is invalid.')
    prefix = f"owners/{manifest['owner_namespace']}/jobs/{manifest['job_id']}/inputs/"
    validate_object(manifest['composition'], bucket=target['bucket'], prefix=prefix, content_type='application/json', max_bytes=2_000_000)
    validate_object(manifest['narration'], bucket=target['bucket'], prefix=prefix, content_type='audio/wav', max_bytes=100_000_000)
    clips, order = manifest['clips'], manifest['source_order']
    require(isinstance(order, list) and 4 <= len(order) <= 5 and all(isinstance(x, str) and ID.fullmatch(x) for x in order)
            and len(order) == len(set(order)) and isinstance(clips, list) and len(clips) == len(order), 'Keep four or five explicitly ordered images.')
    require(order == frozen['input_order'] and [(x['image_id'], x['position'], x['original_sha256']) for x in clips if isinstance(x, dict) and {'image_id','position','original_sha256'} <= set(x)]
            == [(x['id'], x['position'], x.get('sha256')) for x in selected], 'Render clips differ from independently frozen selected photos/order.')
    refs = [manifest['composition'], manifest['narration']]
    for position, (clip, image_id) in enumerate(zip(clips, order), 1):
        require(isinstance(clip, dict) and set(clip) == {'image_id', 'position', 'original_sha256', 'video'}, 'Incomplete source-bound clip.')
        require(clip['image_id'] == image_id and type(clip['position']) is int and clip['position'] == position, 'Clip order differs from selected photo numbers.')
        require(isinstance(clip['original_sha256'], str) and SHA.fullmatch(clip['original_sha256']), 'Original image fingerprint required.')
        validate_object(clip['video'], bucket=target['bucket'], prefix=prefix, content_type='video/mp4', max_bytes=50_000_000)
        refs.append(clip['video'])
    require(len({x['object'] for x in refs}) == len(refs), 'Input object reused under another role.')
    require(len({x['video']['sha256'] for x in clips}) == len(clips), 'Repeated clip bytes are not unique footage.')
    require(sum(x['bytes'] for x in refs) <= MAX_TOTAL_BYTES, 'Combined input exceeds this draft workspace envelope.')
    require(manifest['sha256'] == fingerprint({k: v for k, v in manifest.items() if k != 'sha256'}), 'Render manifest changed after freezing.')
    return copy.deepcopy(manifest)


def build_execution_request(target, manifest, manifest_object, *, trusted_job):
    """Construct the documented run request, but never send it or obtain a token."""
    target = validate_target(target)
    manifest = validate_manifest(manifest, target, trusted_job=trusted_job)
    prefix = f"owners/{manifest['owner_namespace']}/jobs/{manifest['job_id']}/"
    validate_object(manifest_object, bucket=target['bucket'], prefix=prefix,
                    content_type='application/json', max_bytes=2_000_000)
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    require(manifest_object['sha256'] == hashlib.sha256(encoded).hexdigest()
            and manifest_object['bytes'] == len(encoded), 'Manifest object does not bind the exact canonical handoff bytes.')
    name = f"projects/{target['project_id']}/locations/{target['region']}/jobs/{target['job_name']}"
    values = {'BYRON_JOB_ID': manifest['job_id'], 'BYRON_OWNER_NAMESPACE': manifest['owner_namespace'],
              'BYRON_MANIFEST_BUCKET': target['bucket'], 'BYRON_MANIFEST_OBJECT': manifest_object['object'],
              'BYRON_MANIFEST_GENERATION': manifest_object['generation'], 'BYRON_MANIFEST_SHA256': manifest_object['sha256'],
              'BYRON_PLAN_SHA256': manifest['sha256']}
    return {'method': 'POST', 'url': f'https://run.googleapis.com/v2/{name}:run',
            'json': {'overrides': {'taskCount': 1, 'timeout': f"{target['task_timeout_seconds']}s",
                'containerOverrides': [{'name': 'renderer', 'env': [{'name': k, 'value': v} for k, v in sorted(values.items())]}]}},
            'required_verified_target': {'container_image': target['container_image'], 'max_retries': 0,
                                        'parallelism': 1, 'task_count': 1, 'cpu': '2', 'memory': '4Gi'},
            'live_release': False}


def validate_deployed_target(actual, target):
    """A trusted control adapter must fetch this before dispatch, never trust a client."""
    expected = validate_target(target)
    # Deliberately accepts a normalized adapter record, not arbitrary provider JSON.
    require(actual == expected and fingerprint(actual) == fingerprint(expected), 'Deployed job does not match the reviewed immutable target.')
    return True


def materialize_inputs(manifest, target, root, reader, *, trusted_job):
    """Bounded storage-boundary test harness; no live transport or media-quality claim.

    reader(ref) returns {generation, content_type, stream}. Production must use a
    separately reviewed private object-store reader and real codec/compositor QA.
    """
    require(getattr(reader, 'synthetic_fixture', False) is True, 'Live storage transport is not activated.')
    manifest = validate_manifest(manifest, target, trusted_job=trusted_job)
    root = Path(root)
    require(root.is_absolute() and not any(p.is_symlink() for p in (root, *root.parents))
            and (not root.exists() or (root.is_dir() and not any(root.iterdir()))), 'Use a new empty private workspace without symlinks.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    entries = [('composition.json', manifest['composition']), ('narration.wav', manifest['narration'])]
    entries += [(f'clip-{i:02}.mp4', clip['video']) for i, clip in enumerate(manifest['clips'], 1)]
    result, created = {}, []
    try:
        for name, ref in entries:
            response = reader(copy.deepcopy(ref))
            stream = response.get('stream') if isinstance(response, dict) else None
            try:
                require(isinstance(response, dict) and set(response) == {'generation', 'content_type', 'stream'}
                        and response['generation'] == ref['generation'] and response['content_type'] == ref['content_type'], 'Downloaded object generation/type changed.')
                require(hasattr(stream, 'read') and hasattr(stream, 'close'), 'A bounded object stream is required.')
                chunks, remaining = [], ref['bytes'] + 1
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    require(isinstance(chunk, bytes) and len(chunk) <= min(65536, remaining), 'Invalid or unbounded object reader.')
                    if not chunk:
                        break
                    chunks.append(chunk); remaining -= len(chunk)
                data = b''.join(chunks)
            finally:
                if hasattr(stream, 'close'):
                    stream.close()
            require(len(data) == ref['bytes'] and hashlib.sha256(data).hexdigest() == ref['sha256'], 'Downloaded object size/fingerprint differs.')
            path = root / name
            with path.open('xb') as output:
                created.append((path, path.stat().st_ino))
                output.write(data)
            path.chmod(0o600)
            result[name] = {'local_path': str(path.resolve()), 'sha256': ref['sha256'], 'generation': ref['generation']}
    except BaseException:
        for path, inode in created:
            if path.exists() and not path.is_symlink() and path.stat().st_ino == inode:
                path.unlink()
        raise
    return {'files': result, 'manifest_sha256': manifest['sha256'], 'source_order': list(manifest['source_order']),
            'media_verified': False, 'rendered': False, 'live_release': False}


def submit_live(*args, **kwargs):
    raise RuntimeError('Cloud rendering is not activated; no credentials are read and no request is sent.')
