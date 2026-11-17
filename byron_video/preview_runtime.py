"""Separate signed private-preview runtime authorization, never final acceptance.

A first-film draft cannot truthfully provide prior acceptance of that same film.
This envelope instead binds reviewed technical/privacy/cost gates and an explicit
private draft policy; the historical real-media acceptance verifier is unchanged.
"""
import base64,json,time
from .hosted_control import canonical
from .elevenlabs_adapter import fingerprint

PREVIEW_EVIDENCE=frozenset({'identity_flow','private_storage','provider_cost_and_disclosure',
    'draft_policy_review','recovery','pinned_runtime','browser_device','handoff_adapter'})
PURPOSE='byron-private-preview-runtime-authorization-v1'


def verify_runtime_authorization(envelope,public_key,config_sha256,plan_sha256,*,preview_contract=None,now=None):
    if preview_contract is None:
        from .runtime_assembly import verify_acceptance
        return verify_acceptance(envelope,public_key,config_sha256,plan_sha256,now=now)
    from .private_preview import validate_intent
    from .automated_review import SHA
    try:
        intent=validate_intent(preview_contract)
        now=time.time() if now is None else now
        if not isinstance(envelope,dict) or set(envelope)!={'payload','signature'}:raise ValueError()
        p=envelope['payload']
        if (not isinstance(p,dict) or set(p)!={'schema_version','purpose','status','config_sha256','plan_sha256',
            'preview_contract_sha256','issued_at','expires_at','evidence','owner_only','public_release'}
            or type(p['schema_version']) is not int or p['schema_version']!=1
            or p['purpose']!=PURPOSE or p['status']!='PRIVATE_PREVIEW_AUTHORIZED'
            or p['config_sha256']!=config_sha256 or p['plan_sha256']!=plan_sha256
            or p['preview_contract_sha256']!=fingerprint(intent) or p['owner_only'] is not True or p['public_release'] is not False
            or type(p['issued_at']) is not int or type(p['expires_at']) is not int
            or not p['issued_at']<=now<p['expires_at'] or not 0<p['expires_at']-p['issued_at']<=7*86400
            or not isinstance(p['evidence'],dict) or set(p['evidence'])!=PREVIEW_EVIDENCE
            or not all(isinstance(v,str) and SHA.fullmatch(v) for v in p['evidence'].values())
            or not isinstance(public_key,bytes) or len(public_key)!=32):raise ValueError()
        signature=envelope['signature']
        if not isinstance(signature,str) or len(signature)>100:raise ValueError()
        signature=base64.b64decode(signature,validate=True)
        if len(signature)!=64:raise ValueError()
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature,canonical(p).encode())
        return json.loads(canonical(p))
    except Exception:
        raise ValueError('Private preview authorization is absent, expired, unsigned or differs from exact draft configuration/policy.') from None
