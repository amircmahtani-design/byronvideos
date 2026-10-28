"""One lean film per Cloud Run job execution: narration → animation → 5/2/3 film.

Run as `python3 -m byron_video.lean_worker --bootstrap-env NAME --reviewer-key-sha256 PIN
--lean-generation` with the film id in --job or BYRON_LEAN_JOB_ID. Config comes only from
the signed setup bundle; provider keys come from Secret Manager via the attached worker
identity. Each step's verified output is saved privately and recorded; a rerun skips it.
Unknown paid outcomes stop the film with a plain message and are never resent.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import time
from types import SimpleNamespace

from PIL import Image, ImageOps

from .elevenlabs_adapter import fingerprint, plan_requests
from .generation_coordinator import narration_timing
from .hosted_control import JOB_ID, Principal, VerifiedImage, canonical, require
from .lean_film import (LeanBudgetExceeded, LeanJobStore, LeanLedger, LeanObjects, lean_activation, lean_settings,
                        motion_approval, motion_cents, motion_operation, narration_approval, owner_for)
from .provider_execution import ProviderOutcomeUnknown, plan_anchored_motion, submit_motion_once

AUDIO = {'integrated_lufs': -16, 'true_peak_dbfs': -1, 'lra': 11}
ANCHOR_BYTES = 4 * 1024 * 1024
UNCERTAIN = ('A paid step could not be confirmed, so it was not repeated and nothing was charged twice. '
             'Ask the studio owner to check this film before trying again.')
TOO_SLOW = ('The painting animations took longer than expected. Nothing was charged twice; '
            'ask the studio owner to run this same film again to collect them.')
GENERIC = 'Your film could not be finished. Nothing was charged twice. Ask the studio owner to check this film.'


class LeanTimeout(RuntimeError):
    pass


def narrate(*, owner_id, job_id, text, profile, approval, journal, transport, response_store, job_root, expected_ffmpeg_version):
    """Exact full text, each planned chunk at most once; then decode, join, normalize."""
    from .provider_execution import execute_narration_once
    from .elevenlabs_release import build_narration_candidate
    execute_narration_once(owner_id=owner_id, job_id=job_id, text=text, profile=profile, approval=approval,
                           journal=journal, transport=transport, response_store=response_store)
    return build_narration_candidate(owner_id=owner_id, job_id=job_id, text=text, profile=profile, approval=approval,
        journal=journal, response_store=response_store, expected_profile_sha256=fingerprint(profile),
        expected_text_sha256=hashlib.sha256(text.encode()).hexdigest(), job_root=job_root,
        expected_ffmpeg_version=expected_ffmpeg_version, audio_settings=AUDIO, source_kind='provider_receipts')


def compose(**arguments):
    from .lean_composition import compose_film
    return compose_film(**arguments)


def motion_anchor(data, content_type):
    """fal anchor: the original bytes, or (only above 4 MB) a deterministic JPEG reduction."""
    if len(data) <= ANCHOR_BYTES:
        return data, content_type
    with Image.open(io.BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        for side in (2048, 1600, 1280):
            copy = image.copy(); copy.thumbnail((side, side), Image.Resampling.LANCZOS)
            out = io.BytesIO(); copy.save(out, format='JPEG', quality=90)
            if len(out.getvalue()) <= ANCHOR_BYTES:
                return out.getvalue(), 'image/jpeg'
    raise ValueError('The painting could not be prepared for animation.')


def sha(data):
    return hashlib.sha256(data).hexdigest()


class LeanPipeline:
    def __init__(self, *, owner_id, jobs, ledger_factory, uploads, objects, response_factory, narration_transport,
                 motion_transport, ffmpeg_version, workspace, activation_check=lambda: True, narrate=narrate, compose=compose,
                 timing=narration_timing, sleep=time.sleep, clock=time.monotonic, poll_seconds=5,
                 motion_deadline_seconds=1200, read_failures=6):
        self.owner, self.jobs, self.ledger_factory, self.uploads, self.objects = owner_id, jobs, ledger_factory, uploads, objects
        self.response_factory, self.narration, self.motion = response_factory, narration_transport, motion_transport
        self.ffmpeg_version, self.workspace, self.check = ffmpeg_version, Path(workspace), activation_check
        self.narrate, self.compose, self.timing, self.sleep, self.clock = narrate, compose, timing, sleep, clock
        self.poll, self.deadline, self.read_failures = poll_seconds, motion_deadline_seconds, read_failures

    def run(self, job_id):
        require(isinstance(job_id, str) and bool(JOB_ID.fullmatch(job_id)), 'Invalid film reference.')
        self.check()
        job = self.jobs.begin(self.owner, job_id)
        if job.status == 'complete':
            return job
        ledger = None
        try:
            ledger = self.ledger_factory(job.id)
            return self._run(job, ledger)
        except ProviderOutcomeUnknown:
            message = UNCERTAIN
        except LeanBudgetExceeded as error:
            message = str(error)
        except LeanTimeout:
            message = TOO_SLOW
        except Exception:
            message = GENERIC
        self._cost(job, ledger)
        return self.jobs.fail(self.owner, job.id, message)

    def _cost(self, job, ledger):
        try:
            i = job.inputs
            self.jobs.record_cost(self.owner, job.id, ledger.summary(i['plan_id'], i['plan_sha256'], i['allocations']))
        except Exception:
            pass  # Status display only; the ledger itself is authoritative.

    def _progress(self, job, value, message):
        self.check()
        self.jobs.progress(self.owner, job.id, value, message)

    def _run(self, job, ledger):
        inputs = job.inputs
        root = self.workspace / (job.id + '-' + secrets.token_hex(4))
        root.mkdir(parents=True, mode=0o700)
        self.check()
        ledger.fund(inputs['plan_id'], inputs['plan_sha256'], inputs['allocations'])  # Idempotent whole-film reservation.
        journal = ledger.operations(inputs['plan_id'], inputs['plan_sha256'], self.check)
        self._progress(job, 10, 'Recording the narration…')
        report, audio = self._narration(job, journal, root)
        self._cost(job, ledger)
        clips = self._animation(job, journal, root)
        self._cost(job, ledger)
        self._progress(job, 85, 'Putting your film together…')
        snapshot = {'images': [{'id': x['id'], 'sha256': x['sha256']} for x in inputs['images']], 'input_order': inputs['input_order'],
                    'narrated_text': inputs['narrated_text'], 'processing_plan': {'narration_profile': inputs['narration_profile']}}
        timing, _ = self.timing(SimpleNamespace(snapshot=snapshot), report, {x['id']: {} for x in inputs['images']})
        scenes = [{'image_id': x['id'], 'original_path': self._original(x, root), 'clip_path': clips[x['position']]} for x in inputs['images']]
        output = root / 'film' / 'film.mp4'
        manifest = self.compose(root=root, narration_wav=audio, report=report, timing=timing, scenes=scenes, output=output,
                                join_threshold=inputs['settings']['join_difference_threshold'])
        self._progress(job, 95, 'Saving your film…')
        video = self.objects.put_file(self.owner, job.id, 'film', output, 'video/mp4')
        saved = self.objects.put(self.owner, job.id, 'film-manifest', canonical({**manifest, 'timing': timing}).encode(), 'application/json')
        self.check()
        return self.jobs.complete(self.owner, job.id, {'video': video, 'manifest': saved, 'duration_seconds': manifest['duration_seconds'],
                                                       'flagged_joins': manifest.get('flagged_joins', 0), 'joins': manifest.get('joins', [])})

    def _narration(self, job, journal, root):
        folder = root / 'narration'
        step = job.steps.get('narration')
        if step:  # Verified saved narration: reuse, never request again.
            report = json.loads(self.objects.read(self.owner, job.id, step['report']))
            require(fingerprint(report) == step['report_sha256'], 'Saved narration report changed.')
            folder.mkdir(mode=0o700)
            audio = self.objects.save(self.owner, job.id, step['audio'], folder / 'narration.wav')
            require(sha(audio.read_bytes()) == report['audio_sha256'], 'Saved narration audio changed.')
            return report, audio
        inputs = job.inputs
        text, profile = inputs['narrated_text'], inputs['narration_profile']
        requests = plan_requests(text, profile)
        approval = narration_approval(job.id, requests, inputs['settings'], inputs['budget'])
        report = self.narrate(owner_id=self.owner, job_id=job.id, text=text, profile=profile, approval=approval, journal=journal,
                              transport=self.narration, response_store=self.response_factory(job.id), job_root=folder,
                              expected_ffmpeg_version=self.ffmpeg_version)
        require(report.get('exact_text') == text and report.get('media_verified') is True, 'Narration differs from the exact story.')
        audio = folder / 'narration.wav'
        require(sha(audio.read_bytes()) == report['audio_sha256'], 'Narration audio changed.')
        self.jobs.record_step(self.owner, job.id, 'narration', {
            'audio': self.objects.put_file(self.owner, job.id, 'narration', audio, 'audio/wav'),
            'report': self.objects.put(self.owner, job.id, 'narration-report', canonical(report).encode(), 'application/json'),
            'report_sha256': fingerprint(report), 'duration_seconds': report['duration_seconds']})
        return report, audio

    def _original(self, image, root):
        path = root / 'originals' / f"painting-{image['position']}{'.png' if image['content_type'] == 'image/png' else '.jpg'}"
        if path.exists():
            return path
        path.parent.mkdir(exist_ok=True, mode=0o700)
        record = VerifiedImage(**{k: image[k] for k in VerifiedImage.__dataclass_fields__})
        stream = self.uploads.read_original(Principal(self.owner), record)
        try:
            data = stream.read()
        finally:
            stream.close()
        require(sha(data) == image['sha256'], 'The original painting changed.')
        path.write_bytes(data)
        return path

    def _anchor(self, job, image, root):
        name = f"anchor_{image['position']}"
        ref = job.steps.get(name)
        if ref:
            return self.objects.read(self.owner, job.id, ref), ref['content_type']
        data, mime = motion_anchor(self._original(image, root).read_bytes(), image['content_type'])
        self.jobs.record_step(self.owner, job.id, name, self.objects.put(self.owner, job.id, f"anchor-{image['position']}", data, mime))
        return data, mime

    def _animation(self, job, journal, root):
        inputs, pending = job.inputs, {}
        settings, budget = inputs['settings'], inputs['budget']
        for image in inputs['images']:  # Submit every painting first, then poll them together.
            if f"clip_{image['position']}" in job.steps:
                continue
            data, mime = self._anchor(job, image, root)
            request = plan_anchored_motion(image_id=image['id'], original_bytes=data, content_type=mime,
                                           original_sha256=sha(data), prompt=settings['motion_prompt'])
            require(inputs['allocations'].get(motion_operation(self.owner, job.id, image['id'])) == motion_cents(settings),
                    'Animation quote differs from the funded film.')
            self.check()
            pending[image['position']] = (image, submit_motion_once(owner_id=self.owner, job_id=job.id, request=request,
                approval=motion_approval(job.id, request, settings, budget), journal=journal, transport=self.motion))
        total = len(inputs['images']); deadline = self.clock() + self.deadline; failures = {}
        while pending:
            for position, (image, handle) in list(pending.items()):
                try:
                    if self.motion.status(handle) != 'COMPLETED':
                        continue
                    media = self.motion.download(self.motion.result(handle))
                except ProviderOutcomeUnknown:
                    failures[position] = failures.get(position, 0) + 1  # Read-only polling may be repeated.
                    if failures[position] >= self.read_failures:
                        raise
                    continue
                body = media.get('bytes')
                require(isinstance(body, bytes) and body and sha(body) == media.get('sha256'), 'Downloaded clip changed.')
                self.jobs.record_step(self.owner, job.id, f'clip_{position}', {
                    'video': self.objects.put(self.owner, job.id, f'clip-{position}', body, 'video/mp4'),
                    'request_id': handle['request_id'], 'image_id': image['id']})
                del pending[position]
                self._progress(job, 35 + round(45 * (total - len(pending)) / total),
                               f'Bringing your paintings to life ({total - len(pending)} of {total} ready)…')
            if pending:
                if self.clock() >= deadline:
                    raise LeanTimeout()
                self._progress(job, 35 + round(45 * (total - len(pending)) / total),
                               f'Bringing your paintings to life ({total - len(pending)} of {total} ready)…')
                self.sleep(self.poll)
        steps = self.jobs.get(self.owner, job.id).steps
        folder = root / 'clips'; folder.mkdir(mode=0o700)
        clips = {}
        for image in inputs['images']:
            step = steps[f"clip_{image['position']}"]
            require(step['image_id'] == image['id'], 'Saved clip belongs to another painting.')
            clips[image['position']] = self.objects.save(self.owner, job.id, step['video'], folder / f"clip-{image['position']}.mp4")
        return clips


def _key_reader(secret_client, config, check, verify):
    def read(name):
        check(); verify()
        response = secret_client.access_secret_version(request={'name': config['secret_resources'][name] + '/versions/latest'}, retry=None, timeout=15)
        raw = response.payload.data
        require(isinstance(raw, bytes) and 8 <= len(raw) <= 512, 'Provider key is unavailable or malformed.')
        value = raw.decode('ascii')
        require(not any(c.isspace() for c in value), 'Provider key contains whitespace.')
        return value  # Never logged, stored or returned to a browser.
    return read


def build_pipeline(config, check, workspace, *, credential_bundle=None, sdk_factories=None, http_client_factory=None):
    from .cloud_storage import CloudUploadStore, CloudProviderResponseStore
    from .provider_execution import ElevenLabsHTTPTransport, FalQueueTransport
    from .workload_credentials import create_attached_credentials
    settings_ok = lean_settings(config); owner = owner_for(config)
    require(config['firestore']['database'] != '(default)' and settings_ok, 'Lean generation needs its dedicated database.')
    credentials = credential_bundle or create_attached_credentials(config, role='worker', activation_check=check, enabled=True)
    credentials.verify_identity()
    if sdk_factories is None:
        try:
            from google.cloud import firestore, storage, secretmanager
        except ImportError:
            raise RuntimeError('Pinned Google SDK runtime is missing; no provider request attempted.') from None
        sdk_factories = dict(firestore=firestore.Client, storage=storage.Client, secrets=secretmanager.SecretManagerServiceClient)
    target, db = config['target'], config['firestore']
    project, bucket = target['project_id'], target['bucket']
    storage_client = sdk_factories['storage'](project=project, credentials=credentials.credentials)
    firestore_client = sdk_factories['firestore'](project=project, database=db['database'], credentials=credentials.credentials)
    require(getattr(firestore_client, '_database_string', None) == f"projects/{project}/databases/{db['database']}",
            'SDK client must address exactly the configured named database.')
    keys = _key_reader(sdk_factories['secrets'](credentials=credentials.credentials), config, check, credentials.verify_identity)
    dbargs = dict(project_id=project, namespace=db['namespace'])
    return LeanPipeline(owner_id=owner, jobs=LeanJobStore(firestore_client, **dbargs),
        ledger_factory=lambda job_id: LeanLedger(firestore_client, **dbargs, owner_id=owner, job_id=job_id, budget=config['budget']),
        uploads=CloudUploadStore(storage_client, bucket=bucket), objects=LeanObjects(storage_client, bucket=bucket),
        response_factory=lambda job_id: CloudProviderResponseStore(storage_client, bucket=bucket, owner=Principal(owner), job_id=job_id),
        narration_transport=ElevenLabsHTTPTransport(lambda: keys('elevenlabs'), enabled=True, client_factory=http_client_factory),
        motion_transport=FalQueueTransport(lambda: keys('fal'), enabled=True, client_factory=http_client_factory),
        ffmpeg_version=config['runtime']['ffmpeg_version'], workspace=workspace, activation_check=check)


def main(argv=None, *, environ=None):
    parser = argparse.ArgumentParser(description='Run one lean owner-only film from the signed setup bundle; never deploys.')
    parser.add_argument('--bootstrap-env', required=True); parser.add_argument('--reviewer-key-sha256', required=True)
    parser.add_argument('--lean-generation', action='store_true', help='Explicit opt-in to lean paid generation.')
    parser.add_argument('--job'); parser.add_argument('--workspace', type=Path, default=Path('/tmp/byron-lean'))
    parser.add_argument('--sdk-lock', type=Path, default=Path('/opt/byron-runtime/sdk-lock.json'))
    args = parser.parse_args(argv)
    values = os.environ if environ is None else environ
    try:
        require(args.lean_generation, 'Lean generation requires the explicit --lean-generation flag.')
        job_id = args.job or values.get('BYRON_LEAN_JOB_ID', '')
        require(bool(JOB_ID.fullmatch(job_id)), 'Explicit film reference required.')
        from .bootstrap_bundle import load_bootstrap_env
        from .worker_bootstrap import private_bytes, validate_runtime
        bundle = load_bootstrap_env(args.bootstrap_env, args.reviewer_key_sha256, environ=values, administration_only=True)
        config, plan = bundle['config'], bundle['processing_plan']
        check = lean_activation(config, plan, bundle['acceptance'], bundle['reviewer_public_key'])
        validate_runtime(config, private_bytes(args.sdk_lock, 128_000))
        pipeline = build_pipeline(config, check, args.workspace)
    except Exception:
        parser.exit(2, 'Lean worker configuration is incomplete or unverified; nothing was generated.\n')
    job = pipeline.run(job_id)
    print(json.dumps({'job_id': job_id, 'status': job.status}))
    return 0 if job.status == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
