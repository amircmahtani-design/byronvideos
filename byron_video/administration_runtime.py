"""Explicit signed setup-only authority; never a generation capability."""
import base64
import json
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.staticfiles import StaticFiles
from .hosted_control import canonical, digest, require

PURPOSE = 'byron-administration-only-v1'


def verify_administration(envelope, key, config_sha, plan_sha, *, now=None):
    now = time.time() if now is None else now
    require(isinstance(envelope, dict) and set(envelope) == {'payload', 'signature'}, 'Signed setup authorization required.')
    p = envelope['payload']
    require(isinstance(p, dict) and set(p) == {'schema_version', 'purpose', 'config_sha256', 'plan_sha256',
        'issued_at', 'expires_at', 'owner_only', 'generation_allowed', 'provider_calls_allowed'}, 'Exact setup scope required.')
    require(type(p['schema_version']) is int and p['schema_version'] == 1 and p['purpose'] == PURPOSE
        and p['config_sha256'] == config_sha and p['plan_sha256'] == plan_sha
        and p['owner_only'] is True and p['generation_allowed'] is False and p['provider_calls_allowed'] is False
        and type(p['issued_at']) is int and type(p['expires_at']) is int
        and p['issued_at'] <= now < p['expires_at'] and 0 < p['expires_at'] - p['issued_at'] <= 7 * 86400,
        'Setup authorization expired or scope changed.')
    require(isinstance(key, bytes) and len(key) == 32 and isinstance(envelope['signature'], str)
        and len(envelope['signature']) <= 100, 'Pinned reviewer key/signature required.')
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    signature = base64.b64decode(envelope['signature'], validate=True)
    require(len(signature) == 64, 'Invalid setup signature.')
    Ed25519PublicKey.from_public_bytes(key).verify(signature, canonical(p).encode())
    return json.loads(canonical(p))


def assemble_administration(*, config, processing_plan, bindings, acceptance, reviewer_public_key, generation=None):
    """generation is an internal hook used only by assemble_lean; default is setup-only."""
    from .deployment_plan import validate_config
    from .hosted_control import validate_private_plan
    from .google_identity import GoogleIdentityResolver
    from .google_login import GoogleLogin, GoogleCodeExchange, FirestoreLoginJournal
    from .runtime_assembly import _AdminSecrets, RuntimeAssembly
    from .admin import install_admin_routes
    config = validate_config(config); plan = validate_private_plan(processing_plan)
    frozen = canonical(acceptance); key = bytes(reviewer_public_key)
    config_sha, plan_sha = digest(config), digest(plan)
    def check():
        verify_administration(json.loads(frozen), key, config_sha, plan_sha)
        return True
    check()
    auth = config['identity']; target = config['target']; db = config['firestore']
    require(db['database'] != '(default)' and getattr(bindings.firestore_client, '_database_string', None)
        == f"projects/{target['project_id']}/databases/{db['database']}", 'Dedicated setup database required.')
    require(set(auth['creator_subjects']) == set(auth['admin_subjects']) and len(auth['admin_subjects']) == 1,
        'Setup mode is restricted to one owner.')
    journal = FirestoreLoginJournal(bindings.firestore_client, project_id=target['project_id'], namespace=db['namespace'])
    identity = GoogleIdentityResolver(audience=auth['oauth_audience'], allowed_subjects=auth['admin_subjects'],
        admin_subjects=auth['admin_subjects'], csrf_secret=bindings.csrf_secret, revoked=journal.revoked)
    import hashlib
    owners = ['google_' + hashlib.sha256(('accounts.google.com\0' + s).encode()).hexdigest() for s in auth['admin_subjects']]
    mode = config.get('native_api_mode', 'vertex_eu')
    names = ('elevenlabs', 'fal', 'default_voice') + (('gemini',) if mode == 'gemini_developer' else ())
    secrets = _AdminSecrets(bindings.secret_client, owners, {k: config['secret_resources'][k] for k in names})
    app = FastAPI()
    def oauth_secret():
        check()
        return bindings.secret_client.access_secret_version(request={
            'name': config['secret_resources']['google_oauth'] + '/versions/latest'}).payload.data.decode('utf-8')
    GoogleLogin(origin=auth['origin'], client_id=auth['oauth_audience'], redirect_uri=auth['origin'] + '/auth/callback',
        subject_emails=auth['subject_emails'], journal=journal, exchange=GoogleCodeExchange(oauth_secret, enabled=True),
        identity_resolver=identity, enabled=True, activation_check=check).mount(app)
    def admin_session(request):
        check()
        return identity.admin_session(request)
    from .voice_library import FirestoreVoiceLibrary
    library = FirestoreVoiceLibrary(bindings.firestore_client, project_id=target['project_id'], namespace=db['namespace'])
    # No provider callbacks, including voice lookup: names are typed as shown in ElevenLabs.
    from .key_records import FirestoreKeyRecords
    records = FirestoreKeyRecords(bindings.firestore_client, project_id=target['project_id'], namespace=db['namespace'])
    install_admin_routes(app, session_resolver=admin_session, private_store=secrets, voice_library=library,
        key_records=records, allowed_origin=auth['origin'], activated=True, native_api_mode=mode)
    if generation is None:
        @app.get('/api/creator-capabilities')
        def capabilities():
            return {'enabled': False, 'render_ready': False, 'administration_only': True}
    else:
        from types import SimpleNamespace
        generation(app, SimpleNamespace(check=check, admin_session=admin_session, identity=identity, library=library,
            config=config, plan=plan, owners=owners, release_sha256=digest(acceptance)))
    @app.get('/api/voices')
    def named_voices(request: Request):
        try:
            admin_session(request)
        except Exception:
            raise HTTPException(403, 'Sign in to see the saved voices.') from None
        try:
            return library.read()
        except Exception:
            raise HTTPException(503, 'Saved voices could not be loaded. Please try again.') from None
    @app.get('/admin', include_in_schema=False)
    def admin_page():
        return RedirectResponse('/admin.html', status_code=308)
    # Setup-only mode has no job/upload/resume/exact-film routes or dispatcher; lean
    # routes exist only through assemble_lean's explicit hook above.
    web_root = Path(__file__).resolve().parent.parent / 'web'
    require(not web_root.is_symlink() and (web_root / 'index.html').is_file(), 'Reviewed assets required.')
    app.mount('/', StaticFiles(directory=str(web_root), html=True, follow_symlink=False), name='creator-assets')
    return RuntimeAssembly(app, None, None, None, None, None, identity, True, digest(acceptance))


def assemble_lean(*, config, processing_plan, bindings, acceptance, reviewer_public_key, transaction_runner=None):
    """Same signed setup bundle plus its signed `lean_generation` section; owner-only paid films.

    No production policy, billing contract, preview acceptance or Gemini is used.
    """
    from .deployment_plan import validate_config
    from .lean_film import lean_settings, owner_for
    from .lean_service import install_lean_generation
    checked = validate_config(config)
    settings = lean_settings(checked); owner_for(checked)
    def generation(app, parts):
        install_lean_generation(app, parts=parts, bindings=bindings, settings=settings, transaction_runner=transaction_runner)
    return assemble_administration(config=checked, processing_plan=processing_plan, bindings=bindings,
        acceptance=acceptance, reviewer_public_key=reviewer_public_key, generation=generation)
