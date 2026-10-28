"""Packaged declarative service bindings, disabled by default; never ADC/key files.

No import-time I/O. Explicit serving first validates the installed lock and signed
private configuration, then verifies attached project/account before SDK or secret
access. This constructs existing-resource clients; it never creates resources.
"""
import json
from .deployment_plan import validate_config
from .hosted_control import canonical,digest,require,validate_private_plan
from .preview_runtime import verify_runtime_authorization
from .workload_credentials import create_attached_credentials


def crc32c(data):
    value=0xffffffff
    for byte in data:
        value^=byte
        for _ in range(8):value=(value>>1)^ (0x82f63b78 if value&1 else 0)
    return value^0xffffffff


def _sdk_clients(config,credentials):
    try:
        from google.cloud import firestore,storage,secretmanager
    except ImportError:raise RuntimeError('Pinned service SDK dependencies are unavailable; no fallback or install attempted.') from None
    project=config['target']['project_id']
    return (firestore.Client(project=project,database=config['firestore']['database'],credentials=credentials,
                client_options={'api_endpoint':'firestore.googleapis.com'}),
            storage.Client(project=project,credentials=credentials,client_options={'api_endpoint':'https://storage.googleapis.com'}),
            secretmanager.SecretManagerServiceClient(credentials=credentials,client_options={'api_endpoint':'secretmanager.googleapis.com'}))


def build_service_bindings(*,config,processing_plan,sdk_lock_bytes,acceptance,reviewer_public_key,enabled=False,administration_only=False):
    require(enabled is True,'Packaged service bindings are disabled; validation never accesses credentials or secrets.')
    checked=validate_config(config);plan=validate_private_plan(processing_plan)
    require(checked['firestore']['database']!='(default)','Packaged production requires a dedicated named database; preserve existing default/Firebase data.')
    require({'service_service_account','csrf_secret_version'}<=set(checked['runtime']),
            'Packaged service needs a distinct reviewed attached service account and numeric CSRF version.')
    from .service_bootstrap import validate_service_runtime,ServiceBindings
    validate_service_runtime(checked,sdk_lock_bytes)
    # Detached signed bytes: later caller mutation cannot expand authority.
    config_json,plan_json=canonical(checked),canonical(plan)
    envelope_json=canonical(acceptance);public_key=bytes(reviewer_public_key)
    def activation_check():
        current_config,current_plan=json.loads(config_json),json.loads(plan_json)
        if administration_only:
            from .administration_runtime import verify_administration
            verify_administration(json.loads(envelope_json),public_key,digest(current_config),digest(current_plan))
        else:
            verify_runtime_authorization(json.loads(envelope_json),public_key,digest(current_config),digest(current_plan),
                preview_contract=current_plan.get('preview_contract'))
        if 'exact_film_contract' in current_plan:
            from .exact_film_runtime import validate_contract
            validate_contract(current_plan['exact_film_contract'])
        return True
    activation_check()
    bundle=create_attached_credentials(checked,role='service',activation_check=activation_check,enabled=True)
    require(bundle.verify_identity() is True and bundle.credentials.service_account_email==checked['runtime']['service_service_account'],
            'Attached service identity differs from reviewed configuration.')
    activation_check()
    firestore,storage,secrets=_sdk_clients(checked,bundle.credentials)
    require(getattr(firestore,'_database_string',None)==f"projects/{checked['target']['project_id']}/databases/{checked['firestore']['database']}"
            and getattr(storage,'project',None)==checked['target']['project_id'],'SDK client project/database differs from reviewed isolated resources.')
    activation_check()
    name=checked['secret_resources']['csrf']+'/versions/'+checked['runtime']['csrf_secret_version']
    response=secrets.access_secret_version(request={'name':name},retry=None,timeout=20)
    activation_check()
    data=response.payload.data
    require(getattr(response,'name',None)==name and isinstance(data,bytes) and 32<=len(data)<=4096,
            'Pinned private CSRF secret version is unavailable or invalid.')
    require(type(response.payload.data_crc32c) is int and response.payload.data_crc32c==crc32c(data),
            'CSRF secret integrity verification failed.')
    return ServiceBindings(firestore_client=firestore,storage_client=storage,secret_client=secrets,
                           workload_token_reader=bundle.token_reader,csrf_secret=data)
