"""Arabic room-message fallback and Arabic source-message catalog."""

class ArabicRoomMessagesSection:
    def _translate_arabic_room_message(self, text):
        """Return Arabic source text unchanged, using the Arabic catalog as the source of truth."""
        value = str(text or "")
        return _load_room_messages("ar").get(value, value)
