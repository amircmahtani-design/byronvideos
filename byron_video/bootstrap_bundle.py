"""Explicit signed noncredential ENV bundle; no implicit environment discovery.

Cloud Run may inject this private configuration through a numeric Secret Manager
ENV reference. Provider/OAuth/CSRF payloads are excluded by exact schemas. The
reviewer key hash is independently pinned outside this bundle.
"""
import base64,hashlib,json,os,re
from .deployment_plan import validate_config
from .hosted_control import validate_private_plan,digest,require
from .preview_runtime import verify_runtime_authorization

MAX_BUNDLE_BYTES=30000  # Below Cloud Run's 32768-byte per-variable ceiling.


def parse_bootstrap_bundle(raw,reviewer_key_sha256,*,administration_only=False):
    require(isinstance(raw,str) and 0<len(raw.encode('utf-8'))<=MAX_BUNDLE_BYTES,'Explicit bounded private bootstrap bundle required.')
    require(isinstance(reviewer_key_sha256,str) and re.fullmatch(r'[0-9a-f]{64}',reviewer_key_sha256),
            'Independently pinned reviewer public-key fingerprint required.')
    def pairs(items):
        value={}
        for key,item in items:
            require(key not in value,'Duplicate bootstrap configuration field.')
            value[key]=item
        return value
    value=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite bootstrap value.')))
    require(isinstance(value,dict) and set(value)=={'schema_version','config','processing_plan','acceptance','reviewer_public_key_base64'}
            and type(value['schema_version']) is int and value['schema_version']==1,'Exact noncredential bootstrap schema required.')
    require(isinstance(value['reviewer_public_key_base64'],str) and len(value['reviewer_public_key_base64'])==44,'Raw public reviewer key encoding required.')
    key=base64.b64decode(value['reviewer_public_key_base64'],validate=True)
    require(len(key)==32 and hashlib.sha256(key).hexdigest()==reviewer_key_sha256,'Bootstrap bundle cannot nominate its own reviewer trust anchor.')
    config=validate_config(value['config']);plan=validate_private_plan(value['processing_plan'])
    if administration_only:
        from .administration_runtime import verify_administration
        verify_administration(value['acceptance'],key,digest(config),digest(plan))
    else:
        verify_runtime_authorization(value['acceptance'],key,digest(config),digest(plan),preview_contract=plan.get('preview_contract'))
    if 'exact_film_contract' in plan:
        from .exact_film_runtime import validate_contract
        validate_contract(plan['exact_film_contract'])
    return dict(config=config,processing_plan=plan,acceptance=value['acceptance'],reviewer_public_key=key)


def load_bootstrap_env(name,reviewer_key_sha256,*,environ=None,administration_only=False):
    require(isinstance(name,str) and re.fullmatch(r'BYRON_[A-Z0-9_]{1,100}',name),'Use an explicit BYRON-prefixed configuration variable name.')
    values=os.environ if environ is None else environ
    require(name in values,'Selected bootstrap configuration variable is absent; no file/credential fallback.')
    return parse_bootstrap_bundle(values[name],reviewer_key_sha256,administration_only=administration_only)
