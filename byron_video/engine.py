"""Deterministic artwork compositor. No upload, rewriting, stock, or image generation.

Kokoro supplies the narration and its token-duration alignment; Pillow composes
uncropped originals; FFmpeg encodes H.264/AAC. This is not a hosted service.
"""
from __future__ import annotations

import argparse
import bisect
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import warnings
import importlib.metadata
from typing import Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from .style import load_style, style_hash, resolve_font, validate_style

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_SCRIPT_CHARS = 4000
SAMPLE_RATE = 24000
BACKGROUND = (20, 20, 22)
PARCHMENT = (239, 230, 210)
VOICES = ("bm_george", "bm_fable")


@dataclass
class Word:
    text: str
    start: float
    end: float
    char_start: int
    char_end: int


@dataclass
class Caption:
    text: str
    start: float
    end: float


def validate_script(script: str, allow_short: bool = False) -> str:
    # allow_short is retained for older local callers; all nonempty lengths are
    # now accepted within the separately documented technical input limits.
    if not isinstance(script, str) or not script.strip():
        raise ValueError("Paste the essay you want narrated.")
    if len(script) > MAX_SCRIPT_CHARS:
        raise ValueError("This prototype accepts up to 4,000 characters. It never silently shortens your essay.")
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in script):
        raise ValueError("The script contains unsupported control characters.")
    if re.search(r"\[[^\]]+\]\([^)]*\)", script):
        raise ValueError("Paste plain text, not Markdown links or pronunciation markup.")
    words = re.findall(r"\S+", script)
    if len(words) > 450:
        raise ValueError("This local prototype has a technical limit of 450 words per film. Nothing was rewritten or removed.")
    if any(len(word) > 80 for word in words):
        raise ValueError("The script contains an unusually long word or address. Please use readable plain text.")
    return script  # Preserve the original input, including punctuation and paragraphs.


def validate_images(paths: list[Path], background=BACKGROUND) -> list[Image.Image]:
    if len(paths) not in (4, 5):
        raise ValueError("Choose four or five paintings, in the order you want them shown.")
    images = []
    for path in paths:
        path = Path(path)
        if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError("Each painting must be a readable PNG or JPEG smaller than 20 MB.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if image.format not in ("PNG", "JPEG") or getattr(image, "n_frames", 1) != 1:
                        raise ValueError("Use still PNG or JPEG paintings, not animated or other image files.")
                    if image.width * image.height > MAX_IMAGE_PIXELS or min(image.size) < 64:
                        raise ValueError("Paintings must be at least 64 pixels wide and tall, and no more than 40 megapixels.")
                    image.verify()
                with Image.open(path) as image:
                    image = ImageOps.exif_transpose(image)
                    # Composite transparency without inventing image content.
                    rgba = image.convert("RGBA")
                    solid = Image.new("RGBA", rgba.size, background + (255,))
                    solid.alpha_composite(rgba)
                    images.append(solid.convert("RGB"))
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("One painting could not be decoded as a complete PNG or JPEG.") from exc
    return images


def _paragraphs(script: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group()) for m in re.finditer(r"[^\r\n]+(?:\r?\n(?!\s*\r?\n)[^\r\n]+)*", script) if m.group().strip()]


def _align_tokens(script: str, tokens: list, offset: float, cursor: int, duration: float) -> tuple[list[Word], int]:
    """Use source slices, never an ASR transcript or guessed word-length timing."""
    aligned = []
    for token in tokens:
        text = token.text
        if not text:
            continue
        while cursor < len(script) and script[cursor].isspace():
            cursor += 1
        if not script.startswith(text, cursor):
            raise ValueError("The speech engine changed the text during alignment. No video was exported; please use plain English text.")
        start, end = token.start_ts, token.end_ts
        char_start, char_end = cursor, cursor + len(text)
        cursor = char_end
        if start is None or end is None:
            if re.search(r"\w", text):
                raise ValueError("A word could not be given a reliable speech timestamp. No video was exported.")
            if aligned:
                aligned[-1].text += script[aligned[-1].char_end:char_end]
                aligned[-1].char_end = char_end
            continue
        start = max(0.0, min(float(start), duration)) + offset
        end = max(start, min(float(end), duration) + offset)
        aligned.append(Word(script[char_start:char_end], start, end, char_start, char_end))
    return aligned, cursor


def synthesize(script: str, voice: str, speed: float = .93, progress=None, style=None):
    """Synthesize complete paragraphs and retain native token timestamps.

    All model execution is local. A first run downloads official open model files.
    Timings are model-predicted alignment, not independently forced-aligned.
    """
    import torch
    from kokoro import KPipeline, KModel
    from huggingface_hub import hf_hub_download
    style = style or load_style()
    settings = style["narration"]
    for name in ("kokoro", "misaki", "torch", "transformers", "spacy", "espeakng-loader", "soundfile"):
        if importlib.metadata.version(name) != style["dependencies"][name]:
            raise RuntimeError(f"Install the pinned {name} version before rendering this style.")
    if importlib.metadata.version(style["dependencies"]["spacy_model"]) != style["dependencies"]["spacy_model_version"]:
        raise RuntimeError("Install the pinned English language model before rendering this style.")
    def pinned_file(filename, expected_hash):
        path = Path(hf_hub_download(repo_id=settings["model_repo"], filename=filename, revision=settings["model_revision"]))
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise RuntimeError("A speech asset does not match the frozen style fingerprint.")
        return str(path)
    model_path = pinned_file(settings["model_file"], settings["model_sha256"])
    config_path = pinned_file("config.json", settings["model_config_sha256"])
    voice_path = pinned_file(f"voices/{voice}.pt", settings["voice_sha256"])
    torch.set_num_threads(min(settings["max_threads"], os.cpu_count() or 1))
    torch.manual_seed(settings["random_seed"])
    model = KModel(repo_id=settings["model_repo"], config=config_path, model=model_path).to(settings["device"]).eval()
    pipeline = KPipeline(lang_code=settings["language"], repo_id=settings["model_repo"], model=model)
    if getattr(pipeline.g2p, "fallback", None) is None:
        # Kokoro otherwise can silently omit unfamiliar names.
        from misaki.espeak import EspeakFallback
        pipeline.g2p.fallback = EspeakFallback(british=True)
    parts = _paragraphs(script)
    arrays, aligned, paragraph_times = [], [], []
    samples, cursor = int(settings["leading_silence_seconds"] * SAMPLE_RATE), 0
    arrays.append(np.zeros(samples, dtype=np.float32))
    for index, (_, paragraph) in enumerate(parts):
        paragraph_times.append(samples / SAMPLE_RATE)
        # G2P and chunk preflight rejects any path Kokoro would truncate.
        _, tokens = pipeline.g2p(paragraph)
        chunks = list(pipeline.en_tokenize(tokens))
        if any(len(ps) > 510 for _, ps, _ in chunks):
            raise ValueError("The speech engine cannot safely split a passage. No words were truncated.")
        if any(re.search(r"\w", t.text) and not t.phonemes for t in tokens):
            raise ValueError("The voice could not pronounce a word. No video was exported.")
        for _, _, chunk in chunks:
            for result in pipeline.generate_from_tokens(chunk, voice=voice_path, speed=speed):
                audio = result.audio.detach().cpu().numpy().astype(np.float32)
                if not len(audio) or not np.isfinite(audio).all():
                    raise ValueError("The speech engine did not produce valid audio.")
                new_words, cursor = _align_tokens(script, result.tokens, samples / SAMPLE_RATE, cursor, len(audio) / SAMPLE_RATE)
                aligned.extend(new_words)
                arrays.append(audio)
                samples += len(audio)
        pause = np.zeros(int(settings["paragraph_pause_seconds"] * SAMPLE_RATE), dtype=np.float32)
        arrays.append(pause)
        samples += len(pause)
        if progress:
            progress(10 + round(30 * (index + 1) / len(parts)), "Recording the narration")
    if script[cursor:].strip():
        raise ValueError("Not every word was narrated. No incomplete film was exported.")
    arrays.append(np.zeros(int(settings["trailing_silence_seconds"] * SAMPLE_RATE), dtype=np.float32))
    audio = np.concatenate(arrays)
    if not aligned or np.max(np.abs(audio)) < .0001:
        raise ValueError("Narration is empty or silent.")
    # Peak protection only, no pitch shift, no voice cloning.
    peak = np.max(np.abs(audio))
    if peak > style["audio"]["peak_limit"]:
        audio *= style["audio"]["peak_limit"] / peak
    return audio, aligned, paragraph_times


def make_captions(script: str, words: list[Word], max_chars: int | None = None, style=None) -> list[Caption]:
    style = style or load_style()
    settings = style["captions"]
    max_chars = settings["max_characters"] if max_chars is None else max_chars
    captions, group = [], []
    for word in words:
        if group:
            candidate = script[group[0].char_start:word.char_end]
            if len(" ".join(candidate.split())) > max_chars or word.end - group[0].start > settings["max_duration_seconds"]:
                captions.append(_caption(script, group))
                group = []
        group.append(word)
        if re.search(r"[.!?;][\"'’”)]?$", word.text) and word.end - group[0].start >= settings["sentence_minimum_seconds"]:
            captions.append(_caption(script, group))
            group = []
    if group:
        captions.append(_caption(script, group))
    # Captions may outlast punctuation by a breath, but never overlap.
    for i, caption in enumerate(captions):
        if i + 1 < len(captions):
            caption.end = min(caption.end + settings["tail_extension_seconds"], captions[i + 1].start)
    return captions


def _caption(script, group):
    return Caption(" ".join(script[group[0].char_start:group[-1].char_end].split()), group[0].start, max(group[-1].end, group[0].start + .1))


def scene_boundaries(duration: float, count: int, paragraph_times: list[float], words: list[Word], style=None) -> list[float]:
    """Exact paragraph-to-painting mapping when counts match; otherwise nearby pauses."""
    if len(paragraph_times) == count:
        return [0.0] + paragraph_times[1:] + [duration]
    candidates = sorted(set(paragraph_times + [w.end for w in words if re.search(r"[.!?;]", w.text)]))
    boundaries = [0.0]
    settings = (style or load_style())["scene_mapping"]
    min_hold = min(settings["minimum_hold_seconds"], duration / (count * 2))
    for i in range(1, count):
        target = duration * i / count
        options = [t for t in candidates if boundaries[-1] + min_hold <= t <= duration - (count - i) * min_hold and abs(t - target) <= duration / count * settings["boundary_search_fraction"]]
        boundaries.append(min(options, key=lambda t: abs(t - target)) if options else target)
    return boundaries + [duration]


def _font(size: int, style=None):
    return ImageFont.truetype(str(resolve_font(style or load_style())), size)


def _colour(style, name):
    value = style["palette"][name].lstrip("#")
    return tuple(int(value[i:i+2], 16) for i in (0, 2, 4))


def artwork_frame(image: Image.Image, p: float, index: int, size: tuple[int, int], style=None) -> Image.Image:
    """Gentle motion inside a matte: the whole painting remains visible at all times."""
    width, height = size
    style = style or load_style()
    motion, art = style["motion"], style["artwork"]
    p = min(1, max(0, p))
    smooth = p * p * (3 - 2 * p)
    forward = not motion["alternate_direction"] or index % 2 == 0
    zoom = motion["zoom_min"] + (motion["zoom_max"] - motion["zoom_min"]) * (smooth if forward else 1 - smooth)
    scale = min(width * art["viewport_width"] / image.width, height * art["viewport_height"] / image.height) * zoom
    w, h = max(1, round(image.width * scale)), max(1, round(image.height * scale))
    resized = image.resize((w, h), Image.Resampling.LANCZOS)
    # Motion stays within available margins, never crops the painting.
    pan = (smooth - .5) * width * motion["pan_width_fraction"] * (1 if forward else -1)
    x, y = round((width - w) / 2 + pan), round(height * art["viewport_top"] + (height * art["viewport_height"] - h) / 2)
    frame = Image.new("RGB", size, _colour(style, "charcoal"))
    frame.paste(resized, (x, y))
    return frame


def wrap_caption(text: str, font, max_width: int) -> list[str]:
    words = text.split()
    if font.getlength(text) <= max_width:
        return [text]
    choices = [(" ".join(words[:i]), " ".join(words[i:])) for i in range(1, len(words))]
    choices = [pair for pair in choices if max(font.getlength(line) for line in pair) <= max_width]
    if not choices:
        raise ValueError("A caption cannot fit safely on two lines. Use shorter plain-text words.")
    return list(min(choices, key=lambda pair: abs(font.getlength(pair[0]) - font.getlength(pair[1]))))


def draw_caption(frame: Image.Image, caption: Caption | None, font, style=None):
    if not caption:
        return
    width, height = frame.size
    style = style or load_style()
    settings = style["captions"]
    lines = wrap_caption(caption.text, font, round(width * settings["safe_width_fraction"]))
    draw = ImageDraw.Draw(frame)
    line_height = round(height * settings["line_height_fraction"])
    y = round(height * settings["two_line_top_fraction"]) if len(lines) == 2 else round(height * settings["one_line_top_fraction"])
    for line in lines:
        draw.text((width / 2, y), line, fill=_colour(style, settings["text_colour"]), font=font, anchor="mm", stroke_width=max(1, round(height / settings["stroke_height_divisor"])), stroke_fill=_colour(style, settings["stroke_colour"]))
        y += line_height


def write_srt(path: Path, captions: list[Caption]):
    def stamp(value):
        ms = max(0, round(value * 1000))
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    path.write_text("\n\n".join(f"{i + 1}\n{stamp(c.start)} --> {stamp(c.end)}\n{c.text}" for i, c in enumerate(captions)) + "\n", encoding="utf-8")


def normalize_audio(source: Path, destination: Path, style: dict) -> dict:
    """EBU R128 two-pass loudness; never alter tempo, wording or pitch."""
    cfg = style["audio"]
    if cfg["normalization"] != "ebu_r128_two_pass":
        raise ValueError("This renderer requires the versioned two-pass speech loudness policy.")
    target = f"I={cfg['target_lufs']}:TP={cfg['true_peak_db']}:LRA={cfg['lra_target']}"
    result = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(source), "-af", "loudnorm=" + target + ":print_format=json", "-f", "null", "-"], capture_output=True, text=True, timeout=120, check=True)
    match = re.search(r'\{\s*"input_i".*?\}', result.stderr, re.DOTALL)
    if not match:
        raise RuntimeError("The audio loudness measurement was unavailable.")
    measured = json.loads(match.group())
    if not all(math.isfinite(float(measured[k])) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")):
        raise RuntimeError("The narration could not be loudness-normalized safely.")
    measured_filter = ":".join(f"{key}={measured[value]}" for key, value in (("measured_I", "input_i"), ("measured_TP", "input_tp"), ("measured_LRA", "input_lra"), ("measured_thresh", "input_thresh"), ("offset", "target_offset")))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-af", "loudnorm=" + target + ":" + measured_filter + ":linear=true", "-ar", str(cfg["sample_rate"]), "-c:a", "pcm_s16le", str(destination)], capture_output=True, timeout=120, check=True)
    return {"method": "ebu_r128_two_pass", "target_lufs": cfg["target_lufs"], "true_peak_db": cfg["true_peak_db"], "source_measurement": measured}


def duration_advisory(duration: float, style: dict) -> str | None:
    """Editorial guidance never prevents a valid film from being downloaded."""
    policy = style["duration"]
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The narration has no valid duration.")
    if duration > policy["technical_max_seconds"]:
        raise ValueError(f"The complete narration exceeds this prototype's {policy['technical_max_seconds']:g}-second technical processing limit. Nothing was cut or retimed.")
    if not policy["recommended_min_seconds"] <= duration <= policy["recommended_max_seconds"]:
        return (f"Outside the usual {policy['recommended_min_seconds']:g}–{policy['recommended_max_seconds']:g}-second range. "
                "Your complete script and narration speed were preserved.")
    return None


def prepare_narration(script: str, style: dict, progress=None):
    """One synthesis pass at the frozen voice speed; never regenerate to fit."""
    audio, words, paragraphs = synthesize(script, style["narration"]["voice"],
                                          speed=style["narration"]["speed"],
                                          progress=progress, style=style)
    duration = len(audio) / SAMPLE_RATE
    return audio, words, paragraphs, duration, duration_advisory(duration, style)


def render(script: str, image_paths: list[Path], output: Path, *, width=None, height=None, fps=None, voice=None, allow_short=False, progress: Callable[[int, str], None] | None = None, style_snapshot=None) -> dict:
    started = time.monotonic()
    style = json.loads(json.dumps(style_snapshot if style_snapshot is not None else load_style()))
    validate_style(style)
    width = style["canvas"]["width"] if width is None else width
    height = style["canvas"]["height"] if height is None else height
    fps = style["canvas"]["fps"] if fps is None else fps
    voice = style["narration"]["voice"] if voice is None else voice
    preview_override = (width, height, fps) != (style["canvas"]["width"], style["canvas"]["height"], style["canvas"]["fps"])
    if preview_override and style["approval"]["status"] != "DRAFT":
        raise ValueError("Approved styles cannot be overridden. Create a reviewed new style version instead.")
    validate_script(script, allow_short)
    images = validate_images(image_paths, background=_colour(style, "charcoal"))
    if voice != style["narration"]["voice"]:
        raise ValueError("The voice must match the frozen style. Change the draft style source to audition another voice.")
    if (width, height) not in ((1920, 1080), (1280, 720), (960, 540)) or fps not in (24, 25, 30):
        raise ValueError("Use a supported 16:9 output size and 24, 25 or 30 frames per second.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("Install FFmpeg and FFprobe before converting.")
    for package in ("pillow", "numpy"):
        if importlib.metadata.version(package) != style["dependencies"][package]:
            raise RuntimeError(f"Install the pinned {package} version before rendering this style.")
    ffmpeg_version = subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0].split()[2]
    if ffmpeg_version != style["dependencies"]["ffmpeg"]:
        raise RuntimeError("The FFmpeg version differs from this style lock. Review a new draft runtime rather than silently changing the encoder.")
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError("The output already exists. Choose a new filename to avoid overwriting it.")
    progress = progress or (lambda percent, message: None)
    progress(5, "Preparing the British narration")
    narration_started = time.monotonic()
    effective_speed = style["narration"]["speed"]
    audio, words, paragraphs, duration, advisory = prepare_narration(script, style, progress)
    narration_elapsed = time.monotonic() - narration_started
    captions = make_captions(script, words, style=style)
    # Validate exact caption content, allowing only whitespace/layout changes.
    expected = re.sub(r"\s+", "", script)
    actual = re.sub(r"\s+", "", "".join(c.text for c in captions))
    if expected != actual:
        raise ValueError("Captions did not preserve every character. No incomplete film was exported.")
    font = _font(round(height * style["captions"]["font_height_fraction"]), style)
    for caption in captions:
        wrap_caption(caption.text, font, round(width * style["captions"]["safe_width_fraction"]))
    boundaries = scene_boundaries(duration, len(images), paragraphs, words, style)
    frames = math.ceil(duration * fps)
    caption_starts = [c.start for c in captions]
    progress(45, "Preparing paintings and captions")
    render_started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="byron-render-", dir=output.parent) as temp:
        import soundfile as sf
        temp = Path(temp)
        wav, mp4, log = temp / "narration.wav", temp / "film.mp4", temp / "encoder.log"
        sf.write(wav, audio, SAMPLE_RATE, subtype=style["audio"]["pcm_subtype"])
        normalized = temp / "normalized.wav"
        loudness = normalize_audio(wav, normalized, style)
        enc = style["encoding"]
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "pipe:0", "-i", str(normalized), "-map", "0:v:0", "-map", "1:a:0", "-c:v", enc["video_codec"], "-preset", enc["video_preset"], "-crf", str(enc["crf"]), "-threads", str(enc["encoder_threads"]), "-pix_fmt", enc["pixel_format"], "-colorspace", style["canvas"]["colour_space"], "-color_primaries", style["canvas"]["colour_space"], "-color_trc", style["canvas"]["colour_space"], "-c:a", enc["audio_codec"], "-b:a", enc["audio_bitrate"], "-movflags", "+faststart", "-t", f"{duration:.6f}", str(mp4)]
        with log.open("wb") as errors:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
            try:
                for number in range(frames):
                    t = number / fps
                    scene = min(len(images) - 1, bisect.bisect_right(boundaries, t) - 1)
                    # A very short paragraph must not accelerate the camera.
                    p = (t - boundaries[scene]) / max(style["scene_mapping"]["minimum_hold_seconds"], boundaries[scene + 1] - boundaries[scene])
                    frame = artwork_frame(images[scene], p, scene, (width, height), style)
                    if scene and t < boundaries[scene] + style["transitions"]["crossfade_seconds"]:
                        prior_progress = min(1, (boundaries[scene] - boundaries[scene - 1]) / style["scene_mapping"]["minimum_hold_seconds"])
                        prior = artwork_frame(images[scene - 1], prior_progress, scene - 1, (width, height), style)
                        mix = min(1, (t - boundaries[scene]) / style["transitions"]["crossfade_seconds"])
                        frame = Image.blend(prior, frame, mix * mix * (3 - 2 * mix))
                    fade = min(1, t / style["transitions"]["opening_fade_seconds"], max(0, (duration - t) / style["transitions"]["closing_fade_seconds"]))
                    if fade < 1:
                        frame = Image.blend(Image.new("RGB", (width, height), _colour(style, "charcoal")), frame, fade)
                    ci = bisect.bisect_right(caption_starts, t) - 1
                    caption = captions[ci] if ci >= 0 and t <= captions[ci].end else None
                    draw_caption(frame, caption, font, style)
                    process.stdin.write(frame.tobytes())
                    if number % fps == 0:
                        progress(45 + round(50 * number / frames), "Composing your film")
                process.stdin.close()
                if process.wait(timeout=180) != 0:
                    raise RuntimeError("The video encoder failed. No incomplete output was kept.")
            except BaseException:
                process.kill()
                process.wait()
                raise
        probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(mp4)]))
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        sound = next(s for s in probe["streams"] if s["codec_type"] == "audio")
        if video["codec_name"] != "h264" or sound["codec_name"] != "aac" or abs(float(probe["format"]["duration"]) - duration) > .15:
            raise RuntimeError("Export verification failed. No incomplete output was kept.")
        mp4.replace(output)
    # Sidecars contain essay text: keep them with the private output, never in web/.
    write_srt(output.with_suffix(".srt"), captions)
    manifest = {
        "prototype": True, "voice": voice, "voice_approval": "provisional; requires human listening review",
        "style_version": style["style_version"], "style_sha256": style_hash(style), "style_snapshot": style,
        "effective_narration_speed": effective_speed,
        "synthesis_passes": 1, "duration_advisory": advisory,
        "draft_preview_override": preview_override, "loudness": loudness,
        "duration_seconds": round(duration, 3), "width": width, "height": height, "fps": fps,
        "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "source_image_sha256": [hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in image_paths],
        "captions": [asdict(c) for c in captions], "words": [asdict(w) for w in words],
        "scene_boundaries": boundaries, "caption_timing": "Kokoro token-duration predictions, not independent forced alignment",
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "narration_seconds": round(narration_elapsed, 2), "render_seconds": round(time.monotonic() - render_started, 2),
        "audio_rms": round(float(np.sqrt(np.mean(audio ** 2))), 5), "audio_peak": round(float(np.max(np.abs(audio))), 5),
        "source_words_preserved": True, "source_paintings_uncropped": True,
    }
    output.with_suffix(".json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    progress(100, "Your film is ready")
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Make a local, private narrated artwork film.")
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--images", type=Path, nargs="+", required=True, help="Four or five PNG/JPEG paths in display order")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--voice", choices=VOICES, default="bm_george")
    parser.add_argument("--resolution", choices=("1080p", "720p", "540p"), default="1080p")
    parser.add_argument("--demo-short", action="store_true", help="Compatibility flag: short scripts are now accepted without an override")
    args = parser.parse_args()
    sizes = {"1080p": (1920, 1080), "720p": (1280, 720), "540p": (960, 540)}
    try:
        result = render(args.script.read_text(encoding="utf-8"), args.images, args.output, width=sizes[args.resolution][0], height=sizes[args.resolution][1], voice=args.voice, allow_short=args.demo_short, progress=lambda n, message: print(f"{n:3d}% {message}", flush=True))
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, f"Could not convert: {exc}\n")
    print(json.dumps({k: result[k] for k in ("duration_seconds", "elapsed_seconds", "narration_seconds", "render_seconds")}, indent=2))


if __name__ == "__main__":
    main()
