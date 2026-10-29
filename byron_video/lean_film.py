"""Lean owner-only film pipeline: frozen jobs, funded cost ledger, private media.

Additive to the signed setup-only bundle. Lean generation is enabled only by the
explicit --lean-generation flag AND a signed `lean_generation` configuration
section (the config digest is bound by the setup signature). Every paid call is
reserved in a Firestore transaction before transmission; an uncertain reservation
is never retried automatically. No Gemini/Vertex call or extend-video exists here.
"""
import copy
import hashlib
import json
import math
import re
import secrets
import time

from .cloud_journal import _FirestoreBoundary, FirestoreSpendJournal, _hash, _key, _safe_owner
from .cloud_storage import _PrivateBucket, _code, _generation
from .elevenlabs_adapter import fingerprint, validate_profile
from .hosted_control import IdempotencyConflict, Principal, canonical, digest, require, JOB_ID, SHA256
from .provider_execution import SpendApproval, _narration_operations

MODEL = 'eleven_multilingual_v2'
MOTION_SECONDS = 10
DEFAULT_PROMPT = ('Very subtle, gentle natural motion within the painting. Keep the original composition, colours, '
                  'brushwork and framing unchanged. Slow, calm, minimal camera movement. No new objects, people, text or actions.')
DEFAULTS = {'motion_cents_per_second': 4, 'narration_cents_per_1000_characters': 30,
            'motion_prompt': DEFAULT_PROMPT, 'join_difference_threshold': 18.0}
STATES = ('queued', 'running', 'complete', 'failed')
STARTS = ('pending', 'requested', 'started', 'uncertain')
STEP = re.compile(r'[a-z][a-z0-9_]{0,40}\Z')
FIELDS = {'owner_id', 'job_id', 'key_sha256', 'request_sha256', 'inputs_json', 'inputs_sha256', 'status', 'progress',
          'message', 'steps_json', 'cost_json', 'result_json', 'start_state', 'operation', 'created_at', 'updated_at'}
QUEUED = 'Your film is waiting to start.'
READY = 'Your film is ready to watch and download.'
START_UNCERTAIN = ('We could not confirm that your film has started. Please check again in a few minutes. '
                   'It will not be started or charged twice.')


class LeanBudgetExceeded(ValueError):
    """Plain-English refusal; nothing was reserved, started or charged."""


class LeanBusy(ValueError):
    """Another film is still being made; nothing was reserved, started or charged."""


# The renderer job times out at 1800 s; a film silent for longer has stopped without reporting back.
STALE_SECONDS = 2100
BUSY = 'A film is already being made. Wait for it to finish or fail before starting another. Nothing was started or charged.'
STOPPED = ('Stopped while the film was being made: the film job stopped without reporting back '
           '(it may have run out of time or memory). Nothing was charged twice. (Code: worker stopped)')


def dollars(cents):
    return f'${cents // 100}.{cents % 100:02}'


def validate_lean_settings(value):
    require(isinstance(value, dict) and value.get('schema_version') == 1 and type(value['schema_version']) is int
            and set(value) <= set(DEFAULTS) | {'schema_version'}, 'Exact lean generation settings required.')
    result = {**DEFAULTS, **{k: v for k, v in value.items() if k != 'schema_version'}}
    require(type(result['motion_cents_per_second']) is int and 1 <= result['motion_cents_per_second'] <= 100,
            'Motion price must be whole cents per second (1 to 100).')
    require(type(result['narration_cents_per_1000_characters']) is int and 1 <= result['narration_cents_per_1000_characters'] <= 500,
            'Narration price must be whole cents per 1000 characters (1 to 500).')
    prompt = result['motion_prompt']
    require(isinstance(prompt, str) and prompt.strip() and len(prompt) <= 2000
            and not any(ord(c) < 32 and c not in '\n' for c in prompt), 'A short plain motion prompt is required.')
    threshold = result['join_difference_threshold']
    require(type(threshold) in (int, float) and math.isfinite(threshold) and 0 < threshold <= 255, 'Invalid join threshold.')
    return json.loads(canonical(result))


def lean_settings(config):
    require(isinstance(config, dict) and 'lean_generation' in config, 'Lean generation is not enabled in the signed configuration.')
    return validate_lean_settings(config['lean_generation'])


def owner_for(config):
    auth = config['identity']
    require(len(auth['admin_subjects']) == 1 and set(auth['creator_subjects']) == set(auth['admin_subjects']),
            'Lean generation is restricted to one owner.')
    return 'google_' + hashlib.sha256(('accounts.google.com\0' + auth['admin_subjects'][0]).encode()).hexdigest()


def lean_activation(config, plan, acceptance, key):
    """Signed setup scope + signed lean section; re-verified (with expiry) per call."""
    from .administration_runtime import verify_administration
    lean_settings(config); owner_for(config)
    config_sha, plan_sha, frozen, key = digest(config), digest(plan), canonical(acceptance), bytes(key)
    def check():
        verify_administration(json.loads(frozen), key, config_sha, plan_sha)
        return True
    check()
    return check


def narration_profile(config, voice_id):
    return validate_profile({**config['narration_profile'], 'voice_id': voice_id, 'model_id': MODEL})


def narration_cents(request, settings):
    return max(1, math.ceil(len(request['body']['text']) * settings['narration_cents_per_1000_characters'] / 1000))


def motion_cents(settings):
    return settings['motion_cents_per_second'] * MOTION_SECONDS


def motion_operation(owner_id, job_id, image_id):
    # Same operation identity as provider_execution.submit_motion_once.
    return 'motion_' + hashlib.sha256((owner_id + '\0' + job_id + '\0' + image_id).encode()).hexdigest()


def narration_approval(job_id, requests, settings, budget):
    return SpendApproval('lean_narration_' + job_id, fingerprint(requests), tuple(narration_cents(r, settings) for r in requests),
                         budget['job_cap_cents'], budget['operation_cap_cents'])


def motion_approval(job_id, request, settings, budget):
    return SpendApproval('lean_motion_' + job_id, fingerprint([request]), (motion_cents(settings),),
                         budget['job_cap_cents'], budget['operation_cap_cents'])


def estimate_cents(requests, image_count, settings, budget):
    """Upper-bound quote; refuses before any job, reservation or provider call."""
    narration = [narration_cents(r, settings) for r in requests]
    total = sum(narration) + image_count * motion_cents(settings)
    if max(narration + [motion_cents(settings)]) > budget['operation_cap_cents']:
        raise LeanBudgetExceeded('One step of this film would cost more than the per-step limit of '
                                 f"{dollars(budget['operation_cap_cents'])}. Nothing was started or charged.")
    if total > budget['job_cap_cents']:
        raise LeanBudgetExceeded(f'This film would cost up to {dollars(total)}, above the per-film limit of '
                                 f"{dollars(budget['job_cap_cents'])}. Nothing was started or charged.")
    return total


def allocations(owner_id, job_id, requests, image_ids, settings, budget):
    approval = narration_approval(job_id, requests, settings, budget)
    result = {op: cents for op, _binding, _request, cents in _narration_operations(owner_id, job_id, requests, approval)}
    for image_id in image_ids:
        result[motion_operation(owner_id, job_id, image_id)] = motion_cents(settings)
    require(len(result) == len(requests) + len(image_ids), 'Duplicate paid operation.')
    return result


def freeze_inputs(*, owner_id, job_id, script, narrated_text, images, voice, profile, requests, settings, budget, request_sha256):
    """Immutable job inputs; paintings keep the creator's numbered order."""
    require([x['position'] for x in images] == list(range(1, len(images) + 1)) and 4 <= len(images) <= 5,
            'Four or five paintings in their numbered order are required.')
    caps = {k: budget[k] for k in ('job_cap_cents', 'operation_cap_cents')}
    funded = allocations(owner_id, job_id, requests, [x['id'] for x in images], settings, caps)
    plan_sha = fingerprint({'owner_id': owner_id, 'job_id': job_id, 'request_sha256': request_sha256, 'allocations': funded})
    return {'schema_version': 1, 'script': script, 'script_sha256': hashlib.sha256(script.encode()).hexdigest(),
            'narrated_text': narrated_text, 'narrated_text_sha256': hashlib.sha256(narrated_text.encode()).hexdigest(),
            'images': copy.deepcopy(images), 'input_order': [x['id'] for x in images],
            'voice': {'id': voice['id'], 'name': voice['name']}, 'narration_profile': profile,
            'settings': settings, 'budget': caps, 'allocations': funded, 'estimate_cents': sum(funded.values()),
            'plan_id': 'lean_' + job_id, 'plan_sha256': plan_sha}


class LeanJob:
    def __init__(self, doc):
        require(isinstance(doc, dict) and set(doc) == FIELDS and doc['status'] in STATES and doc['start_state'] in STARTS
                and type(doc['progress']) is int and 0 <= doc['progress'] <= 100 and isinstance(doc['message'], str)
                and bool(JOB_ID.fullmatch(doc['job_id'])) and bool(SHA256.fullmatch(doc['request_sha256'])), 'Invalid stored film.')
        self.doc = copy.deepcopy(doc)
        self.inputs = json.loads(doc['inputs_json'])
        require(digest(self.inputs) == doc['inputs_sha256'], 'Stored film inputs changed.')
        self.steps, self.cost = json.loads(doc['steps_json']), json.loads(doc['cost_json'])
        self.result = json.loads(doc['result_json']) if doc['result_json'] else None
        require((self.status == 'complete') == (self.result is not None), 'Stored film result and state disagree.')

    id = property(lambda self: self.doc['job_id'])
    owner_id = property(lambda self: self.doc['owner_id'])
    status = property(lambda self: self.doc['status'])
    progress = property(lambda self: self.doc['progress'])
    message = property(lambda self: self.doc['message'])
    start_state = property(lambda self: self.doc['start_state'])
    request_sha256 = property(lambda self: self.doc['request_sha256'])
    key_sha256 = property(lambda self: self.doc['key_sha256'])

    def stalled(self, now):
        return self.status in ('queued', 'running') and now - self.doc['updated_at'] > STALE_SECONDS

    def public(self, now=None):
        """Owner status; never the script, storage names, provider IDs or keys."""
        expected, spent = self.inputs['estimate_cents'], self.cost.get('spent_cents', 0)
        uncertain = self.cost.get('uncertain_cents', 0)
        status, message = ('failed', STOPPED) if now is not None and self.stalled(now) else (self.status, self.message)
        value = {'id': self.id, 'status': status, 'progress': self.progress, 'message': message,
                 'mode': 'hosted', 'live_release': True, 'lean_generation': True,
                 'expected_cost_cents': expected, 'actual_cost_cents': spent, 'uncertain_cost_cents': uncertain,
                 'cost_message': f'Estimated up to {dollars(expected)}; used so far {dollars(spent + uncertain)}.'}
        if self.status == 'complete':
            value.update(download_url=f'/api/jobs/{self.id}/video', duration_seconds=self.result['duration_seconds'],
                         flagged_joins=self.result.get('flagged_joins', 0))
        return value


class LeanJobStore(_FirestoreBoundary):
    """Owner-scoped job documents and a transactional idempotency index."""
    def __init__(self, client, *, project_id, namespace, transaction_runner=None, clock=time.time):
        super().__init__(client, project_id=project_id, namespace=namespace, transaction_runner=transaction_runner)
        self._clock = clock

    def _job_ref(self, owner_id, job_id):
        require(isinstance(job_id, str) and bool(JOB_ID.fullmatch(job_id)), 'Film not found.')
        return self._doc(self._owner(owner_id) + '/lean_jobs/' + job_id)

    def _request_ref(self, owner_id, key):
        return self._doc(self._owner(owner_id) + '/lean_requests/' + _hash(_key(key)))

    @staticmethod
    def _job(data, owner_id, job_id=None):
        if data is None:
            return None
        job = LeanJob(data)
        require(job.owner_id == _safe_owner(owner_id) and (job_id is None or job.id == job_id), 'Film ownership mismatch.')
        return job

    def get(self, owner_id, job_id):
        return self._job(self._read(self._job_ref(owner_id, job_id)), owner_id, job_id)

    def _find(self, owner_id, key, tx=None):
        index = self._read(self._request_ref(owner_id, key), tx)
        if index is None:
            return None
        require(set(index) == {'job_id', 'request_sha256', 'owner_id', 'key_sha256'} and index['owner_id'] == _safe_owner(owner_id)
                and index['key_sha256'] == _hash(key), 'Submission index integrity failed.')
        job = self._job(self._read(self._job_ref(owner_id, index['job_id']), tx), owner_id, index['job_id'])
        require(job is not None and job.request_sha256 == index['request_sha256'] and job.key_sha256 == _hash(key),
                'Submission index integrity failed.')
        return job

    def find(self, owner_id, key):
        return self._run(lambda tx: self._find(owner_id, key, tx))

    def reserve(self, owner_id, key, request_sha256, build):
        """Same key + same request → same job; same key + different request → conflict."""
        owner_id, key = _safe_owner(owner_id), _key(key)
        require(isinstance(request_sha256, str) and bool(SHA256.fullmatch(request_sha256)), 'Invalid request fingerprint.')
        job_id = secrets.token_hex(16)
        inputs = build(job_id)
        encoded, now = canonical(inputs), int(self._clock())
        require(len(encoded.encode()) < 700_000, 'Film inputs exceed the private document bound.')
        doc = dict(owner_id=owner_id, job_id=job_id, key_sha256=_hash(key), request_sha256=request_sha256,
                   inputs_json=encoded, inputs_sha256=digest(inputs), status='queued', progress=0, message=QUEUED,
                   steps_json='{}', cost_json=canonical({'expected_cents': inputs['estimate_cents'], 'spent_cents': 0, 'uncertain_cents': 0}),
                   result_json=None, start_state='pending', operation=None, created_at=now, updated_at=now)
        def transaction(tx):
            existing = self._find(owner_id, key, tx)
            if existing is not None:
                if existing.request_sha256 != request_sha256:
                    raise IdempotencyConflict('This submission key already belongs to different input.')
                return existing, False
            # One film at a time per owner, even from another tab or device.
            active_ref = self._doc(self._owner(owner_id) + '/lean_state/active')
            active = self._read(active_ref, tx)
            if active is not None:
                other = self._job(self._read(self._job_ref(owner_id, active['job_id']), tx), owner_id, active['job_id'])
                if other is not None and other.status in ('queued', 'running') and not other.stalled(now):
                    raise LeanBusy(BUSY)
                tx.update(active_ref, {'job_id': job_id})
            else:
                tx.create(active_ref, {'job_id': job_id})
            tx.create(self._job_ref(owner_id, job_id), doc)
            tx.create(self._request_ref(owner_id, key), dict(job_id=job_id, request_sha256=request_sha256,
                                                              owner_id=owner_id, key_sha256=_hash(key)))
            return LeanJob(doc), True
        return self._run(transaction)

    def _mutate(self, owner_id, job_id, edit):
        ref = self._job_ref(owner_id, job_id)
        def transaction(tx):
            job = self._job(self._read(ref, tx), owner_id, job_id)
            require(job is not None, 'Film not found.')
            changes = edit(job)
            if not changes:
                return job
            changes = {**changes, 'updated_at': int(self._clock())}
            tx.update(ref, changes)
            return LeanJob({**job.doc, **changes})
        return self._run(transaction)

    def claim_start(self, owner_id, job_id):
        """Exactly one Cloud Run start request per job, ever."""
        claimed = []
        def edit(job):
            if job.start_state != 'pending':
                return None
            claimed.append(True)
            return {'start_state': 'requested'}
        self._mutate(owner_id, job_id, edit)
        return bool(claimed)

    def record_start(self, owner_id, job_id, operation):
        require(isinstance(operation, str) and 0 < len(operation) <= 300, 'Invalid execution reference.')
        return self._mutate(owner_id, job_id, lambda job: {'start_state': 'started', 'operation': operation}
                            if job.start_state == 'requested' else None)

    def start_uncertain(self, owner_id, job_id):
        return self._mutate(owner_id, job_id, lambda job: {'start_state': 'uncertain', **({'message': START_UNCERTAIN}
                            if job.status == 'queued' else {})} if job.start_state == 'requested' else None)

    def begin(self, owner_id, job_id):
        return self._mutate(owner_id, job_id, lambda job: None if job.status == 'complete' else
                            {'status': 'running', 'progress': max(job.progress, 5), 'message': 'Your film is being made…'})

    def progress(self, owner_id, job_id, value, message):
        require(type(value) is int and 0 <= value < 100 and isinstance(message, str) and len(message) <= 300, 'Invalid progress.')
        return self._mutate(owner_id, job_id, lambda job: {'progress': max(job.progress, value), 'message': message}
                            if job.status == 'running' else None)

    def record_step(self, owner_id, job_id, name, value):
        """Write-once verified step output; a rerun reuses it and never repays."""
        require(isinstance(name, str) and bool(STEP.fullmatch(name)), 'Invalid step name.')
        value = json.loads(canonical(value))
        def edit(job):
            if name in job.steps:
                require(job.steps[name] == value, 'A saved film step changed.')
                return None
            return {'steps_json': canonical({**job.steps, name: value})}
        return self._mutate(owner_id, job_id, edit)

    def record_cost(self, owner_id, job_id, cost):
        require(isinstance(cost, dict) and all(type(cost.get(k)) is int and cost[k] >= 0
                for k in ('expected_cents', 'spent_cents', 'uncertain_cents')), 'Invalid cost record.')
        return self._mutate(owner_id, job_id, lambda job: {'cost_json': canonical(cost)})

    def complete(self, owner_id, job_id, result):
        require(isinstance(result, dict) and isinstance(result.get('video'), dict)
                and type(result.get('duration_seconds')) in (int, float), 'Verified film result required.')
        def edit(job):
            require(job.status == 'running', 'Only a running film can complete.')
            return {'status': 'complete', 'progress': 100, 'message': READY, 'result_json': canonical(result)}
        return self._mutate(owner_id, job_id, edit)

    def fail(self, owner_id, job_id, message):
        require(isinstance(message, str) and 0 < len(message) <= 500, 'Plain failure message required.')
        return self._mutate(owner_id, job_id, lambda job: None if job.status == 'complete' else
                            {'status': 'failed', 'message': message})


class LeanBudget(_FirestoreBoundary):
    """Read-only view of the shared campaign reservation total."""
    def remaining_cents(self, budget):
        shared = self._read(self._doc('spend_scopes/' + _hash(budget['scope'])))
        used = budget['prior_spend_cents'] if shared is None else shared.get('reserved_cents')
        require(type(used) is int and budget['prior_spend_cents'] <= used <= budget['approved_total_cap_cents']
                and (shared is None or shared.get('cap_cents') == budget['approved_total_cap_cents']), 'Budget record needs review.')
        return budget['approved_total_cap_cents'] - used


class LeanLedger:
    """Per-job funding inside the shared hard cap (FirestoreSpendJournal semantics).

    fund() atomically reserves the WHOLE job quote against job and campaign caps.
    Each paid call then claims its own allocation in a transaction before it is
    sent; a claimed-but-unconfirmed call stays `may_have_submitted` forever.
    """
    def __init__(self, client, *, project_id, namespace, owner_id, job_id, budget, transaction_runner=None):
        self.journal = FirestoreSpendJournal(client, project_id=project_id, namespace=namespace, owner_id=owner_id, job_id=job_id,
            budget_scope=budget['scope'], approved_total_cap_cents=budget['approved_total_cap_cents'],
            prior_spend_cents=budget['prior_spend_cents'], transaction_runner=transaction_runner)
        self.job_cap = budget['job_cap_cents']

    def remaining_cents(self):
        shared = self.journal._read(self.journal.shared_ref)
        return self.journal.total_cap - (self.journal.prior_spend if shared is None else shared['reserved_cents'])

    def fund(self, plan_id, plan_sha256, allocations):
        try:
            return self.journal.reserve_plan(plan_id, plan_sha256, allocations, self.job_cap)
        except ValueError:
            total, remaining = sum(allocations.values()), self.remaining_cents()
            if total > remaining:
                raise LeanBudgetExceeded(f'This film would cost up to {dollars(total)}, but only {dollars(max(0, remaining))} '
                                         'of the approved budget is left. Nothing was started or charged.') from None
            raise

    def operations(self, plan_id, plan_sha256, before_claim):
        """Journal view for execute_narration_once/submit_motion_once: claim, never re-arm."""
        from .narration_stage import _ReservedNarrationJournal
        return _ReservedNarrationJournal(self.journal, plan_id, plan_sha256, before_claim)

    def summary(self, plan_id, plan_sha256, allocations):
        spent = uncertain = 0
        for operation, cents in allocations.items():
            state = self.journal.get_reserved(operation, plan_id, plan_sha256)['state']
            if state == 'received':
                spent += cents
            elif state == 'may_have_submitted':
                uncertain += cents
        return {'expected_cents': sum(allocations.values()), 'spent_cents': spent, 'uncertain_cents': uncertain}


class LeanObjects(_PrivateBucket):
    """Content-addressed write-once private job media; every read verifies bytes."""
    MIME = {'video/mp4': 'mp4', 'audio/wav': 'wav', 'application/json': 'json', 'image/png': 'png', 'image/jpeg': 'jpg'}
    MAX = 1024 * 1024 * 1024
    ROLE = re.compile(r'[a-z0-9-]{1,40}\Z')

    def __init__(self, client, *, bucket, timeout=120):
        super().__init__(client, bucket=bucket, timeout=timeout)

    def _prefix(self, owner_id, job_id):
        require(isinstance(job_id, str) and bool(JOB_ID.fullmatch(job_id)), 'Invalid film reference.')
        return self._name(Principal(owner_id), f'lean/{job_id}/')

    def _object(self, owner_id, job_id, role, sha, mime):
        require(isinstance(role, str) and bool(self.ROLE.fullmatch(role)) and mime in self.MIME, 'Invalid private media role.')
        return self._prefix(owner_id, job_id) + f'{role}-{sha[:16]}.{self.MIME[mime]}'

    def _verify(self, name, generation, sha, size, mime):
        stream, gen, _ = self._download(name, generation=generation, maximum=self.MAX, mime=mime, expected_sha=sha, expected_size=size)
        stream.close()
        return dict(object=name, generation=gen, sha256=sha, bytes=size, content_type=mime)

    def put(self, owner_id, job_id, role, data, mime):
        require(isinstance(data, bytes) and 0 < len(data) <= self.MAX, 'Bounded private media required.')
        sha = hashlib.sha256(data).hexdigest(); name = self._object(owner_id, job_id, role, sha, mime)
        try:
            generation = self._write(name, data, mime)
        except Exception as error:
            if _code(error) != 412:
                raise
            generation = None  # Same content-addressed object already exists; verify, never overwrite.
        return self._verify(name, generation, sha, len(data), mime)

    def put_file(self, owner_id, job_id, role, path, mime):
        sha, size = hashlib.sha256(), 0
        with open(path, 'rb') as source:
            while chunk := source.read(1024 * 1024):
                sha.update(chunk); size += len(chunk)
        require(0 < size <= self.MAX, 'Bounded private media required.')
        sha = sha.hexdigest(); name = self._object(owner_id, job_id, role, sha, mime)
        self._require_private_bucket()
        blob = self.bucket.blob(name)
        try:
            with open(path, 'rb') as source:
                blob.upload_from_file(source, size=size, content_type=mime, if_generation_match=0,
                                      timeout=self.timeout, retry=None, checksum='auto')
            generation = str(_generation(blob.generation))
        except Exception as error:
            if _code(error) != 412:
                raise
            generation = None
        return self._verify(name, generation, sha, size, mime)

    def open(self, owner_id, job_id, ref):
        require(isinstance(ref, dict) and set(ref) == {'object', 'generation', 'sha256', 'bytes', 'content_type'}
                and isinstance(ref['object'], str) and ref['object'].startswith(self._prefix(owner_id, job_id))
                and ref['content_type'] in self.MIME and bool(SHA256.fullmatch(ref['sha256'])), 'Private media reference changed.')
        stream, _, _ = self._download(ref['object'], generation=ref['generation'], maximum=self.MAX, mime=ref['content_type'],
                                      expected_sha=ref['sha256'], expected_size=ref['bytes'])
        return stream

    def read(self, owner_id, job_id, ref):
        stream = self.open(owner_id, job_id, ref)
        try:
            return stream.read()
        finally:
            stream.close()

    def save(self, owner_id, job_id, ref, path):
        stream = self.open(owner_id, job_id, ref)
        try:
            with open(path, 'xb') as out:
                while chunk := stream.read(1024 * 1024):
                    out.write(chunk)
        finally:
            stream.close()
        return path


class LeanStarter:
    """One Cloud Run Admin API v2 jobs:run per film (attached identity token, no SDK).

    The job id travels as a container env override; the job definition keeps its
    own command/args (signed bundle name + reviewer key pin + --lean-generation).
    """
    ENV = 'BYRON_LEAN_JOB_ID'

    def __init__(self, target, transport, *, container='renderer', timeout_seconds=1800):
        require(isinstance(target, dict) and re.fullmatch(r'byron-renderer(?:-[a-z0-9-]{1,30})?', target.get('job_name', ''))
                and callable(transport) and 60 <= timeout_seconds <= 1800, 'Explicit renderer job and transport required.')
        self.target, self.transport, self.container, self.timeout = dict(target), transport, container, timeout_seconds

    def request(self, job_id):
        require(isinstance(job_id, str) and bool(JOB_ID.fullmatch(job_id)), 'Invalid film reference.')
        t = self.target
        name = f"projects/{t['project_id']}/locations/{t['region']}/jobs/{t['job_name']}"
        return {'method': 'POST', 'url': f'https://run.googleapis.com/v2/{name}:run',
                'json': {'overrides': {'taskCount': 1, 'timeout': f'{self.timeout}s',
                         'containerOverrides': [{'name': self.container, 'env': [{'name': self.ENV, 'value': job_id}]}]}}}

    def __call__(self, job_id):
        request = self.request(job_id)
        result = self.transport(request['method'], request['url'], request['json'])  # Never retried here.
        operation = result.get('name') if isinstance(result, dict) else None
        t = self.target
        require(isinstance(operation, str) and bool(re.fullmatch(r'projects/(?:' + re.escape(t['project_id']) + r'|[0-9]{1,20})/locations/'
                + re.escape(t['region']) + r'/operations/[A-Za-z0-9_-]{8,128}', operation)), 'Cloud Run returned no operation.')
        return operation
