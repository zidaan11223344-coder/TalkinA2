import bot
import bot_sections.music as music_section


class _SynchronousThread:
    def __init__(self, *, target, **_kwargs):
        self._target = target

    def start(self):
        self._target()


def _test_bot(monkeypatch, *, direct_result):
    instance = bot.TalkinBot.__new__(bot.TalkinBot)
    events = {"texts": [], "media": [], "downloads": 0}
    instance._music_live_source = lambda query: direct_result
    instance._music_download_uncached = lambda *_args, **_kwargs: events.__setitem__("downloads", events["downloads"] + 1)
    instance.send_room_text = lambda room, text: events["texts"].append((room, text))
    instance.send_room_media = lambda room, url, kind, duration=0: events["media"].append((room, url, kind, duration)) or True
    instance.log = lambda *_args: None
    monkeypatch.setattr(bot.threading, "Thread", _SynchronousThread)
    return instance, events


def test_aax_sends_direct_youtube_audio_without_download(monkeypatch):
    direct_url = "https://rr.example.googlevideo.com/audio?signature=private-test-token"
    instance, events = _test_bot(
        monkeypatch,
        direct_result={
            "source": "Youtube",
            "url": direct_url,
            "title": "Test song",
            "duration": 123,
        },
    )

    assert instance.handle_youtube_direct_test_command("room-1", "Test song", "listener") is True
    assert events["media"] == [("room-1", direct_url, "audio", 123)]
    assert events["downloads"] == 0
    assert any("لم يُنزّل البوت ملفًا" in text for _, text in events["texts"])
    assert all(direct_url not in text for _, text in events["texts"])


def test_aax_rejects_non_youtube_results(monkeypatch):
    instance, events = _test_bot(
        monkeypatch,
        direct_result={
            "source": "SoundCloud",
            "url": "https://soundcloud.example/audio",
            "title": "Wrong source",
            "duration": 20,
        },
    )

    assert instance.handle_youtube_direct_test_command("room-1", "Test song", "listener") is True
    assert events["media"] == []
    assert events["downloads"] == 0
    assert any("فشل اختبار YouTube المباشر" in text for _, text in events["texts"])
