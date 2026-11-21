import bot
from bot_sections.room_locales import load_room_messages


def test_room_message_catalogs_and_per_room_language_selection():
    arabic = load_room_messages("ar")
    english = load_room_messages("en")
    assert arabic
    assert arabic.keys() == english.keys()
    assert all(arabic[key] == key for key in arabic)

    source = next(key for key, value in english.items() if value != key)
    instance = bot.TalkinBot.__new__(bot.TalkinBot)
    instance._effective_room_language = lambda room: "en" if room == "EnglishRoom" else "ar"

    assert instance._localize_room_text("EnglishRoom", source) == english[source]
    assert instance._localize_room_text("ArabicRoom", source) == source
    assert instance._localize_room_text("EnglishRoom", english[source]) == english[source]
