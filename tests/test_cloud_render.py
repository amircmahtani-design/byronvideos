"""Synthetic object boundary fixtures; no real storage, cloud API or codec proof."""
import copy
import hashlib
import io
import json
from pathlib import Path
import pytest
from byron_video import cloud_render as cloud
from byron_video.elevenlabs_adapter import fingerprint


def target():
    return {'schema_version': 1, 'status': 'DRAFT', 'live_release': False,
            'project_id': 'synthetic-project', 'region': 'us-central1', 'job_name': 'byron-renderer',
            'bucket': 'synthetic-private-bucket',
            'container_image': 'us-central1-docker.pkg.dev/synthetic-project/renderer/film@sha256:' + 'a'*64,
            'cpu': '2', 'memory': '4Gi', 'task_timeout_seconds': 1800,
            'task_count': 1, 'parallelism': 1, 'max_retries': 0}


OWNER_NS = hashlib.sha256(b'owner-0001').hexdigest()


def trusted_job():
    snapshot = {'input_order': [f'image-00{i}' for i in range(1, 5)],
                'images': [{'id': f'image-00{i}', 'position': i, 'sha256': str(i)*64} for i in range(1, 5)]}
    return {'job_id':'job-00001','owner_id':'owner-0001','snapshot_sha256':fingerprint(snapshot),'frozen_job':snapshot}


def fixture():
    data = {}
    def ref(name, payload, mime):
        key = f'owners/{OWNER_NS}/jobs/job-00001/inputs/' + name
        data[key] = payload
        return {'bucket': target()['bucket'], 'object': key, 'generation': '1234',
                'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload), 'content_type': mime}
    m = {'schema_version': 1, 'status': 'DRAFT_NOT_ACTIVATED', 'job_id': 'job-00001', 'owner_namespace': OWNER_NS,
         'source_job_sha256': trusted_job()['snapshot_sha256'], 'source_order': [f'image-00{i}' for i in range(1, 5)],
         'composition': ref('composition.json', b'{"fixture":"not real compositor input"}', 'application/json'),
         'narration': ref('narration.wav', b'SYNTHETIC-BYTES-NOT-REAL-WAV', 'audio/wav'), 'clips': []}
    for i, image_id in enumerate(m['source_order'], 1):
        m['clips'].append({'image_id': image_id, 'position': i, 'original_sha256': str(i)*64,
                           'video': ref(f'clip-{i}.mp4', f'SYNTHETIC-BYTES-NOT-MP4-{i}'.encode(), 'video/mp4')})
    m['sha256'] = fingerprint(m)
    return m, data


def repin(m):
    m['sha256'] = fingerprint({k: v for k, v in m.items() if k != 'sha256'})
    return m


def manifest_ref(m):
    raw = json.dumps(m, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return {'bucket': target()['bucket'], 'object': f'owners/{OWNER_NS}/jobs/job-00001/render.json',
            'generation': '9876', 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'content_type': 'application/json'}


class Reader:
    synthetic_fixture = True
    def __init__(self, data): self.data, self.calls, self.streams = data, [], []
    def __call__(self, ref):
        self.calls.append(copy.deepcopy(ref)); stream = io.BytesIO(self.data[ref['object']]); self.streams.append(stream)
        return {'generation': ref['generation'], 'content_type': ref['content_type'], 'stream': stream}


def test_immutable_offline_handoff_and_small_google_request():
    m, _ = fixture(); before = copy.deepcopy(m)
    request = cloud.build_execution_request(target(), m, manifest_ref(m), trusted_job=trusted_job())
    assert request['url'] == 'https://run.googleapis.com/v2/projects/synthetic-project/locations/us-central1/jobs/byron-renderer:run'
    assert request['json']['overrides']['taskCount'] == 1
    assert request['required_verified_target']['max_retries'] == 0 and request['live_release'] is False
    env = request['json']['overrides']['containerOverrides'][0]['env']
    assert {x['name'] for x in env} == {'BYRON_JOB_ID','BYRON_OWNER_NAMESPACE','BYRON_MANIFEST_BUCKET','BYRON_MANIFEST_OBJECT','BYRON_MANIFEST_GENERATION','BYRON_MANIFEST_SHA256','BYRON_PLAN_SHA256'}
    assert m == before and 'SYNTHETIC-BYTES' not in json.dumps(request)


@pytest.mark.parametrize('field,value', [('live_release', True), ('status','APPROVED'), ('schema_version',True),
    ('container_image','python:latest'), ('max_retries',3), ('max_retries',False), ('parallelism',2),
    ('task_count',True), ('task_timeout_seconds',3601), ('cpu','16'), ('memory','64Gi'),
    ('project_id',None), ('region','https://evil.test'), ('bucket','user@example.com')])
def test_unconfigured_or_weakened_target_rejected(field,value):
    t=target();t[field]=value
    with pytest.raises(ValueError):cloud.validate_target(t)


@pytest.mark.parametrize('field,value', [('generation','latest'),('generation',1234),('generation','0'),
    ('object','owners/other/jobs/job-00001/inputs/clip.mp4'),('object','owners/owner-0001/jobs/job-00001/inputs/../clip.mp4'),
    ('object','https://example.test/clip.mp4'),('bytes',True),('bytes',50_000_001),('sha256','0'*63),('bucket','other-bucket')])
def test_tampered_object_reference_rejected_even_with_new_manifest_hash(field,value):
    m,_=fixture();m['clips'][0]['video'][field]=value;repin(m)
    with pytest.raises(ValueError):cloud.validate_manifest(m,target(),trusted_job=trusted_job())


def test_exact_selected_order_and_unique_bytes():
    m,_=fixture();m['clips'].reverse();repin(m)
    with pytest.raises(ValueError,match='order'):cloud.validate_manifest(m,target(),trusted_job=trusted_job())
    m,_=fixture();m['clips'][1]['video']['sha256']=m['clips'][0]['video']['sha256'];repin(m)
    with pytest.raises(ValueError,match='Repeated'):cloud.validate_manifest(m,target(),trusted_job=trusted_job())


def test_outer_reference_must_hash_actual_canonical_manifest_not_any_json():
    m,_=fixture();ref=manifest_ref(m);ref['sha256']='a'*64
    with pytest.raises(ValueError,match='canonical'):cloud.build_execution_request(target(),m,ref,trusted_job=trusted_job())
    ref=manifest_ref(m);ref['bytes']+=1
    with pytest.raises(ValueError,match='canonical'):cloud.build_execution_request(target(),m,ref,trusted_job=trusted_job())


def test_trusted_target_must_match_exact_types_not_boolean_coercion():
    assert cloud.validate_deployed_target(target(),target())
    actual=target();actual['max_retries']=False
    with pytest.raises(ValueError):cloud.validate_deployed_target(actual,target())


def test_streamed_materialization_is_private_and_does_not_claim_rendered(tmp_path):
    m,data=fixture();reader=Reader(data);root=tmp_path/'job'
    result=cloud.materialize_inputs(m,target(),root,reader,trusted_job=trusted_job())
    assert len(result['files'])==6 and len(reader.calls)==6
    assert result['source_order']==m['source_order']
    assert result['media_verified'] is result['rendered'] is result['live_release'] is False
    assert root.stat().st_mode & 0o777 == 0o700
    assert all(Path(x['local_path']).stat().st_mode & 0o777 == 0o600 for x in result['files'].values())
    assert all(x.closed for x in reader.streams)


@pytest.mark.parametrize('fault',['extra','short','hash','generation','mime'])
def test_download_mismatch_cleans_partial_staging_and_fails(tmp_path,fault):
    m,data=fixture();reader=Reader(data);ref=m['clips'][0]['video'];key=ref['object']
    if fault=='extra':data[key]+=b'x'
    if fault=='short':data[key]=data[key][:-1]
    if fault=='hash':data[key]=b'x'*len(data[key])
    def modified(r):
        out=reader(r)
        if r['object']==key:
            if fault=='generation':out['generation']='9999'
            if fault=='mime':out['content_type']='application/octet-stream'
        return out
    modified.synthetic_fixture=True
    with pytest.raises(ValueError):cloud.materialize_inputs(m,target(),tmp_path/'job',modified,trusted_job=trusted_job())
    assert not list((tmp_path/'job').iterdir())


def test_never_overwrites_files_or_follows_workspace_symlink(tmp_path):
    m,data=fixture();root=tmp_path/'existing';root.mkdir();(root/'keep').write_text('keep')
    with pytest.raises(ValueError):cloud.materialize_inputs(m,target(),root,Reader(data),trusted_job=trusted_job())
    assert (root/'keep').read_text()=='keep'
    link=tmp_path/'link';link.symlink_to(root,target_is_directory=True)
    with pytest.raises(ValueError):cloud.materialize_inputs(m,target(),link/'new',Reader(data),trusted_job=trusted_job())


def test_live_reader_and_submission_remain_closed(tmp_path):
    m,data=fixture();reader=Reader(data);reader.synthetic_fixture=False
    with pytest.raises(ValueError,match='not activated'):cloud.materialize_inputs(m,target(),tmp_path/'job',reader,trusted_job=trusted_job())
    assert not reader.calls
    with pytest.raises(RuntimeError,match='not activated'):cloud.submit_live()


def test_repository_target_template_is_deliberately_unconfigured():
    path = Path(__file__).resolve().parents[1] / 'hosted/cloud-run.target.draft.json'
    value = json.loads(path.read_text())
    assert value['live_release'] is False and value['container_image'] is None
    with pytest.raises(ValueError, match='project ID'): cloud.validate_target(value)


def test_independent_frozen_order_cannot_be_reversed_by_repinned_manifest():
    m,_=fixture();m['source_order'].reverse();m['clips'].reverse()
    for i,c in enumerate(m['clips'],1):c['position']=i
    repin(m)
    with pytest.raises(ValueError,match='independently frozen'):cloud.validate_manifest(m,target(),trusted_job=trusted_job())


def test_staging_failure_does_not_delete_a_file_it_did_not_create(tmp_path):
    m,data=fixture();reader=Reader(data);root=tmp_path/'job'
    def collide(ref):
        result=reader(ref)
        if ref==m['clips'][0]['video']:(root/'clip-01.mp4').write_bytes(b'not created by materializer')
        return result
    collide.synthetic_fixture=True
    with pytest.raises(FileExistsError):cloud.materialize_inputs(m,target(),root,collide,trusted_job=trusted_job())
    assert (root/'clip-01.mp4').read_bytes()==b'not created by materializer'
    assert not (root/'composition.json').exists()


def test_owner_and_job_namespace_are_bound_to_trusted_handoff():
    m,_=fixture();m['owner_namespace']='f'*64;repin(m)
    with pytest.raises(ValueError,match='owner'):cloud.validate_manifest(m,target(),trusted_job=trusted_job())
    m,_=fixture();m['job_id']='other-job';repin(m)
    with pytest.raises(ValueError,match='job'):cloud.validate_manifest(m,target(),trusted_job=trusted_job())


@pytest.mark.parametrize('length,accepted', [(128,True),(129,False)])
def test_image_id_length_matches_control_api(length,accepted):
    m,_=fixture();handoff=trusted_job();identifier='i'*length
    m['source_order'][0]=identifier;m['clips'][0]['image_id']=identifier
    handoff['frozen_job']['input_order'][0]=identifier;handoff['frozen_job']['images'][0]['id']=identifier
    handoff['snapshot_sha256']=fingerprint(handoff['frozen_job']);m['source_job_sha256']=handoff['snapshot_sha256'];repin(m)
    if accepted:assert cloud.validate_manifest(m,target(),trusted_job=handoff)
    else:
        with pytest.raises(ValueError):cloud.validate_manifest(m,target(),trusted_job=handoff)
