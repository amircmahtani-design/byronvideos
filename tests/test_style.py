import copy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from byron_video.style import STYLE_DIR, canonical_bytes, compile_style, effective_style_hash, load_style, resolve_font, style_hash, validate_style


@pytest.fixture
def root(tmp_path):
    target = tmp_path / "style-guide"
    shutil.copytree(STYLE_DIR, target)
    (tmp_path / "byron_video").mkdir()
    for name in ("engine.py", "style.py"):
        shutil.copyfile(STYLE_DIR.parent / "byron_video" / name, tmp_path / "byron_video" / name)
    return target


def read(root):
    return json.loads((root / "style.source.json").read_text())


def write(root, source):
    (root / "style.source.json").write_text(json.dumps(source))


def leaves(value, path=()):
    for key, item in value.items():
        if isinstance(item, dict):
            yield from leaves(item, path + (key,))
        else:
            yield path + (key,)


def test_compiled_snapshot_is_current():
    compile_style(check=True)
    style = load_style()
    assert style["approval"]["status"] == "DRAFT"
    assert style["style_version"] == "draft-0.1.0"
    assert style["narration"]["voice_approval"] == "PROVISIONAL"
    assert style_hash(style) == hashlib.sha256(canonical_bytes(style)).hexdigest()


def test_compile_is_deterministic_and_compact(root):
    first = compile_style(root)
    content = (root / "style.runtime.json").read_bytes()
    compile_style(root)
    assert (root / "style.runtime.json").read_bytes() == content
    assert content.count(b"\n") == 1
    assert first["source_sha256"] == first["preset_sha256"]
    assert first["preset"] == read(root)


def test_every_missing_leaf_fails(root):
    source = read(root)
    for path in leaves(source):
        modified = copy.deepcopy(source)
        target = modified
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
        with pytest.raises(ValueError, match="missing keys"):
            validate_style(modified, root)


def test_unknown_setting_fails(root):
    source = read(root)
    source["motion"]["invent_a_pan"] = True
    with pytest.raises(ValueError, match="unknown keys"):
        validate_style(source, root)


def test_stale_source_fails_closed(root):
    source = read(root)
    source["transitions"]["crossfade_seconds"] = .8
    write(root, source)
    with pytest.raises(ValueError, match="stale"):
        load_style(root)
    compile_style(root)
    assert load_style(root)["transitions"]["crossfade_seconds"] == .8


def test_tampered_compiled_file_fails(root):
    compiled = root / "style.runtime.json"
    compiled.write_bytes(compiled.read_bytes().replace(b'"crf":20', b'"crf":21'))
    with pytest.raises(ValueError, match="stale"):
        load_style(root)


def test_missing_and_tampered_font_fail(root):
    source = read(root)
    font = root / source["captions"]["font_file"]
    font.write_bytes(b"wrong font")
    with pytest.raises(ValueError, match="fingerprint"):
        resolve_font(source, root)
    font.unlink()
    with pytest.raises(ValueError, match="Missing"):
        load_style(root)


def test_reference_manifest_hash_is_locked(root):
    source = read(root)
    (root / source["references"]["manifest"]).write_text("{}")
    with pytest.raises(ValueError, match="manifest fingerprint"):
        load_style(root)


@pytest.mark.parametrize("section,key,value", [
    ("captions", "font_file", "../outside.ttf"),
    ("motion", "zoom_max", 1.2),
    ("motion", "crop_artwork", True),
    ("artwork", "ai_modification", True),
    ("captions", "max_lines", 3),
    ("canvas", "fps", True),
    ("narration", "model_revision", "main"),
    ("audio", "peak_limit", float("nan")),
])
def test_invalid_settings_fail(root, section, key, value):
    source = read(root)
    source[section][key] = value
    with pytest.raises(ValueError):
        validate_style(source, root)


def test_approval_cannot_be_inferred_from_success(root):
    source = read(root)
    source["approval"]["status"] = "APPROVED"
    write(root, source)
    with pytest.raises(ValueError):
        compile_style(root)


def test_approved_version_requires_exact_preexisting_snapshot(root):
    source = read(root)
    source["style_version"] = "0.1.0"
    source["narration"]["voice_approval"] = "APPROVED"
    source["audio"]["loudness_status"] = "APPROVED"
    approval = {"evidence_id": "synthetic-test-not-real-approval", "approved_at": "2026-01-01T00:00:00Z"}
    manifest = {"schema_version": 1, "status": "APPROVED_REFERENCE", "approval": approval, "assets": [
        {"id": f"test-{kind}", "kind": kind, "sha256": "0" * 64, "storage": "private", "reference": f"synthetic-test-only-{kind}"}
        for kind in ("video", "frame", "subtitles", "metadata")
    ]}
    manifest_file = root / source["references"]["manifest"]
    manifest_file.write_text(json.dumps(manifest))
    source["references"]["manifest_sha256"] = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    record = root / "approved" / "test-approval.json"
    record.write_text(json.dumps({"schema_version": 1, "status": "APPROVED", "style_version": source["style_version"], "effective_style_sha256": effective_style_hash(source), "reference_manifest_sha256": source["references"]["manifest_sha256"], "approved_by": "user", "scope": ["visual_style", "voice", "audio"], **approval}))
    source["approval"] = {"status": "APPROVED", "record": "approved/test-approval.json", "record_sha256": hashlib.sha256(record.read_bytes()).hexdigest()}
    write(root, source)
    with pytest.raises(ValueError, match="immutable snapshot"):
        compile_style(root)
    digest = style_hash(source)
    envelope = {"schema_version": 1, "source_sha256": digest, "preset_sha256": digest, "preset": source}
    frozen = root / "approved" / "0.1.0.json"
    frozen.write_bytes(canonical_bytes(envelope) + b"\n")
    compile_style(root)
    source["encoding"]["crf"] = 21
    write(root, source)
    with pytest.raises(ValueError, match="bound to this exact style"):
        compile_style(root)


def test_protocol_edits_cannot_drift_silently(root):
    (root / "PROTOCOL.md").write_text("Opposite rule: rewrite the user's essay and replace every painting.")
    with pytest.raises(ValueError, match="Human protocol fingerprint"):
        load_style(root)


def test_renderer_code_cannot_change_without_new_lock(root):
    renderer = root.parent / "byron_video" / "engine.py"
    renderer.write_bytes(renderer.read_bytes() + b"\n# changed renderer\n")
    with pytest.raises(ValueError, match="Renderer code fingerprint"):
        load_style(root)


def test_empty_approval_record_and_pending_audio_fail(root):
    source = read(root)
    source["style_version"] = "0.1.0"
    source["narration"]["voice_approval"] = "APPROVED"
    record = root / "approved" / "empty.json"
    record.write_bytes(b"")
    source["approval"] = {"status": "APPROVED", "record": "approved/empty.json", "record_sha256": hashlib.sha256(b"").hexdigest()}
    with pytest.raises(ValueError, match="approved voice and audio"):
        validate_style(source, root)
    source["audio"]["loudness_status"] = "APPROVED"
    with pytest.raises(ValueError, match="structured JSON record"):
        validate_style(source, root)


def test_approval_cannot_release_an_empty_reference_set(root):
    source = read(root)
    source["style_version"] = "0.1.0"
    source["narration"]["voice_approval"] = "APPROVED"
    source["audio"]["loudness_status"] = "APPROVED"
    record = root / "approved" / "test-empty-references.json"
    record.write_text(json.dumps({"schema_version": 1, "status": "APPROVED", "style_version": source["style_version"], "effective_style_sha256": effective_style_hash(source), "reference_manifest_sha256": source["references"]["manifest_sha256"], "approved_by": "user", "scope": ["visual_style", "voice", "audio"], "evidence_id": "synthetic-test-not-real-approval", "approved_at": "2026-01-01T00:00:00Z"}))
    source["approval"] = {"status": "APPROVED", "record": "approved/test-empty-references.json", "record_sha256": hashlib.sha256(record.read_bytes()).hexdigest()}
    with pytest.raises(ValueError, match="complete approved reference set"):
        validate_style(source, root)
