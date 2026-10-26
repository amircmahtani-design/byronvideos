from io import BytesIO
from pathlib import Path
import os
import copy
import hashlib
import json
import threading
import time

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from byron_video import server


def png_bytes(colour="red"):
    output = BytesIO()
    Image.new("RGB", (64, 64), colour).save(output, format="PNG")
    return output.getvalue()


def uploads(count=4, content=None, content_type="image/png", filename="painting.png"):
    return [("images", (filename, content if content is not None else png_bytes(), content_type)) for _ in range(count)]


def complete_renderer(script, paths, output, progress=None, style_snapshot=None):
    progress(50, "Rendering.")
    output.write_bytes(b"synthetic-test-video")
    return {}


TEST_STYLE = {"style_version": "draft-test", "approval": {"status": "DRAFT"}}


def fixture_style_provider():
    snapshot = copy.deepcopy(TEST_STYLE)
    return snapshot, {"version": snapshot["style_version"], "approval_status": "DRAFT",
                      "hash": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()}


def make_client(tmp_path, renderer=complete_renderer, **kwargs):
    app = server.create_app(tmp_path / "jobs", renderer=renderer,
                            script_validator=lambda text: text, readiness=lambda: (True, "Ready"),
                            style_provider=kwargs.pop("style_provider", fixture_style_provider), **kwargs)
    return TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000))


def wait_for_status(client, job_id, expected):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/api/jobs/{job_id}").json()
        if result["status"] == expected:
            return result
        time.sleep(.01)
    pytest.fail(f"Job did not become {expected}: {result}")


def test_health_and_complete_flow_preserve_order_and_text(tmp_path):
    seen = {}
    def render(script, paths, output, progress, style_snapshot=None):
        seen["script"] = script
        seen["names"] = [p.name for p in paths]
        seen["colours"] = [Image.open(p).getpixel((0, 0)) for p in paths]
        complete_renderer(script, paths, output, progress)
    with make_client(tmp_path, render) as client:
        assert client.get("/api/health").json()["mode"] == "local"
        script = "  Exact words.\n\nA second paragraph — unchanged.  "
        files = [("images", ("../../private.png", png_bytes(c), "image/png")) for c in ["red", "green", "blue", "yellow"]]
        response = client.post("/api/jobs", data={"script": script}, files=files,
                               headers={"Origin": "http://127.0.0.1:8765"})
        assert response.status_code == 202
        job_id = response.json()["id"]
        assert len(job_id) == 32
        result = wait_for_status(client, job_id, "complete")
        download = client.get(result["download_url"])
        assert download.content == b"synthetic-test-video"
        assert download.headers["cache-control"] == "no-store"
        assert seen["script"] == script
        assert seen["names"] == [f"image-{i:02}.png" for i in range(1, 5)]
        assert seen["colours"] == [(255, 0, 0), (0, 128, 0), (0, 0, 255), (255, 255, 0)]
        assert client.get(f"/{job_id}/image-01.png").status_code == 404
        assert not (tmp_path / "private.png").exists()


def test_long_full_paste_and_five_original_jpegs_complete_without_mutation(tmp_path):
    from byron_video.engine import validate_script
    exact = 'Synthetic title\nBy Example Writer\nwww.example.com\n\n' + ('Every exact source word remains. ' * 180)
    assert len(exact) > 4000 and len(exact.split()) > 450
    originals = []
    for colour in ['red', 'green', 'blue', 'yellow', 'purple']:
        data = BytesIO()
        Image.new('RGB', (80, 120), colour).save(data, format='JPEG')
        originals.append(data.getvalue())
    seen = []
    def renderer(script, paths, output, progress=None, style_snapshot=None):
        seen.append((script, [path.read_bytes() for path in paths]))
        complete_renderer(script, paths, output, progress)
    app = server.create_app(tmp_path / 'jobs', renderer=renderer, script_validator=validate_script,
                            readiness=lambda: (True, 'Ready'), style_provider=fixture_style_provider)
    with TestClient(app, base_url='http://127.0.0.1:8765', client=('127.0.0.1', 50000)) as client:
        files = [('images', (f'{i}.jpg', data, 'image/jpeg')) for i, data in enumerate(originals)]
        response = client.post('/api/jobs', data={'script': exact}, files=files)
        assert response.status_code == 202
        result = wait_for_status(client, response.json()['id'], 'complete')
        assert client.get(result['download_url']).status_code == 200
    assert seen == [(exact, originals)]


@pytest.mark.parametrize('seconds,advisory', [(89,'Outside the usual range.'),(90,None),(180,None),(181,'Outside the usual range.')])
def test_duration_advisory_does_not_block_completed_download(tmp_path, seconds, advisory):
    calls = []
    def render(script, paths, output, progress=None, style_snapshot=None):
        calls.append(script)
        complete_renderer(script, paths, output, progress)
        return {'duration_seconds':seconds,'duration_advisory':advisory,'synthesis_passes':1}
    with make_client(tmp_path,render) as client:
        script='  Keep these exact words.  '
        response=client.post('/api/jobs',data={'script':script},files=uploads())
        assert response.status_code==202
        result=wait_for_status(client,response.json()['id'],'complete')
        assert result['duration_seconds']==seconds
        assert result['duration_advisory']==advisory
        assert client.get(result['download_url']).status_code==200
        assert calls==[script]


@pytest.mark.parametrize("count", [1, 3, 6])
def test_image_count(tmp_path, count):
    with make_client(tmp_path) as client:
        assert client.post("/api/jobs", data={"script": "Words"}, files=uploads(count)).status_code == 422
        assert not list((tmp_path / "jobs").iterdir())


@pytest.mark.parametrize("content,kind", [(b"not image", "image/png"), (png_bytes(), "image/jpeg"), (png_bytes(), "image/svg+xml")])
def test_actual_image_type_and_mime(tmp_path, content, kind):
    with make_client(tmp_path) as client:
        response = client.post("/api/jobs", data={"script": "Words"}, files=uploads(content=content, content_type=kind))
        assert response.status_code == 422
        assert not list((tmp_path / "jobs").iterdir())
        assert not client.app.state.manager.busy


def test_invalid_script(tmp_path):
    with make_client(tmp_path) as client:
        for text in [" ", "x" * 16001]:
            assert client.post("/api/jobs", data={"script": text}, files=uploads()).status_code == 422


def test_cross_origin_and_rebinding_rejected(tmp_path):
    with make_client(tmp_path) as client:
        for origin in ["https://attacker.invalid", "http://localhost:8765", "null", "http://127.0.0.1:9999"]:
            assert client.post("/api/jobs", data={"script": "Words"}, files=uploads(), headers={"Origin": origin}).status_code == 403
        assert client.get("/api/health", headers={"Host": "attacker.invalid"}).status_code == 403
        assert client.post("/api/jobs", data={"script": "Words"}, files=uploads(), headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_remote_peer_rejected(tmp_path):
    app = server.create_app(tmp_path / "jobs", renderer=complete_renderer)
    with TestClient(app, base_url="http://127.0.0.1", client=("192.168.1.22", 123)) as client:
        assert client.get("/api/health").status_code == 403


def test_busy_prevents_duplicate_jobs_and_not_ready_download(tmp_path):
    started, finish = threading.Event(), threading.Event()
    def waiting(script, paths, output, progress, style_snapshot=None):
        started.set()
        assert finish.wait(5)
        output.write_bytes(b"film")
    with make_client(tmp_path, waiting) as client:
        first = client.post("/api/jobs", data={"script": "Words"}, files=uploads())
        assert first.status_code == 202
        assert started.wait(2)
        job_id = first.json()["id"]
        try:
            assert client.get(f"/api/jobs/{job_id}/video").status_code == 409
            assert client.post("/api/jobs", data={"script": "Words"}, files=uploads()).status_code == 429
            assert len(list((tmp_path / "jobs").iterdir())) == 1
        finally:
            finish.set()
        wait_for_status(client, job_id, "complete")


def test_failed_renderer_releases_queue_and_does_not_leak_error(tmp_path):
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE SCRIPT SHOULD NEVER APPEAR")
    with make_client(tmp_path, fail) as client:
        first = client.post("/api/jobs", data={"script": "Words"}, files=uploads())
        result = wait_for_status(client, first.json()["id"], "failed")
        assert "PRIVATE" not in str(result)
        assert "download_url" not in result
        second = client.post("/api/jobs", data={"script": "Words"}, files=uploads())
        assert second.status_code == 202


def test_limits_and_unknown_job(tmp_path, monkeypatch):
    with make_client(tmp_path) as client:
        assert client.get("/api/jobs/not-an-id").status_code == 404
        assert client.get("/api/jobs/" + "a" * 32).status_code == 404
        monkeypatch.setattr(server, "MAX_IMAGE_BYTES", 10)
        assert client.post("/api/jobs", data={"script": "Words"}, files=uploads()).status_code == 413
        assert not client.app.state.manager.busy
        assert not list((tmp_path / "jobs").iterdir())
        assert client.post("/api/jobs", headers={"Content-Length": str(server.MAX_REQUEST_BYTES + 1)}).status_code == 413


def test_ttl_cleanup_only_marked_inactive_job_directories(tmp_path):
    root = tmp_path / "jobs"
    root.mkdir()
    old = root / ("a" * 32)
    old.mkdir()
    marker = old / ".byron-job"
    marker.touch()
    os.utime(marker, (1, 1))
    unmarked = root / ("b" * 32)
    unmarked.mkdir()
    other = root / "unrelated"
    other.mkdir()
    linked = root / ("c" * 32)
    linked.symlink_to(other, target_is_directory=True)
    manager = server.JobManager(root, complete_renderer)
    try:
        assert not old.exists()
        assert unmarked.exists() and other.exists() and linked.is_symlink()
    finally:
        manager.close()


def test_private_root_cannot_be_public_web():
    with pytest.raises(ValueError, match="public web"):
        server.create_app(server.PROJECT_ROOT / "web" / "uploads")


def test_chunked_body_limit_is_enforced_before_disk_write(tmp_path, monkeypatch):
    with make_client(tmp_path) as client:
        monkeypatch.setattr(server, "MAX_REQUEST_BYTES", 32)
        def chunks():
            yield b"--boundary\r\nContent-Disposition: form-data; name=script\r\n\r\n"
            yield b"x" * 128
            yield b"\r\n--boundary--\r\n"
        result = client.post("/api/jobs", content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=boundary"})
        assert result.status_code == 413
        assert not list((tmp_path / "jobs").iterdir())


def test_animated_png_rejected(tmp_path):
    output = BytesIO()
    first = Image.new("RGB", (64, 64), "red")
    first.save(output, format="PNG", save_all=True, append_images=[Image.new("RGB", (64, 64), "blue")], duration=100)
    with make_client(tmp_path) as client:
        assert client.post("/api/jobs", data={"script": "Words"}, files=uploads(content=output.getvalue())).status_code == 422


def test_readiness_and_script_validation_failure(tmp_path):
    def invalid(script):
        raise ValueError("This script exceeds the technical input limit.")
    app = server.create_app(tmp_path / "jobs", script_validator=invalid, readiness=lambda: (True, "ready"), style_provider=fixture_style_provider)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        result = client.post("/api/jobs", data={"script": "Words"}, files=uploads())
        assert result.status_code == 422
        assert result.json()["detail"] == "This script exceeds the technical input limit."
    app = server.create_app(tmp_path / "jobs", readiness=lambda: (False, "missing"), style_provider=fixture_style_provider)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/health").json()["ready"] is False
        assert client.post("/api/jobs", data={"script": "Words"}, files=uploads()).status_code == 503


def test_style_is_frozen_per_job_and_cannot_be_overridden_by_client(tmp_path):
    source = copy.deepcopy(TEST_STYLE)
    seen = {}
    def provider():
        return source, {"version": source["style_version"], "approval_status": "DRAFT", "hash": "a" * 64}
    def renderer(script, paths, output, progress, style_snapshot):
        seen["style"] = style_snapshot
        complete_renderer(script, paths, output, progress)
    with make_client(tmp_path, renderer, style_provider=provider) as client:
        assert client.get("/api/health").json()["style"]["approval_status"] == "DRAFT"
        response = client.post("/api/jobs", data={"script": "Words", "style": '{"voice":"different"}'}, files=uploads())
        assert response.status_code == 202
        job_id = response.json()["id"]
        source["style_version"] = "changed-after-submit"
        result = wait_for_status(client, job_id, "complete")
        assert result["style"]["version"] == "draft-test"
        assert seen["style"]["style_version"] == "draft-test"
        assert "voice" not in seen["style"]
        saved = json.loads((tmp_path / "jobs" / job_id / "style.json").read_text())
        assert saved["style_version"] == "draft-test"
        assert json.loads((tmp_path / "jobs" / job_id / "job.json").read_text())["style"] == result["style"]


def test_invalid_style_fails_closed(tmp_path):
    def broken_style():
        raise ValueError("Style hash mismatch")
    with make_client(tmp_path, style_provider=broken_style) as client:
        assert client.get("/api/health").json()["ready"] is False
        response = client.post("/api/jobs", data={"script": "Words"}, files=uploads())
        assert response.status_code == 503
        assert not list((tmp_path / "jobs").iterdir())


def test_friendly_renderer_validation_error_reaches_operator(tmp_path):
    def fail(*args, **kwargs):
        raise ValueError("The narration exceeds the technical processing limit. Nothing was retimed.")
    with make_client(tmp_path, fail) as client:
        response = client.post("/api/jobs", data={"script": "Words"}, files=uploads())
        result = wait_for_status(client, response.json()["id"], "failed")
        assert "technical processing limit" in result["message"]
