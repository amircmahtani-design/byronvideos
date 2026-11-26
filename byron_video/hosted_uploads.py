"""Disabled-by-default creator/upload boundary and atomic offline store.

No network client, credential discovery, resource creation, or public app exists
here. Production must supply verified session and atomic private storage adapters.
SQLiteUploadFixture is a single-machine test store, not production cloud storage.
"""
from dataclasses import dataclass, field
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from urllib.parse import urlsplit
import uuid
import warnings

from fastapi import HTTPException, Request
from PIL import Image, UnidentifiedImageError
from starlette.datastructures import UploadFile

MAX_UPLOAD_BODY_BYTES = 100 * 1024 * 1024 + 128 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
KEY = re.compile(r'[A-Za-z0-9_-]{16,128}\Z')


@dataclass(frozen=True)
class CreatorSession:
    principal: object
    csrf_token: str = field(repr=False)


class CreatorAccess:
    """Inject a verifier of real sessions, not browser-supplied identity headers.

    Sessions must be expiry-checked, creator-allowlisted, revocable and contain
    random session-bound CSRF tokens. This class never creates access grants.
    """
    def __init__(self, session_resolver, allowed_origin):
        origin = urlsplit(allowed_origin)
        if (origin.scheme != 'https' or not origin.netloc or origin.username or origin.password
                or origin.path or origin.query or origin.fragment):
            raise ValueError('An exact HTTPS creator origin is required.')
        self.resolver, self.origin = session_resolver, allowed_origin

    async def authorize(self, request):
        from .hosted_control import invoke, Principal
        try:
            session = await invoke(self.resolver, request)
            if (not isinstance(session, CreatorSession) or not isinstance(session.principal, Principal)
                    or not isinstance(session.csrf_token, str) or not session.csrf_token.isascii()
                    or len(session.csrf_token) < 32):
                raise ValueError()
        except Exception:
            raise HTTPException(401, 'A verified creator session is required.') from None
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            token = request.headers.get('x-csrf-token', '')
            if (request.headers.get('origin') != self.origin or request.headers.get('sec-fetch-site') == 'cross-site'
                    or not token.isascii() or not secrets.compare_digest(token, session.csrf_token)):
                raise HTTPException(403, 'Reload the creator page before trying again.')
        return session


@dataclass(frozen=True)
class PreparedImage:
    data: bytes = field(repr=False)
    sha256: str
    content_type: str
    width: int
    height: int


def prepare_image(data, content_type):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, 'Use paintings no larger than 20 MB each.')
    if content_type not in {'image/png', 'image/jpeg'}:
        raise HTTPException(422, 'Use still JPEG or PNG paintings only.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                expected = 'PNG' if content_type == 'image/png' else 'JPEG'
                if (image.format != expected or getattr(image, 'n_frames', 1) != 1
                        or min(width, height) < 64 or width * height > 40_000_000):
                    raise ValueError()
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, 'A painting is not a valid still JPEG or PNG from 64 pixels to 40 megapixels.') from None
    return PreparedImage(data, hashlib.sha256(data).hexdigest(), content_type, width, height)


def batch_fingerprint(images):
    from .hosted_control import digest
    return digest([dict(position=i, sha256=x.sha256, bytes=len(x.data), content_type=x.content_type)
                   for i, x in enumerate(images, 1)])


class SQLiteUploadFixture:
    """Atomic original-byte upload storage for tests only.

    A production adapter implements commit_batch(owner, key, digest, images) and
    resolve(owner, image_id). Commit must atomically bind immutable original bytes
    and metadata to owner + idempotency key, reject a changed digest (409), return
    the original result on exact replay, and never expose cross-owner objects.
    Unknown commit outcomes must raise; callers must not blindly repeat writes.
    The production adapter owns retention, encryption, quotas, readback checking,
    transactional recovery and cleanup. No storage paths reach the browser.
    """
    synthetic_fixture = True

    def __init__(self, path):
        path = Path(path)
        if any(candidate.is_symlink() for candidate in (path, *path.parents)):
            raise ValueError('Fixture database and all parents must not be symlinks.')
        public_root = Path(__file__).resolve().parent.parent / 'web'
        if path.resolve().is_relative_to(public_root):
            raise ValueError('Private originals must not be stored under the public web directory.')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        path.chmod(0o600)
        self._db.execute('PRAGMA synchronous=FULL')
        self._db.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS upload_batches(owner TEXT, key TEXT, digest TEXT NOT NULL,
                upload_id TEXT NOT NULL, PRIMARY KEY(owner,key));
            CREATE TABLE IF NOT EXISTS upload_images(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                key TEXT NOT NULL, position INTEGER NOT NULL, metadata TEXT NOT NULL, data BLOB NOT NULL,
                UNIQUE(owner,key,position), FOREIGN KEY(owner,key) REFERENCES upload_batches(owner,key));
        ''')

    def close(self):
        self._db.close()

    def commit_batch(self, owner, key, fingerprint, images):
        from .hosted_control import VerifiedImage, IdempotencyConflict, canonical
        from dataclasses import asdict
        if not KEY.fullmatch(key) or len(images) not in (4, 5) or batch_fingerprint(images) != fingerprint:
            raise ValueError('Invalid upload batch.')
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                existing = self._db.execute('SELECT digest,upload_id FROM upload_batches WHERE owner=? AND key=?', (owner.subject,key)).fetchone()
                if existing:
                    if existing[0] != fingerprint:
                        raise IdempotencyConflict('This upload key already belongs to different paintings.')
                    rows = self._db.execute('SELECT metadata,data FROM upload_images WHERE owner=? AND key=? ORDER BY position', (owner.subject,key)).fetchall()
                    verified = []
                    if len(rows) != len(images):
                        raise ValueError('Stored upload batch is incomplete.')
                    for row, expected in zip(rows, images):
                        record = VerifiedImage(**json.loads(row[0]))
                        record.validate(owner, record.id)
                        if (row[1] != expected.data or record.sha256 != expected.sha256
                                or len(row[1]) != record.byte_count or hashlib.sha256(row[1]).hexdigest() != record.sha256):
                            raise ValueError('Stored original bytes changed.')
                        verified.append(record)
                    result = (existing[1], verified)
                else:
                    upload_id = uuid.uuid4().hex
                    self._db.execute('INSERT INTO upload_batches VALUES (?,?,?,?)', (owner.subject,key,fingerprint,upload_id))
                    verified = []
                    for position, image in enumerate(images,1):
                        # Defense in depth: callers cannot substitute an unverified digest.
                        if hashlib.sha256(image.data).hexdigest() != image.sha256:
                            raise ValueError('Image bytes changed.')
                        record = VerifiedImage(uuid.uuid4().hex,owner.subject,image.sha256,len(image.data),image.width,image.height,image.content_type,uuid.uuid4().hex,'1',True)
                        record.validate(owner,record.id)
                        self._db.execute('INSERT INTO upload_images VALUES (?,?,?,?,?,?)',
                            (record.id,owner.subject,key,position,canonical(asdict(record)),image.data))
                        verified.append(record)
                    result = (upload_id,verified)
                self._db.execute('COMMIT')
                return result
            except BaseException:
                self._db.execute('ROLLBACK')
                raise

    def resolve(self, owner, image_id):
        from .hosted_control import VerifiedImage
        with self._lock:
            row = self._db.execute('SELECT metadata,data FROM upload_images WHERE owner=? AND id=?', (owner.subject,image_id)).fetchone()
        if row is None:
            raise ValueError('Private image not found.')
        record = VerifiedImage(**json.loads(row[0]))
        record.validate(owner,image_id)
        if len(row[1]) != record.byte_count or hashlib.sha256(row[1]).hexdigest() != record.sha256:
            raise ValueError('Stored original bytes changed.')
        return record


def install_creator_routes(app, *, access, storage, enabled=False, runtime_binding=None, plan_provider=None):
    """Mount only on hosted_control.create_app, never the public static preview."""
    from .hosted_control import invoke, IdempotencyConflict, SAFE_ID, VerifiedImage

    def check_runtime():
        if runtime_binding is not None:
            try:
                runtime_binding.check()
            except Exception:
                raise HTTPException(503, 'Runtime acceptance is unavailable or expired.') from None

    @app.get('/api/creator-capabilities')
    async def capabilities(request: Request):
        if not enabled:
            return dict(enabled=False,mode='hosted_setup',protocol_version=1,live_release=False)
        check_runtime()
        session = await access.authorize(request)
        if runtime_binding is not None:
            from .hosted_control import validate_private_plan
            plan=validate_private_plan(await invoke(plan_provider,session.principal))
            if 'preview_contract' in plan:
                return dict(enabled=True,mode='hosted_private_preview',protocol_version=1,csrf_token=session.csrf_token,
                            live_release=False,private_preview=True,synthetic_fixture=False,
                            runtime_release_sha256=runtime_binding.release_sha256)
            return dict(enabled=True, mode='hosted', protocol_version=1, csrf_token=session.csrf_token,
                        live_release=True, synthetic_fixture=False, runtime_release_sha256=runtime_binding.release_sha256)
        return dict(enabled=True,mode='hosted',protocol_version=1,csrf_token=session.csrf_token,
                    live_release=False,synthetic_fixture=True)

    @app.post('/api/uploads',status_code=201)
    async def upload(request: Request):
        if not enabled:
            raise HTTPException(503,'Private uploads are not activated.')
        check_runtime()
        session = await access.authorize(request)  # Before parsing any uploaded bytes.
        key = request.headers.get('idempotency-key','')
        if not KEY.fullmatch(key):
            raise HTTPException(422,'A stable random Idempotency-Key is required.')
        try:
            async with request.form(max_files=5,max_fields=0) as form:
                files = form.getlist('images')
                if len(files) not in (4,5) or len(form.multi_items()) != len(files) or not all(isinstance(x,UploadFile) for x in files):
                    raise HTTPException(422,'Upload four or five ordered paintings only.')
                prepared = []
                for file in files:
                    data = await file.read(MAX_IMAGE_BYTES+1)
                    prepared.append(await invoke(prepare_image,data,file.content_type))
            fingerprint = batch_fingerprint(prepared)
            try:
                upload_id, records = await invoke(storage.commit_batch,session.principal,key,fingerprint,tuple(prepared))
                if not isinstance(upload_id,str) or not SAFE_ID.fullmatch(upload_id) or len(records)!=len(prepared):
                    raise ValueError()
                for record, expected in zip(records,prepared):
                    if not isinstance(record,VerifiedImage):
                        raise ValueError()
                    record.validate(session.principal,record.id)
                    if (record.sha256,record.byte_count,record.content_type,record.width,record.height)!=(expected.sha256,len(expected.data),expected.content_type,expected.width,expected.height):
                        raise ValueError()
                if len({r.id for r in records})!=len(records):
                    raise ValueError()
            except IdempotencyConflict:
                raise HTTPException(409,'This upload key already belongs to different paintings.') from None
            except Exception:
                raise HTTPException(503,'Upload outcome could not be confirmed. Do not submit again; request recovery for this upload key.') from None
            return dict(upload_id=upload_id,images=[dict(id=r.id,position=i) for i,r in enumerate(records,1)])
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(422,'The painting upload could not be read. Use four or five still JPEG/PNG files.') from None
