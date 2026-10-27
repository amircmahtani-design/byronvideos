"""Offline API → immutable cloud handoff → private staging, NOT a rendered film."""
import hashlib
import io
import json
from fastapi.testclient import TestClient
from byron_video import cloud_render as cloud, hosted_control as control
from byron_video.elevenlabs_adapter import fingerprint
from test_cloud_render import target
from test_hosted_control import plan, upload, principal, body, headers, OWNER, OTHER


class CloudFixtureDispatcher:
    synthetic_fixture = True

    def __init__(self, store, root, fail_after_stage=False):
        self.store, self.root, self.fail_after_stage = store, root, fail_after_stage
        self.calls = []

    def __call__(self, handoff):
        job = self.store.get(handoff['owner_id'], handoff['job_id'])
        assert job.state == 'HOLD' and job.snapshot == handoff['frozen_job']
        owner_namespace = hashlib.sha256(handoff['owner_id'].encode()).hexdigest()
        prefix = f"owners/{owner_namespace}/jobs/{job.id}/"
        objects = {}

        def ref(name, payload, mime):
            key = prefix + 'inputs/' + name; objects[key] = payload
            return {'bucket': target()['bucket'], 'object': key, 'generation': '42',
                    'sha256': hashlib.sha256(payload).hexdigest(), 'bytes': len(payload), 'content_type': mime}

        manifest = {'schema_version': 1, 'status': 'DRAFT_NOT_ACTIVATED', 'job_id': job.id,
                    'owner_namespace': owner_namespace, 'source_job_sha256': job.snapshot_sha256,
                    'source_order': job.snapshot['input_order'],
                    'composition': ref('composition.json', b'{"synthetic_contract_fixture":true}', 'application/json'),
                    'narration': ref('narration.wav', b'NOT-REAL-WAV-CONTRACT-FIXTURE', 'audio/wav'), 'clips': []}
        for i, image in enumerate(job.snapshot['images'], 1):
            manifest['clips'].append({'image_id': image['id'], 'position': i, 'original_sha256': image['sha256'],
                'video': ref(f'clip-{i}.mp4', f'NOT-REAL-VIDEO-FIXTURE-{i}'.encode(), 'video/mp4')})
        manifest['sha256'] = fingerprint(manifest)
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        outer = {'bucket': target()['bucket'], 'object': prefix + 'render.json', 'generation': '43',
                 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'content_type': 'application/json'}
        request = cloud.build_execution_request(target(), manifest, outer, trusted_job=handoff)

        def reader(r):
            return {'generation': r['generation'], 'content_type': r['content_type'], 'stream': io.BytesIO(objects[r['object']])}
        reader.synthetic_fixture = True
        staged = cloud.materialize_inputs(manifest, target(), self.root / job.id, reader, trusted_job=handoff)
        self.calls.append({'manifest': manifest, 'request': request, 'staged': staged})
        if self.fail_after_stage:
            raise TimeoutError('Synthetic uncertain dispatch; no actual cloud request made')
        return control.DispatchReceipt(job.id, job.snapshot_sha256, 'fixture_execution_' + job.id)


def app(store, dispatcher):
    return control.create_app(principal_resolver=principal, store=store, dispatcher=dispatcher,
                              upload_resolver=upload, plan_provider=lambda _: plan())


def test_numbered_selection_reaches_cloud_plan_and_private_staging_once(tmp_path):
    store = control.SQLiteFixtureStore(tmp_path / 'ledger.sqlite3')
    dispatcher = CloudFixtureDispatcher(store, tmp_path / 'workspaces')
    payload = body(); payload['images'].reverse()
    for i, image in enumerate(payload['images'], 1): image['position'] = i
    try:
        with TestClient(app(store, dispatcher)) as client:
            first = client.post('/api/jobs', json=payload, headers=headers())
            repeated = client.post('/api/jobs', json=payload, headers=headers())
            assert first.status_code == repeated.status_code == 202
            assert first.json()['id'] == repeated.json()['id'] and repeated.json()['reused'] is True
            assert len(dispatcher.calls) == 1
            expected = [x['id'] for x in payload['images']]
            assert dispatcher.calls[0]['manifest']['source_order'] == expected
            assert dispatcher.calls[0]['staged']['source_order'] == expected
            assert dispatcher.calls[0]['staged']['media_verified'] is False
            assert dispatcher.calls[0]['staged']['rendered'] is False
            assert payload['script'] not in json.dumps(dispatcher.calls[0]['request'])
            job_id = first.json()['id']
            assert client.get(f'/api/jobs/{job_id}', headers=headers(OTHER)).status_code == 404
            assert client.get(f'/api/jobs/{job_id}/video', headers=headers()).status_code == 409
            assert client.get('/api/health').json()['ready'] is False
    finally: store.close()


def test_unknown_handoff_restart_keeps_hold_without_new_staging(tmp_path):
    db = tmp_path / 'ledger.sqlite3'; store = control.SQLiteFixtureStore(db)
    dispatcher = CloudFixtureDispatcher(store, tmp_path / 'workspaces', fail_after_stage=True)
    with TestClient(app(store, dispatcher)) as client:
        first = client.post('/api/jobs', json=body(), headers=headers())
        assert first.json()['status'] == 'HOLD' and len(dispatcher.calls) == 1
    store.close()
    reopened = control.SQLiteFixtureStore(db)
    second = CloudFixtureDispatcher(reopened, tmp_path / 'workspaces')
    try:
        with TestClient(app(reopened, second)) as client:
            response = client.post('/api/jobs', json=body(), headers=headers())
            assert response.json()['id'] == first.json()['id'] and response.json()['status'] == 'HOLD'
            assert response.json()['reused'] is True and not second.calls
    finally: reopened.close()
