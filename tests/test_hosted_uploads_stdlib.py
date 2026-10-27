"""Offline hosted upload-to-job integration; synthetic images and adapters only."""
import hashlib
import io
import pathlib
import random
import tempfile
import unittest
from dataclasses import replace
from fastapi.testclient import TestClient
from PIL import Image
from byron_video import hosted_control as control
from byron_video import hosted_uploads as uploads

ORIGIN = 'https://studio.example'
TOKEN = 'synthetic-session-token-' + 'x' * 32
OWNER = 'fixture_owner'
OTHER = 'fixture_other'
KEY = 'fixture_request_000001'


def image_bytes(index=0, size=(64, 64), fmt='PNG'):
    stream = io.BytesIO()
    Image.new('RGB', size, (index * 20 % 256, 40, 80)).save(stream, format=fmt)
    return stream.getvalue()


def plan():
    return dict(schema_version=1, status='DRAFT_NOT_ACTIVATED', visual_style_sha256='b'*64,
        motion_policy_sha256='c'*64, output=dict(width=1080, height=1920, fps=24),
        narration_profile=dict(schema_version=1, version='synthetic-fixture-1', status='DRAFT', provider='elevenlabs',
            voice_id='SYNTHETIC_TEST_VOICE', model_id='eleven_multilingual_v2',
            voice_settings=dict(stability=.5, similarity_boost=.75, style=0, use_speaker_boost=True, speed=.93),
            output_format='mp3_44100_128', seed=1, apply_text_normalization='off'))


class HostedUploadChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.images = [image_bytes(i) for i in range(4)]
        self.uploads = uploads.SQLiteUploadFixture(self.root / 'uploads.sqlite')
        self.jobs = control.SQLiteFixtureStore(self.root / 'jobs.sqlite')
        self.dispatches = []
        self.dispatch_error = False
        def dispatch(handoff):
            self.dispatches.append(handoff)
            if self.dispatch_error:
                raise TimeoutError('synthetic uncertain dispatch')
            return control.DispatchReceipt(handoff['job_id'], handoff['snapshot_sha256'], 'execution_' + handoff['job_id'])
        dispatch.synthetic_fixture = True
        self.dispatch = dispatch

    def tearDown(self):
        self.jobs.close()
        self.uploads.close()
        self.temp.cleanup()

    def app(self, storage=None, resolver=None):
        # Explicit test-only identity header is never a production auth adapter.
        def session(request):
            owner = request.headers.get('x-fixture-owner')
            return uploads.CreatorSession(control.Principal(owner), TOKEN) if owner else None
        return control.create_app(hosted_client_enabled=True,
            creator_access=uploads.CreatorAccess(resolver or session, ORIGIN),
            private_upload_store=storage or self.uploads, store=self.jobs, dispatcher=self.dispatch,
            plan_provider=lambda owner: plan(), voice_provider=lambda owner: [dict(id='fixture_voice_1', name='Narrator')])

    def headers(self, owner=OWNER, key=KEY):
        return {'Origin': ORIGIN, 'X-CSRF-Token': TOKEN, 'Idempotency-Key': key, 'X-Fixture-Owner': owner}

    def files(self, images=None, mime='image/png'):
        return [('images', (f'../untrusted-{i}.png', data, mime)) for i, data in enumerate(self.images if images is None else images)]

    def upload(self, client, **kwargs):
        return client.post('/api/uploads', headers=kwargs.pop('headers', self.headers()),
                           files=kwargs.pop('files', self.files()), **kwargs)

    def body(self, response):
        return dict(script='Exact synthetic words, unchanged.', voice_id='fixture_voice_1', images=response.json()['images'])

    def test_default_disabled(self):
        with TestClient(control.create_app()) as client:
            response = client.get('/api/creator-capabilities')
            self.assertFalse(response.json()['enabled'])
            self.assertNotIn('csrf_token', response.json())
            self.assertEqual(self.upload(client).status_code, 503)
            self.assertFalse(client.get('/api/health').json()['ready'])

    def test_missing_session_and_csrf_origin_fail_before_write(self):
        with TestClient(self.app()) as client:
            self.assertEqual(client.get('/api/creator-capabilities').status_code, 401)
            cases = [({}, 401), ({'X-Fixture-Owner': OWNER}, 403),
                     ({**self.headers(), 'Origin':'https://attacker.example'},403),
                     ({**self.headers(), 'X-CSRF-Token':'wrong'},403),
                     ({**self.headers(), 'Sec-Fetch-Site':'cross-site'},403)]
            for headers, code in cases:
                with self.subTest(headers=list(headers)):
                    self.assertEqual(self.upload(client, headers=headers).status_code,code)
        self.assertEqual(self.uploads._db.execute('SELECT COUNT(*) FROM upload_batches').fetchone()[0],0)

    def test_capabilities_are_private_and_not_live(self):
        with TestClient(self.app()) as client:
            response = client.get('/api/creator-capabilities', headers=self.headers())
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['csrf_token'],TOKEN)
            self.assertTrue(response.json()['enabled'])
            self.assertTrue(response.json()['synthetic_fixture'])
            self.assertFalse(response.json()['live_release'])
            self.assertEqual(response.headers['cache-control'],'no-store')

    def test_order_and_original_bytes_are_preserved_without_paths(self):
        with TestClient(self.app()) as client:
            response = self.upload(client)
            self.assertEqual(response.status_code,201,response.text)
            result = response.json()
            self.assertEqual(set(result),{'images','upload_id'})
            for position, selection in enumerate(result['images'],1):
                self.assertEqual(set(selection),{'id','position'})
                self.assertEqual(selection['position'],position)
                record = self.uploads.resolve(control.Principal(OWNER),selection['id'])
                self.assertEqual(record.sha256,hashlib.sha256(self.images[position-1]).hexdigest())
                raw = self.uploads._db.execute('SELECT data FROM upload_images WHERE id=?',(record.id,)).fetchone()[0]
                self.assertEqual(raw,self.images[position-1])
            self.assertNotIn('untrusted',response.text)
            self.assertNotIn('storage_id',response.text)

    def test_invalid_count_mime_dimensions_and_bytes_never_commit(self):
        cases = [self.files(self.images[:3]), self.files(self.images + self.images[:2]),
                 self.files(mime='image/jpeg'), self.files([image_bytes(size=(63,64))]*4),
                 self.files([b'not an image']*4), self.files([b'']*4)]
        with TestClient(self.app()) as client:
            for index, files in enumerate(cases):
                with self.subTest(index=index):
                    response = self.upload(client,files=files)
                    self.assertIn(response.status_code,[400,413,422],response.text)
        self.assertEqual(self.uploads._db.execute('SELECT COUNT(*) FROM upload_batches').fetchone()[0],0)

    def test_unexpected_form_field_and_invalid_key_rejected(self):
        with TestClient(self.app()) as client:
            response = self.upload(client,data={'filename':'private/path'})
            self.assertIn(response.status_code,[400,422])
            self.assertEqual(self.upload(client,headers=self.headers(key='short')).status_code,422)
        self.assertEqual(self.uploads._db.execute('SELECT COUNT(*) FROM upload_batches').fetchone()[0],0)

    def test_idempotent_replay_and_reorder_conflict(self):
        with TestClient(self.app()) as client:
            first = self.upload(client)
            repeated = self.upload(client)
            self.assertEqual(first.status_code,201,first.text)
            self.assertEqual(repeated.json(),first.json())
            changed = self.upload(client,files=self.files(list(reversed(self.images))))
            self.assertEqual(changed.status_code,409)
        self.assertEqual(self.uploads._db.execute('SELECT COUNT(*) FROM upload_images').fetchone()[0],4)

    def test_cross_owner_resolve_and_job_references_denied(self):
        with TestClient(self.app()) as client:
            response = self.upload(client)
            with self.assertRaises(ValueError):
                self.uploads.resolve(control.Principal(OTHER),response.json()['images'][0]['id'])
            other_job = client.post('/api/jobs',json=self.body(response),headers=self.headers(owner=OTHER))
            self.assertEqual(other_job.status_code,404,other_job.text)
            self.assertEqual(self.dispatches,[])
            # The same random key belongs to a separate private namespace.
            other_upload = self.upload(client,headers=self.headers(owner=OTHER))
            self.assertEqual(other_upload.status_code,201,other_upload.text)
            self.assertNotEqual(response.json(),other_upload.json())

    def test_tampered_stored_bytes_rejected(self):
        with TestClient(self.app()) as client:
            response = self.upload(client)
            image_id = response.json()['images'][0]['id']
            self.uploads._db.execute('UPDATE upload_images SET data=? WHERE id=?',(b'changed',image_id))
            with self.assertRaises(ValueError):
                self.uploads.resolve(control.Principal(OWNER),image_id)
            job = client.post('/api/jobs',json=self.body(response),headers=self.headers())
            self.assertEqual(job.status_code,404)
            self.assertEqual(self.dispatches,[])

    def test_unknown_upload_outcome_no_retry_and_error_redacted(self):
        calls = []
        delegate = self.uploads
        class UnknownStore:
            resolve = delegate.resolve
            def commit_batch(self,*args):
                calls.append(args)
                delegate.commit_batch(*args)
                raise TimeoutError('synthetic-secret-never-disclose')
        with TestClient(self.app(storage=UnknownStore())) as client:
            response = self.upload(client)
            self.assertEqual(response.status_code,503,response.text)
            self.assertNotIn('synthetic-secret-never-disclose',response.text)
            self.assertIn('Do not submit again',response.text)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.uploads._db.execute('SELECT COUNT(*) FROM upload_batches').fetchone()[0],1)
        self.assertEqual(self.dispatches,[])

    def test_complete_upload_voice_job_and_hold_recovery_contract(self):
        self.dispatch_error = True
        with TestClient(self.app()) as client:
            self.assertEqual(client.get('/api/creator-capabilities',headers=self.headers()).status_code,200)
            uploaded = self.upload(client)
            self.assertEqual(uploaded.status_code,201,uploaded.text)
            body = self.body(uploaded)
            first = client.post('/api/jobs',json=body,headers=self.headers())
            self.assertEqual(first.status_code,202,first.text)
            self.assertEqual(first.json()['status'],'HOLD')
            job_id = first.json()['id']
            snapshot = self.jobs.get(OWNER,job_id).snapshot
            self.assertEqual(snapshot['raw_script'],body['script'])
            self.assertEqual(snapshot['input_order'],[x['id'] for x in body['images']])
            self.assertEqual(snapshot['processing_plan']['narration_profile']['voice_id'],'fixture_voice_1')
            self.assertEqual([x['sha256'] for x in snapshot['images']],[hashlib.sha256(x).hexdigest() for x in self.images])
            polled = client.get('/api/jobs/'+job_id,headers=self.headers())
            self.assertEqual(polled.json()['status'],'HOLD')
            self.assertNotIn('download_url',polled.json())
            repeated = client.post('/api/jobs',json=body,headers=self.headers())
            self.assertEqual(repeated.json()['id'],job_id)
            self.assertTrue(repeated.json()['reused'])
            self.assertEqual(len(self.dispatches),1)
            self.assertEqual(client.get('/api/jobs/'+job_id,headers=self.headers(owner=OTHER)).status_code,404)

    def test_five_jpegs_supported_and_mime_matches_actual_bytes(self):
        originals = [image_bytes(i, fmt='JPEG') for i in range(5)]
        with TestClient(self.app()) as client:
            response = self.upload(client, files=self.files(originals, mime='image/jpeg'))
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(len(response.json()['images']), 5)
            for index, selected in enumerate(response.json()['images']):
                record = self.uploads.resolve(control.Principal(OWNER), selected['id'])
                self.assertEqual(record.content_type, 'image/jpeg')
                self.assertEqual(record.sha256, hashlib.sha256(originals[index]).hexdigest())

    def test_uploads_larger_than_control_limit_remain_valid(self):
        # Deterministic synthetic pixels, not user media, produce >128 KiB PNGs.
        rng = random.Random(42)
        output = io.BytesIO()
        Image.frombytes('RGB', (256,256), rng.randbytes(256*256*3)).save(output,format='PNG')
        data = output.getvalue()
        self.assertGreater(len(data), control.MAX_CONTROL_BODY_BYTES)
        with TestClient(self.app()) as client:
            response = self.upload(client, files=self.files([data]*4))
            self.assertEqual(response.status_code, 201, response.text)
            too_large_control = client.post('/api/jobs', content=b'x'*(control.MAX_CONTROL_BODY_BYTES+1),
                headers={**self.headers(),'Content-Type':'application/json'})
            self.assertIn(too_large_control.status_code, [400,413])

    def test_invalid_adapter_receipt_is_not_trusted_or_retried(self):
        delegate = self.uploads
        calls = []
        class WrongOwnerStore:
            resolve = delegate.resolve
            def commit_batch(self,*args):
                calls.append(args)
                upload_id, records = delegate.commit_batch(*args)
                records[0] = replace(records[0], owner_id=OTHER)
                return upload_id, records
        with TestClient(self.app(storage=WrongOwnerStore())) as client:
            response = self.upload(client)
            self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.dispatches, [])

    def test_corrupted_blob_replay_fails_closed(self):
        with TestClient(self.app()) as client:
            first = self.upload(client)
            self.assertEqual(first.status_code, 201, first.text)
            image_id = first.json()['images'][0]['id']
            self.uploads._db.execute('UPDATE upload_images SET data=? WHERE id=?', (b'corrupt-original', image_id))
            repeated = self.upload(client)
            self.assertEqual(repeated.status_code, 503, repeated.text)
            self.assertNotIn('corrupt-original', repeated.text)
        self.assertEqual(self.dispatches, [])

    def test_private_store_rejects_symlinks_and_public_web_directory(self):
        public_path = pathlib.Path(uploads.__file__).resolve().parent.parent / 'web' / 'must-not-create.sqlite'
        self.assertFalse(public_path.exists())
        with self.assertRaises(ValueError):
            uploads.SQLiteUploadFixture(public_path)
        self.assertFalse(public_path.exists())
        linked_dir = self.root / 'linked-directory'
        linked_dir.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            uploads.SQLiteUploadFixture(linked_dir / 'must-not-create.sqlite')
        linked_db = self.root / 'linked-database.sqlite'
        linked_db.symlink_to(self.root / 'uploads.sqlite')
        with self.assertRaises(ValueError):
            uploads.SQLiteUploadFixture(linked_db)

    def test_private_database_mode_and_sync_policy(self):
        self.assertEqual((self.root / 'uploads.sqlite').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.uploads._db.execute('PRAGMA synchronous').fetchone()[0], 2)

    def test_status_only_submission_recovery_is_owner_scoped_and_never_dispatches(self):
        with TestClient(self.app()) as client:
            self.assertEqual(client.get('/api/submissions/'+KEY).status_code,401)
            absent = client.get('/api/submissions/'+KEY,headers=self.headers())
            self.assertEqual(absent.status_code,404)
            self.assertIn('do not submit again',absent.json()['detail'])
            uploaded = self.upload(client)
            submitted = client.post('/api/jobs',json=self.body(uploaded),headers=self.headers())
            recovered = client.get('/api/submissions/'+KEY,headers=self.headers())
            self.assertEqual(recovered.json(),{'id':submitted.json()['id'],'status':'queued'})
            self.assertEqual(recovered.headers['cache-control'],'no-store')
            self.assertEqual(client.get('/api/submissions/'+KEY,headers=self.headers(owner=OTHER)).status_code,404)
            self.assertEqual(len(self.dispatches),1)

    def test_job_mutation_uses_same_session_csrf_boundary(self):
        with TestClient(self.app()) as client:
            uploaded = self.upload(client)
            response = client.post('/api/jobs',json=self.body(uploaded),headers={'X-Fixture-Owner':OWNER,'Idempotency-Key':KEY})
            self.assertEqual(response.status_code,403)
            self.assertEqual(self.dispatches,[])


if __name__ == '__main__':
    unittest.main(verbosity=2)
