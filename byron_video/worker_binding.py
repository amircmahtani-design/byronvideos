"""Explicit worker binding and full media verification, never an auto-start service.

No SDK, credentials, account identifiers, transport retry or compositor is selected
implicitly. Existing landscape/Kokoro rendering is NOT a portrait substitute.
"""
from __future__ import annotations
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import uuid

from . import cloud_render as cloud
from .elevenlabs_adapter import fingerprint
from .hosted_control import VerifiedArtifact, MAX_RESULT_BYTES


def require(value, message):
    if not value:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def safe_private_root(root):
    root = Path(root)
    require(root.is_absolute() and not any(p.is_symlink() for p in (root, *root.parents)), 'Use an absolute private workspace without symlinks.')
    require(not root.exists(), 'Never reuse a worker execution workspace.')
    public = Path(__file__).resolve().parent.parent / 'web'
    require(not root.resolve().is_relative_to(public), 'Worker workspace must not be public.')
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    return root


def copy_verified_object(reader, ref, path):
    """Stream exactly the generation/type/length/hash into a create-only file."""
    response = reader(dict(ref))
    stream = response.get('stream') if isinstance(response, dict) else None
    path = Path(path)
    created = False
    try:
        require(isinstance(response, dict) and set(response) == {'generation','content_type','stream'}
                and response['generation'] == ref['generation'] and response['content_type'] == ref['content_type'],
                'Storage generation or type differs from the frozen reference.')
        require(hasattr(stream,'read') and hasattr(stream,'close'), 'A bounded binary reader is required.')
        sha, count = hashlib.sha256(), 0
        with path.open('xb') as out:
            created = True
            path.chmod(0o600)
            while True:
                size = min(65536, ref['bytes'] + 1 - count)
                require(size > 0, 'Object exceeded its frozen byte length.')
                chunk = stream.read(size)
                require(isinstance(chunk,bytes) and len(chunk)<=size, 'Object reader violated its chunk bound.')
                if not chunk:
                    break
                count += len(chunk)
                require(count<=ref['bytes'], 'Object exceeded its frozen byte length.')
                sha.update(chunk); out.write(chunk)
        require(count==ref['bytes'] and sha.hexdigest()==ref['sha256'], 'Object bytes differ from the frozen reference.')
        return path
    except BaseException:
        if created and path.exists() and not path.is_symlink():
            path.unlink()
        raise
    finally:
        if hasattr(stream,'close'):
            stream.close()


def stage_bound_inputs(*, target, actual_target, manifest, manifest_ref, trusted_job, reader, root):
    """Production-capable bounded staging; transport remains explicitly injected.

    actual_target and trusted_job must be independently loaded by trusted server
    adapters, never accepted from a creator request or manifest itself. This new
    path does not modify the old cloud_render synthetic-only fixture harness.
    """
    cloud.validate_deployed_target(actual_target,target)
    manifest = cloud.validate_manifest(manifest,target,trusted_job=trusted_job)
    cloud.build_execution_request(target,manifest,manifest_ref,trusted_job=trusted_job)
    root = safe_private_root(root)
    downloaded = copy_verified_object(reader,manifest_ref,root/'render.json')
    require(downloaded.read_bytes()==canonical(manifest), 'Stored manifest does not match the canonical independently validated manifest.')
    refs = [('composition.json',manifest['composition']),('narration.wav',manifest['narration'])]
    refs += [(f'clip-{i:02}.mp4', item['video']) for i,item in enumerate(manifest['clips'],1)]
    files = {name:copy_verified_object(reader,ref,root/name) for name,ref in refs}
    return {'root':root,'files':files,'manifest_sha256':manifest['sha256'],
            'target_sha256':fingerprint(target),'snapshot_sha256':trusted_job['snapshot_sha256'],
            'owner_id':trusted_job['owner_id'],'job_id':trusted_job['job_id']}


@dataclass(frozen=True)
class SemanticEvidence:
    """Returned by an independent trusted compositor/content verifier only.

    Codec/container metadata cannot prove spoken words, caption words or scene
    identity/order. No default verifier exists; false or missing evidence fails.
    """
    output_sha256: str
    snapshot_sha256: str
    source_order: tuple[str,...]
    narrated_text_sha256: str
    caption_text_sha256: str
    narration_content_verified: bool = False
    captions_content_verified: bool = False
    source_order_verified: bool = False


def command(args, *, timeout):
    return subprocess.run(args,check=True,capture_output=True,timeout=timeout)


def _file_digest(path):
    h=hashlib.sha256(); total=0
    with path.open('rb') as source:
        while chunk:=source.read(1024*1024):
            total+=len(chunk)
            require(total<=MAX_RESULT_BYTES,'Rendered film exceeds the private result limit.')
            h.update(chunk)
    return total,h.hexdigest()


def verify_artifact(path, *, job, artifact_id, semantic_verifier, expected_ffmpeg_version, run=command, allow_candidate=False):
    """Probe AND decode the entire MP4 before minting a VerifiedArtifact.

    Local file must be immutable/private during verification and publication.
    Historical mode requires independent SemanticEvidence. Explicit candidate
    mode instead requires the concrete provenance/integrity verifier and must
    only be consumed by a distinct preview/exact-film artifact wrapper.
    """
    require(callable(semantic_verifier),'A trusted narration/caption/source verifier is required.')
    path=Path(path)
    require(path.is_absolute() and path.is_file() and not any(p.is_symlink() for p in (path,*path.parents)), 'Use a private regular film file without symlinks.')
    version=run(['ffmpeg','-version'],timeout=15).stdout.decode().splitlines()[0].split()[2]
    require(version==expected_ffmpeg_version,'FFmpeg runtime differs from the reviewed worker binding.')
    byte_count,sha=_file_digest(path)
    require(byte_count>0,'Rendered film is empty.')
    raw=run(['ffprobe','-v','error','-protocol_whitelist','file,pipe','-f','mov','-enable_drefs','0','-use_absolute_path','0','-show_entries','format=duration,format_name:stream=index,codec_type,codec_name,width,height,avg_frame_rate','-of','json',str(path)],timeout=60).stdout
    require(len(raw)<=65536,'Probe response exceeds its metadata bound.')
    probe=json.loads(raw)
    streams=probe.get('streams',[])
    videos=[s for s in streams if s.get('codec_type')=='video']
    audios=[s for s in streams if s.get('codec_type')=='audio']
    require(len(videos)==len(audios)==1 and len(streams)==2,'Require exactly one video stream and one audio stream.')
    video,audio=videos[0],audios[0]
    require(video.get('codec_name')=='h264' and audio.get('codec_name')=='aac','Require H.264/AAC output.')
    require((video.get('width'),video.get('height'))==(1080,1920) and Fraction(video.get('avg_frame_rate','0'))==24,'Require frozen portrait 1080x1920 at 24fps.')
    duration=float(probe['format']['duration'])
    require(math.isfinite(duration) and 0<duration<=900 and 'mp4' in probe['format'].get('format_name','').split(','),'Require a finite MP4 within the duration limit.')
    # -xerror fails on decoding errors; no truncation (-t) or frame sampling.
    run(['ffmpeg','-nostdin','-v','error','-xerror','-protocol_whitelist','file,pipe','-f','mov','-enable_drefs','0','-use_absolute_path','0','-i',str(path),'-map','0:v:0','-map','0:a:0','-f','null','-'],timeout=1800)
    if allow_candidate:
        from .candidate_media_evidence import RetainedMediaVerifier
        require(type(semantic_verifier) is RetainedMediaVerifier,'Arbitrary candidate truth callbacks are forbidden.')
    evidence=semantic_verifier(path,job)
    if allow_candidate:
        from .candidate_media_evidence import CandidateMediaEvidence,RetainedMediaVerifier
        require(type(semantic_verifier) is RetainedMediaVerifier and type(evidence) is CandidateMediaEvidence
                and evidence.output_sha256==sha and evidence.snapshot_sha256==job.snapshot_sha256,
                'Concrete candidate retained-input fidelity verifier required.')
    else:
        require(isinstance(evidence,SemanticEvidence) and evidence.output_sha256==sha
                and evidence.snapshot_sha256==job.snapshot_sha256
                and evidence.narration_content_verified is True and evidence.captions_content_verified is True
                and evidence.source_order_verified is True,'Independent semantic evidence is missing or belongs to another film.')
    require(_file_digest(path)==(byte_count,sha),'Film bytes changed during verification.')
    require(job.receipt is not None,'A committed worker execution receipt is required.')
    artifact_type=VerifiedArtifact
    if allow_candidate:
        from .candidate_media_evidence import CandidateVerifiedMedia
        artifact_type=CandidateVerifiedMedia
    artifact=artifact_type(artifact_id,job.id,job.owner_id,job.snapshot_sha256,job.receipt.execution_id,
        sha,byte_count,duration,1080,1920,'h264','aac',24,tuple(evidence.source_order),
        evidence.narrated_text_sha256,evidence.caption_text_sha256,True,'ffmpeg-full-decode-v1')
    artifact.validate(job)
    return artifact


def execute_worker(*, target, actual_target, manifest, manifest_ref, job, execution_id,
                   reader, root, compositor, semantic_verifier, artifact_writer,
                   expected_ffmpeg_version, enabled=False, run=command, activation_check=None):
    """Single execution; no retries, implicit synthesis or completion mutation.

    compositor(staged, frozen_snapshot, output_path) must be the separately
    reviewed portrait compositor. It must not generate paid media implicitly.
    artifact_writer(owner, job, artifact_id, output_path, proof) publishes immutable private
    bytes and verifies readback; it returns its private storage reference. A
    trusted orchestrator reconciles proof only after that writer succeeds.
    """
    require(enabled is True,'Worker execution is disabled until explicitly bound.')
    def active():
        if activation_check is not None:require(activation_check() is True,'Worker acceptance expired.')
    active()
    require(callable(compositor) and callable(semantic_verifier) and callable(artifact_writer),
            'Reviewed portrait compositor, semantic verifier and private artifact writer are required.')
    require(job.receipt is not None and job.receipt.execution_id==execution_id,'Worker execution differs from the committed dispatch receipt.')
    trusted={'job_id':job.id,'owner_id':job.owner_id,'snapshot_sha256':job.snapshot_sha256,'frozen_job':job.snapshot}
    staged=stage_bound_inputs(target=target,actual_target=actual_target,manifest=manifest,manifest_ref=manifest_ref,
                              trusted_job=trusted,reader=reader,root=root)
    output=staged['root']/'film.mp4'
    active();compositor(staged,job.snapshot,output)
    proof=verify_artifact(output,job=job,artifact_id=uuid.uuid4().hex,semantic_verifier=semantic_verifier,
                          expected_ffmpeg_version=expected_ffmpeg_version,run=run)
    from .hosted_control import Principal
    active()
    reference=artifact_writer(Principal(job.owner_id),job,proof.artifact_id,output,proof)
    require(_file_digest(output)==(proof.byte_count,proof.sha256),'Output changed during publication.')
    return {'artifact':proof,'private_reference':reference,'target_sha256':fingerprint(target),
            'manifest_sha256':manifest['sha256'],'completion_reconciled':False}


def validate_container_base(value):
    require(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}',value),
            'Supply an independently reviewed digest-pinned runtime image; no mutable default exists.')
    return value




class DispatchPending(RuntimeError):
    """An operation may exist; recover it by GET, never submit the run again."""


class HttpxCloudRunTransport:
    """Non-retrying bounded JSON transport with explicitly injected token reader.

    No application-default credentials are discovered. Construction makes no
    requests. Tokens must be obtained by the separately reviewed workload auth
    adapter, never supplied by browser requests or committed source.
    """
    def __init__(self, token_reader, client_factory=None):
        self.token_reader,self.client_factory=token_reader,client_factory

    def __call__(self, method, url, body):
        return self.submit_authorized(method,url,body,None)

    def submit_authorized(self,method,url,body,authorization_check):
        require(authorization_check is None or callable(authorization_check),'Invalid final dispatch gate.')
        require(method=='POST' and re.fullmatch(r'https://run\.googleapis\.com/v2/projects/[a-z][a-z0-9-]+/locations/[a-z]+-[a-z]+[0-9]/jobs/byron-renderer(?:-[a-z0-9-]+)?:run',url), 'Unexpected Cloud Run operation URL.')
        token=self.token_reader()
        require(isinstance(token,str) and token.isascii() and len(token)>=16 and not any(c.isspace() for c in token),'A workload token is required.')
        if self.client_factory is None:
            try:
                import httpx
            except ImportError:
                raise RuntimeError('Install the reviewed HTTP runtime before activation.') from None
            factory=httpx.Client
        else:factory=self.client_factory
        try:
            with factory(timeout=30,follow_redirects=False,trust_env=False) as client:
                if authorization_check is not None:
                    require(authorization_check() is True,'Exact render authorization expired before transmission.')
                with client.stream(method,url,headers={'Authorization':'Bearer '+token},json=body) as response:
                    require(response.status_code==200,'Cloud Run submission could not be confirmed.')
                    raw=bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        require(len(raw)<=65536,'Cloud Run operation response exceeds its bound.')
                    result=json.loads(raw)
                    require(isinstance(result,dict),'Cloud Run returned no operation record.')
                    return result
        except Exception:
            raise DispatchPending('Cloud Run submission outcome is uncertain. Recover the existing operation; never resubmit automatically.') from None


class CloudRunDispatcher:
    """Actual one-shot run request; default disabled and never a synthetic fixture.

    target_provider() must independently fetch and normalize the deployed target.
    handoff_loader(job) returns validated manifest/ref. journal reserves BEFORE
    transport and records the operation. execution_resolver(operation,target,job)
    must independently GET/verify the corresponding execution and return the
    binding record described in _receipt. An operation name is NOT an execution
    receipt. Without verified execution the job remains HOLD, not fake queued.
    """
    def __init__(self, *, target, target_provider, store, journal, handoff_loader,
                 transport, execution_resolver=None, enabled=False, activation_check=None):
        self.target=cloud.validate_target(target)
        self.target_provider,self.store,self.journal=target_provider,store,journal
        self.handoff_loader,self.transport=handoff_loader,transport
        self.execution_resolver,self.enabled=execution_resolver,enabled
        require(activation_check is None or callable(activation_check),'Invalid activation verifier.')
        self.activation_check=activation_check

    def _check_activation(self):
        require(self.enabled is True,'Cloud Run dispatch is disabled.')
        if self.activation_check is not None:require(self.activation_check() is True,'Activation verification did not explicitly succeed.')

    def _receipt(self,operation,job):
        self._check_activation()
        if self.execution_resolver is None:
            raise DispatchPending('Cloud Run operation recorded; independent execution verification is still required.')
        binding=self.execution_resolver(operation,dict(self.target),job)
        prefix=f"projects/{self.target['project_id']}/locations/{self.target['region']}/jobs/{self.target['job_name']}/executions/"
        require(isinstance(binding,dict) and set(binding)=={'operation_name','execution_name','target_sha256','snapshot_sha256'}
            and binding['operation_name']==operation and binding['target_sha256']==fingerprint(self.target)
            and binding['snapshot_sha256']==job.snapshot_sha256 and isinstance(binding['execution_name'],str)
            and binding['execution_name'].startswith(prefix),'Execution verification does not bind the submitted operation/target/job.')
        execution=binding['execution_name'][len(prefix):]
        require(re.fullmatch(r'[A-Za-z0-9_-]{8,128}',execution),'Invalid verified execution identity.')
        from .hosted_control import DispatchReceipt
        receipt=DispatchReceipt(job.id,job.snapshot_sha256,execution);receipt.validate(job)
        return receipt

    def __call__(self,handoff):
        self._check_activation()
        job=self.store.get(handoff['owner_id'],handoff['job_id'])
        require(job is not None and job.id==handoff['job_id'] and job.owner_id==handoff['owner_id'] and job.state=='HOLD' and job.receipt is None
                and job.snapshot_sha256==handoff['snapshot_sha256'] and job.snapshot==handoff['frozen_job'],
                'A matching committed HOLD checkpoint is required before dispatch.')
        actual=self.target_provider();cloud.validate_deployed_target(actual,self.target)
        self._check_activation()
        manifest,reference=self.handoff_loader(job)
        self._check_activation()
        request=cloud.build_execution_request(self.target,manifest,reference,trusted_job=handoff)
        request_sha=fingerprint(request)
        self._check_activation()
        created=self.journal.reserve_dispatch(job.owner_id,job.id,job.snapshot_sha256,request_sha)
        require(created is True,'This dispatch was already armed; recover it without another POST.')
        try:
            self._check_activation()
            result=self.transport('POST',request['url'],request['json'])
            operation=result.get('name') if isinstance(result,dict) else None
            prefix=f"projects/{self.target['project_id']}/locations/{self.target['region']}/operations/"
            require(isinstance(operation,str) and operation.startswith(prefix)
                    and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',operation[len(prefix):]),'Cloud Run returned an invalid operation identity.')
            self.journal.record_dispatch_operation(job.owner_id,job.id,job.snapshot_sha256,operation)
            return self._receipt(operation,job)
        except Exception:
            raise DispatchPending('Dispatch may have been accepted; keep HOLD and recover the recorded operation without resubmitting.') from None

    def recover(self,owner_id,job_id):
        self._check_activation()
        job=self.store.get(owner_id,job_id)
        require(job is not None and job.owner_id==owner_id,'Private job not found.')
        operation=self.journal.get_dispatch_operation(owner_id,job_id,job.snapshot_sha256)
        if not operation:raise DispatchPending('No operation receipt is recorded; manual recovery is required, never a new POST.')
        cloud.validate_deployed_target(self.target_provider(),self.target)
        return self._receipt(operation,job)



def execute_and_commit_worker(*, artifact_registry, **arguments):
    """Reconcile completion only after verified immutable writer readback.

    The registry must atomically persist the private generation reference and
    mark the matching execution/job complete. A failed/unknown commit is surfaced
    without re-running the compositor or publishing another output.
    """
    result=execute_worker(**arguments)
    from .hosted_control import Principal
    job=arguments['job']
    activation=arguments.get('activation_check')
    if activation is not None:require(activation() is True,'Worker acceptance expired before artifact commit.')
    committed=artifact_registry.commit_verified(Principal(job.owner_id),job,result['artifact'],result['private_reference'])
    return {**result,'completion_reconciled':True,'committed_job':committed}


if __name__=='__main__':
    # Backwards-compatible CLI delegates to the explicit validate-only bootstrap.
    from .worker_bootstrap import main
    raise SystemExit(main())
