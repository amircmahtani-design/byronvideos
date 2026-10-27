"""Offline boundary tests. Fake media verifiers do not establish MP4 quality."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import hashlib
import io
import json
import threading

from fastapi.testclient import TestClient
import pytest

from byron_video import hosted_control as control

OWNER = "creator_alpha"
OTHER = "creator_bravo"
KEY = "request_000000001"
SCRIPT = "  The [painting](https://example.com/painting) waits.\n\nIts colours remain.  "


def body(script=SCRIPT):
    return {"script": script, "images": [{"id": f"painting_{i:03}", "position": i} for i in range(1, 5)]}


def headers(owner=OWNER, key=KEY):
    return {"X-Fixture-Owner": owner, "Idempotency-Key": key}


def principal(request):
    # Test fixture only. Production MUST NOT treat this header as authentication.
    value = request.headers.get("X-Fixture-Owner")
    return control.Principal(value) if value else None


def upload(owner, image_id):
    return control.VerifiedImage(image_id, owner.subject, "a" * 64, 1234, 640, 480,
                                 "image/png", "object_" + image_id, "42", True)


class Dispatcher:
    synthetic_fixture = True
    def __init__(self, store, error=None):
        self.store, self.error, self.calls = store, error, []
        self.lock = threading.Lock()

    def __call__(self, handoff):
        with self.lock:
            self.calls.append(handoff)
        durable = self.store.get(handoff["owner_id"], handoff["job_id"])
        assert durable.state == "HOLD"  # Checkpoint BEFORE the simulated call.
        assert durable.snapshot_sha256 == handoff["snapshot_sha256"]
        assert durable.snapshot == handoff["frozen_job"]
        if self.error:
            raise self.error
        return control.DispatchReceipt(handoff["job_id"], handoff["snapshot_sha256"], "execution_" + handoff["job_id"])


@pytest.fixture
def store(tmp_path):
    value = control.SQLiteFixtureStore(tmp_path / "jobs.sqlite3")
    yield value
    value.close()


def plan():
    return {"schema_version": 1, "status": "DRAFT_NOT_ACTIVATED", "visual_style_sha256": "b" * 64,
            "motion_policy_sha256": "c" * 64, "output": {"width": 1080, "height": 1920, "fps": 24},
            "narration_profile": {"schema_version": 1, "version": "synthetic-fixture-1", "status": "DRAFT",
                "provider": "elevenlabs", "voice_id": "SYNTHETIC_TEST_VOICE", "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": .5, "similarity_boost": .75, "style": 0,
                                   "use_speaker_boost": True, "speed": .93},
                "output_format": "mp3_44100_128", "seed": 1, "apply_text_normalization": "off"}}


def make_app(store, dispatcher=None, **kwargs):
    return control.create_app(principal_resolver=kwargs.pop("principal_resolver", principal), store=store,
                              dispatcher=dispatcher or Dispatcher(store), upload_resolver=kwargs.pop("upload_resolver", upload),
                              plan_provider=kwargs.pop("plan_provider", lambda owner: plan()),
                              **kwargs)


def submit(client, payload=None, owner=OWNER, key=KEY):
    return client.post("/api/jobs", json=payload or body(), headers=headers(owner, key))


def fixture_proof(job, data):
    # Deliberately injected fake verifier output: these are contract tests, NOT
    # media decode evidence and never a delivered/generated MP4 fixture.
    return control.VerifiedArtifact("artifact_000001", job.id, job.owner_id, job.snapshot_sha256,
                                    job.receipt.execution_id, hashlib.sha256(data).hexdigest(), len(data),
                                    31.0, 1080, 1920, "h264", "aac", 24, tuple(job.snapshot["input_order"]),
                                    job.snapshot["narrated_text_sha256"], job.snapshot["narrated_text_sha256"],
                                    True, "ffmpeg-full-decode-v1")


def test_default_factory_denies_and_never_claims_readiness():
    with TestClient(control.create_app()) as client:
        assert client.get("/api/health").json() == {
            "ready": False, "mode": "hosted_setup", "live_release": False,
            "message": "Inactive control boundary. Authentication, durable hosting and live dispatch are not activated."}
        assert submit(client).status_code == 401
        assert client.get("/api/jobs/" + "a" * 32).status_code == 401
        assert client.get("/api/jobs/" + "a" * 32 + "/video").status_code == 401
        assert client.get("/docs").status_code == 404


def test_missing_auth_and_unconfigured_dependencies_deny(store):
    with TestClient(make_app(store)) as client:
        assert client.post("/api/jobs", json=body(), headers={"Idempotency-Key": KEY}).status_code == 401
    with TestClient(control.create_app(principal_resolver=principal)) as client:
        assert submit(client).status_code == 503
    live = Dispatcher(store)
    live.synthetic_fixture = False
    with TestClient(make_app(store, live)) as client:
        assert submit(client).status_code == 503
        assert live.calls == []


def test_exact_text_numbered_order_and_immutable_snapshot(store):
    fixed_plan = plan()
    dispatcher = Dispatcher(store)
    with TestClient(make_app(store, dispatcher, plan_provider=lambda owner: fixed_plan)) as client:
        result = submit(client)
        assert result.status_code == 202
        job_id = result.json()["id"]
        job = store.get(OWNER, job_id)
        assert job.state == "queued"
        assert job.snapshot["raw_script"] == SCRIPT
        assert job.snapshot["narrated_text"] == "  The painting waits.\n\nIts colours remain.  "
        assert job.snapshot["input_order"] == [f"painting_{i:03}" for i in range(1, 5)]
        assert [x["position"] for x in job.snapshot["images"]] == [1, 2, 3, 4]
        assert job.snapshot["images"][0]["storage_version"] == "42"
        fixed_plan["visual_style_sha256"] = "f" * 64
        dispatcher.calls[0]["frozen_job"]["raw_script"] = "changed"
        assert store.get(OWNER, job_id).snapshot["raw_script"] == SCRIPT
        assert store.get(OWNER, job_id).snapshot["processing_plan"]["visual_style_sha256"] == "b" * 64
        status = client.get("/api/jobs/" + job_id, headers=headers()).json()
        assert status["status"] == "queued" and "download_url" not in status
        assert SCRIPT not in json.dumps(status) and "painting_001" not in json.dumps(status)
        assert "execution_" not in json.dumps(status)
        assert client.get("/api/health").json()["ready"] is False


def test_duplicate_and_conflicting_submissions(store):
    dispatcher = Dispatcher(store)
    with TestClient(make_app(store, dispatcher)) as client:
        first = submit(client)
        second = submit(client)
        assert first.status_code == second.status_code == 202
        assert first.json()["id"] == second.json()["id"]
        assert second.json()["reused"] is True
        assert len(dispatcher.calls) == 1
        assert submit(client, body("Different script.")).status_code == 409
        assert len(dispatcher.calls) == 1


def test_reservation_survives_restart_and_never_redispatches(tmp_path):
    path = tmp_path / "restart.sqlite3"
    first_store = control.SQLiteFixtureStore(path)
    dispatcher = Dispatcher(first_store, error=TimeoutError("unknown external outcome"))
    with TestClient(make_app(first_store, dispatcher)) as client:
        response = submit(client)
        assert response.json()["status"] == "HOLD"
        job_id = response.json()["id"]
    first_store.close()
    reopened = control.SQLiteFixtureStore(path)
    second = Dispatcher(reopened)
    try:
        with TestClient(make_app(reopened, second, upload_resolver=lambda *_: pytest.fail("No new upload resolution on duplicate"))) as client:
            replay = submit(client)
            assert replay.status_code == 202 and replay.json()["id"] == job_id
            assert replay.json()["status"] == "HOLD" and replay.json()["reused"] is True
            assert second.calls == []
            status = client.get("/api/jobs/" + job_id, headers=headers()).json()
            assert "not be retried automatically" in status["message"]
            assert client.get(f"/api/jobs/{job_id}/video", headers=headers()).status_code == 409
    finally:
        reopened.close()


def test_process_crash_after_reservation_before_dispatch_stays_hold(store):
    checkpoint, created = store.reserve(OWNER, KEY, control.digest(body()), {"snapshot": "already frozen"})
    assert created
    dispatcher = Dispatcher(store)
    with TestClient(make_app(store, dispatcher)) as client:
        result = submit(client)
        assert result.json()["id"] == checkpoint.id and result.json()["status"] == "HOLD"
        assert not dispatcher.calls


def test_concurrent_same_submission_dispatches_once(store):
    dispatcher = Dispatcher(store)
    app = make_app(store, dispatcher)
    def send(_):
        with TestClient(app) as client:
            return submit(client).json()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(send, range(4)))
    assert len({r["id"] for r in results}) == 1
    assert len(dispatcher.calls) == 1


def test_cross_owner_denial_and_separate_idempotency_namespace(store):
    dispatcher = Dispatcher(store)
    with TestClient(make_app(store, dispatcher)) as client:
        job_id = submit(client).json()["id"]
        assert client.get(f"/api/jobs/{job_id}", headers=headers(OTHER)).status_code == 404
        assert client.get(f"/api/jobs/{job_id}/video", headers=headers(OTHER)).status_code == 404
        other = submit(client, owner=OTHER).json()["id"]
        assert other != job_id and len(dispatcher.calls) == 2


def test_wrong_owner_upload_and_unverified_metadata_rejected(store):
    bad_images = [replace(upload(control.Principal(OWNER), "painting_001"), owner_id=OTHER),
                  replace(upload(control.Principal(OWNER), "painting_001"), original_bytes_verified=False),
                  replace(upload(control.Principal(OWNER), "painting_001"), storage_id="../../private"),
                  replace(upload(control.Principal(OWNER), "painting_001"), storage_version="../42"),
                  replace(upload(control.Principal(OWNER), "painting_001"), sha256="invalid"),
                  replace(upload(control.Principal(OWNER), "painting_001"), byte_count=MAX_BYTES)]
    for image in bad_images:
        dispatcher = Dispatcher(store)
        with TestClient(make_app(store, dispatcher, upload_resolver=lambda *_: image)) as client:
            assert submit(client).status_code == 404
            assert dispatcher.calls == []


MAX_BYTES = 20 * 1024 * 1024 + 1


@pytest.mark.parametrize("alter", [
    lambda b: b.update(status="complete"),
    lambda b: b.update(style={"voice": "client-choice"}),
    lambda b: b["images"][0].update(path="/etc/passwd"),
    lambda b: b["images"][0].update(sha256="a" * 64),
    lambda b: b["images"][0].update(id="../../private"),
    lambda b: b["images"][0].update(id="https://attacker.invalid/image"),
    lambda b: b["images"][0].update(position=True),
    lambda b: b["images"][0].update(position=2),
    lambda b: b["images"][1].update(id="painting_001"),
    lambda b: b["images"].pop(),
])
def test_malicious_or_ambiguous_client_metadata_rejected(store, alter):
    payload = body()
    alter(payload)
    dispatcher = Dispatcher(store)
    with TestClient(make_app(store, dispatcher)) as client:
        assert submit(client, payload).status_code == 422
        assert dispatcher.calls == []


def test_script_limits_reuse_current_engine_and_do_not_shorten(store):
    dispatcher = Dispatcher(store)
    with TestClient(make_app(store, dispatcher)) as client:
        long_text = " ".join(["fourteenletters"] * 1000)
        assert len(long_text) < control.MAX_SCRIPT_CHARS
        response = submit(client, body(long_text))
        assert response.status_code == 202
        assert store.get(OWNER, response.json()["id"]).snapshot["raw_script"] == long_text
        for text in [" ", "x" * (control.MAX_SCRIPT_CHARS + 1), "word " * 2001, "bad\x00text"]:
            assert submit(client, body(text), key="another_request_001").status_code == 422


def test_untrusted_dispatch_receipts_never_complete(store):
    bad_receipts = [{"status": "complete", "download_url": "https://attacker.invalid/video"},
                    control.DispatchReceipt("a" * 32, "b" * 64, "execution_wrong"), None]
    for index, receipt in enumerate(bad_receipts):
        class BadDispatcher:
            synthetic_fixture = True
            def __call__(self, handoff):
                return receipt
        with TestClient(make_app(store, BadDispatcher())) as client:
            result = submit(client, key=f"bad_request_0000{index}")
            assert result.status_code == 202 and result.json()["status"] == "HOLD"
            job_id = result.json()["id"]
            assert client.get(f"/api/jobs/{job_id}/video", headers=headers()).status_code == 409
            assert client.post(f"/api/jobs/{job_id}/complete", json={"complete": True}, headers=headers()).status_code == 404


def test_credential_bearing_plan_and_invalid_idempotency_key_rejected(store):
    with TestClient(make_app(store, plan_provider=lambda owner: {"api_key": "NO_REAL_SECRET_TEST"})) as client:
        assert submit(client).status_code == 503
    with TestClient(make_app(store)) as client:
        for key in ["short", "../../../../private_key", "https://attacker.invalid/"]:
            assert submit(client, key=key).status_code == 422


def test_no_complete_without_trusted_media_verification(store):
    with TestClient(make_app(store)) as client:
        job_id = submit(client).json()["id"]
        job = store.get(OWNER, job_id)
        proof = fixture_proof(job, b"EXPLICIT MOCK BYTES - NOT A REAL MP4")
        for bad in [replace(proof, full_decode_verified=False), replace(proof, owner_id=OTHER),
                    replace(proof, execution_id="execution_unrelated"), replace(proof, artifact_id="../../etc/passwd"),
                    replace(proof, snapshot_sha256="f" * 64), replace(proof, duration_seconds=float("nan")),
                    replace(proof, width=1920, height=1080), replace(proof, fps=30),
                    replace(proof, caption_text_sha256="f" * 64), replace(proof, narrated_text_sha256="f" * 64),
                    replace(proof, source_order=tuple(reversed(proof.source_order)))]:
            with pytest.raises(ValueError):
                asyncio.run(control.reconcile_verified_completion(store, control.Principal(OWNER), job_id,
                                                                   "artifact_000001", lambda *_: bad))
        assert store.get(OWNER, job_id).state == "queued"
        assert client.get(f"/api/jobs/{job_id}/video", headers=headers()).status_code == 409


def test_verified_completion_and_download_boundary_with_explicit_mock_verifier(store):
    data = b"EXPLICIT MOCK BYTES - NOT A REAL MP4"
    with TestClient(make_app(store)) as client:
        job_id = submit(client).json()["id"]
    job = store.get(OWNER, job_id)
    proof = fixture_proof(job, data)
    verifier_calls, reader_calls = [], []
    def verifier(owner, given_job, artifact_id):
        verifier_calls.append((owner.subject, given_job.id, artifact_id))
        return proof
    def reader(owner, given_job, given_proof):
        reader_calls.append(owner.subject)
        return io.BytesIO(data)
    asyncio.run(control.reconcile_verified_completion(store, control.Principal(OWNER), job_id, proof.artifact_id, verifier))
    with TestClient(make_app(store, artifact_verifier=verifier, artifact_reader=reader)) as client:
        status = client.get(f"/api/jobs/{job_id}", headers=headers()).json()
        assert status["status"] == "complete" and status["progress"] == 100
        download = client.get(status["download_url"], headers=headers())
        assert download.status_code == 200 and download.content == data
        assert download.headers["cache-control"] == "no-store"
        assert len(verifier_calls) == 2 and reader_calls == [OWNER]
        assert client.get(status["download_url"], headers=headers(OTHER)).status_code == 404
        assert len(verifier_calls) == 2 and reader_calls == [OWNER]


def test_changed_download_bytes_or_proof_never_released(store):
    data = b"EXPLICIT MOCK BYTES - NOT A REAL MP4"
    with TestClient(make_app(store)) as client:
        job_id = submit(client).json()["id"]
    job = store.get(OWNER, job_id)
    proof = fixture_proof(job, data)
    asyncio.run(control.reconcile_verified_completion(store, control.Principal(OWNER), job_id, proof.artifact_id, lambda *_: proof))
    with TestClient(make_app(store, artifact_verifier=lambda *_: proof, artifact_reader=lambda *_: io.BytesIO(b"changed"))) as client:
        result = client.get(f"/api/jobs/{job_id}/video", headers=headers())
        assert result.status_code == 503 and b"changed" not in result.content
    with TestClient(make_app(store, artifact_verifier=lambda *_: replace(proof, sha256="f" * 64),
                             artifact_reader=lambda *_: pytest.fail("Must not read after changed proof"))) as client:
        assert client.get(f"/api/jobs/{job_id}/video", headers=headers()).status_code == 503


def test_corrupt_complete_row_and_snapshot_fail_closed(store):
    with TestClient(make_app(store)) as client:
        job_id = submit(client).json()["id"]
        store.db.execute("UPDATE control_job SET state='complete' WHERE id=?", (job_id,))
        assert client.get(f"/api/jobs/{job_id}", headers=headers()).status_code == 503
        assert client.get(f"/api/jobs/{job_id}/video", headers=headers()).status_code == 409
        store.db.execute("UPDATE control_job SET snapshot_json='{}' WHERE id=?", (job_id,))
        assert client.get(f"/api/jobs/{job_id}", headers=headers()).status_code == 503


def test_control_body_limit_and_unknown_routes(store):
    with TestClient(make_app(store)) as client:
        result = client.post("/api/jobs", content=b"x" * (control.MAX_CONTROL_BODY_BYTES + 1),
                             headers={**headers(), "Content-Type": "application/json"})
        assert result.status_code == 413
        assert client.get("/api/jobs/not-a-job", headers=headers()).status_code == 404
        assert client.post("/api/upload-grants", headers=headers()).status_code == 404


@pytest.mark.parametrize("alter", [
    lambda p: p.clear(),
    lambda p: p.pop("narration_profile"),
    lambda p: p["narration_profile"].update(voice_id=None),
    lambda p: p["narration_profile"]["voice_settings"].pop("speed"),
    lambda p: p.update(visual_style_sha256="missing"),
    lambda p: p.update(motion_policy_sha256="missing"),
    lambda p: p["output"].update(width=1920, height=1080),
    lambda p: p["output"].update(fps=True),
    lambda p: p.update(status="APPROVED"),
])
def test_processing_plan_cannot_be_empty_or_unpinned(store, alter):
    fixed = plan()
    alter(fixed)
    dispatcher = Dispatcher(store)
    with TestClient(make_app(store, dispatcher, plan_provider=lambda owner: fixed)) as client:
        assert submit(client).status_code == 503
        assert dispatcher.calls == []


def test_exact_narration_requests_and_all_profile_settings_frozen(store):
    with TestClient(make_app(store)) as client:
        result = submit(client, body("abcdefg " * 2000))
        assert result.status_code == 202
        snapshot = store.get(OWNER, result.json()["id"]).snapshot
        narration = snapshot["narration_contract"]
        assert "".join(r["body"]["text"] for r in narration["narration_requests"]) == snapshot["narrated_text"]
        assert len(narration["narration_requests"]) == 2
        assert narration["narration_profile"] == plan()["narration_profile"]
        assert narration["visual_style_sha256"] == plan()["visual_style_sha256"]


def test_receipt_persistence_failure_stays_hold_and_never_retries(store):
    class FailedReceiptStore:
        def __getattr__(self, name):
            return getattr(store, name)
        def record_dispatch(self, *args):
            raise OSError("Fixture failed before receipt commit")
    wrapper = FailedReceiptStore()
    dispatcher = Dispatcher(store)
    with TestClient(make_app(wrapper, dispatcher)) as client:
        first = submit(client)
        assert first.json()["status"] == "HOLD"
        second = submit(client)
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["status"] == "HOLD"
        assert len(dispatcher.calls) == 1


def test_two_independent_store_connections_share_atomic_reservation(tmp_path):
    path = tmp_path / "two-process-model.sqlite3"
    stores = [control.SQLiteFixtureStore(path), control.SQLiteFixtureStore(path)]
    try:
        def reserve(index):
            return stores[index].reserve(OWNER, KEY, control.digest(body()), {"frozen": "fixture"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        assert sum(created for _, created in results) == 1
        assert results[0][0].id == results[1][0].id
    finally:
        for value in stores:
            value.close()


def test_missing_download_adapter_and_corrupt_proof_fail_closed(store):
    with TestClient(make_app(store)) as client:
        job_id = submit(client).json()["id"]
        proof = fixture_proof(store.get(OWNER, job_id), b"EXPLICIT MOCK MEDIA")
        asyncio.run(control.reconcile_verified_completion(store, control.Principal(OWNER), job_id,
                                                           proof.artifact_id, lambda *_: proof))
        status = client.get(f"/api/jobs/{job_id}", headers=headers()).json()
        assert status["status"] == "complete" and "download_url" not in status
        assert client.get(f"/api/jobs/{job_id}/video", headers=headers()).status_code == 503
        store.db.execute("UPDATE control_job SET artifact_json='not-json' WHERE id=?", (job_id,))
        assert client.get(f"/api/jobs/{job_id}", headers=headers()).status_code == 503
        assert client.get(f"/api/jobs/{job_id}/video", headers=headers()).status_code == 503


def test_one_backend_execution_cannot_belong_to_two_jobs_or_owners(store):
    class ReusedExecution:
        synthetic_fixture = True
        def __call__(self, handoff):
            return control.DispatchReceipt(handoff["job_id"], handoff["snapshot_sha256"], "execution_shared_fixture")
    with TestClient(make_app(store, ReusedExecution())) as client:
        first = submit(client)
        assert first.json()["status"] == "queued"
        second = submit(client, owner=OTHER)
        assert second.status_code == 202 and second.json()["status"] == "HOLD"
        second_id = second.json()["id"]
        assert store.get(OTHER, second_id).receipt is None
        replay = submit(client, owner=OTHER)
        assert replay.json()["reused"] is True and replay.json()["status"] == "HOLD"


def test_fixture_store_rejects_parent_symlink(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        control.SQLiteFixtureStore(alias / "jobs.sqlite3")
    assert not (actual / "jobs.sqlite3").exists()


def test_artifact_reader_chunk_bound_and_invalid_resource_closed(store):
    with TestClient(make_app(store)) as client:
        job_id = submit(client).json()["id"]
    job = store.get(OWNER, job_id)
    huge = b"x" * (1024 * 1024 + 1)
    proof = fixture_proof(job, huge)
    class Oversized:
        closed = False
        def read(self, size):
            assert size == 1024 * 1024
            return huge
        def close(self):
            self.closed = True
    source = Oversized()
    with pytest.raises(ValueError, match="chunk bound"):
        control._verified_spool(lambda *_: source, control.Principal(OWNER), job, proof)
    assert source.closed
    class Invalid:
        closed = False
        def close(self):
            self.closed = True
    source = Invalid()
    with pytest.raises(ValueError, match="binary stream"):
        control._verified_spool(lambda *_: source, control.Principal(OWNER), job, proof)
    assert source.closed


def test_lone_unicode_surrogate_is_rejected_before_reservation(store):
    dispatcher = Dispatcher(store)
    with TestClient(make_app(store, dispatcher)) as client:
        result = client.post("/api/jobs", content=json.dumps(body("\ud800 Exact test.")),
                             headers={**headers(), "Content-Type": "application/json"})
        assert result.status_code == 422 and "invalid Unicode" in result.json()["detail"]
        assert dispatcher.calls == []
        assert store.db.execute("SELECT count(*) FROM control_job").fetchone()[0] == 0


def test_validation_errors_do_not_echo_private_input(store):
    payload = body()
    payload["private_extra"] = "THIS PRIVATE VALUE MUST NOT BE ECHOED"
    with TestClient(make_app(store)) as client:
        result = submit(client, payload)
        assert result.status_code == 422
        assert "THIS PRIVATE" not in result.text
        assert SCRIPT not in result.text
