"""Explicit signed-plan opt-in for owner-specific clean-copy actions.

A historical or preview-only runtime does not gain a new render action merely
because newer server code is installed. No configuration or permission is created.
"""
import hashlib
from pathlib import Path
from .hosted_control import digest,require


def contract():
    from .exact_film_composition import release_hash
    root=Path(__file__).parent
    names=('exact_film_policy.py','exact_film_composition.py','exact_film_worker.py',
        'exact_film_registry.py','exact_film_control.py','exact_film_dispatch.py','exact_film_runtime.py',
        'exact_film_assembly.py','cloud_run_readback.py','worker_bootstrap.py','runtime_assembly.py','worker_binding.py','candidate_media_evidence.py','production_worker.py','production_admission.py','service_bootstrap.py','declarative_policy.py',
        'vertex_token_preflight.py','workload_credentials.py','service_bindings.py','bootstrap_bundle.py','infrastructure_authorization.py')
    sources={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in names}
    return dict(schema_version=1,mode='OWNER_APPROVED_THIS_FILM_ONLY',final_release_sha256=release_hash(),
        implementation_sha256=digest(sources),paid_generation_allowed=False,publication_allowed=False,future_style_approved=False)


def validate_contract(value):
    require(isinstance(value,dict) and value==contract(),'Exact-film implementation/release differs from the signed plan.')
    return dict(value)


def require_job_contract(job):
    plan=job.snapshot.get('processing_plan',{})
    require(isinstance(plan,dict) and 'preview_contract' in plan,'Private preview lineage required.')
    return validate_contract(plan.get('exact_film_contract'))
