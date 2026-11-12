"""Fresh-job narration generation, native decode and immutable private publication.

Default disabled. Real execution still requires separately approved cost/provider
scope and a reviewed runtime; no credential or live acceptance is inferred.
"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
from .cloud_journal import _FirestoreBoundary
from .cloud_storage import _PrivateBucket
from .cloud_render import validate_object
from .elevenlabs_adapter import fingerprint,plan_requests
from .elevenlabs_release import build_narration_candidate
from .provider_execution import execute_narration_once,_narration_operations,ProviderOutcomeUnknown
from .hosted_control import canonical


def need(ok,message):
    if not ok:raise ValueError(message)


def prefix(job):
    return 'owners/'+hashlib.sha256(job.owner_id.encode()).hexdigest()+'/jobs/'+job.id+'/'


class CloudNarrationAudioStore(_PrivateBucket):
    def __init__(self,client,*,bucket,timeout=30):
        super().__init__(client,bucket=bucket,timeout=timeout);self.bucket_name=bucket
    def _name_for(self,job):return prefix(job)+'narration/narration.wav'
    def _read(self,job,sha,size,generation=None):
        stream,gen,actual=self._download(self._name_for(job),generation=generation,maximum=100_000_000,
            mime='audio/wav',expected_sha=sha,expected_size=size)
        stream.close()
        return dict(bucket=self.bucket_name,object=self._name_for(job),generation=gen,sha256=actual,bytes=size,content_type='audio/wav')
    def publish(self,job,data):
        need(isinstance(data,bytes) and 0<len(data)<=100_000_000,'Bounded native narration bytes required.')
        generation=self._write(self._name_for(job),data,'audio/wav')
        return self._read(job,hashlib.sha256(data).hexdigest(),len(data),generation)
    def reconcile(self,job,sha,size):
        """Read-only discovery/readback of an ambiguous prior write; never upload."""
        return self._read(job,sha,size)
    def verify(self,job,reference):
        validate_object(reference,bucket=self.bucket_name,prefix=prefix(job),content_type='audio/wav',max_bytes=100_000_000)
        need(reference['object']==self._name_for(job),'Narration object role changed.')
        need(self._read(job,reference['sha256'],reference['bytes'],reference['generation'])==reference,'Narration reference changed.')
        return reference


class FirestoreNarrationRecords(_FirestoreBoundary):
    def _ref(self,job):return self._doc(self._owner(job.owner_id)+'/native_narration/'+job.id)
    def load(self,job,approval_sha256):
        row=self._read(self._ref(job))
        if row is None:return None
        need(row.get('owner_id')==job.owner_id and row.get('job_id')==job.id and row.get('snapshot_sha256')==job.snapshot_sha256
             and row.get('approval_sha256')==approval_sha256 and row.get('state') in {'HOLD','READY'},'Committed narration identity changed.')
        report=json.loads(row['report_json'])
        need(fingerprint(report)==row['report_sha256'] and canonical(report)==row['report_json'],'Committed narration report changed.')
        return {**row,'report':report}
    def reserve(self,job,approval_sha256,report,audio_bytes):
        need(type(audio_bytes) is int and 0<audio_bytes<=100_000_000,'Bounded native narration required.')
        need(report.get('exact_text')==job.snapshot['narrated_text']
             and report.get('profile_sha256')==fingerprint(job.snapshot['processing_plan']['narration_profile'])
             and report.get('provider_receipts_verified') is True and report.get('media_verified') is True,
             'Report must bind exact frozen narration and independently decoded provider receipts.')
        record=dict(owner_id=job.owner_id,job_id=job.id,snapshot_sha256=job.snapshot_sha256,approval_sha256=approval_sha256,
            report_json=canonical(report),report_sha256=fingerprint(report),audio_bytes=audio_bytes,state='HOLD',narration_ref=None)
        need(len(canonical(record).encode())<=800_000,'Narration report exceeds private document bound.')
        ref=self._ref(job)
        def reserve(tx):
            need(self._read(ref,tx) is None,'Narration publication already reserved; reconcile instead of uploading again.')
            tx.create(ref,record)
        self._run(reserve)
    def complete(self,job,approval_sha256,reference):
        ref=self._ref(job)
        def complete(tx):
            row=self._read(ref,tx)
            need(row is not None and row['owner_id']==job.owner_id and row['job_id']==job.id
                 and row['snapshot_sha256']==job.snapshot_sha256 and row['approval_sha256']==approval_sha256,
                 'Narration publication identity changed.')
            report=json.loads(row['report_json'])
            need(reference['sha256']==report['audio_sha256'] and reference['bytes']==row['audio_bytes'],'Published audio differs from native report.')
            if row['state']=='READY':
                need(row['narration_ref']==reference,'Completed narration reference changed.');return
            need(row['state']=='HOLD','Narration publication state changed.')
            tx.update(ref,{'state':'READY','narration_ref':reference})
        self._run(complete)


class _ReservedNarrationJournal:
    def __init__(self,journal,plan_id,plan_sha,before_claim):self.journal,self.plan_id,self.plan_sha,self.before_claim=journal,plan_id,plan_sha,before_claim
    def get(self,operation):
        row=self.journal.get_reserved(operation,self.plan_id,self.plan_sha)
        return None if row['state']=='reserved' else row
    def arm(self,operation,binding,cost,cap):
        self.before_claim()
        return self.journal.claim_reserved(operation,binding,cost,cap,self.plan_id,self.plan_sha)
    def record(self,*args):return self.journal.record(*args)


class NarrationStage:
    def __init__(self,*,journal_factory,response_store_factory,transport,audio_store,records,expected_ffmpeg_version,
                 activation_check,approval_loader=None,submission_check=None,source_kind='provider_receipts',enabled=False):
        need(source_kind in {'provider_receipts','synthetic_test'} and callable(activation_check),'Explicit narration provenance and activation verifier required.')
        need(source_kind=='synthetic_test' or callable(submission_check),'Current independently reviewed narration quote verifier required.')
        self.submission_check=submission_check
        self.journal_factory,self.response_factory,self.transport=journal_factory,response_store_factory,transport
        self.audio,self.records,self.version=audio_store,records,expected_ffmpeg_version
        self.activation_check,self.approval_loader,self.source_kind,self.enabled=activation_check,approval_loader,source_kind,enabled is True
    def _active(self):need(self.enabled and self.activation_check() is True,'Narration stage is disabled or deployment acceptance expired.')
    def __call__(self,job,root):
        need(callable(self.approval_loader),'Independent narration cost approval loader required.')
        return self.stage(job,root,self.approval_loader(job))
    def stage(self,job,root,approval):
        self._active()
        need(fingerprint(job.snapshot)==job.snapshot_sha256,'Frozen narration job changed.')
        text=job.snapshot['narrated_text'];profile=job.snapshot['processing_plan']['narration_profile']
        approval_sha=fingerprint(asdict(approval));requests=plan_requests(text,profile)
        operations=_narration_operations(job.owner_id,job.id,requests,approval)
        existing=self.records.load(job,approval_sha)
        if existing is not None:
            if existing['state']=='READY':reference=self.audio.verify(job,existing['narration_ref'])
            else:
                try:
                    reference=self.audio.reconcile(job,existing['report']['audio_sha256'],existing['audio_bytes'])
                    self._active();self.records.complete(job,approval_sha,reference)
                except Exception:
                    raise ProviderOutcomeUnknown('Native audio publication remains unresolved. Read-only reconciliation only; never resubmit narration or upload again.') from None
            return dict(report=existing['report'],report_sha256=existing['report_sha256'],narration_ref=reference)
        root=Path(root)
        need(root.is_absolute() and not any(p.is_symlink() for p in (root,*root.parents))
             and not root.resolve().is_relative_to(Path(__file__).resolve().parent.parent/'web'),'Private non-symlink narration workspace required.')
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        version=subprocess.run(['ffmpeg','-version'],check=True,capture_output=True,timeout=15).stdout.decode().splitlines()[0].split()[2]
        need(version==self.version,'Narration runtime differs from the reviewed version; no provider request attempted.')
        journal=self.journal_factory(job)
        need(journal.owner_id==job.owner_id and journal.job_id==job.id,'Narration spend journal differs from this owner/job.')
        plan_id='narration_plan_'+hashlib.sha256((job.owner_id+'\0'+job.id).encode()).hexdigest()
        plan_sha=fingerprint({'snapshot_sha256':job.snapshot_sha256,'approval':asdict(approval),'requests':requests})
        def new_submission():
            self._active()
            if self.submission_check is not None:need(self.submission_check(job,approval) is True,'Narration quote expired or approval changed.')
        if journal.get_plan_reservation(plan_id,plan_sha) is None:new_submission()
        journal.reserve_plan(plan_id,plan_sha,{op:cost for op,binding,request,cost in operations},approval.job_cap_cents)
        funded=_ReservedNarrationJournal(journal,plan_id,plan_sha,new_submission);responses=self.response_factory(job)
        def transmit(request):new_submission();return self.transport(request)
        execute_narration_once(owner_id=job.owner_id,job_id=job.id,text=text,profile=profile,approval=approval,
            journal=funded,transport=transmit,response_store=responses)
        with tempfile.TemporaryDirectory(prefix='native-narration-',dir=root) as scratch:
            report=build_narration_candidate(owner_id=job.owner_id,job_id=job.id,text=text,profile=profile,approval=approval,
                journal=funded,response_store=responses,expected_profile_sha256=fingerprint(profile),expected_text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                job_root=Path(scratch)/'decoded',expected_ffmpeg_version=self.version,
                audio_settings={'integrated_lufs':-16,'true_peak_dbfs':-1,'lra':11},source_kind=self.source_kind)
            data=(Path(scratch)/'decoded/narration.wav').read_bytes()
            self._active();self.records.reserve(job,approval_sha,report,len(data))
            try:
                self._active();reference=self.audio.publish(job,data)
                self._active();self.records.complete(job,approval_sha,reference)
            except Exception:
                raise ProviderOutcomeUnknown('Narration audio publication is unresolved. Keep provider reservations and reconcile existing audio; do not resubmit.') from None
        return dict(report=report,report_sha256=fingerprint(report),narration_ref=reference)
