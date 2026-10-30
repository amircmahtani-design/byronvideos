"""Concrete render-only adapter graph; no calls or credentials on construction."""
from types import SimpleNamespace
from .hosted_control import Principal,digest,require


def build_exact_film_adapters(*,config,firestore_client,storage_client,token_reader,
    activation_check,transaction_runner=None,read_transport=None,enabled=False,now=None):
    from .generation_coordinator import FirestoreGenerationPlan
    from .preview_worker import FirestorePreviewEvidence
    from .exact_film_policy import FirestoreRetainedPreviewLineage,FirestoreExactFilmActions,validate_exact_film_lineage
    from .exact_film_composition import release_hash
    from .exact_film_worker import FirestoreExactFilmEvidence
    from .exact_film_dispatch import FirestoreExactFilmDispatchJournal,ExactFilmDispatcher,build_request
    from .exact_film_registry import FirestoreExactFilmRegistry,EXACT_PREFIX
    from .cloud_storage import CloudArtifactReader,CloudArtifactWriter
    from .cloud_run_readback import CloudRunReadback,CloudRunReadTransport
    from .worker_binding import HttpxCloudRunTransport
    from .exact_film_runtime import require_job_contract
    target=config['target'];common=dict(project_id=target['project_id'],namespace=config['firestore']['namespace'],transaction_runner=transaction_runner)
    plans=FirestoreGenerationPlan(firestore_client,**common)
    def budget(job):
        require_job_contract(job)
        frozen=plans.load(job)
        require(isinstance(frozen,dict) and isinstance(frozen.get('budget'),dict),'Retained exact-film budget unavailable.')
        value=frozen['budget'];configured=config['budget']
        require(all(value.get(k)==configured[k] for k in ('scope','approved_total_cap_cents','prior_spend_cents','job_cap_cents'))
            and all(segment['max_cost_cents']<=configured['operation_cap_cents'] for scene in frozen['scenes'] for segment in scene['segments']),'Retained film budget differs from accepted configuration.')
        return value
    preview_evidence=FirestorePreviewEvidence(firestore_client,**common)
    lineage=FirestoreRetainedPreviewLineage(firestore_client,**common,budget_loader=budget,target=target,preview_evidence=preview_evidence,now=now)
    actions=FirestoreExactFilmActions(firestore_client,**common,lineage_loader=lineage,final_release_sha256=release_hash(),now=now)
    journal=FirestoreExactFilmDispatchJournal(firestore_client,**common)
    evidence=FirestoreExactFilmEvidence(firestore_client,**common)
    def validate(job,approval):
        require_job_contract(job)
        current=actions.approval_lineage(Principal(job.owner_id),job,approval['approval_id'])
        require(current['approval']==approval,'Owner film authorization changed.')
        validate_exact_film_lineage(job,current,expected_final_release_sha256=release_hash(),now=now() if callable(now) else None)
        return True
    request_loader=lambda job,target:build_request(target,job,digest(config),digest(actions.get_approval(job)))
    read=read_transport or CloudRunReadTransport(token_reader,enabled=enabled)
    runtime=config['runtime']
    readback=CloudRunReadback(target=target,read=read,expected_task_template_sha256=runtime['cloud_run_task_template_sha256'],
        expected_service_account=runtime['worker_service_account'],expected_request_loader=request_loader,enabled=enabled)
    dispatcher=ExactFilmDispatcher(target=target,config_sha256=digest(config),journal=journal,approval_loader=actions.get_approval,
        approval_validator=validate,readback=readback,transport=HttpxCloudRunTransport(token_reader),activation_check=activation_check,enabled=enabled)
    registry=FirestoreExactFilmRegistry(firestore_client,**common,actions=actions,receipt_loader=journal.get_receipt,evidence_verifier=evidence.verify)
    reader=CloudArtifactReader(storage_client,bucket=target['bucket'],reference_resolver=registry,prefix=EXACT_PREFIX)
    writer=CloudArtifactWriter(storage_client,bucket=target['bucket'],prefix=EXACT_PREFIX)
    return SimpleNamespace(actions=actions,lineage=lineage,dispatch=dispatcher,journal=journal,readback=readback,evidence=evidence,
        registry=registry,reader=reader,writer=writer,preview_evidence=preview_evidence)
