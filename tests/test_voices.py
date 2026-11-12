import pytest
from fastapi.testclient import TestClient
from byron_video.voices import list_voices, VoiceUnavailable
from byron_video import hosted_control as control


def voice(id="fixture_voice_1", name="Narrator", **extra):
    return {"voice_id": id, "name": name, "category": "generated", "is_owner": True, **extra}


def test_paginated_personal_designed_voices_minimal_metadata_and_duplicate_names():
    calls = []
    pages = [dict(voices=[voice(), voice("library_voice", is_owner=False)], has_more=True, next_page_token="a+b"),
             dict(voices=[voice("fixture_voice_2"), voice(), voice("premade_voice", category="premade")], has_more=False)]
    def get(url):
        calls.append(url)
        return 200, pages.pop(0)
    assert list_voices(get) == [{"id":"fixture_voice_1", "name":"Narrator"}, {"id":"fixture_voice_2", "name":"Narrator"}]
    assert len(calls) == 2
    assert all("/v2/voices?" in url and "voice_type=personal" in url and "category=generated" in url for url in calls)
    assert "next_page_token=a%2Bb" in calls[1]


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_provider_errors_never_expose_payload(status):
    with pytest.raises(VoiceUnavailable) as error:
        list_voices(lambda url: (status, {"secret": "never disclose"}))
    assert "never disclose" not in str(error.value)


def test_partial_and_repeated_pagination_fail_whole_refresh():
    with pytest.raises(VoiceUnavailable):
        list_voices(lambda url: (200, dict(voices=[voice()], has_more=True, next_page_token="repeat")))


def test_route_is_creator_scoped_and_private():
    calls = []
    def provider(owner):
        calls.append(owner.subject)
        return [{"id":"fixture_voice_1", "name":"Narrator", "account":"private"}]
    with TestClient(control.create_app(voice_provider=provider)) as client:
        assert client.get('/api/voices').status_code == 401
        assert calls == []
    with TestClient(control.create_app(principal_resolver=lambda request: control.Principal('fixture_owner'), voice_provider=provider,
                                      default_voice_provider=lambda owner:'fixture_voice_1')) as client:
        response = client.get('/api/voices')
        assert response.json() == {"voices":[{"id":"fixture_voice_1", "name":"Narrator"}], "default_voice_id":"fixture_voice_1"}
        assert response.headers['cache-control'] == 'no-store'
    with TestClient(control.create_app(principal_resolver=lambda request: control.Principal('fixture_owner'))) as client:
        assert client.get('/api/voices').status_code == 503
