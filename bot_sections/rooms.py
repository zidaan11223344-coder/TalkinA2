"""Per-room language, language overrides, help sections and join-language choice."""

class RoomLanguageSection:
    def _localize_room_text(self, room, text):
        language = self._effective_room_language(room)
        if language == "en":
            return self._translate_english_room_message(text)
        return self._translate_arabic_room_message(text)

    def _active_room_language_override(self, room):
        context = getattr(self, "_room_response_language_context", None)
        active = getattr(context, "active", None) if context is not None else None
        if active and active[0] == _norm_room(room):
            return active[1]
        return None

    def _effective_room_language(self, room):
        """Use the room's saved language consistently for every room reply."""
        return self._configured_room_language(room)

    def _configured_room_language(self, room):
        """Return the saved room language, ignoring command-scoped overrides."""
        return getattr(self, "room_languages", {}).get(_norm_room(room), "ar")

    def _push_room_language_override(self, room, language):
        if language not in ("ar", "en"):
            return None
        context = getattr(self, "_room_response_language_context", None)
        if context is None:
            context = self._room_response_language_context = threading.local()
        previous = getattr(context, "active", None)
        context.active = (_norm_room(room), language)
        return context, previous

    @staticmethod
    def _restore_room_language_override(token):
        if token is None:
            return
        context, previous = token
        if previous is None:
            try:
                del context.active
            except AttributeError:
                pass
        else:
            context.active = previous

    def _capture_room_response_language(self, room, command_text=""):
        # Asynchronous results must use the room's persisted language too;
        # otherwise a single English command could make a later result English
        # in a room whose saved language is Arabic.
        return self._configured_room_language(room)

    def _localized_room_worker(self, room, language, worker):
        """Wrap a background worker so its replies keep the originating language."""
        def run():
            token = self._push_room_language_override(room, language)
            try:
                return worker()
            finally:
                self._restore_room_language_override(token)
        return run

    def _send_help_section(self, room=None, private_to=None, page=1, part=1):
        sections = _help_sections_from_messages()
        page_sections = sections.get(int(page), [])
        if not page_sections:
            text = _command_help(page)
            part = 1
        else:
            idx = max(1, min(int(part), len(page_sections))) - 1
            text = page_sections[idx]
            if idx < len(page_sections) - 1 and not (int(page) == 3 and idx == 0):
                text += "\n\n📌 للقائمة التالية اكتب ns"
            elif idx >= len(page_sections) - 1:
                text += "\n\n✅ انتهت أقسام هذه القائمة."
        # a1..a6 are language-neutral menu codes: follow the saved room
        # preference rather than interpreting the Latin code as English.
        if room and self._configured_room_language(room) == "en":
            english = {
                1: [
                    "📋 Admin — moderation\n━━━━━━━━━━━━\nkick@name / kick name — kick a member\nban@name / ban name — ban a member\nub@name / unban name — unban a member\n.u — undo the bot’s last action",
                    "📋 Admin — roles\n━━━━━━━━━━━━\nm@name — make a member\na@name / admin name — make an admin\no@name / owner name — make an owner\n+creator@name — add a room creator\n-creator@name — remove a room creator",
                    "📋 Admin — verification and VIP\n━━━━━━━━━━━━\nvi@name — verify a user\nuns@name — remove verification\nvip@name / unvip@name — manage VIP status\nverify all — verify room members",
                    "🛡️ Admin — protection\n━━━━━━━━━━━━\nprotection@room — open room protection settings\nprotection status — show protection status\nenable protection / disable protection — toggle room protection\nType protection to see numbered options.",
                    "📋 Admin — filter and publishing\n━━━━━━━━━━━━\nmf@on / mf@off — enable/disable the word filter\n+mf@word / -mf@word — add/remove a filtered word\nl@mf — list filtered words\nmbp@name — remove a publishing ban",
                    "📋 Admin — system and diagnostics\n━━━━━━━━━━━━\nstatus — bot status\nram / cpu — resource status\nslow reply diagnostics (authorized master only)\nclean temporary files / create backup\nrestart bot service\nmaster service status/on/off\ntoggle invitations and games",
                    "📋 Admin — points and logs\n━━━━━━━━━━━━\nmsb@name@amount — add points (master private chat)\n-sb@name@amount — deduct points (primary master private chat)\nts@name@amount — replace the saved balance\ntransfer all@amount — bulk points to room members\ntransfer history — show points transfer log",
                    "📋 Admin — rooms and lists\n━━━━━━━━━━━━\nmyrooms — connected rooms\njoin all — rejoin saved rooms\nadd rooms to file — save current rooms\ninv / invite — invite members\ns@name / is@name — search and presence\nl@a / l@m / l@o / l@b — room role lists\nl@all — show room lists in sequence",
                    "📋 Admin — extra settings and lists\n━━━━━━━━━━━━\namf@name / -amf@name — manage filter exceptions\nl@amf — list filter exceptions\nl@mfb / l@mbp — filter and publishing-ban lists\nmr@number — set repeat limit\nsf@on / sf@off — room safety filter\nl@mas / l@mvip — master lists",
                ],
                2: [
                    "🎵 Music\n━━━━━━━━━━━━\n.sa song name / play song name — play music\nstop music / stop — stop the current song\nsher@name — share the current song",
                    "❤️ Reactions, photos and lookalikes\n━━━━━━━━━━━━\nlk@code — like | lv@code — love | dl@code — dislike\ncm@code text — comment | report@code text — report\nصورتي / my picture / my photo — send your account avatar directly to the room\n.صوره <username/topic> — strict-SafeSearch internet image\n.صوره — random image: 30% monkeys; otherwise adult portrait matching known gender\nشبيه@name / شبيهك@name — safe lookalike-style image\nExplicit/obscene image searches are blocked.",
                ],
                3: [
                    "🎮 Games — solo games\n━━━━━━━━━━━━\nrock / paper / scissors — play against the bot\ninvestment, luck, coin@heads/tails, wheel\nbox@1-3, cup@1-3, monster, volcano, bird, star\ntable, uno\nType ns for the next games list.",
                    "🎮 Games — wagers\n━━━━━━━━━━━━\nbet@amount — wager | duel@amount — duel\nmyluck@amount — luck wager\ninvestment@amount — investment game\nChoose the stake after the game command.",
                    "🎮 Games — bank and rewards\n━━━━━━━━━━━━\nbank million — million bank | billion — billion game\nfarm@item — plant a crop | stock — choose a market\nface@name — face game\nType ns for the next games list.",
                    "🎲 Room games — Ludo and Snake & Ladders\n━━━━━━━━━━━━\nludo — start Ludo; choose 1–4 players\n1 = play against the bot; 2–4 = multiplayer\nOther players type join. When the game starts, the player whose turn it is types rool to roll the die and move. First token to the finish wins.\nsnake — start Snake & Ladders; type join to enter.\nType ns for the next games list.",
                    "🎮 More games\n━━━━━━━━━━━━\nmarriage, challenge, riddle, mood, horse, entertainment\nstock/market, steal@name, lookalike@name\nhunt a beauty / hunt a man — random member with a generic stock photo\nmy ex / khul / divorce — playful room-court games\nShot / شوت — ordinary dragon and animal hunt; arrow / سهم claims the royal dragon when its round is open. The first shot after a trip announcement catches the 1,000,000-point dragon.\nlightning, ruby, crash, detector\nType ns for the next games list.",
                    "🏏 Cricket\n━━━━━━━━━━━━\nMaster: start cricket game / stop cricket game.\nVerified players: .cr 1, choose 1–4 players, then each player types join. Choose 1 (bat) or 2 (defend), then send a score from 0 to 6 for each ball.\nType ns to return to the first game list.",
                ],
                4: [
                    "🎁 Gifts\n━━━━━━━━━━━━\nsa@number@name — send a gift\ngifts / gv — open the gift menu (up to 30 items per list; type ns for next)\nThe sender and recipient must be eligible; gift cost is deducted from points.",
                    "📢 Publishing\n━━━━━━━━━━━━\npost or publish — prepare an image post\npost@description / publish@description — add a caption\nSend the image when prompted. Publishing follows the room’s permissions and content filter.",
                ],
                5: [
                    "💰 Points and leaderboards\n━━━━━━━━━━━━\npoints — your balance and game stats\ntop — leaderboard\ntop bet / top duel / top investment — game leaderboards",
                    "💸 Point transfers\n━━━━━━━━━━━━\nsb@name@amount — transfer from your balance\nExample: sb@ahmed@1000\nTransfers are limited to eligible users and deduct points from the sender.",
                ],
                6: [
                    "🚪 Rooms\n━━━━━━━━━━━━\njoin@room_name — join a room, then select its language\nmyrooms — list connected rooms\nleave room — leave and rejoin (primary master only)\ninv / invite — invite members from this room\ninvmsg@text — change invitation text",
                    "🏠 Room messages, welcomes and protection\n━━━━━━━━━━━━\nsay text — make the bot speak\n+sr@trigger@reply — add an auto-reply (master)\nswc@on / swc@off — toggle welcome messages\nfarewell@on / farewell@off — toggle the generic farewell; letter replies stay enabled\nprotection — open protection settings\nType ns for the next room list.",
                ],
            }
            page_english = english.get(int(page), [])
            if page_english:
                idx = max(1, min(int(part), len(page_english))) - 1
                text = page_english[idx]
                if idx < len(page_english) - 1:
                    text += "\n\nType ns for the next section."
                elif int(page) == 3:
                    text += "\n\nType ns to return to the first game list."
        if private_to:
            self._send_help_chunks("chat_message", text, to=private_to)
        elif room:
            self._send_help_chunks("room_message", text, room=room)

    def _send_game_help_section(self, room=None, private_to=None, part=1):
        # Each A3 section is kept as one logical page; the transport helper
        # splits only if needed to satisfy Talkin's hard packet limit.
        sections = _help_sections_from_messages().get(3, [])
        if not sections:
            return False
        if room and self._effective_room_language(room) == "en":
            return self._send_help_section(room=room, page=3, part=part)
        idx = max(1, min(int(part), len(sections))) - 1
        text = sections[idx]
        # The first A3 page already contains its exact navigation footer.
        # Do not append a second `ns` line to it.
        if idx < len(sections) - 1 and idx != 0:
            text += "\n\n📌 للقائمة التالية اكتب ns"
        elif idx >= len(sections) - 1:
            text += "\n\n📌 هذه آخر قائمة في A3."
        if private_to:
            return self._send_help_chunks("chat_message", text, to=private_to)
        if room:
            return self._send_help_chunks("room_message", text, room=room)
        return False

    def _send_help(self, room=None, private_to=None, page=1, game_part=1):
        self._send_help_section(room=room, private_to=private_to, page=page, part=game_part)

    def _room_language_pending_key(self, sender):
        return _norm_user(sender)

    def _begin_join_rooms_language(self, sender, rooms, response_room="", is_private=True):
        """Ask once for language, then join all requested rooms.

        Keep the reply context with the pending request so a normal user can
        issue `دخول@الغرفة` from inside a room and answer `1`/`2` there.
        """
        cleaned=[]
        seen=set()
        for value in rooms or []:
            room_name=str(value or "").strip()
            if not room_name:
                continue
            key=_norm_room(room_name).casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(room_name)
        if not cleaned:
            return False
        # Every explicit دخول@ request becomes a saved-room candidate. This
        # is intentionally done before the language prompt and before the
        # asynchronous join, so a reconnect cannot lose a newly requested
        # room while the server is still acknowledging room_join.
        existing = _persistent_rooms()
        existing_keys = {_norm_room(value).casefold() for value in existing}
        added = False
        for room_name in cleaned:
            key = _norm_room(room_name).casefold()
            if key and key not in existing_keys:
                existing.append(room_name)
                existing_keys.add(key)
                added = True
        if added:
            _save_persistent_rooms(existing)
        if not isinstance(getattr(self, "known_rooms", None), set):
            self.known_rooms = set()
        self.known_rooms.update(cleaned)
        if not hasattr(self,"_pending_room_language"):
            self._pending_room_language={}
        self._pending_room_language[self._room_language_pending_key(sender)]={
            "rooms": cleaned,
            "created": time.time(),
            "response_room": str(response_room or "").strip(),
            "is_private": bool(is_private),
        }
        preview=" ".join(cleaned[:20])
        extra=" ..." if len(cleaned)>20 else ""
        prompt=(
            "🌐 اختر لغة البوت للغرف المحددة\n"
            f"🏠 الغرف: {preview}{extra}\n\n"
            "1️⃣ العربية\n2️⃣ English\n\n"
            "أرسل 1 أو 2."
        )
        if is_private:
            self.send_private_text(sender, prompt)
        elif response_room:
            self.send_room_text(response_room, prompt)
        else:
            self.send_private_text(sender, prompt)
        return True

    def _complete_join_rooms_language(self, sender, choice, response_room="", is_private=None):
        pending_key, pending = self._find_pending_join_for_message(sender, response_room)
        if not isinstance(pending,dict):
            return False
        created=float(pending.get("created",0) or 0)
        if created and time.time()-created > 180:
            self._pending_room_language.pop(pending_key or self._room_language_pending_key(sender),None)
            self.send_private_text(sender,"⌛ انتهت مهلة اختيار اللغة. أرسل أمر دخول الغرف من جديد.")
            return True
        choice_norm = _normalize_lang_choice(choice)
        if choice_norm not in ("1", "2"):
            return False
        lang = "ar" if choice_norm == "1" else "en"
        rooms=list(pending.get("rooms") or [])
        self._pending_room_language.pop(pending_key or self._room_language_pending_key(sender),None)
        if not hasattr(self,"room_languages"):
            self.room_languages={}
        for target in rooms:
            self.room_languages[_norm_room(target)]=lang
        _save_local_json(DATA_DIR / "room_languages.json", self.room_languages)
        # Run in the background so the WebSocket reader remains responsive;
        # the worker itself serializes every join and waits for its ACK.
        threading.Thread(
            target=self._join_rooms_serially, args=(rooms, sender),
            daemon=True, name="serial-room-join",
        ).start()
        sent=len(rooms)
        skipped=0
        label="العربية" if lang=="ar" else "English"
        saved_room = str(pending.get("response_room") or "").strip()
        target_room = str(response_room or saved_room).strip()
        if is_private is None:
            private_reply = bool(pending.get("is_private", True))
        else:
            private_reply = bool(is_private)
        result_msg=(
            f"✅ تم اختيار اللغة: {label}\n"
            f"🏠 عدد الغرف: {len(rooms)}\n"
            f"📨 أُرسلت طلبات الدخول: {sent}\n"
            f"⚠️ تخطّي/فشل: {skipped}\n"
            "⏳ انتظر تأكيد الخادم لكل غرفة."
        )
        if target_room:
            self.send_room_text(target_room, result_msg)
        if sender and (private_reply or not target_room):
            self.send_private_text(sender, result_msg)
        return True
