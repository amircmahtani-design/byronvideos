"""Cloud SDK contract tests with an in-memory generation-aware fake; no network."""
import hashlib
import io
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from PIL import Image
from byron_video.cloud_storage import CloudUploadStore,CloudArtifactReader,CloudArtifactWriter,UploadUncertain,artifact_object_name
from byron_video.hosted_control import Principal,IdempotencyConflict
from byron_video.hosted_uploads import prepare_image,batch_fingerprint


class ApiError(Exception):
    def __init__(self,code):self.code=code


class FakeBlob:
    def __init__(self,bucket,name,generation=None):self.bucket,self.name,self.generation=bucket,name,generation
    def _value(self,match=None):
        if self.name not in self.bucket.objects:raise ApiError(404)
        value=self.bucket.objects[self.name]
        expected=match if match is not None else self.generation
        if expected is not None and int(expected)!=value[0]:raise ApiError(412)
        return value
    def reload(self,**kw):
        assert kw['retry'] is None
        gen,data,mime=self._value(kw.get('if_generation_match'))
        self.generation,self.size,self.content_type=gen,len(data),mime
    def download_to_file(self,target,**kw):
        assert kw['retry'] is None
        gen,data,mime=self._value(kw.get('if_generation_match'))
        target.write(data)
    def upload_from_string(self,data,**kw):
        assert kw['retry'] is None
        old=self.bucket.objects.get(self.name)
        match=kw['if_generation_match']
        if (match==0 and old is not None) or (match!=0 and (old is None or old[0]!=match)):raise ApiError(412)
        self.bucket.serial+=1;self.generation=self.bucket.serial
        self.bucket.objects[self.name]=(self.generation,data,kw['content_type'])
        self.bucket.writes.append((self.name,match))
        if self.bucket.fail_after and self.bucket.fail_after(self.name,match):raise TimeoutError('synthetic committed timeout')
    def upload_from_file(self,source,**kw):
        kw.pop('size');self.upload_from_string(source.read(),**kw)


class FakeBucket:
    def __init__(self):
        self.objects={};self.serial=0;self.writes=[];self.fail_after=None
        self.iam_configuration=SimpleNamespace(uniform_bucket_level_access_enabled=True,public_access_prevention='enforced')
    def reload(self,**kw):assert kw['retry'] is None
    def blob(self,name,generation=None):return FakeBlob(self,name,generation)


class StorageChecks(unittest.TestCase):
    def setUp(self):
        self.bucket=FakeBucket();self.client=SimpleNamespace(bucket=lambda name:self.bucket)
        self.store=CloudUploadStore(self.client,bucket='synthetic-private-bucket')
        self.owner=Principal('fixture_owner');self.other=Principal('fixture_other');self.key='fixture_request_000001'
        self.images=[]
        for i in range(4):
            b=io.BytesIO();Image.new('RGB',(64,64),(i*20,40,80)).save(b,format='PNG')
            self.images.append(prepare_image(b.getvalue(),'image/png'))
        self.digest=batch_fingerprint(self.images)
    def commit(self):return self.store.commit_batch(self.owner,self.key,self.digest,self.images)

    def test_originals_and_order_preserved_with_pinned_readback(self):
        upload,records=self.commit()
        self.assertEqual(len(records),4)
        self.assertEqual(len(self.bucket.writes),6)
        self.assertEqual([r.sha256 for r in records],[x.sha256 for x in self.images])
        for record,image in zip(records,self.images):
            self.assertEqual(self.store.resolve(self.owner,record.id),record)
            with self.store.read_original(self.owner,record) as stream:self.assertEqual(stream.read(),image.data)
        self.assertEqual(len(upload),64)

    def test_ready_replay_is_read_only_and_changed_input_conflicts(self):
        first=self.commit();writes=len(self.bucket.writes)
        self.assertEqual(self.commit(),first);self.assertEqual(len(self.bucket.writes),writes)
        reversed_images=list(reversed(self.images))
        with self.assertRaises(IdempotencyConflict):
            self.store.commit_batch(self.owner,self.key,batch_fingerprint(reversed_images),reversed_images)
        self.assertEqual(len(self.bucket.writes),writes)

    def test_unknown_reservation_never_reuploads(self):
        self.bucket.fail_after=lambda name,match:name.endswith('manifest.json') and match==0
        with self.assertRaises(UploadUncertain):self.commit()
        self.bucket.fail_after=None
        with self.assertRaises(UploadUncertain):self.commit()
        self.assertEqual(len(self.bucket.writes),1)
        with self.assertRaises(UploadUncertain):self.store.reconcile_upload(self.owner,self.key)
        self.assertEqual(len(self.bucket.writes),1)

    def test_unknown_last_original_reconciles_without_reupload(self):
        count=[0]
        def fail(name,match):
            if '/originals/' in name:count[0]+=1
            return count[0]==4 and '/originals/' in name
        self.bucket.fail_after=fail
        with self.assertRaises(UploadUncertain):self.commit()
        self.bucket.fail_after=None
        before=len(self.bucket.writes)
        with self.assertRaises(UploadUncertain):self.commit()
        upload,records=self.store.reconcile_upload(self.owner,self.key)
        self.assertEqual(len(records),4);self.assertEqual(len(self.bucket.writes),before+1)
        self.assertTrue(self.bucket.writes[-1][0].endswith('manifest.json'))
        self.assertNotEqual(self.bucket.writes[-1][1],0)

    def test_committed_completion_timeout_recovers_by_read_only_replay(self):
        self.bucket.fail_after=lambda name,match:name.endswith('manifest.json') and match!=0
        with self.assertRaises(UploadUncertain):self.commit()
        self.bucket.fail_after=None;before=len(self.bucket.writes)
        self.assertEqual(len(self.commit()[1]),4)
        self.assertEqual(len(self.bucket.writes),before)

    def test_cross_owner_reference_cannot_resolve(self):
        _,records=self.commit()
        with self.assertRaises(ApiError):self.store.resolve(self.other,records[0].id)
        with self.assertRaises(ValueError):self.store.resolve(self.owner,'../unsafe')

    def test_changed_object_generation_and_bytes_are_rejected(self):
        _,records=self.commit();record=records[0]
        name=next(k for k in self.bucket.objects if k.endswith('/'+record.id))
        gen,data,mime=self.bucket.objects[name]
        self.bucket.objects[name]=(gen+100,data,mime)
        with self.assertRaises(ApiError):self.store.resolve(self.owner,record.id)
        self.bucket.objects[name]=(gen,b'x'*len(data),mime)
        with self.assertRaises(ValueError):self.store.resolve(self.owner,record.id)

    def test_prepared_image_is_revalidated(self):
        from dataclasses import replace
        bad=[replace(self.images[0],data=b'bad')]+self.images[1:]
        with self.assertRaises(Exception):self.store.commit_batch(self.owner,self.key,batch_fingerprint(bad),bad)
        self.assertEqual(self.bucket.writes,[])

    def test_public_bucket_policy_fails_before_writes(self):
        self.bucket.iam_configuration.public_access_prevention='inherited'
        with self.assertRaises(ValueError):self.commit()
        self.assertEqual(self.bucket.writes,[])
        self.bucket.iam_configuration.public_access_prevention='enforced'
        self.bucket.iam_configuration.uniform_bucket_level_access_enabled=False
        with self.assertRaises(ValueError):self.commit()
        self.assertEqual(self.bucket.writes,[])

    def test_configuration_rejects_urls_and_paths(self):
        for options in [dict(bucket='https://evil.example'),dict(bucket='fixture-bucket',prefix='../public'),dict(bucket='fixture-bucket',timeout=0)]:
            with self.assertRaises(ValueError):CloudUploadStore(self.client,**options)

    def artifact(self,data=b'not-real-video-fixture'):
        job=SimpleNamespace(owner_id=self.owner.subject,id='a'*32)
        proof=SimpleNamespace(artifact_id='fixture_artifact',sha256=hashlib.sha256(data).hexdigest(),byte_count=len(data),validate=lambda job:None)
        return job,proof,data

    def test_artifact_writer_and_reader_pin_and_verify_all_bytes(self):
        job,proof,data=self.artifact()
        writer=CloudArtifactWriter(self.client,bucket='fixture-bucket')
        with tempfile.TemporaryDirectory() as root:
            p=pathlib.Path(root)/'output.mp4';p.write_bytes(data)
            reference=writer(self.owner,job,proof.artifact_id,p,proof)
        reader=CloudArtifactReader(self.client,bucket='fixture-bucket',reference_resolver=lambda *args:reference)
        with reader(self.owner,job,proof) as stream:self.assertEqual(stream.read(),data)
        self.assertEqual(self.bucket.writes[0][1],0)
        with self.assertRaises(ValueError):reader(self.other,job,proof)
        bad={**reference,'generation':str(int(reference['generation'])+1)}
        reader.reference_resolver=lambda *args:bad
        with self.assertRaises(ApiError):reader(self.owner,job,proof)

    def test_artifact_writer_rejects_changed_local_bytes_before_upload(self):
        job,proof,data=self.artifact();writer=CloudArtifactWriter(self.client,bucket='fixture-bucket')
        with tempfile.TemporaryDirectory() as root:
            p=pathlib.Path(root)/'output.mp4';p.write_bytes(b'bad')
            with self.assertRaises(ValueError):writer(self.owner,job,proof.artifact_id,p,proof)
        self.assertEqual(self.bucket.writes,[])

    def test_provider_response_archive_is_write_once_owner_job_and_generation_bound(self):
        from byron_video.cloud_storage import CloudProviderResponseStore
        from byron_video.provider_execution import WireResponse
        store=CloudProviderResponseStore(self.client,bucket='fixture-bucket',owner=self.owner,job_id='a'*32)
        wire=WireResponse('fixture_request_id',b'{"synthetic":true}')
        receipt=store.write_response('fixture_operation',wire)
        self.assertEqual(store.read_response(receipt),wire)
        with self.assertRaises(ApiError):store.write_response('fixture_operation',wire)
        other=CloudProviderResponseStore(self.client,bucket='fixture-bucket',owner=self.other,job_id='a'*32)
        with self.assertRaises(ApiError):other.read_response(receipt)
        with self.assertRaises(ValueError):store.read_response({**receipt,'response_sha256':'f'*64})

    def test_artifact_registry_must_match_proof(self):
        job,proof,data=self.artifact()
        reader=CloudArtifactReader(self.client,bucket='fixture-bucket',reference_resolver=lambda *args:dict(generation='1',sha256='f'*64,byte_count=len(data),content_type='video/mp4'))
        with self.assertRaises(ValueError):reader(self.owner,job,proof)


if __name__=='__main__':unittest.main()
