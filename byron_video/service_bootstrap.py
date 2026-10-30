"""Explicit private ASGI service bootstrap; validate-only unless --serve supplied.

No default app, credential discovery, network fetch, install or deployment.
"""
import argparse
from dataclasses import dataclass,field
import hashlib
import types
import ipaddress
import json
from pathlib import Path

from .deployment_plan import validate_config
from .hosted_control import validate_private_plan,digest
from .runtime_assembly import assemble_runtime
from .preview_runtime import verify_runtime_authorization
from .worker_bootstrap import private_bytes,strict_json,validate_runtime


@dataclass(frozen=True)
class ServiceBindings:
    firestore_client: object
    storage_client: object
    secret_client: object
    workload_token_reader: object
    csrf_secret: bytes = field(repr=False)


def load_bindings(path,expected_sha256,config):
    raw=private_bytes(Path(path),512_000)
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:raise ValueError('Private service bindings differ from their reviewed digest.')
    module=types.ModuleType('_byron_private_service_bindings')
    exec(compile(raw,str(path),'exec'),module.__dict__)  # Execute exactly the already-hashed bytes.
    result=module.build_service_bindings(config)
    if not isinstance(result,ServiceBindings):raise ValueError('Explicit authenticated service bindings required.')
    return result


def validate_service_runtime(config,lock_bytes):
    lock=validate_runtime(config,lock_bytes)
    required={'google-cloud-secret-manager','cryptography','uvicorn','python-multipart'}
    if not required<=set(lock['packages']):raise ValueError('Server, secret and acceptance dependencies must be explicitly pinned.')
    return lock


def build_service(*,config,processing_plan,sdk_lock_bytes,bindings,acceptance,reviewer_public_key,enabled=False,production_admission=None):
    checked=validate_config(config);plan=validate_private_plan(processing_plan)
    # Missing/mismatched SDK/runtime fails before any credential-dependent call.
    validate_service_runtime(checked,sdk_lock_bytes)
    if not isinstance(bindings,ServiceBindings):raise ValueError('Service bindings required.')
    return assemble_runtime(config=checked,processing_plan=plan,firestore_client=bindings.firestore_client,
        storage_client=bindings.storage_client,secret_client=bindings.secret_client,
        workload_token_reader=bindings.workload_token_reader,csrf_secret=bindings.csrf_secret,
        enabled=enabled,acceptance=acceptance,reviewer_public_key=reviewer_public_key,production_admission=production_admission)


def proxy_allowlist(values):
    if not values:raise ValueError('Explicit trusted ingress proxy addresses are required before serving OAuth.')
    parsed=[]
    for value in values:
        network=ipaddress.ip_network(value,strict=False)
        if network.prefixlen==0:raise ValueError('Do not trust every forwarding source.')
        parsed.append(str(network))
    return ','.join(parsed)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Validate or explicitly run private Byron ASGI candidate; never deploy.')
    parser.add_argument('--config',type=Path);parser.add_argument('--processing-plan',type=Path)
    parser.add_argument('--bootstrap-env');parser.add_argument('--reviewer-key-sha256')
    parser.add_argument('--administration-only',action='store_true',help='Signed owner setup only; no generation or provider calls.')
    parser.add_argument('--lean-generation',action='store_true',help='With --administration-only and a signed lean_generation config section: owner-only capped films.')
    parser.add_argument('--sdk-lock',default=Path('/opt/byron-runtime/sdk-lock.json'),type=Path);parser.add_argument('--serve',action='store_true')
    parser.add_argument('--legacy-private-bindings',action='store_true',help='Explicit backwards-compatible reviewed private Python adapter mode; not required for packaged serving.')
    parser.add_argument('--bindings',type=Path);parser.add_argument('--bindings-sha256')
    parser.add_argument('--acceptance',type=Path);parser.add_argument('--reviewer-public-key',type=Path)
    parser.add_argument('--production-policy',type=Path);parser.add_argument('--policy-public-key',type=Path)
    parser.add_argument('--billing-contract',type=Path);parser.add_argument('--billing-public-key',type=Path)
    parser.add_argument('--infrastructure-authorization',type=Path)
    parser.add_argument('--policy-env');parser.add_argument('--policy-key-sha256');parser.add_argument('--billing-key-sha256')
    parser.add_argument('--trusted-proxy',action='append',default=[]);parser.add_argument('--port',type=int,default=8080)
    args=parser.parse_args(argv)
    try:
        if args.administration_only and any((args.legacy_private_bindings,args.bindings,args.bindings_sha256,
                args.production_policy,args.policy_public_key,args.billing_contract,args.billing_public_key,
                args.infrastructure_authorization,args.policy_env,args.policy_key_sha256,args.billing_key_sha256)):
            raise ValueError('Setup-only mode cannot mix generation authority or legacy adapters.')
        if args.lean_generation and not args.administration_only:
            raise ValueError('Lean generation starts only from the signed setup-only bundle.')
        bundle=None
        if args.bootstrap_env:
            if any((args.config,args.processing_plan,args.acceptance,args.reviewer_public_key,args.legacy_private_bindings)):
                raise ValueError('Explicit ENV bundle mode cannot mix private file or legacy Python configuration.')
            from .bootstrap_bundle import load_bootstrap_env
            bundle=load_bootstrap_env(args.bootstrap_env,args.reviewer_key_sha256,administration_only=args.administration_only)
            config,plan=bundle['config'],bundle['processing_plan']
        else:
            if not all((args.config,args.processing_plan)) or args.reviewer_key_sha256:
                raise ValueError('Select explicit private config/plan files or an explicit pinned ENV bundle.')
            config=validate_config(strict_json(private_bytes(args.config,512_000)))
            plan=validate_private_plan(strict_json(private_bytes(args.processing_plan,128_000)))
        lock=private_bytes(args.sdk_lock,128_000);validate_service_runtime(config,lock)
        if args.lean_generation:
            from .lean_film import lean_settings,owner_for
            lean_settings(config);owner_for(config)
        if not args.serve:
            print(json.dumps({'status':'VALIDATED_NOT_STARTED','live_ready':False}));return 0
        if (bundle is None and not all((args.acceptance,args.reviewer_public_key))) or not 1024<=args.port<=65535:
            raise ValueError('Signed acceptance, public key and nonprivileged port required.')
        if (args.bindings or args.bindings_sha256) and not args.legacy_private_bindings:
            raise ValueError('Private Python adapters require explicit legacy mode.')
        if args.legacy_private_bindings and not all((args.bindings,args.bindings_sha256)):
            raise ValueError('Legacy mode requires exact reviewed private bindings.')
        forwarded=proxy_allowlist(args.trusted_proxy)
        key=bundle['reviewer_public_key'] if bundle else private_bytes(args.reviewer_public_key,32)
        if len(key)!=32:raise ValueError('Pinned raw Ed25519 reviewer public key required.')
        acceptance=bundle['acceptance'] if bundle else strict_json(private_bytes(args.acceptance,128_000))
        if args.administration_only:
            from .administration_runtime import verify_administration
            verify_administration(acceptance,key,digest(config),digest(plan))
        else:
            verify_runtime_authorization(acceptance,key,digest(config),digest(plan),preview_contract=plan.get("preview_contract"))
        admission=None
        if not args.legacy_private_bindings and not args.administration_only:
            from .production_admission import ProductionAdmission
            from .production_worker import load_policy_env
            private_policy_files=(args.production_policy,args.policy_public_key,args.billing_contract,args.billing_public_key)
            if args.policy_env:
                if any(private_policy_files) or args.infrastructure_authorization:
                    raise ValueError('Policy ENV cannot mix private policy files.')
                policy=load_policy_env(args.policy_env,args.policy_key_sha256,args.billing_key_sha256)
            else:
                if not all(private_policy_files) or args.policy_key_sha256 or args.billing_key_sha256:
                    raise ValueError('Explicit signed production policy and independent billing evidence required.')
                policy=dict(policy_envelope=strict_json(private_bytes(args.production_policy,128_000)),
                    policy_public_key=private_bytes(args.policy_public_key,32),
                    billing_envelope=strict_json(private_bytes(args.billing_contract,128_000)),
                    billing_public_key=private_bytes(args.billing_public_key,32))
                if args.infrastructure_authorization:
                    policy['infrastructure_envelope']=strict_json(private_bytes(args.infrastructure_authorization,128_000))
            # Pure signature/scope validation precedes credential or SDK construction.
            # Current paid-generation permission is checked only at dispatch, not reads.
            admission=ProductionAdmission(config=config,processing_plan=plan,**policy)
        # Import private adapter code only after its SHA and signed runtime evidence are checked.
        if args.legacy_private_bindings:
            bindings=load_bindings(args.bindings,args.bindings_sha256,config)
        else:
            from .service_bindings import build_service_bindings
            bindings=build_service_bindings(config=config,processing_plan=plan,sdk_lock_bytes=lock,
                acceptance=acceptance,reviewer_public_key=key,enabled=True,administration_only=args.administration_only)
        if args.lean_generation:
            from .administration_runtime import assemble_lean
            runtime=assemble_lean(config=config,processing_plan=plan,bindings=bindings,
                acceptance=acceptance,reviewer_public_key=key)
        elif args.administration_only:
            from .administration_runtime import assemble_administration
            runtime=assemble_administration(config=config,processing_plan=plan,bindings=bindings,
                acceptance=acceptance,reviewer_public_key=key)
        else:
            runtime=build_service(config=config,processing_plan=plan,sdk_lock_bytes=lock,bindings=bindings,
                acceptance=acceptance,reviewer_public_key=key,enabled=True,production_admission=admission)
        import uvicorn
        uvicorn.run(runtime.app,host='0.0.0.0',port=args.port,proxy_headers=True,forwarded_allow_ips=forwarded,server_header=False)
        return 0
    except Exception:
        parser.exit(2,'Service configuration/runtime is incomplete or unverified; no deployment was attempted.\n')


if __name__=='__main__':raise SystemExit(main())
