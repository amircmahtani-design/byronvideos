"""Static authenticity versus current paid-dispatch admission.

Expired generation policy can still identify the installed private configuration
for retained read-only access. Historical validation never issues job authority.
New generation requires every quote/permission to be current before Cloud Run.
"""
import copy,math,time
from .hosted_control import digest,require
from .declarative_policy import ApprovedProductionPolicy,verify_signed
from .vertex_token_preflight import validate_billing_contract


class ProductionAdmission:
    def __init__(self,*,config,processing_plan,policy_envelope,policy_public_key,billing_envelope,billing_public_key,
                 infrastructure_envelope=None,infrastructure_public_key=None,now=time.time):
        self.config=copy.deepcopy(config);self.plan=copy.deepcopy(processing_plan);self.now=now
        self._policy_envelope=copy.deepcopy(policy_envelope);self._policy_key=bytes(policy_public_key)
        self.billing=copy.deepcopy(billing_envelope);self.billing_key=bytes(billing_public_key)
        self.infrastructure=copy.deepcopy(infrastructure_envelope)
        self.infrastructure_key=bytes(infrastructure_public_key) if infrastructure_public_key is not None else self._policy_key
        self.validate_configuration()
    def _policy_at(self,clock):
        return ApprovedProductionPolicy(self._policy_envelope,public_key=self._policy_key,
            config_sha256=digest(self.config),processing_plan_sha256=digest(self.plan),now=clock)
    @property
    def policy(self):
        """Only a CURRENT policy escapes; no historical capability is returned."""
        return self._policy_at(self.now)
    def _validate(self,policy,clock):
        policy.validate()
        infrastructure=policy.validate_infrastructure(self.config)
        require(infrastructure['requires_separate_reservation'] is False,
            'Quoted infrastructure mode needs a separate enforced reservation adapter; bounded-use consent is not a dollar cap.')
        require(all(policy.payload['budget'][k]==self.config['budget'][k] for k in self.config['budget']),
            'Signed cumulative budget differs from private configuration.')
        validate_billing_contract(policy,self.billing,self.billing_key,now=clock)
        return True
    def validate_configuration(self):
        """Authenticate static installation even after generation expiry.

        The common validation time is derived solely from independently signed
        issuance fields and cannot be selected by the caller. Documents issued
        in the future, missing signatures and nonoverlapping validity fail. The
        local historical policy is discarded; this returns only a boolean.
        """
        p=verify_signed(self._policy_envelope,self._policy_key)
        b=verify_signed(self.billing,self.billing_key);now=self.now()
        require(type(now) in (int,float) and math.isfinite(now),'Finite actual configuration clock required.')
        for document in (p,b):
            require(type(document.get('issued_at')) is int and type(document.get('expires_at')) is int
                and 0<=document['issued_at']<document['expires_at'] and document['issued_at']<=now,
                'Future-issued or invalid static authorization interval.')
        common=max(p['issued_at'],b['issued_at'])
        require(common<min(p['expires_at'],b['expires_at']),'Signed configuration and billing evidence never had a valid overlap.')
        historical_clock=lambda:common
        return self._validate(self._policy_at(historical_clock),historical_clock)
    def validate(self):
        """Current generation admission only; never use in read-only checks."""
        return self._validate(self.policy,self.now)
    def for_job(self,job):
        policy=self.policy;self._validate(policy,self.now);policy._job(job)
        validate_billing_contract(policy,self.billing,self.billing_key,job,now=self.now)
        policy.narration_approval_loader(job)
        return True
    def for_exact_film(self,job):
        """Current render-only compute consent, never paid-generation authority."""
        self.validate_configuration()
        from .infrastructure_authorization import validate_exact_infrastructure
        require(self.infrastructure is not None,'Current independent exact-film infrastructure permission is missing.')
        validate_exact_infrastructure(self.infrastructure,self.infrastructure_key,self.config,self.plan,job=job,now=self.now)
        return True
