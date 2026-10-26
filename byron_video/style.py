"""Strict deterministic style compiler; no AI summarisation and no defaults.

Human protocols live in style-guide/*.md. The machine-readable source is the
single editable authority for effective settings. The runtime is a minified,
hashed snapshot, never an independently edited second style definition.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re

STYLE_DIR = Path(__file__).resolve().parent.parent / "style-guide"

# This schema is code, not inferred from the editable source: deleting a source
# key must fail rather than silently turning a missing value into a default.
SCHEMA = {
    "schema_version": int, "style_version": str,
    "protocols": {"README.md": str, "PROTOCOL.md": str},
    "implementation": {"renderer_file": str, "renderer_sha256": str, "compiler_file": str, "compiler_sha256": str},
    "approval": {"status": str, "record": (str, type(None)), "record_sha256": (str, type(None))},
    "canvas": {"width": int, "height": int, "fps": int, "aspect": str, "colour_space": str},
    "palette": {key: str for key in ("charcoal", "crimson", "gold", "parchment")},
    "artwork": {"fit": str, "viewport_width": float, "viewport_height": float, "viewport_top": float, "resampling": str, "image_count_min": int, "image_count_max": int, "ai_modification": bool, "preserve_order": bool},
    "motion": {"zoom_min": float, "zoom_max": float, "pan_width_fraction": float, "easing": str, "alternate_direction": bool, "crop_artwork": bool},
    "scene_mapping": {"policy": str, "minimum_hold_seconds": float, "boundary_search_fraction": float},
    "transitions": {key: float for key in ("crossfade_seconds", "opening_fade_seconds", "closing_fade_seconds")},
    "captions": {
        "font_family": str, "font_style": str, "font_file": str, "font_sha256": str, "font_license_file": str,
        "font_height_fraction": float, "line_height_fraction": float, "two_line_top_fraction": float,
        "one_line_top_fraction": float, "safe_width_fraction": float, "max_lines": int,
        "max_characters": int, "max_duration_seconds": float, "tail_extension_seconds": float, "sentence_minimum_seconds": float,
        "stroke_height_divisor": int, "text_colour": str, "stroke_colour": str, "timing": str,
        "source_text_only": bool,
    },
    "narration": {
        "provider": str, "voice": str, "voice_sha256": str, "language": str,
        "model_repo": str, "model_revision": str, "model_file": str, "model_sha256": str, "model_config_sha256": str,
        "model_license": str, "speed": float, "minimum_speed": float, "maximum_speed": float,
        "leading_silence_seconds": float, "paragraph_pause_seconds": float,
        "trailing_silence_seconds": float, "random_seed": int, "device": str,
        "max_threads": int, "wording_policy": str, "voice_approval": str,
    },
    "audio": {
        "sample_rate": int, "pcm_subtype": str, "normalization": str, "peak_limit": float,
        "target_lufs": (float, int, type(None)), "true_peak_db": (float, int, type(None)),
        "lra_target": float, "loudness_status": str, "music": str,
    },
    "encoding": {"video_codec": str, "video_preset": str, "crf": int, "pixel_format": str, "encoder_threads": int, "audio_codec": str, "audio_bitrate": str, "faststart": bool},
    "dependencies": {key: str for key in ("kokoro", "misaki", "torch", "transformers", "spacy", "spacy_model", "spacy_model_version", "pillow", "numpy", "ffmpeg", "espeakng-loader", "soundfile")},
    "references": {"manifest": str, "manifest_sha256": str, "approval_gate": str, "user_assets_public": bool},
}


def canonical_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def style_hash(style: dict) -> str:
    return hashlib.sha256(canonical_bytes(style)).hexdigest()


def effective_style_hash(style: dict) -> str:
    """Bind approval to every setting without a circular approval-file hash."""
    return style_hash({key: value for key, value in style.items() if key != "approval"})


def _schema(value, schema, path="style"):
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        if set(value) != set(schema):
            missing, extra = sorted(set(schema) - set(value)), sorted(set(value) - set(schema))
            raise ValueError(f"{path}: missing keys {missing}; unknown keys {extra}")
        for key, expected in schema.items():
            _schema(value[key], expected, f"{path}.{key}")
    elif schema is float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"{path} must be a finite number")
    elif isinstance(schema, tuple):
        if type(value) not in schema or (type(value) is float and not math.isfinite(value)):
            raise ValueError(f"{path} has an invalid type")
    elif type(value) is not schema:
        raise ValueError(f"{path} has an invalid type")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _relative_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    _require(not path.is_absolute() and ".." not in path.parts, "Style assets must use safe relative paths")
    resolved = (root / path).resolve()
    _require(resolved.is_relative_to(root.resolve()) and resolved.is_file(), f"Missing or unsafe style asset: {relative}")
    return resolved


def resolve_font(style: dict, style_dir: Path = STYLE_DIR) -> Path:
    font = _relative_file(Path(style_dir), style["captions"]["font_file"])
    _require(hashlib.sha256(font.read_bytes()).hexdigest() == style["captions"]["font_sha256"], "Caption font fingerprint differs from the style lock")
    _relative_file(Path(style_dir), style["captions"]["font_license_file"])
    return font


def validate_style(style: dict, style_dir: Path = STYLE_DIR) -> dict:
    _schema(style, SCHEMA)
    _require(style["schema_version"] == 1, "Unsupported style schema")
    for name, digest in style["protocols"].items():
        document = _relative_file(Path(style_dir), name)
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", digest)) and hashlib.sha256(document.read_bytes()).hexdigest() == digest, f"Human protocol fingerprint differs from the style lock: {name}")
    for role in ("renderer", "compiler"):
        expected_file = f"byron_video/{'engine' if role == 'renderer' else 'style'}.py"
        _require(style["implementation"][f"{role}_file"] == expected_file, "Unexpected style implementation path")
        implementation = _relative_file(Path(style_dir).parent, expected_file)
        digest = style["implementation"][f"{role}_sha256"]
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", digest)) and hashlib.sha256(implementation.read_bytes()).hexdigest() == digest, f"{role.title()} code fingerprint differs from the style lock")
    _require(bool(re.fullmatch(r"(?:draft-)?\d+\.\d+\.\d+", style["style_version"])), "Invalid style version")
    for value in style["palette"].values():
        _require(bool(re.fullmatch(r"#[0-9A-F]{6}", value)), "Palette values must be uppercase hexadecimal colours")
    canvas, art, motion, captions, voice, audio = (style[key] for key in ("canvas", "artwork", "motion", "captions", "narration", "audio"))
    _require((canvas["width"], canvas["height"]) in ((1920, 1080), (1280, 720), (960, 540)) and canvas["fps"] in (24, 25, 30), "Unsupported canvas")
    _require(canvas["aspect"] == "16:9" and canvas["colour_space"] == "bt709", "Unsupported aspect or colour space")
    _require(art["fit"] == "contain" and art["resampling"] == "lanczos", "Paintings must use uncropped contain rendering")
    _require(not art["ai_modification"] and art["preserve_order"] and not motion["crop_artwork"], "Art integrity cannot be disabled")
    _require(art["image_count_min"] == 4 and art["image_count_max"] == 5, "Exactly four or five paintings are supported")
    _require(0 < art["viewport_width"] <= .96 and 0 < art["viewport_height"] <= .8 and 0 <= art["viewport_top"] < .1, "Unsafe artwork viewport")
    _require(0 < motion["zoom_min"] <= motion["zoom_max"] <= 1 and motion["zoom_max"] - motion["zoom_min"] <= .05, "Motion would crop or over-zoom the artwork")
    _require(0 <= motion["pan_width_fraction"] <= .02 and motion["easing"] == "smoothstep", "Unsupported motion")
    _require(art["viewport_width"] * motion["zoom_max"] + motion["pan_width_fraction"] <= 1, "Horizontal motion can clip artwork")
    _require(art["viewport_top"] + art["viewport_height"] <= .85, "Art overlaps caption area")
    mapping = style["scene_mapping"]
    _require(mapping["policy"] == "paragraphs_when_counts_match_else_nearby_sentence_end" and 2 <= mapping["minimum_hold_seconds"] <= 10 and 0 <= mapping["boundary_search_fraction"] <= .5, "Unsupported scene mapping")
    _require(all(0 < v <= 2 for v in style["transitions"].values()), "Transition duration is outside the safe range")
    _require(captions["max_lines"] == 2 and captions["timing"] == "kokoro_token_duration" and captions["source_text_only"], "Unsupported caption policy")
    _require(.025 <= captions["font_height_fraction"] <= .05 and captions["font_height_fraction"] <= captions["line_height_fraction"] <= .07, "Invalid caption typography")
    _require(.7 <= captions["safe_width_fraction"] <= .9 and .84 <= captions["two_line_top_fraction"] <= .9 and .88 <= captions["one_line_top_fraction"] <= .94, "Captions are outside the safe area")
    _require(captions["two_line_top_fraction"] + captions["line_height_fraction"] + captions["font_height_fraction"] / 2 <= .97, "Caption baseline exceeds lower safe area")
    _require(20 <= captions["max_characters"] <= 90 and 1 <= captions["max_duration_seconds"] <= 6 and 0 <= captions["tail_extension_seconds"] <= .5 and .5 <= captions["sentence_minimum_seconds"] <= 3 and captions["stroke_height_divisor"] > 0, "Invalid caption grouping")
    _require(captions["text_colour"] in style["palette"] and captions["stroke_colour"] in style["palette"], "Unknown caption colour")
    _require(voice["provider"] == "local_kokoro" and voice["voice"] in ("bm_george", "bm_fable") and voice["language"] == "b", "Unsupported narration provider or voice")
    _require(voice["model_repo"] == "hexgrad/Kokoro-82M" and voice["model_license"] == "Apache-2.0" and voice["device"] == "cpu", "Unapproved model source or device")
    _require(bool(re.fullmatch(r"[0-9a-f]{40}", voice["model_revision"])), "Model must be pinned to a revision")
    _require(Path(voice["model_file"]).name == voice["model_file"] and voice["model_file"].endswith(".pth"), "Unsafe model filename")
    _require(.5 <= voice["minimum_speed"] <= voice["speed"] <= voice["maximum_speed"] <= 1.5, "Invalid narration speeds")
    _require(all(0 <= voice[key] <= 2 for key in ("leading_silence_seconds", "paragraph_pause_seconds", "trailing_silence_seconds")) and 1 <= voice["max_threads"] <= 16, "Invalid narration timing or threads")
    _require(voice["wording_policy"] == "preserve_exact_source" and voice["voice_approval"] in ("PROVISIONAL", "APPROVED"), "Unsupported narration policy")
    _require(audio["sample_rate"] == 24000 and audio["pcm_subtype"] == "PCM_16" and 0 < audio["peak_limit"] < 1 and audio["music"] == "none", "Unsupported audio format")
    _require(audio["normalization"] == "ebu_r128_two_pass", "Unsupported normalization")
    _require(type(audio["target_lufs"]) in (int, float) and -24 <= audio["target_lufs"] <= -12 and type(audio["true_peak_db"]) in (int, float) and -6 <= audio["true_peak_db"] <= -1 and 1 <= audio["lra_target"] <= 20 and audio["loudness_status"] in ("IMPLEMENTED_PENDING_LISTENING_APPROVAL", "APPROVED"), "Invalid loudness target")
    for key, value in style["dependencies"].items():
        _require(bool(value.strip()) and not any(x in value for x in ("*", ">", "<", "~", "^")), f"Dependency {key} must have an exact version")
    encoding = style["encoding"]
    _require(encoding["video_codec"] == "libx264" and encoding["audio_codec"] == "aac" and encoding["pixel_format"] == "yuv420p" and encoding["faststart"], "Unsupported output encoding")
    _require(encoding["video_preset"] in ("fast", "medium", "slow") and 16 <= encoding["crf"] <= 24 and 1 <= encoding["encoder_threads"] <= 16 and bool(re.fullmatch(r"\d{2,3}k", encoding["audio_bitrate"])), "Invalid encoding settings")
    for value in (captions["font_sha256"], voice["voice_sha256"], voice["model_sha256"], voice["model_config_sha256"], style["references"]["manifest_sha256"]):
        _require(bool(re.fullmatch(r"[0-9a-f]{64}", value)), "Assets require exact SHA256 fingerprints")
    _require(not style["references"]["user_assets_public"] and style["references"]["approval_gate"] == "human_review_required", "Private references and human approval are required")
    resolve_font(style, style_dir)
    manifest = _relative_file(Path(style_dir), style["references"]["manifest"])
    _require(hashlib.sha256(manifest.read_bytes()).hexdigest() == style["references"]["manifest_sha256"], "Reference manifest fingerprint differs from the style lock")
    references = json.loads(manifest.read_text(encoding="utf-8"))
    _require(isinstance(references, dict) and references.get("schema_version") == 1 and isinstance(references.get("assets"), list), "Invalid reference manifest schema")
    _require(references.get("status") in ("DRAFT_NO_APPROVED_REFERENCE", "DRAFT_SYNTHETIC_ONLY", "APPROVED_REFERENCE"), "Invalid reference manifest status")
    for asset in references["assets"]:
        _require(isinstance(asset, dict) and set(asset) == {"id", "kind", "sha256", "storage", "reference"}, "Invalid reference asset schema")
        _require(isinstance(asset["id"], str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{3,80}", asset["id"])) and asset["kind"] in ("video", "frame", "subtitles", "metadata"), "Invalid reference asset identity")
        _require(isinstance(asset["sha256"], str) and bool(re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])), "Reference asset fingerprint is required")
        _require(asset["storage"] in ("private", "public_synthetic") and isinstance(asset["reference"], str), "Invalid reference storage")
        if asset["storage"] == "public_synthetic":
            asset_file = _relative_file(Path(style_dir), asset["reference"])
            _require(hashlib.sha256(asset_file.read_bytes()).hexdigest() == asset["sha256"], "Public synthetic reference fingerprint mismatch")
        else:
            _require(bool(re.fullmatch(r"[A-Za-z0-9_-]{3,160}", asset["reference"])), "Private references must use opaque IDs, never URLs or paths")
    approval = style["approval"]
    _require(approval["status"] in ("DRAFT", "APPROVED"), "Invalid approval status")
    if approval["status"] == "DRAFT":
        _require(approval["record"] is None and approval["record_sha256"] is None and style["style_version"].startswith("draft-"), "Drafts must not imply approval")
    else:
        _require(not style["style_version"].startswith("draft-") and voice["voice_approval"] == "APPROVED" and audio["loudness_status"] == "APPROVED", "Approved styles require a release version and approved voice and audio")
        _require(isinstance(approval["record"], str) and isinstance(approval["record_sha256"], str), "An approval record is required")
        record = _relative_file(Path(style_dir), approval["record"])
        _require(hashlib.sha256(record.read_bytes()).hexdigest() == approval["record_sha256"], "Approval record hash mismatch")
        try:
            evidence = json.loads(record.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Approval requires a structured JSON record, not an empty file or inferred approval") from exc
        expected_keys = {"schema_version", "status", "style_version", "effective_style_sha256", "reference_manifest_sha256", "evidence_id", "approved_at", "approved_by", "scope"}
        _require(isinstance(evidence, dict) and set(evidence) == expected_keys and evidence["schema_version"] == 1 and evidence["status"] == "APPROVED", "Invalid structured approval record")
        _require(evidence["style_version"] == style["style_version"] and evidence["effective_style_sha256"] == effective_style_hash(style) and evidence["reference_manifest_sha256"] == style["references"]["manifest_sha256"], "Approval is not bound to this exact style and reference manifest")
        _require(evidence["approved_by"] == "user" and evidence["scope"] == ["visual_style", "voice", "audio"], "Approval must cover visual style, voice and audio")
        _require(isinstance(evidence["evidence_id"], str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{3,160}", evidence["evidence_id"])) and isinstance(evidence["approved_at"], str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", evidence["approved_at"])), "Approval requires a non-sensitive evidence ID and UTC timestamp")
        _require(references["status"] == "APPROVED_REFERENCE" and {asset["kind"] for asset in references["assets"]} >= {"video", "frame", "subtitles", "metadata"}, "Approved styles require a complete approved reference set")
        reference_approval = references.get("approval")
        _require(isinstance(reference_approval, dict) and reference_approval == {"evidence_id": evidence["evidence_id"], "approved_at": evidence["approved_at"]}, "Reference approval must match the style approval evidence")
    return style


def compile_style(style_dir: Path = STYLE_DIR, *, check: bool = False) -> dict:
    root = Path(style_dir)
    source = json.loads((root / "style.source.json").read_text(encoding="utf-8"))
    validate_style(source, root)
    digest = style_hash(source)
    envelope = {"schema_version": 1, "source_sha256": digest, "preset_sha256": digest, "preset": source}
    content = canonical_bytes(envelope) + b"\n"
    target = root / "style.runtime.json"
    if source["approval"]["status"] == "APPROVED":
        frozen = root / "approved" / f"{source['style_version']}.json"
        _require(frozen.is_file() and frozen.read_bytes() == content, "An approved immutable snapshot must be reviewed and frozen before compilation; never overwrite an approved version")
    if check:
        _require(target.is_file() and target.read_bytes() == content, "Compiled style is stale: run python -m byron_video.style and review the diff")
    else:
        target.write_bytes(content)
    return envelope


def load_style(style_dir: Path = STYLE_DIR) -> dict:
    """Read the condensed snapshot only after source/snapshot/asset verification."""
    root = Path(style_dir)
    expected = compile_style(root, check=True)
    runtime = json.loads((root / "style.runtime.json").read_text(encoding="utf-8"))
    _require(runtime == expected, "Style snapshot changed during verification")
    return runtime["preset"]


def main():
    parser = argparse.ArgumentParser(description="Compile the draft style deterministically, or verify it is current.")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        result = compile_style(check=args.check)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Style check failed: {exc}\n")
    print(f"{result['preset']['style_version']} {result['preset']['approval']['status']} {result['preset_sha256']}")


if __name__ == "__main__":
    main()
