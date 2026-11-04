import pytest

from socialops.meta_graph import (
    GraphError,
    MetaGraph,
    NetworkFailure,
    publish_fb_photo,
    publish_fb_video,
    publish_ig_image,
    publish_ig_reel,
)


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, fields, headers):
        self.calls.append({"method": method, "url": url, "fields": dict(fields), "headers": dict(headers)})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def graph(transport):
    return MetaGraph("TOKEN-XYZ", "v21.0", transport)


def test_token_only_in_header_never_url_or_fields():
    rec = Recorder((200, {"id": "1"}))
    graph(rec).get("123", {"fields": "status_code"})
    call = rec.calls[0]
    assert "TOKEN-XYZ" not in call["url"]
    assert "TOKEN-XYZ" not in str(call["fields"])
    assert call["headers"]["authorization"] == "Bearer TOKEN-XYZ"
    assert "TOKEN-XYZ" not in repr(graph(rec))


def test_url_uses_version():
    rec = Recorder((200, {"id": "1"}))
    graph(rec).post("999/feed", {"message": "hi"})
    assert rec.calls[0]["url"] == "https://graph.facebook.com/v21.0/999/feed"


def test_client_error_is_definitive():
    rec = Recorder((400, {"error": {"code": 100, "message": "bad param"}}))
    with pytest.raises(GraphError) as exc:
        graph(rec).post("1/feed", {})
    assert exc.value.ambiguous is False and exc.value.status == 400
    assert "bad param" in str(exc.value)


def test_post_5xx_is_ambiguous():
    rec = Recorder((503, {"error": {"message": "unavailable"}}))
    with pytest.raises(GraphError) as exc:
        graph(rec).post("1/feed", {})
    assert exc.value.ambiguous is True


def test_post_429_is_ambiguous():
    rec = Recorder((429, {"error": {"message": "rate"}}))
    with pytest.raises(GraphError) as exc:
        graph(rec).post("1/feed", {})
    assert exc.value.ambiguous is True


def test_get_5xx_is_not_ambiguous():
    rec = Recorder((500, {"error": {"message": "x"}}))
    with pytest.raises(GraphError) as exc:
        graph(rec).get("1")
    assert exc.value.ambiguous is False


def test_network_failure_on_post_is_ambiguous():
    rec = Recorder(NetworkFailure("timeout"))
    with pytest.raises(GraphError) as exc:
        graph(rec).post("1/feed", {})
    assert exc.value.ambiguous is True


def test_fb_photo_returns_post_id():
    rec = Recorder((200, {"id": "photo1", "post_id": "page_post1"}))
    assert publish_fb_photo(graph(rec), "PAGE", "https://m/x.png", "cap") == "page_post1"
    assert rec.calls[0]["fields"]["url"] == "https://m/x.png"
    assert rec.calls[0]["fields"]["published"] == "true"


def test_fb_video_uses_file_url():
    rec = Recorder((200, {"id": "v1"}))
    assert publish_fb_video(graph(rec), "PAGE", "https://m/v.mp4", "desc") == "v1"
    assert rec.calls[0]["url"].endswith("/PAGE/videos")
    assert rec.calls[0]["fields"]["file_url"] == "https://m/v.mp4"


def test_ig_image_waits_for_finished_then_publishes():
    rec = Recorder(
        (200, {"id": "C1"}),                    # create container
        (200, {"status_code": "IN_PROGRESS"}),  # poll 1
        (200, {"status_code": "FINISHED"}),     # poll 2
        (200, {"id": "M1"}),                    # media_publish
    )
    sleeps = []
    out = publish_ig_image(graph(rec), "IG", "https://m/i.png", "cap", sleep=sleeps.append)
    assert out == "M1"
    assert rec.calls[-1]["fields"] == {"creation_id": "C1"}
    assert sleeps == [3.0]


def test_ig_container_error_raises_definitive():
    rec = Recorder((200, {"id": "C1"}), (200, {"status_code": "ERROR"}))
    with pytest.raises(GraphError, match="ERROR") as exc:
        publish_ig_image(graph(rec), "IG", "https://m/i.png", "cap", sleep=lambda s: None)
    assert exc.value.ambiguous is False


def test_ig_container_never_ready_is_not_ambiguous():
    rec = Recorder(*([(200, {"id": "C1"})] + [(200, {"status_code": "IN_PROGRESS"})] * 3))
    with pytest.raises(GraphError, match="not ready") as exc:
        publish_ig_image(graph(rec), "IG", "https://m/i.png", "cap", sleep=lambda s: None, max_polls=3)
    assert exc.value.ambiguous is False


def test_ig_reel_sends_reels_media_type():
    rec = Recorder((200, {"id": "C2"}), (200, {"status_code": "FINISHED"}), (200, {"id": "R1"}))
    assert publish_ig_reel(graph(rec), "IG", "https://m/v.mp4", "cap", sleep=lambda s: None) == "R1"
    assert rec.calls[0]["fields"]["media_type"] == "REELS"
    assert rec.calls[0]["fields"]["video_url"] == "https://m/v.mp4"
