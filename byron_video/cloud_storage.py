"""Owner-scoped immutable GCS originals and verified artifact downloads.

No client, credentials, network call, public ACL, signed URL or bucket is created
on import/construction. Supply an authenticated storage Client and existing private
bucket from deployment configuration. All writes disable automatic SDK retries.
Upload journal uses generation-precondition CAS, not process-local locks.
"""
from dataclasses import asdict
import hashlib
import io
import json
import re
import tempfile
import uuid

from .hosted_control import Principal, VerifiedImage, IdempotencyConflict, SAFE_ID, canonical, digest
from .hosted_uploads import KEY, batch_fingerprint, prepare_image

GENERATION = re.compile(r'[1-9][0-9]*\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
UPLOAD_ID = re.compile(r'[0-9a-f]{64}\Z')


class UploadUncertain(RuntimeError):
    """An existing HOLD must be reconciled, never blindly uploaded again."""


def owner_namespace(owner):
    if not isinstance(owner, Principal):
        raise ValueError('Verified owner required.')
    return hashlib.sha256(owner.subject.encode()).hexdigest()


def _safe_id(value):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError('Invalid private identifier.')
    return value


def _generation(value):
    value = str(value)
    if not GENERATION.fullmatch(value):
        raise ValueError('An immutable storage generation is required.')
    return int(value)


def _code(error):
    value = getattr(error, 'code', None)
    return value() if callable(value) else value


class _BoundedWriter:
    def __init__(self, stream, bound):
        self.stream, self.bound, self.count = stream, bound, 0
        self.sha = hashlib.sha256()
    def write(self, value):
        if not isinstance(value, bytes) or self.count + len(value) > self.bound:
            raise ValueError('Private object exceeded its bound.')
        self.count += len(value)
        self.sha.update(value)
        return self.stream.write(value)
    def tell(self):
        return self.count


class _PrivateBucket:
    def __init__(self, client, *, bucket, prefix='byron-private', timeout=30):
        if (not isinstance(bucket,str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]',bucket)
                or not isinstance(prefix,str) or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*',prefix)
                or type(timeout) not in (int,float) or not 1 <= timeout <= 120):
            raise ValueError('Explicit private bucket/prefix and bounded timeout required.')
        self.bucket = client.bucket(bucket)
        self.prefix, self.timeout = prefix, timeout

    def _require_private_bucket(self):
        # Read-only fail-closed check; never repair policies or grant access here.
        self.bucket.reload(timeout=self.timeout,retry=None)
        configuration=self.bucket.iam_configuration
        if (configuration.uniform_bucket_level_access_enabled is not True
                or configuration.public_access_prevention != 'enforced'):
            raise ValueError('Uniform private bucket access and public-access prevention must be enforced.')

    def _name(self, owner, tail):
        return f'{self.prefix}/owners/{owner_namespace(owner)}/{tail}'

    def _download(self, name, *, generation=None, maximum, mime, expected_sha=None, expected_size=None):
        self._require_private_bucket()
        blob = self.bucket.blob(name, generation=None if generation is None else _generation(generation))
        blob.reload(if_generation_match=None if generation is None else _generation(generation),timeout=self.timeout,retry=None)
        gen = _generation(blob.generation)
        if (type(blob.size) is not int or not 0 < blob.size <= maximum or blob.content_type != mime
                or expected_size is not None and blob.size != expected_size):
            raise ValueError('Private object metadata changed.')
        spool = tempfile.SpooledTemporaryFile(max_size=8*1024*1024,mode='w+b')
        sink = _BoundedWriter(spool,blob.size)
        try:
            blob.download_to_file(sink,if_generation_match=gen,timeout=self.timeout,retry=None)
            if sink.count != blob.size or expected_sha is not None and sink.sha.hexdigest() != expected_sha:
                raise ValueError('Private object integrity changed.')
            spool.seek(0)
            return spool,str(gen),sink.sha.hexdigest()
        except BaseException:
            spool.close()
            raise

    def _write(self, name, data, mime, *, generation=0):
        self._require_private_bucket()
        blob = self.bucket.blob(name)
        blob.upload_from_string(data,content_type=mime,if_generation_match=generation,timeout=self.timeout,retry=None,checksum='auto')
        return str(_generation(blob.generation))


class CloudUploadStore(_PrivateBucket):
    """GCS-backed upload store with atomic manifest reservation/completion.

    One request maps deterministically to an owner-scoped upload manifest. Creation
    with generation=0 commits HOLD before originals are written. Originals are
    write-once; READY is a compare-and-swap of that manifest after pinned readback.
    A repeated HOLD never causes another original upload. Explicit reconciliation
    can read already-created originals and CAS READY; it never recreates objects.
    Configure bucket encryption/retention/quotas/access separately; this class does
    not grant permissions or claim that those deployment controls are configured.
    """
    def _upload_id(self, key):
        if not isinstance(key,str) or not KEY.fullmatch(key):
            raise ValueError('Invalid submission reference.')
        return hashlib.sha256(key.encode()).hexdigest()

    def _manifest_name(self, owner, upload_id):
        if not UPLOAD_ID.fullmatch(upload_id):
            raise ValueError('Invalid upload reference.')
        return self._name(owner,f'uploads/{upload_id}/manifest.json')

    def _object_name(self, owner, upload_id, image_id):
        _safe_id(image_id)
        return self._name(owner,f'uploads/{upload_id}/originals/{image_id}')

    def _load(self, owner, upload_id):
        stream,gen,_ = self._download(self._manifest_name(owner,upload_id),maximum=64*1024,mime='application/json')
        try:
            data = json.load(stream)
        finally:
            stream.close()
        if (not isinstance(data,dict) or set(data)!={'schema_version','owner_namespace','upload_id','fingerprint','state','images'}
                or type(data['schema_version']) is not int or data['schema_version'] != 1 or data['owner_namespace'] != owner_namespace(owner)
                or data['upload_id'] != upload_id or not isinstance(data['fingerprint'],str) or not SHA.fullmatch(data['fingerprint'])
                or data['state'] not in {'HOLD','READY'} or not isinstance(data['images'],list) or len(data['images']) not in (4,5)):
            raise ValueError('Stored upload journal is invalid.')
        ids = set()
        for position,item in enumerate(data['images'],1):
            if not isinstance(item,dict) or set(item)!={'id','position','sha256','byte_count','width','height','content_type','generation'}:
                raise ValueError('Stored image descriptor is invalid.')
            if (item['position'] != position or not isinstance(item['id'],str)
                    or not re.fullmatch(upload_id+r'_[0-9a-f]{32}',item['id']) or item['id'] in ids):
                raise ValueError('Stored image order is invalid.')
            ids.add(item['id'])
            # Full common bounds are independently enforced by VerifiedImage.
            record = VerifiedImage(item['id'],owner.subject,item['sha256'],item['byte_count'],item['width'],item['height'],item['content_type'],item['id'],item['generation'] or '1',True)
            record.validate(owner,record.id)
            if data['state']=='READY':
                _generation(item['generation'])
            elif item['generation'] is not None:
                raise ValueError('HOLD descriptor cannot assert a generation.')
        fingerprint=digest([dict(position=i['position'],sha256=i['sha256'],bytes=i['byte_count'],content_type=i['content_type']) for i in data['images']])
        if fingerprint!=data['fingerprint']:
            raise ValueError('Stored upload fingerprint changed.')
        return data,gen

    def _verified(self, owner, manifest, *, recover=False):
        records=[]
        completed=json.loads(canonical(manifest))
        for item in completed['images']:
            stream,gen,_ = self._download(self._object_name(owner,manifest['upload_id'],item['id']),
                generation=item['generation'],maximum=20*1024*1024,mime=item['content_type'],expected_sha=item['sha256'],expected_size=item['byte_count'])
            stream.close()
            if recover:
                item['generation']=gen
            record=VerifiedImage(item['id'],owner.subject,item['sha256'],item['byte_count'],item['width'],item['height'],item['content_type'],item['id'],gen,True)
            record.validate(owner,record.id)
            records.append(record)
        completed['state']='READY'
        return completed,records

    def commit_batch(self, owner, key, fingerprint, images):
        owner_namespace(owner)
        upload_id=self._upload_id(key)
        if len(images) not in (4,5) or batch_fingerprint(images)!=fingerprint:
            raise ValueError('Invalid original batch.')
        # Never treat an injected descriptor alone as proof of actual image bytes.
        for item in images:
            actual=prepare_image(item.data,item.content_type)
            if actual != item:
                raise ValueError('Prepared original changed.')
        try:
            existing,_=self._load(owner,upload_id)
        except Exception as error:
            if _code(error)!=404:
                raise
        else:
            if existing['fingerprint']!=fingerprint:
                raise IdempotencyConflict('Upload reference belongs to different paintings.')
            if existing['state']!='READY':
                raise UploadUncertain('Upload is on HOLD; reconcile original objects before continuing.')
            _,records=self._verified(owner,existing)
            return upload_id,records
        manifest=dict(schema_version=1,owner_namespace=owner_namespace(owner),upload_id=upload_id,fingerprint=fingerprint,state='HOLD',images=[])
        for position,item in enumerate(images,1):
            manifest['images'].append(dict(id=upload_id+'_'+uuid.uuid4().hex,position=position,sha256=item.sha256,
                byte_count=len(item.data),width=item.width,height=item.height,content_type=item.content_type,generation=None))
        name=self._manifest_name(owner,upload_id)
        try:
            reserved_gen=self._write(name,canonical(manifest).encode(),'application/json')
        except Exception as error:
            if _code(error)!=412:
                raise UploadUncertain('Upload reservation outcome is unknown; inspect the saved reference.') from None
            existing,_=self._load(owner,upload_id)
            if existing['fingerprint']!=fingerprint:
                raise IdempotencyConflict('Upload reference belongs to different paintings.')
            if existing['state']!='READY':
                raise UploadUncertain('Upload is on HOLD; reconcile original objects before continuing.')
            _,records=self._verified(owner,existing)
            return upload_id,records
        try:
            for item,prepared in zip(manifest['images'],images):
                self._write(self._object_name(owner,upload_id,item['id']),prepared.data,prepared.content_type)
            completed,records=self._verified(owner,manifest,recover=True)
            self._write(name,canonical(completed).encode(),'application/json',generation=_generation(reserved_gen))
            return upload_id,records
        except Exception:
            raise UploadUncertain('Upload completion is uncertain. No original will be automatically rewritten.') from None

    def reconcile_upload(self, owner, key):
        """Explicit recovery: readback + journal CAS only; no original uploads."""
        upload_id=self._upload_id(key)
        manifest,gen=self._load(owner,upload_id)
        if manifest['state']=='READY':
            _,records=self._verified(owner,manifest)
            return upload_id,records
        try:
            completed,records=self._verified(owner,manifest,recover=True)
            self._write(self._manifest_name(owner,upload_id),canonical(completed).encode(),'application/json',generation=_generation(gen))
            return upload_id,records
        except Exception:
            raise UploadUncertain('Originals are incomplete or journal outcome is unknown; do not reupload.') from None

    def resolve(self, owner, image_id):
        _safe_id(image_id)
        upload_id=image_id.split('_',1)[0]
        manifest,_=self._load(owner,upload_id)
        if manifest['state']!='READY':
            raise UploadUncertain('Upload has not been committed.')
        if not any(item['id']==image_id for item in manifest['images']):
            raise ValueError('Private original not found.')
        # Verify only selected original rather than redownloading the whole batch.
        selected={**manifest,'images':[next(item for item in manifest['images'] if item['id']==image_id)]}
        _,records=self._verified(owner,selected)
        return records[0]

    def read_original(self, owner, record):
        """Worker-only immutable stream; not exposed as an HTTP handler."""
        current=self.resolve(owner,record.id)
        if current!=record:
            raise ValueError('Frozen original reference changed.')
        upload_id=record.id.split('_',1)[0]
        stream,_,_=self._download(self._object_name(owner,upload_id,record.id),generation=record.storage_version,
            maximum=20*1024*1024,mime=record.content_type,expected_sha=record.sha256,expected_size=record.byte_count)
        return stream


def artifact_object_name(prefix, owner, job_id, artifact_id):
    _safe_id(job_id);_safe_id(artifact_id)
    if not isinstance(prefix,str) or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*',prefix):
        raise ValueError('Invalid private prefix.')
    return f'{prefix}/owners/{owner_namespace(owner)}/jobs/{job_id}/artifacts/{artifact_id}'


class CloudArtifactReader(_PrivateBucket):
    """Read a trusted registry's immutable MP4 generation, verify before return.

    reference_resolver(owner,job,artifact_id) returns only generation, sha256,
    byte_count and content_type. The object name is derived, never supplied by a
    browser or arbitrary URL. Full media verification belongs to the verifier;
    this reader independently verifies ownership, generation and bytes.
    """
    def __init__(self, client, *, bucket, reference_resolver, prefix='byron-private', timeout=30):
        super().__init__(client,bucket=bucket,prefix=prefix,timeout=timeout)
        self.reference_resolver=reference_resolver

    def __call__(self, owner, job, proof):
        if owner.subject!=job.owner_id:
            raise ValueError('Private artifact not found.')
        proof.validate(job)
        reference=self.reference_resolver(owner,job,proof.artifact_id)
        if (not isinstance(reference,dict) or set(reference)!={'generation','sha256','byte_count','content_type'}
                or reference['sha256']!=proof.sha256 or reference['byte_count']!=proof.byte_count or reference['content_type']!='video/mp4'):
            raise ValueError('Artifact registry changed.')
        stream,_,_=self._download(artifact_object_name(self.prefix,owner,job.id,proof.artifact_id),generation=reference['generation'],
            maximum=1024*1024*1024,mime='video/mp4',expected_sha=proof.sha256,expected_size=proof.byte_count)
        return stream


class CloudArtifactWriter(_PrivateBucket):
    """Worker-only write-once MP4 upload, verified locally and by pinned readback."""
    def __call__(self, owner, job, artifact_id, output_path, proof):
        from pathlib import Path
        if owner.subject!=job.owner_id or artifact_id!=proof.artifact_id:
            raise ValueError('Private artifact binding differs.')
        proof.validate(job)
        path=Path(output_path)
        if path.is_symlink() or not path.is_file():
            raise ValueError('A regular worker output is required.')
        spool=tempfile.SpooledTemporaryFile(max_size=8*1024*1024,mode='w+b')
        try:
            sink=_BoundedWriter(spool,proof.byte_count)
            with path.open('rb') as source:
                while chunk:=source.read(1024*1024):
                    sink.write(chunk)
            if sink.count!=proof.byte_count or sink.sha.hexdigest()!=proof.sha256:
                raise ValueError('Worker output changed after verification.')
            spool.seek(0)
            name=artifact_object_name(self.prefix,owner,job.id,artifact_id)
            self._require_private_bucket()
            blob=self.bucket.blob(name)
            blob.upload_from_file(spool,size=proof.byte_count,content_type='video/mp4',if_generation_match=0,
                timeout=self.timeout,retry=None,checksum='auto')
            gen=str(_generation(blob.generation))
            verified,_,_=self._download(name,generation=gen,maximum=1024*1024*1024,mime='video/mp4',
                expected_sha=proof.sha256,expected_size=proof.byte_count)
            verified.close()
            return dict(generation=gen,sha256=proof.sha256,byte_count=proof.byte_count,content_type='video/mp4')
        finally:
            spool.close()


class CloudProviderResponseStore(_PrivateBucket):
    """Private worker response archive bound to owner/job and object generation.

    Successful provider response bytes are preserved before journal reconciliation.
    No automatic retry or overwrite occurs. An upload timeout remains an unknown
    provider outcome; the provider must not be called again to recreate bytes.
    """
    def __init__(self, client, *, bucket, owner, job_id, prefix='byron-private', timeout=30):
        super().__init__(client,bucket=bucket,prefix=prefix,timeout=timeout)
        owner_namespace(owner);_safe_id(job_id)
        self.owner,self.job_id=owner,job_id

    def _response_name(self, operation_id):
        if not isinstance(operation_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',operation_id):
            raise ValueError('Invalid provider operation reference.')
        return self._name(self.owner,f'jobs/{self.job_id}/provider-responses/{operation_id}')

    def write_response(self, operation_id, response):
        from .provider_execution import WireResponse,REQUEST_ID,MAX_RESPONSE_BYTES
        if (not isinstance(response,WireResponse) or not REQUEST_ID.fullmatch(response.request_id)
                or not isinstance(response.payload,bytes) or not 0<len(response.payload)<=MAX_RESPONSE_BYTES):
            raise ValueError('A bounded provider response is required.')
        name=self._response_name(operation_id)
        gen=self._write(name,response.payload,'application/json')
        sha=hashlib.sha256(response.payload).hexdigest()
        stream,_,_=self._download(name,generation=gen,maximum=MAX_RESPONSE_BYTES,mime='application/json',
            expected_sha=sha,expected_size=len(response.payload))
        stream.close()
        return dict(request_id=response.request_id,response_ref=operation_id+':'+gen,
                    response_sha256=sha,response_bytes=len(response.payload))

    def read_response(self, receipt):
        from .provider_execution import WireResponse,REQUEST_ID,MAX_RESPONSE_BYTES
        if (not isinstance(receipt,dict) or not isinstance(receipt.get('request_id'),str)
                or not REQUEST_ID.fullmatch(receipt['request_id'])
                or not isinstance(receipt.get('response_ref'),str)
                or not isinstance(receipt.get('response_sha256'),str) or not SHA.fullmatch(receipt['response_sha256'])
                or type(receipt.get('response_bytes')) is not int or not 0<receipt['response_bytes']<=MAX_RESPONSE_BYTES):
            raise ValueError('Invalid provider receipt.')
        operation_id,gen=receipt['response_ref'].rsplit(':',1)
        stream,_,_=self._download(self._response_name(operation_id),generation=gen,maximum=MAX_RESPONSE_BYTES,
            mime='application/json',expected_sha=receipt['response_sha256'],expected_size=receipt['response_bytes'])
        try:
            return WireResponse(receipt['request_id'],stream.read())
        finally:
            stream.close()
