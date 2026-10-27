"""Immutable input handoff for a future private worker; no account or network IO."""
import copy
import hashlib
from .engine import validate_script, narration_text
from .elevenlabs_adapter import fingerprint, plan_requests


def freeze_job(raw_script, ordered_images, narration_profile, visual_style_sha256):
    validate_script(raw_script)
    if not isinstance(ordered_images, list) or not 4 <= len(ordered_images) <= 5:
        raise ValueError('Choose four or five numbered images.')
    if not isinstance(visual_style_sha256, str) or len(visual_style_sha256) != 64 or any(c not in '0123456789abcdef' for c in visual_style_sha256):
        raise ValueError('Frozen visual style fingerprint required.')
    seen = set()
    for index, item in enumerate(ordered_images, 1):
        if not isinstance(item, dict) or set(item) != {'id', 'position', 'sha256'}:
            raise ValueError('Each image needs an ID, selected position and original-byte fingerprint.')
        if not isinstance(item['id'], str) or not item['id'] or len(item['id']) > 80 or item['id'] in seen:
            raise ValueError('Unique image IDs required.')
        if type(item['position']) is not int or item['position'] != index:
            raise ValueError('Image order changed; use the explicitly numbered browser order.')
        if not isinstance(item['sha256'], str) or len(item['sha256']) != 64 or any(c not in '0123456789abcdef' for c in item['sha256']):
            raise ValueError('Original image fingerprint missing.')
        seen.add(item['id'])
    spoken = narration_text(raw_script)
    result = {'schema_version': 1, 'status': 'DRAFT_NOT_ACTIVATED',
              'raw_script': raw_script, 'raw_script_sha256': hashlib.sha256(raw_script.encode()).hexdigest(),
              'narrated_text': spoken, 'narrated_text_sha256': hashlib.sha256(spoken.encode()).hexdigest(),
              'images': copy.deepcopy(ordered_images), 'input_order': [x['id'] for x in ordered_images],
              'narration_profile': copy.deepcopy(narration_profile), 'visual_style_sha256': visual_style_sha256,
              'narration_requests': plan_requests(spoken, narration_profile),
              'sequence_policy': 'explicit_numbered_order_no_ai_reordering',
              'publication': 'private_no_automatic_publish'}
    result['sha256'] = fingerprint(result)
    return result


def hosted_readiness():
    # Existing web code accepts only mode=local. Do not pretend remote is local.
    return {'ready': False, 'mode': 'hosted_setup', 'live_release': False,
            'blockers': ['renderer_host_and_storage', 'creator_authentication',
                         'elevenlabs_voice_profile_and_server_secret', 'fal_server_secret_and_reviewed_motion_recipe',
                         'approved_cost_limits_and_external_processing', 'reviewed_compositor_narration_profile',
                         'real_end_to_end_acceptance_and_recovery_test']}
