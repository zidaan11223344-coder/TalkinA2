"""Administrative actions, moderation, room protections, filters and bans."""

class AdminSection:
    def _save_blocked_rooms(self):
        _save_persistent_blocked_rooms(getattr(self, "blocked_rooms", set()))

    def _undo_last_bot_action(self, requester, room=""):
        """Unban the latest confirmed bot-banned user in this exact room."""
        room = str(room or "").strip()
        room_key = _norm_room(room)
        if not room_key:
            self.send_private_text(requester, "❌ نفّذ .u من الغرفة نفسها التي حظر فيها البوت المستخدم.")
            return True
        bans = getattr(self, "last_bot_bans", None)
        if not isinstance(bans, dict):
            bans = _load_local_json(LAST_BOT_BANS_FILE, {})
        action = bans.get(room_key) if isinstance(bans, dict) else None
        if not isinstance(action, dict) or not action.get("active") or action.get("undo_requested"):
            self.send_private_text(requester, f"📭 لا يوجد حظر حديث قابل للتراجع في الغرفة {room}.")
            return True
        target = str(action.get("target") or "").strip().lstrip("@")
        if not target:
            self.send_private_text(requester, "📭 لا يوجد حظر حديث قابل للتراجع في هذه الغرفة.")
            return True
        try:
            # Set the idempotency marker before the packet; the server may
            # confirm the role change before request_admin_action returns.
            action["undo_requested"] = True
            action["undo_requested_at"] = int(time.time())
            bans[room_key] = action
            self.last_bot_bans = bans
            _save_local_json(LAST_BOT_BANS_FILE, bans)
            # Keep the unban in the normal moderation confirmation flow; never
            # silently assign member or undo an action from another room.
            sent = self.request_admin_action(room, target, "member", requester, announce_room=True)
            if not sent:
                action["undo_requested"] = False
                action.pop("undo_requested_at", None)
                _save_local_json(LAST_BOT_BANS_FILE, bans)
                self.send_private_text(requester, f"❌ تعذر إرسال طلب فك حظر @{target} في {room}.")
                return True
            self.send_private_text(requester, f"⏳ أُرسل طلب التراجع عن حظر @{target} في الغرفة {room}؛ بانتظار تأكيد الخادم.")
        except Exception as exc:
            action["undo_requested"] = False
            action.pop("undo_requested_at", None)
            self.last_bot_bans = bans
            _save_local_json(LAST_BOT_BANS_FILE, bans)
            self.log("[UNDO] room ban undo failed:", repr(exc))
            self.send_private_text(requester, f"❌ تعذر التراجع عن حظر الغرفة: {exc}")
        return True

    def send_admin(self, room: str, target: str, operation: str):
        """Execute room moderation directly over TalkinChat's native room_admin query.

        The deployment log confirms the native result states:
          kick -> role_changed with role "kicked", followed by user_left
          ban  -> role_changed with role "outcast"
        Therefore kick/ban must NOT depend on Supabase, config.json, or a DB RPC.
        """
        room = str(room or "").strip()
        target = str(target or "").strip().lstrip("@")
        if not room:
            raise ValueError("room is required")
        if not target:
            raise ValueError("target is required")

        if operation == "kick":
            self.log(f"[MOD] native kick room={room} target=@{target}")
            payload = encode_query(
                "room_admin",
                type_="kick",
                room=room,
                to=target,
                value="none",
            )
            return self.send_query(payload)

        if operation in ("ban_ip", "ip_ban"):
            self.log(f"[MOD] room ban_ip room={room} target=@{target}")
            try:
                self.send_query(
                    encode_query(
                        "room_admin",
                        type_="ban_ip",
                        room=room,
                        to=target,
                        value="outcast",
                    )
                )
            except Exception as e:
                self.log("[MOD] ban_ip packet warning:", repr(e))
            payload = encode_query(
                "room_admin",
                type_="change_role",
                room=room,
                to=target,
                value="outcast",
            )
            return self.send_query(payload)

        if operation == "ban":
            self.log(f"[MOD] room outcast room={room} target=@{target}")
            payload = encode_query(
                "room_admin",
                type_="change_role",
                room=room,
                to=target,
                value="outcast",
            )
            return self.send_query(payload)

        role_map = {
            "outcast": "outcast",
            "admin": "admin",
            "member": "member",
            "owner": "owner",
            "none": "none",
        }
        if operation in role_map:
            return self.send_query(
                encode_query(
                    "room_admin",
                    type_="change_role",
                    room=room,
                    to=target,
                    value=role_map[operation],
                )
            )
        raise ValueError("Unknown admin operation: " + operation)

    def request_admin_action(self, room: str, target: str, operation: str, requester: str, announce_room: bool = False):
        """Send moderation request and report success only after server confirmation."""
        role_by_operation = {
            "kick": "kicked", "ban": "outcast", "member": "member",
            "admin": "admin", "owner": "owner",
        }
        expected_role = role_by_operation.get(operation)
        if not expected_role:
            raise ValueError("Unknown admin operation: " + operation)
        room = str(room or "").strip()
        target = str(target or "").strip().lstrip("@")
        requester = str(requester or "").strip()
        key = (room.casefold(), target.casefold(), expected_role)
        marker = {
            "room": room, "target": target, "role": expected_role,
            "created_at": time.time(), "requester": requester,
        }
        # Publish intent before the packet: Talkin may deliver role_changed
        # immediately, and the event fields can contain both actor and target.
        with self.pending_admin_lock:
            self.pending_admin_actions[key] = {
                **marker, "announced": False, "announce_room": bool(announce_room),
                "previous_role": self.room_users.get(room, {}).get(target),
            }
        if operation in ("ban", "kick"):
            recent = getattr(self, "_recent_room_moderation", None)
            if recent is None:
                recent = self._recent_room_moderation = {}
            recent[_norm_room(room)] = marker
        try:
            self.send_admin(room, target, operation)
        except Exception as exc:
            with self.pending_admin_lock:
                self.pending_admin_actions.pop(key, None)
            recent = getattr(self, "_recent_room_moderation", {})
            if recent.get(_norm_room(room)) == marker:
                recent.pop(_norm_room(room), None)
            self.log(f"[MOD] request failed room={room} target=@{target}: {exc!r}")
            if requester and not _is_master_name(requester):
                self.send_private_text(requester, f"❌ تعذر إرسال أمر الإدارة إلى الخادم: {exc}")
            return False
        previous_role = self.room_users.get(room, {}).get(target)
        inverse = previous_role if previous_role in {"none", "member", "admin", "owner"} else "member"
        self.last_admin_action = {
            "room": room, "target": target, "operation": operation,
            "inverse": inverse, "created_at": int(time.time()), "undone": False,
        }
        _save_local_json(DATA_DIR / "last_admin_action.json", self.last_admin_action)
        labels = {
            "kicked": "طرد",
            "outcast": "حظر",
            "member": "فك الحظر",
            "admin": "تعيين مشرف",
            "owner": "تعيين أونر",
        }
        # Master moderation commands are intentionally silent in both room and private chat.
        # Mark the command as handled so the generic management wrapper does not
        # append: "تم استقبال الأمر، لكن لا توجد نتيجة...". Server confirmation
        # remains logged and the persistent ban record is written only when the
        # role_changed event confirms the actual ban.
        if getattr(self._master_reply_local, "tracking", False):
            if _norm_user(requester) == _norm_user(getattr(self._master_reply_local, "command_sender", "")):
                self._master_reply_local.replied = True
                self._master_reply_local.private_replied = True
        self.log(f"[MOD] awaiting server confirmation room={room} target=@{target} role={expected_role}")
        threading.Thread(
            target=self._admin_confirmation_timeout,
            args=(key,), daemon=True, name="admin-confirmation-timeout",
        ).start()
        return True

    def _admin_confirmation_timeout(self, key):
        time.sleep(float(os.getenv("ADMIN_CONFIRMATION_TIMEOUT", "8")))
        with self.pending_admin_lock:
            pending = self.pending_admin_actions.pop(key, None)
        # A timeout is intentionally silent.  Talkin may apply the role
        # change while delaying or omitting the matching event; showing a
        # failure message after a successful native room notification is
        # misleading.  Only role_changed below emits a success message.
        if pending:
            self.log(f"[MOD] confirmation timeout room={pending['room']} target=@{pending['target']}")

    def _filter_list_key(self, room, sender):
        return (str(room or ""), _norm_user(sender))

    def _build_filter_list_pages(self, words):
        """Build short filter-word pages like A3, keeping every packet under the A3 page size."""
        clean=[]
        seen=set()
        for word in words or []:
            w=str(word or "").strip()
            k=_norm_filter_text(w)
            if w and k and k not in seen:
                seen.add(k); clean.append(w)
        pages=[]; current=[]
        for w in clean:
            # Keep the visible list compact enough for Talkin's room/private packet limit.
            if len(current) >= 7:
                pages.append(current); current=[]
            candidate=current + [w]
            body="\n".join(f"{i}. {x}" for i,x in enumerate(candidate,1))
            header="🚫 كلمات الفلتر\n━━━━━━━━━━━━\n"
            if len(header)+len(body)+len("\n\n📌 للقائمة التالية اكتب ns") > 320 and current:
                pages.append(current); current=[w]
            else:
                current=candidate
        if current: pages.append(current)
        return pages

    def _send_filter_list_page(self, room, sender, is_private, part=1):
        state=getattr(self, "_filter_list_state", {}).get(self._filter_list_key(room,sender))
        if not state:
            return False
        pages=state.get("pages") or []
        if not pages:
            text="🚫 كلمات الفلتر\n━━━━━━━━━━━━\n📭 قائمة الفلتر فارغة حالياً."
        else:
            idx=max(1,min(int(part),len(pages)))-1
            lines=["🚫 كلمات الفلتر", "━━━━━━━━━━━━"]
            lines.extend(f"{i}. {w}" for i,w in enumerate(pages[idx],1))
            if idx < len(pages)-1:
                lines.append("\n📌 للقائمة التالية اكتب Ns")
            else:
                lines.append("\n✅ انتهت قوائم كلمات الفلتر.")
            text="\n".join(lines)
        if is_private:
            return self._send_text_packets("chat_message", text, to=sender)
        if room:
            return self._send_text_packets("room_message", text, room=room)
        return False

    def _find_publish_filter_hit(self, text):
        """Match explicit abusive terms as whole words/phrases, not substrings.

        This prevents harmless captions such as "يلا اكتبو مليار بسرعه بتكسبو"
        from being blocked merely because a configured word appears inside
        another word. Arabic diacritics, tatweel, alef variants and punctuation
        are normalized before matching.
        """
        def normalize(value):
            value = unicodedata.normalize("NFKC", str(value or "")).casefold()
            value = re.sub(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]", "", value)
            value = value.translate(str.maketrans({
                "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
                "ى": "ي", "ؤ": "و", "ئ": "ي",
            }))
            value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
            return " ".join(value.split())

        normalized_text = normalize(text)
        if not normalized_text:
            return None

        # These are deliberately limited to clear profanity, direct insults,
        # and explicit defamatory/sexual slurs. Ordinary game/chat words are
        # not included. Existing master-managed mf@ words remain supported.
        defaults = {
            "شرموط", "شرموطة", "شراميط", "قحبة", "قحاب", "منيوك",
            "كسم", "كسمك", "كس امك", "كس اختك", "كسختك", "طيزك",
            "خول", "عاهرة", "زاني", "زانية", "ديوث",
            "يلعن امك", "يلعن ابوك", "ابن الكلب", "يا ابن الكلب",
            "fuck", "fucking", "motherfucker", "bitch", "whore",
            "slut", "cunt",
        }
        configured = getattr(self, "banned_words", set()) or set()
        candidates = set(defaults)
        candidates.update(str(word) for word in configured if str(word).strip())
        # Common game/chat vocabulary must never become a publish violation
        # through an accidental entry in an old moderation-word file.
        safe_terms = {
            "يلا", "اكتبو", "اكتبوا", "مليار", "مليون", "بسرعه", "بسرعة",
            "بتكسبو", "بتكسبوا", "تكسبو", "تكسبوا", "نقاط", "ربح", "فوز",
        }
        safe_terms = {normalize(word) for word in safe_terms}
        candidates = {word for word in candidates if normalize(word) not in safe_terms}

        # Match normalized tokens/phrases with word boundaries. Punctuation
        # and repeated whitespace cannot bypass the filter, while substrings
        # inside unrelated words do not trigger it.
        for word in sorted(candidates, key=lambda item: len(normalize(item)), reverse=True):
            term = normalize(word)
            if not term:
                continue
            pattern = r"(?<!\w)" + r"\s+".join(re.escape(part) for part in term.split()) + r"(?!\w)"
            # Also catch punctuation/spacing inserted between the letters of
            # an abusive word (e.g. ك.س.م) without matching inside other words.
            compact_term = term.replace(" ", "")
            obfuscated_pattern = r"(?<!\w)" + r"[\W_]*".join(re.escape(ch) for ch in compact_term) + r"(?!\w)"
            if (re.search(pattern, normalized_text, flags=re.UNICODE)
                    or re.search(obfuscated_pattern, normalized_text, flags=re.UNICODE)):
                return str(word)
        return None

    def _find_publish_filter_hit(self, text):
        """Match explicit abusive terms as whole words/phrases, not substrings.

        This prevents harmless captions such as "يلا اكتبو مليار بسرعه بتكسبو"
        from being blocked merely because a configured word appears inside
        another word. Arabic diacritics, tatweel, alef variants and punctuation
        are normalized before matching.
        """
        def normalize(value):
            value = unicodedata.normalize("NFKC", str(value or "")).casefold()
            value = re.sub(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]", "", value)
            value = value.translate(str.maketrans({
                "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
                "ى": "ي", "ؤ": "و", "ئ": "ي",
            }))
            value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
            return " ".join(value.split())

        normalized_text = normalize(text)
        if not normalized_text:
            return None

        # These are deliberately limited to clear profanity, direct insults,
        # and explicit defamatory/sexual slurs. Ordinary game/chat words are
        # not included. Existing master-managed mf@ words remain supported.
        defaults = {
            "شرموط", "شرموطة", "شراميط", "قحبة", "قحاب", "منيوك",
            "كسم", "كسمك", "كس امك", "كس اختك", "كسختك", "طيزك",
            "خول", "عاهرة", "زاني", "زانية", "ديوث",
            "يلعن امك", "يلعن ابوك", "ابن الكلب", "يا ابن الكلب",
            "fuck", "fucking", "motherfucker", "bitch", "whore",
            "slut", "cunt",
        }
        configured = getattr(self, "banned_words", set()) or set()
        candidates = set(defaults)
        candidates.update(str(word) for word in configured if str(word).strip())
        # Common game/chat vocabulary must never become a publish violation
        # through an accidental entry in an old moderation-word file.
        safe_terms = {
            "يلا", "اكتبو", "اكتبوا", "مليار", "مليون", "بسرعه", "بسرعة",
            "بتكسبو", "بتكسبوا", "تكسبو", "تكسبوا", "نقاط", "ربح", "فوز",
        }
        safe_terms = {normalize(word) for word in safe_terms}
        candidates = {word for word in candidates if normalize(word) not in safe_terms}

        # Match normalized tokens/phrases with word boundaries. Punctuation
        # and repeated whitespace cannot bypass the filter, while substrings
        # inside unrelated words do not trigger it.
        for word in sorted(candidates, key=lambda item: len(normalize(item)), reverse=True):
            term = normalize(word)
            if not term:
                continue
            pattern = r"(?<!\w)" + r"\s+".join(re.escape(part) for part in term.split()) + r"(?!\w)"
            # Also catch punctuation/spacing inserted between the letters of
            # an abusive word (e.g. ك.س.م) without matching inside other words.
            compact_term = term.replace(" ", "")
            obfuscated_pattern = r"(?<!\w)" + r"[\W_]*".join(re.escape(ch) for ch in compact_term) + r"(?!\w)"
            if (re.search(pattern, normalized_text, flags=re.UNICODE)
                    or re.search(obfuscated_pattern, normalized_text, flags=re.UNICODE)):
                return str(word)
        return None

    def _auto_unban(self, room, username):
        # A ban must persist until an explicit master action (.u in the same
        # room, or an explicit unban command) confirms the role change.
        self.log("[JOINLEAVE] ignored automatic member restore after ban:", room, username)
        return False

    def _handle_management_command(self, room, body, sender, is_private=False):
        # Management commands are accepted only from the master. Keep normal
        # response routing enabled so the master receives the result privately
        # (or in the command room when the command is public by design).
        # Publishing is intentionally also available to verified accounts.
        # Public help: menu and pages must be available to ALL users,
        # whether verified, unverified, or master. Handle them before the
        # master-only management gate below.
        _body_text = str(body or "").strip()
        _body_low = _body_text.casefold()
        # أمر استعلام تواجد المستخدم is@اسم أو is اسم
        m_is = re.fullmatch(r"(?:is@|is\s+@?)([^\s@]+)", _body_text, re.I)
        if m_is:
            target_user = m_is.group(1).strip()
            norm_target = _norm_user(target_user)
            found_rooms = []
            for r_name, roster in getattr(self, "room_users", {}).items():
                for u_name, u_role in roster.items():
                    if _norm_user(u_name) == norm_target:
                        found_rooms.append((r_name, u_role))
            if found_rooms:
                locs = "، ".join([f"غرفة [{r}] (رتبة: {role})" for r, role in found_rooms])
                reply = "👤 المستخدم: @" + target_user + "\n🟢 الحالة: متصل الآن (Online)\n📍 متواجد في: " + locs
            else:
                reply = "👤 المستخدم: @" + target_user + "\n🔴 الحالة: غير متصل (Offline)\nℹ️ غير متواجد حالياً في أي من الغرف المتصل بها البوت."
            if is_private:
                self.send_private_text(sender, reply)
            elif room:
                self.send_room_text(room, reply)
            return True

        if _body_low in ("اوامر", "الاوامر", "help", "مساعدة"):
            menu = _command_menu_for(_is_primary_master(sender), is_private=is_private)
            if room and not is_private and self._effective_room_language(room) == "en":
                menu = "📚 Talkin bot commands\n━━━━━━━━━━━━\na2 — Music and reactions\na3 — Games\na4 — Gifts and publishing\na5 — Points\na6 — Rooms and welcomes\n━━━━━━━━━━━━\nType a2–a6 to open a section."
            if is_private:
                self.send_private_text(sender, menu)
            elif room:
                self.send_room_text(room, menu)
            return True
        _m_public_help = re.fullmatch(r"a([1-6])", _body_low)
        if _m_public_help:
            _page = int(_m_public_help.group(1))
            # a2..a6 are public help menus. a1 remains private/master-only.
            if _page == 1 and not (_is_primary_master(sender) and is_private):
                return True
            _key = (("__private__" if is_private else str(room)), _norm_user(sender))
            self.help_pages[_key] = _page
            self.help_page_part[_key] = 1
            self.help_game_part[_key] = 1
            self._send_help(room=room, private_to=sender if is_private else None, page=_page)
            return True
        if _body_low in ("ns", "n", "التالي", "القائمة التالية", "next"):
            # Give the wallet its own next-page cursor before generic list/help paging.
            if not is_private and room and self._wallet_handle_next_page(room, sender, _body_low):
                return True
            key = (("__private__" if is_private else str(room)), _norm_user(sender))
            # Filter-word navigation has priority over A1..A6 navigation.
            filter_state = getattr(self, "_filter_list_state", {}).get(self._filter_list_key(room, sender))
            if filter_state:
                pages = filter_state.get("pages") or []
                part = int(filter_state.get("part", 1) or 1)
                if part < len(pages):
                    part += 1
                    filter_state["part"] = part
                    self._send_filter_list_page(room, sender, is_private, part)
                else:
                    if is_private:
                        self.send_private_text(sender, "✅ انتهت قوائم كلمات الفلتر.\n📌 أرسل l@mf لعرضها من البداية.")
                    elif room:
                        self.send_room_text(room, "✅ انتهت قوائم كلمات الفلتر.\n📌 أرسل l@mf لعرضها من البداية.")
                return True

            # Transfer logs are emitted privately and keep their own page
            # cursor. Handle this here (the real dispatcher) before generic
            # result/help pagination can consume the same `ns` command.
            transfer_key = _norm_user(sender)
            transfer_pages = getattr(self, "_transfer_log_pages", {})
            if _is_master_name(sender) and transfer_key in transfer_pages:
                current = max(1, int(transfer_pages.get(transfer_key, 1) or 1))
                next_page = current + 1
                target = getattr(self, "_transfer_log_targets", {}).get(transfer_key)
                language = getattr(self, "_transfer_log_languages", {}).get(transfer_key, False)
                if target is None:
                    _rows, _total, page_count = _all_point_transfer_history(
                        page=1, page_size=_POINT_TRANSFER_HISTORY_PAGE_SIZE
                    )
                else:
                    _rows, _total, page_count = _all_point_transfer_history(
                        page=1, page_size=_POINT_TRANSFER_HISTORY_PAGE_SIZE, recipient=target
                    )
                if next_page > page_count:
                    transfer_pages.pop(transfer_key, None)
                    getattr(self, "_transfer_log_languages", {}).pop(transfer_key, None)
                    getattr(self, "_transfer_log_targets", {}).pop(transfer_key, None)
                    if target is None:
                        message = "📒 هذه آخر قائمة في سجل التحويل." if not language else "📒 This is the last transfer-log list."
                    else:
                        message = (f"📒 هذه آخر قائمة لتحويلات @{_transfer_log_safe_field(target)}."
                                   if not language else f"📒 This is the last list of transfers to @{_transfer_log_safe_field(target)}.")
                else:
                    transfer_pages[transfer_key] = next_page
                    message, _page_count = _format_all_point_transfer_history_page(
                        next_page, language, recipient=target
                    )
                self.send_private_text(sender, message)
                return True

            # Once the master has opened A1, its explicitly numbered admin
            # sections take precedence over stale generic result pages. This
            # keeps ns in the requested moderation -> roles -> ... sequence.
            if self.help_pages.get(key) == 1:
                sections = _help_sections_from_messages().get(1, [])
                current_part = max(1, int(self.help_page_part.get(key, 1) or 1))
                if current_part < len(sections):
                    current_part += 1
                    self.help_page_part[key] = current_part
                    self.help_game_part[key] = current_part
                    self._send_help(room=room, private_to=sender if is_private else None,
                                    page=1, game_part=current_part)
                else:
                    message = "✅ انتهت قوائم الإدارة.\n📌 للعودة اكتب a1."
                    if is_private:
                        self.send_private_text(sender, message)
                    elif room:
                        self.send_room_text(room, message)
                return True

            # Generic long-result navigation. Any command that produced more
            # than one safe text page is continued with Ns.
            result_pages = getattr(self, "_result_pages", {})
            result_key_room = str(room or "")
            # Room lists are keyed by room and requester too, so one user's
            # ns command cannot advance another user's pending roster.
            result_key_user = str(sender or "")
            result_key = ("chat_message" if is_private else "room_message", result_key_room, result_key_user)
            result_state = result_pages.get(result_key)
            if not result_state:
                # l@sr is often issued from a room but replies privately to
                # the master.  Accept Ns from that private chat as well as
                # from the original command context.
                for candidate_key, candidate_state in reversed(list(result_pages.items())):
                    if (candidate_key[0] == "chat_message"
                            and candidate_key[2] == str(sender or "")):
                        result_key, result_state = candidate_key, candidate_state
                        break
            if result_state:
                pages = result_state.get("pages") or []
                part = int(result_state.get("part", 1) or 1)
                if part < len(pages):
                    part += 1
                    result_state["part"] = part
                    chunk = pages[part - 1]
                    if part < len(pages):
                        chunk += "\n\n📌 للقائمة التالية اكتب Ns"
                    else:
                        chunk += "\n\n✅ انتهت القوائم."
                    payload = dict(result_state.get("kwargs") or {})
                    payload["type_"] = "text"
                    payload["body"] = chunk
                    self.send_query(encode_query("chat_message" if is_private else "room_message", **payload))
                else:
                    result_pages.pop(result_key, None)
                    msg = "✅ انتهت القوائم.\n📌 أرسل الأمر من جديد لعرض النتائج من البداية."
                    if is_private:
                        self.send_private_text(sender, msg)
                    elif room:
                        self.send_room_text(room, msg)
                return True

            # ns only works after the user explicitly opened a category with a1..a6.
            # Never default to a1, otherwise a bare ns in a room would expose admin help.
            if key not in self.help_pages:
                target = "a1 إلى a6" if (_is_primary_master(sender) and is_private) else "a2 إلى a6"
                msg = f"📌 اكتب {target} أولًا لفتح قائمة الأوامر، ثم استخدم ns للتالي."
                if is_private:
                    self.send_private_text(sender, msg)
                elif room:
                    self.send_room_text(room, msg)
                return True
            sections_map = _help_sections_from_messages()
            current_page = int(self.help_pages.get(key, 1) or 1)
            current_page = max(1, min(6, current_page))
            sections = sections_map.get(current_page, [])
            total = max(1, len(sections))
            part = int(self.help_page_part.get(key, 1) or 1)

            # ns moves only inside the category the user opened.
            # It must NEVER jump from a3 to a2 (or between a1..a6).
            if part < total:
                part += 1
                self.help_pages[key] = current_page
                self.help_page_part[key] = part
                self.help_game_part[key] = part
                self._send_help(room=room, private_to=sender if is_private else None,
                                page=current_page, game_part=part)
            else:
                # The current category is finished. Stay on it and tell the
                # user that there are no more lists in this category.
                self.help_pages[key] = current_page
                self.help_page_part[key] = part
                self.help_game_part[key] = part
                label = {1: "الإدارة", 2: "الموسيقى والتفاعلات", 3: "الألعاب",
                         4: "الهدايا والنشر", 5: "النقاط", 6: "الغرف"}.get(current_page, "القائمة")
                msg = f"✅ انتهت قوائم {label}.\n📌 للانتقال إلى قائمة أخرى اكتب a1 إلى a6."
                if is_private:
                    self.send_private_text(sender, msg)
                elif room:
                    self.send_room_text(room, msg)
            return True

        is_publish = _is_publish_command(body)
        is_publish_unban = bool(re.fullmatch(r"mbp@.+", str(body or "").strip(), re.I))
        repeat_last_command = str(body or "").strip().casefold() == ".u"
        security_command = bool(
            re.match(r"^(?:تشغيل|إيقاف) الحماية$", str(body or "").strip(), re.I)
            or str(body or "").strip().casefold() in {"protection", "room protection", "protection status", "room protection status", "enable protection", "enable room protection", "disable protection", "disable room protection"}
            or re.match(r"^(?:protection|room protection)(?:@|\s+).+", str(body or "").strip(), re.I)
            or re.match(r"^mr@\d+$", str(body or "").strip(), re.I)
            or str(body or "").strip().casefold() in {"حماية", "حمايه", "حماية الغرفة", "حمايه الغرفه", "l@mfb"}
            or re.match(r"^(?:amf|[-+]amf)@.+$", str(body or "").strip(), re.I)
            or re.match(r"^l@mfb$", str(body or "").strip(), re.I)
            or str(body or "").strip().casefold() in {"inv", "invite", "دعوات"}
            or re.match(r"^(?:inv|invite|دعوات)\s+", str(body or "").strip(), re.I)
            or re.match(r"^l@[amob]$", str(body or "").strip(), re.I)
            or re.match(r"^is@.+", str(body or "").strip(), re.I)
            or re.match(r"^mr@\d+$", str(body or "").strip(), re.I)
            or re.match(r"^(?:إضافة|اضافة|استرجاع|استعادة|زيادة|زود)\s+(?:العاب|ألعاب)@", str(body or "").strip(), re.I)
        )
        # A pending room-join language choice belongs to the user who started
        # `دخول@...`, even when that user is not a master.  Handle it before
        # the master-only management permission gate.
        _norm_ch = _normalize_lang_choice(body)
        if _norm_ch in ("1", "2"):
            response_room = str(room or getattr(self, "last_joined_room", "") or getattr(self, "room", "") or "").strip()
            pending_key, pending_join = self._find_pending_join_for_message(sender, response_room)
            if isinstance(pending_join, dict):
                return self._complete_join_rooms_language(
                    sender, _norm_ch, response_room=response_room, is_private=is_private
                )

        join_command = bool(re.match(r"^(?:دخول|join)\s*@\s*.+$", str(body or "").strip(), re.I))
        join_all_command = str(body or "").strip().casefold() in {
            "دخول الكل", "دخولكل", "دخول الغرف", "دخول غرف السيرفر",
            "دخول كل غرف السيرفر", "join all", "join server rooms",
        }
        verification_manager_command = _is_verification_manager_command(body) or str(body or "").strip().casefold() in {
            "vi", "vip", "ماستر توثيق"
        }
        shop_command_text = str(body or "").strip()
        shop_command_digits = shop_command_text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        pending_welcome_text = _norm_user(sender) in getattr(self, "custom_welcome_text_pending", {})
        member_shop_command = bool(
            shop_command_text.casefold() in {"متجر", "shop", "store", "story", "ترحيب_صورة", "welcome_image"}
            or re.fullmatch(r"(?:sby|buy|purchase|شراء|اشتر|اشتري)(?:\s*@?\s*)[1-6]", shop_command_digits, re.I)
            or re.fullmatch(r"(?:ترحيب|welcome)@.+", shop_command_text, re.I | re.S)
            or pending_welcome_text
        )
        welcome_content_delete_command = shop_command_text.casefold() in {
            "حذف ترحيب", "حذف الترحيب", "إلغاء الترحيب", "الغاء الترحيب",
            "حذف ترحيب نص", "حذف الترحيب نص", "حذف ترحيب النص", "حذف الترحيب النص",
            "حذف ترحيب نصي", "حذف الترحيب نصي", "حذف الترحيب النصي",
            "حذف ترحيب صورة", "حذف الترحيب صورة", "حذف ترحيب الصورة", "حذف الترحيب الصورة",
            "حذف ترحيب صوره", "حذف الترحيب صوره", "حذف ترحيب الصوره", "حذف الترحيب الصوره",
            "delete welcome", "remove welcome", "delete welcome text", "remove welcome text",
            "delete welcome image", "remove welcome image",
        }
        points_transfer_command = bool(re.fullmatch(r"sb@([^@]+)@([^@]+)", str(body or "").strip(), re.I))
        master_points_transfer_command = bool(re.fullmatch(r"msb@([^@]+)@([^@]+)", str(body or "").strip(), re.I))
        master_points_deduction_command = str(body or "").strip().casefold().startswith("-sb@")
        if master_points_deduction_command and (not is_private or not _is_primary_master(sender)):
            if not _is_primary_master(sender):
                message = "🚫 أمر -sb مخصص للماستر المحدد في BOT_MASTER فقط."
            else:
                message = "🚫 أرسل أمر -sb في خاص البوت؛ وهو متاح للماستر المحدد في BOT_MASTER فقط."
            if is_private:
                self.send_private_text(sender, message)
            elif room:
                self.send_room_text(room, message)
            return True
        if master_points_transfer_command and not (is_private and _is_primary_master(sender)):
            message = "🚫 أمر msb للماستر المحدد ومن خاص البوت فقط." if is_private else "🚫 أرسل أمر msb في خاص البوت، وهو متاح للماستر المحدد فقط."
            if is_private:
                self.send_private_text(sender, message)
            elif room:
                self.send_room_text(room, message)
            return True
        m_master_transfer = re.fullmatch(r"msb@([^@]+)@([^@]+)", str(body or "").strip(), re.I)
        if m_master_transfer and is_private and _is_primary_master(sender):
            target = m_master_transfer.group(1).strip().lstrip("@")
            amount = _parse_compact_amount(m_master_transfer.group(2))
            if not target or amount is None or amount <= 0:
                self.send_private_text(sender, "❌ الصيغة: msb@اسم المستخدم@عدد النقاط مثل 10m10b")
                return True
            new = _add_points(target, amount)
            self.send_private_text(sender, f"✅ تم تحويل {_fmt_points(amount)} نقطة إلى @{target} من رصيد الماستر.\n💳 رصيد المستلم: {_fmt_points(new)}")
            if _norm_user(target) != _norm_user(sender):
                self.send_private_text(target, f"💰 إشعار تحويل: استلمت {_fmt_points(amount)} نقطة من الماستر. رصيدك الحالي: {_fmt_points(new)}")
            return True
        public_top_command = str(body or "").strip().casefold() in {
            "توب", "top", "توب الألعاب", "توب الالعاب", "top games", "games top", "ملياري", "مليارات", "مليوني", "ملايين"
        }
        is_game_cmd = str(body or "").strip().casefold() in ("تشغيل الالعاب", "تشغيل الألعاب", "ايقاف الالعاب", "إيقاف الالعاب", "ايقاف الألعاب", "إيقاف الألعاب")
        is_sf_cmd = bool(re.fullmatch(r"sf@(on|off)", str(body or "").strip(), re.I))
        is_creator_cmd = bool(re.match(r"^[+-]?صانع(?:ي| الغرفة| الغرفه)?(?:@.+)?$", str(body or "").strip(), re.I))
        pending_protection_choice = bool(
            room and _room_manager(self, room, sender)
            and isinstance(getattr(self, "_pending_protection_number", {}).get(_norm_user(sender)), dict)
        )
        pending_named_target = isinstance(
            getattr(self, "_pending_named_commands", {}).get(_norm_user(sender)), dict
        )
        # Interactive management flow: `حظر`/`طرد`/`ادمن`/... first asks for
        # the username, then executes in the original room/context.
        pending_key = _norm_user(sender)
        pending_named = getattr(self, "_pending_named_commands", {}).get(pending_key)
        if isinstance(pending_named, dict) and time.time() - float(pending_named.get("created", 0) or 0) <= 180:
            candidate = str(body or "").strip().lstrip("@").split()[0] if str(body or "").strip() else ""
            if candidate and not _looks_like_bot_command(body) and not _looks_like_admin_command(body):
                self._pending_named_commands.pop(pending_key, None)
                action = pending_named.get("action")
                context_room = str(pending_named.get("room") or room or "").strip()
                if action in {"kick", "ban", "member", "admin", "owner"}:
                    if not context_room:
                        self.send_private_text(sender, "❌ لا توجد غرفة لتنفيذ العملية فيها.")
                    else:
                        self.request_admin_action(context_room, candidate, action, sender, announce_room=True)
                    return True
                # Reuse the tested canonical handlers for account-list actions.
                canonical = {"master": f"mas@{candidate}", "mvip": f"mvip@{candidate}",
                              "vip": f"vip@{candidate}", "verify": f"vi@{candidate}"}.get(action)
                if canonical:
                    return self._handle_management_command_impl(context_room, canonical, sender, is_private=bool(pending_named.get("is_private", is_private)))
        elif isinstance(pending_named, dict):
            self._pending_named_commands.pop(pending_key, None)

        named_commands = {
            "ادمن": "admin", "طرد": "kick", "حظر": "ban", "اونر": "owner", "أونر": "owner",
            "عضو": "member", "ماستر": "master", "ماستر توثيق": "mvip", "vip": "vip", "vi": "verify",
        }
        named_action = named_commands.get(str(body or "").strip().casefold())
        if named_action:
            if named_action in {"admin", "kick", "ban", "member", "owner"} and not room:
                self.send_private_text(sender, "❌ أرسل هذا الأمر من الغرفة التي تريد تنفيذ العملية فيها.")
                return True
            self._pending_named_commands[pending_key] = {
                "action": named_action, "room": str(room or ""),
                "is_private": bool(is_private), "created": time.time(),
            }
            prompt = "👤 أرسل اسم المستخدم الآن لتنفيذ العملية." if named_action not in {"master", "mvip", "vip", "verify"} else "👤 أرسل اسم المستخدم الآن، وبعدها سينفذ البوت العملية."
            if is_private:
                self.send_private_text(sender, prompt)
            else:
                self.send_room_text(room, prompt)
            return True
        if (not _is_master_name(sender)
                and not public_top_command
                and not (verification_manager_command and _is_mvip_master(sender))
                and not (is_publish and _is_verified_user(sender))
                and not (points_transfer_command and _is_verified_user(sender))
                and not (member_shop_command and _is_verified_user(sender))
                and not welcome_content_delete_command
                and not is_publish_unban
                and not join_command
                and not join_all_command
                and not repeat_last_command
                and not pending_named_target
                and not ((is_game_cmd or is_creator_cmd or is_sf_cmd) and room and _room_manager(self, room, sender))
                and not (security_command and _room_protection_manager(self, room, sender))
                and not (pending_protection_choice and _room_protection_manager(self, room, sender))):
            return False
        # A private command can be replayed by the Talkin transport with a new
        # frame/uid. Do not answer the same account-list request twice in a row.
        command_key = str(body or "").strip().casefold()
        if command_key in ("vi", "الموثقين", "الموثقون", "الموثقين؟"):
            now = time.time()
            seen_key = (_norm_user(sender), command_key, bool(is_private))
            previous = getattr(self, "_management_command_seen", {}).get(seen_key, 0.0)
            self._management_command_seen[seen_key] = now
            if previous and now - previous < 20.0:
                self.log("[DEDUP] ignored repeated account-list command")
                return True
        old_tracking = getattr(self._master_reply_local, "tracking", False)
        old_replied = getattr(self._master_reply_local, "replied", False)
        old_private_replied = getattr(self._master_reply_local, "private_replied", False)
        old_command_private = getattr(self._master_reply_local, "command_private", True)
        old_command_sender = getattr(self._master_reply_local, "command_sender", "")
        old_command_room = getattr(self._master_reply_local, "command_room", "")
        old_command_text = getattr(self._master_reply_local, "command_text", "")
        self._master_reply_local.tracking = True
        self._master_reply_local.replied = False
        self._master_reply_local.private_replied = False
        self._master_reply_local.command_private = bool(is_private)
        self._master_reply_local.command_sender = sender
        self._master_reply_local.command_room = room
        self._master_reply_local.command_text = str(body or "").strip()
        try:
            handled = self._handle_management_command_impl(room, body, sender, is_private=is_private)
            if handled and not getattr(self, "_replaying_bot_action", False):
                self._remember_bot_action(room, body, sender, is_private=is_private)
            return handled
        finally:
            self._master_reply_local.tracking = old_tracking
            self._master_reply_local.replied = old_replied
            self._master_reply_local.private_replied = old_private_replied
            self._master_reply_local.command_private = old_command_private
            self._master_reply_local.command_sender = old_command_sender
            self._master_reply_local.command_room = old_command_room
            self._master_reply_local.command_text = old_command_text

    def _handle_management_command_impl(self, room, body, sender, is_private=False):
        """Giant-style persistent management commands. Returns True if consumed."""
        text=str(body or "").strip()
        low=text.casefold()

        # Full-control secondary masters. The owner in BOT_MASTER alone may
        # grant or revoke this role; it is separate from verification masters.
        match = re.fullmatch(r"vipmas@(.+)", text, re.I)
        if match:
            if not is_private or not _is_bot_owner(sender):
                self.send_private_text(sender, "🔒 إضافة ماستر كامل متاحة للمالك الأساسي في الخاص فقط.")
                return True
            target = match.group(1).strip().lstrip("@")
            if not target:
                self.send_private_text(sender, "❌ الصيغة: vipmas@اسم المستخدم")
                return True
            if _norm_user(target) in {_norm_user(BOT_MASTER), _norm_user(BOT_ID)}:
                self.send_private_text(sender, "⚠️ هذا الحساب لا يحتاج إلى إضافة ماستر كامل.")
                return True
            rows = _vip_full_master_list()
            if any(_norm_user(x) == _norm_user(target) for x in rows):
                self.send_private_text(sender, f"⚠️ @{target} لديه صلاحية الماستر الكامل بالفعل.")
                return True
            rows.append(target)
            _save_local_json(VIP_FULL_MASTERS_FILE, rows)
            self.send_private_text(sender, f"✅ تم إضافة @{target} كـماستر كامل؛ يستطيع استخدام صلاحيات الماستر الأساسي.")
            return True

        match = re.fullmatch(r"-vipmas@(.+)", text, re.I)
        if match:
            if not is_private or not _is_bot_owner(sender):
                self.send_private_text(sender, "🔒 سحب الماستر الكامل متاح للمالك الأساسي في الخاص فقط.")
                return True
            target = match.group(1).strip().lstrip("@")
            if not target:
                self.send_private_text(sender, "❌ الصيغة: -vipmas@اسم المستخدم")
                return True
            rows = _vip_full_master_list()
            remaining = [x for x in rows if _norm_user(x) != _norm_user(target)]
            if len(remaining) == len(rows):
                self.send_private_text(sender, f"ℹ️ @{target} لا يملك صلاحية ماستر كامل.")
                return True
            _save_local_json(VIP_FULL_MASTERS_FILE, remaining)
            self.send_private_text(sender, f"✅ تم سحب صلاحية الماستر الكامل من @{target}.")
            return True

        match = re.fullmatch(r"unvipmas@(.+)", text, re.I)
        if match:
            if not is_private or not _is_bot_owner(sender):
                self.send_private_text(sender, "🔒 سحب الماستر الكامل متاح للمالك الأساسي في الخاص فقط.")
                return True
            target = match.group(1).strip().lstrip("@")
            rows = [x for x in _vip_full_master_list() if _norm_user(x) != _norm_user(target)]
            _save_local_json(VIP_FULL_MASTERS_FILE, rows)
            self.send_private_text(sender, f"✅ تم سحب صلاحية الماستر الكامل من @{target}.")
            return True

        if low == "l@vipmas":
            if not _is_primary_master(sender):
                self.send_private_text(sender, "🔒 القائمة مخصصة للماستر الكامل.")
                return True
            rows = _vip_full_master_list()
            message = "👑 الماسترات الكاملة\n━━━━━━━━━━━━\n" + ("\n".join(f"• @{x}" for x in rows) if rows else "📭 لا يوجد ماسترات إضافيون.")
            self.send_private_text(sender, message)
            return True

        # Managed wager sequence: 3 consecutive wins, then one loss, per
        # target account. The rule affects only player-vs-player wager results.
        match = re.fullmatch(r"ربح@(.+)", text, re.I)
        if match:
            if not is_private or not _is_primary_master(sender):
                self.send_private_text(sender, "🔒 أمر ربح@ مخصص للماستر الكامل في الخاص فقط.")
                return True
            target = match.group(1).strip().lstrip("@")
            if not target:
                self.send_private_text(sender, "❌ الصيغة: ربح@اسم المستخدم")
                return True
            with _WAGER_ADMIN_RULES_LOCK:
                rules = _load_local_json(WAGER_ADMIN_RULES_FILE, {})
                if not isinstance(rules, dict): rules = {}
                rules[_norm_user(target)] = {"username": target, "enabled": True, "cycle": 0, "wins": 0, "losses": 0, "updated_at": int(time.time())}
                _save_local_json(WAGER_ADMIN_RULES_FILE, rules)
            self.send_private_text(sender, f"✅ تم تفعيل نمط الرهانات لـ@{target}: 3 انتصارات ثم خسارة واحدة بالتتابع.")
            return True

        match = re.fullmatch(r"-ربح@(.+)", text, re.I)
        if match:
            if not is_private or not _is_primary_master(sender):
                self.send_private_text(sender, "🔒 أمر -ربح@ مخصص للماستر الكامل في الخاص فقط.")
                return True
            target = match.group(1).strip().lstrip("@")
            if not target:
                self.send_private_text(sender, "❌ الصيغة: -ربح@اسم المستخدم")
                return True
            with _WAGER_ADMIN_RULES_LOCK:
                rules = _load_local_json(WAGER_ADMIN_RULES_FILE, {})
                if not isinstance(rules, dict): rules = {}
                entry = rules.get(_norm_user(target))
                if isinstance(entry, dict):
                    entry["enabled"] = False
                    entry["updated_at"] = int(time.time())
                _save_local_json(WAGER_ADMIN_RULES_FILE, rules)
            self.send_private_text(sender, f"✅ تم إلغاء نمط الربح عن @{target}.")
            return True

        match = re.fullmatch(r"سحب ربح@(.+)", text, re.I)
        if match:
            if not is_private or not _is_primary_master(sender):
                self.send_private_text(sender, "🔒 أمر سحب ربح@ مخصص للماستر الكامل في الخاص فقط.")
                return True
            target = match.group(1).strip().lstrip("@")
            with _WAGER_ADMIN_RULES_LOCK:
                rules = _load_local_json(WAGER_ADMIN_RULES_FILE, {})
                if not isinstance(rules, dict): rules = {}
                entry = rules.get(_norm_user(target))
                if isinstance(entry, dict): entry["enabled"] = False; entry["updated_at"] = int(time.time())
                _save_local_json(WAGER_ADMIN_RULES_FILE, rules)
            self.send_private_text(sender, f"✅ تم إيقاف نمط الرهانات عن @{target}.")
            return True

        delete_text_commands = {
            "حذف ترحيب نص", "حذف الترحيب نص", "حذف ترحيب النص", "حذف الترحيب النص",
            "حذف ترحيب نصي", "حذف الترحيب نصي", "حذف الترحيب النصي",
            "delete welcome text", "remove welcome text",
        }
        delete_image_commands = {
            "حذف ترحيب صورة", "حذف الترحيب صورة", "حذف ترحيب الصورة", "حذف الترحيب الصورة",
            "حذف ترحيب صوره", "حذف الترحيب صوره", "حذف ترحيب الصوره", "حذف الترحيب الصوره",
            "delete welcome image", "remove welcome image",
        }
        delete_all_commands = {
            "حذف ترحيب", "حذف الترحيب", "إلغاء الترحيب", "الغاء الترحيب",
            "delete welcome", "remove welcome",
        }
        cancel_welcome_purchase_commands = {
            "إلغاء شراء الترحيب", "الغاء شراء الترحيب",
            "إلغاء شراء الترحيب المخصص", "الغاء شراء الترحيب المخصص",
            "cancel welcome purchase",
        }
        delete_scope = (
            "text" if low in delete_text_commands else
            "image" if low in delete_image_commands else
            "all" if low in delete_all_commands else
            "purchase" if low in cancel_welcome_purchase_commands else None
        )
        if delete_scope:
            key = _norm_user(sender)
            welcomes = getattr(self, "custom_welcomes", None)
            if not isinstance(welcomes, dict):
                welcomes = self.custom_welcomes = {}
            pending_text = getattr(self, "custom_welcome_text_pending", None)
            pending_image = getattr(self, "custom_welcome_image_pending", None)
            if not isinstance(pending_text, dict):
                pending_text = self.custom_welcome_text_pending = {}
            if not isinstance(pending_image, dict):
                pending_image = self.custom_welcome_image_pending = {}

            if delete_scope == "purchase":
                removed_unlocks = member_shop.remove_welcome_unlocks(
                    sender,
                    user_id=_stored_user_id(sender, room),
                    aliases=_stored_user_aliases(sender, room),
                )
                removed_pending = pending_text.pop(key, None) is not None
                removed_pending = pending_image.pop(key, None) is not None or removed_pending
                entry = welcomes.pop(key, None)
                removed_content = bool(
                    isinstance(entry, dict) and (entry.get("message") or entry.get("image_url"))
                )
                self._save_social_features()
                removed = bool(removed_unlocks or removed_pending or removed_content)
                reply = (
                    "✅ أُلغي شراء ميزات الترحيب وحُذفت إعداداتها المحفوظة."
                    if removed else
                    "ℹ️ لا توجد ميزة ترحيب مشتراة أو إعدادات محفوظة لهذا الحساب."
                )
            else:
                removed = False
                if delete_scope in {"all", "text"}:
                    removed = pending_text.pop(key, None) is not None or removed
                if delete_scope in {"all", "image"}:
                    removed = pending_image.pop(key, None) is not None or removed
                entry = welcomes.get(key)
                if isinstance(entry, dict):
                    if delete_scope in {"all", "text"}:
                        removed = bool(entry.pop("message", None)) or removed
                    if delete_scope in {"all", "image"}:
                        removed = bool(entry.pop("image_url", None)) or removed
                    if not entry.get("message") and not entry.get("image_url"):
                        welcomes.pop(key, None)
                elif entry is not None and delete_scope in {"all", "text"}:
                    welcomes.pop(key, None)
                    removed = True
                self._save_social_features()
                if delete_scope == "text":
                    reply = "✅ حُذف ترحيبك النصي." if removed else "ℹ️ لا يوجد ترحيب نصي محفوظ أو قيد الإعداد."
                elif delete_scope == "image":
                    reply = "✅ حُذفت صورة ترحيبك." if removed else "ℹ️ لا توجد صورة ترحيب محفوظة أو قيد الإعداد."
                else:
                    reply = (
                        "✅ حُذف الترحيب النصي والصوري المحفوظ. ما زالت ميزات المتجر مفعّلة."
                        if removed else "ℹ️ لا يوجد ترحيب محفوظ أو إعداد قيد الانتظار."
                    )
            self.send_private_text(sender, reply)
            return True

        enable_name_aliases = {
            "تشغيل الاسما", "تشغيل الاسماء", "تشغيل الأسماء", "تشغيل الاسم",
            "تشغيل الأسماء المعدلة", "تشغيل الاسماء المعدله",
        }
        disable_name_aliases = {
            "ايقاف الاسما", "إيقاف الاسما", "ايقاف الاسماء", "إيقاف الأسماء",
            "ايقاف الاسم", "إيقاف الاسماء المعدلة", "إيقاف الأسماء المعدلة",
        }
        if low in enable_name_aliases | disable_name_aliases:
            if not _is_primary_master(sender):
                reply = "🔒 تشغيل وإيقاف الأسماء المعدلة مخصص للماستر الأساسي فقط."
            else:
                enabled = low in enable_name_aliases
                _set_image_name_corrections_enabled(enabled)
                reply = (
                    "✅ تم تشغيل استخدام الأسماء المعدلة المحفوظة في ملف التصحيحات."
                    if enabled else
                    "⏸️ تم إيقاف استخدام الأسماء المعدلة. ستظهر الأسماء كما تصل من الشات، ولم تُحذف التصحيحات المحفوظة."
                )
            if sender:
                if room and not is_private:
                    self.send_room_text(room, reply)
                else:
                    self.send_private_text(sender, reply)
            return True

        name_image_commands = {
            "فحص اسم": "preview",
            "اصلاح اسم": "repair", "إصلاح اسم": "repair",
            "تعديل اسم": "edit", "تعديل الاسم": "edit",
            "حذف تصحيح اسم": "delete", "حذف تصحيح الاسم": "delete",
            "حذف اسم": "delete", "حذفاسم": "delete",
            "حذف تصحييح اسم": "delete", "حذف تصحييح الاسم": "delete",
        }
        name_image_mode = name_image_commands.get(low)
        if name_image_mode:
            if not _is_primary_master(sender):
                message = "🔒 هذا الأمر مخصص للماستر المحدد في BOT_MASTER، ويمكن استخدامه من الخاص أو داخل الغرفة."
                if sender:
                    self.send_room_text(room, message) if room and not is_private else self.send_private_text(sender, message)
                return True
            requests_by_master = getattr(self, "_master_name_image_requests", None)
            if not isinstance(requests_by_master, dict):
                requests_by_master = self._master_name_image_requests = {}
            requests_by_master[_norm_user(sender)] = {
                "mode": name_image_mode, "created_at": time.time(),
                "is_private": bool(is_private), "room": str(room or ""),
            }
            prompt = (
                "أرسل اسم المستخدم الحقيقي المراد تعديلُه كما يظهر في المحادثة (من دون @). "
                "بعد ذلك سأطلب منك نص التعديل اليدوي، وعند «اعتماد الاسم» سأضيف التصحيح أو أحدّث تصحيح هذا المستخدم فقط."
                if name_image_mode == "edit" else
                "أرسل اسم المستخدم الذي تريد حذف تصحيحه (من دون @). سيعود للظهور كما يصل من المحادثة."
                if name_image_mode == "delete" else
                "أرسل اسم المستخدم الأساسي كما يظهر في المحادثة (من دون @). "
                "بعد ذلك سأطلب منك تعديل الاسم، ثم أرسل المعاينة ولا يُحفظ التعديل إلا بعد إرسال «اعتماد الاسم»."
                if name_image_mode == "repair" else
                "أرسل الاسم الذي تريد فحص كتابته في صورة (من دون @)."
            )
            self.send_room_text(room, prompt) if room and not is_private else self.send_private_text(sender, prompt)
            return True

        name_requests = getattr(self, "_master_name_image_requests", {})
        pending_name = name_requests.get(_norm_user(sender)) if isinstance(name_requests, dict) else None
        if isinstance(pending_name, dict) and _is_primary_master(sender):
            request_private = bool(pending_name.get("is_private", True))
            request_room = str(pending_name.get("room") or room or "")
            def _name_text(message):
                if request_private:
                    self.send_private_text(sender, message)
                elif request_room:
                    self.send_room_text(request_room, message)
            def _name_media(url):
                if request_private:
                    return self.send_private_media(sender, url, "image")
                return self.send_room_media(request_room, url, "image") if request_room else False
            if time.time() - float(pending_name.get("created_at") or 0) > 180:
                name_requests.pop(_norm_user(sender), None)
                _name_text("⌛ انتهت مهلة طلب الاسم. أرسل «اصلاح اسم» أو «فحص اسم» للبدء مجددًا.")
                return True
            if low in {"إلغاء", "الغاء", "cancel"}:
                name_requests.pop(_norm_user(sender), None)
                _name_text("تم إلغاء طلب فحص الاسم.")
                return True
            if pending_name.get("mode") == "awaiting_method":
                manual_match = re.fullmatch(r"(?:تعديل الاسم|النص الصحيح)\s*[:：]\s*(.+)", text, re.S)
                if manual_match:
                    candidate_name = manual_match.group(1).strip()
                else:
                    # Accept the corrected spelling as a plain next message
                    # too.  The old flow required a prefix and consequently
                    # rejected a perfectly valid new name that differed from
                    # the original source name.
                    candidate_name = _clean_name_for_image(text).strip()
                if not candidate_name or len(candidate_name) > 256:
                    _name_text(
                        "أرسل الاسم الجديد مباشرة، أو بصيغة:\n"
                        "«تعديل الاسم: النص الصحيح»\n"
                        "بعد المعاينة أرسل «اعتماد الاسم» للحفظ."
                    )
                    return True
                pending_name["mode"] = "repair_ready"
                pending_name["created_at"] = time.time()
                text = candidate_name
            if pending_name.get("mode") == "delete":
                username = _clean_name_for_image(text).strip()
                if not username or len(username) > 256:
                    _name_text("❌ أرسل اسم المستخدم من 1 إلى 256 حرفًا، أو اكتب إلغاء.")
                    return True
                deleted = _delete_image_name_correction(username)
                name_requests.pop(_norm_user(sender), None)
                _name_text(
                    "✅ تم حذف تصحيح الاسم، وسيظهر المستخدم كما في المحادثة."
                    if deleted else "ℹ️ لا يوجد تصحيح محفوظ لهذا المستخدم، وسيظهر كما في المحادثة."
                )
                return True
            if pending_name.get("mode") == "awaiting_approval":
                if low in {
                    "اعتماد الاسم", "اعتمد الاسم", "اعتماد التعديل", "اعتمد التعديل",
                    "اعتماد التصحيح", "اعتمد التصحيح", "اعتماد", "نعم", "yes",
                }:
                    try:
                        _save_image_name_correction(
                            pending_name.get("source_name", ""),
                            pending_name.get("candidate_name", ""),
                        )
                        name_requests.pop(_norm_user(sender), None)
                        _name_text("✅ تم اعتماد تصحيح الاسم وحفظه. سيظهر بالصيغة المعتمدة في بطاقات الصور والترحيب القادمة.")
                    except Exception as exc:
                        _name_text(f"❌ تعذر حفظ تصحيح الاسم: {exc}")
                    return True
                edit_match = re.fullmatch(r"(?:تعديل الاسم|النص الصحيح)\s*[:：]\s*(.+)", text, re.S)
                if edit_match:
                    candidate_name = _clean_name_for_image(edit_match.group(1)).strip()
                    if not candidate_name or len(candidate_name) > 256:
                        _name_text("❌ أرسل الاسم المصحح من 1 إلى 256 حرفًا.")
                        return True
                    try:
                        card = render_name_preview_card(candidate_name, corrected=True)
                        base = _public_base_url()
                        if not base:
                            raise RuntimeError("رابط الوسائط العامة غير مضبوط")
                        url = f"{base}/welcomes/{card.name}"
                        self._verify_public_media_url(url, "image")
                        if _name_media(url) is False:
                            raise RuntimeError("تعذر إرسال صورة المعاينة")
                        pending_name["candidate_name"] = candidate_name
                        pending_name["created_at"] = time.time()
                        _name_text("أرسلت معاينة النص البديل. إذا كانت صحيحة أرسل «اعتماد الاسم»، أو أرسل تعديلًا آخر، أو «إلغاء».")
                    except Exception as exc:
                        _name_text(f"❌ تعذر إنشاء معاينة الاسم: {exc}")
                    return True
                _name_text("بعد المعاينة أرسل «اعتماد الاسم»، أو «تعديل الاسم: النص الصحيح»، أو «إلغاء».")
                return True
            display_name = _clean_name_for_image(text).strip()
            if not display_name or len(display_name) > 256:
                _name_text("❌ أرسل اسمًا من 1 إلى 256 حرفًا، أو اكتب إلغاء.")
                return True
            mode = str(pending_name.get("mode") or "preview")
            if mode in {"repair", "edit"}:
                pending_name.update(
                    mode="awaiting_method", source_name=display_name,
                    candidate_name=display_name, created_at=time.time(),
                )
                _name_text(
                    "✅ تم استلام اسم المستخدم الأساسي. أرسل الاسم الجديد مباشرة، أو بصيغة:\n"
                    "«تعديل الاسم: النص الصحيح»\n"
                    "بعد المعاينة أرسل «اعتماد الاسم» للحفظ."
                )
                return True
            name_requests.pop(_norm_user(sender), None)
            try:
                card = render_name_preview_card(display_name, corrected=(mode in {"repair", "repair_ready"}))
                base = _public_base_url()
                if not base:
                    raise RuntimeError("رابط الوسائط العامة غير مضبوط")
                url = f"{base}/welcomes/{card.name}"
                self._verify_public_media_url(url, "image")
                if _name_media(url) is False:
                    raise RuntimeError("تعذر إرسال صورة المعاينة")
                if mode in {"repair", "repair_ready"}:
                    name_requests[_norm_user(sender)] = {
                        "mode": "awaiting_approval",
                        # Keep the original chat username as the lookup key;
                        # the candidate may intentionally be completely
                        # different from it.
                        "source_name": pending_name.get("source_name") or display_name,
                        "candidate_name": display_name,
                        "created_at": time.time(),
                        "is_private": request_private, "room": request_room,
                    }
                _name_text(
                    "✅ أرسلت المعاينة. أرسل «اعتماد الاسم» لحفظها واستخدامها في الصور القادمة، أو «تعديل الاسم: النص الصحيح» لإنشاء بديل، أو «إلغاء»."
                    if mode == "repair" else "✅ أرسلت صورة فحص الاسم.",
                )
            except Exception as exc:
                _name_text(f"❌ تعذر إنشاء صورة الاسم: {exc}")
            return True

        if low in {"فحص زرع", "فحص الزرع", "farm check", "check farm"}:
            return self._farm_check_command(sender, english=low in {"farm check", "check farm"})

        pending_text = getattr(self, "custom_welcome_text_pending", {})
        pending_key = _norm_user(sender)
        pending_welcome = pending_text.get(pending_key) if isinstance(pending_text, dict) else None
        if isinstance(pending_welcome, dict):
            if time.time() - float(pending_welcome.get("created_at") or 0) > 120:
                pending_text.pop(pending_key, None)
                self.send_private_text(sender, "⌛ انتهت مهلة إرسال نص الترحيب. أرسل شراء 5 لبدء الإعداد من جديد.")
                return True
            if low in {"إلغاء", "الغاء", "cancel"}:
                pending_text.pop(pending_key, None)
                self.send_private_text(sender, "تم إلغاء إعداد الترحيب النصي.")
                return True
            if text:
                if not member_shop.active(
                    "custom_welcome", username=sender,
                    user_id=_stored_user_id(sender, room),
                    aliases=_stored_user_aliases(sender, room),
                ):
                    pending_text.pop(pending_key, None)
                    self.send_private_text(sender, "🔒 ميزة الترحيب النصي غير مفعّلة لهذا الحساب.")
                    return True
                command_welcome = re.fullmatch(r"(?:ترحيب|welcome)@(.+)", text, re.I | re.S)
                welcome = (command_welcome.group(1).strip() if command_welcome else text)[:300]
                current = getattr(self, "custom_welcomes", {}).get(pending_key)
                if not isinstance(current, dict):
                    current = {"username": sender}
                current["username"] = sender
                current["message"] = welcome
                self.custom_welcomes[pending_key] = current
                self.custom_welcome_enabled = True
                self._save_social_features()
                pending_text.pop(pending_key, None)
                self.send_private_text(sender, "✅ تم حفظ نص الترحيب المخصص. للحذف: حذف ترحيب النص، أو حذف ترحيب لحذف النص والصورة.")
                return True

        result_toggle = re.fullmatch(r"sf@(on|off)", text, re.I)
        if result_toggle:
            enable_results = result_toggle.group(1).casefold() == "on"
            role, role_known = _room_role(self, room, sender)
            role_key = re.sub(r"[\s_-]+", "", role.casefold())
            owner_role = role_known and role_key in {"owner", "creator", "roomowner", "roomcreator", "host", "مالك", "صانع", "صانعي", "صانعالغرفة", "صانعالغرفه"}
            is_creator = bool(room and _is_room_creator(room, sender))
            is_primary_master = _is_primary_master(sender)
            if not room:
                if is_private:
                    self.send_private_text(sender, "📍 نفّذ sf@on أو sf@off داخل الغرفة المطلوبة؛ الإعداد خاص بكل غرفة فقط.")
                return True
            if not (is_primary_master or is_creator or owner_role):
                self.send_room_text(room, "🔒 الأمر لمالك الغرفة أو صانعها أو الماستر الأساسي فقط.")
                return True
            data = _game_control_data()
            disabled = [str(value).strip() for value in data.get("results_disabled_rooms", []) if str(value).strip()]
            if enable_results:
                disabled = [value for value in disabled if _norm_room(value) != _norm_room(room)]
            elif all(_norm_room(value) != _norm_room(room) for value in disabled):
                disabled.append(str(room).strip())
            data["results_disabled_rooms"] = disabled
            # Clear the legacy global switch; result controls are room-scoped.
            data["all_room_results_enabled"] = True
            _save_game_control(data)
            response = (
                "✅ تم تشغيل صور نتائج الألعاب في هذه الغرفة فقط."
                if enable_results else
                "⛔ تم إيقاف صور نتائج الألعاب في هذه الغرفة فقط."
            )
            self.send_room_text(room, response)
            return True

        if low in ("مستويات", "مستوياتي"):
            item_lines = []
            tier_titles = {
                "النجوم الفضية": "المستوى الفضي",
                "النجوم الذهبية": "المستوى الذهبي",
                "الماس الأزرق": "المستوى الماسي الأزرق",
                "الماس البرونزي": "المستوى الماسي البرونزي",
                "الماس الأحمر": "المستوى الماسي الأحمر",
                "الماس الأسود": "المستوى الماسي الأسود",
                "الماس البنفسجي": "المستوى الماسي البنفسجي",
                "الماس الملكي": "المستوى الماسي الملكي",
                "الماس الخالد": "المستوى الماسي الخالد",
                "الماس الكوني": "المستوى الماسي الكوني",
            }
            category_count = len(GAME_LEVEL_CATEGORIES) + len(GAME_LEVEL_FUTURE_CATEGORIES)
            for tier_index in range(category_count):
                tier_start = tier_index * 5000
                _category_index, _level_number, category, _title, symbol, _plays = _game_level_details_from_plays(tier_start)
                tier_title = tier_titles.get(category, f"المستوى {category}")
                item_lines.extend((tier_title, symbol * 10))
                for level_in_tier in range(1, 11):
                    plays = tier_start + (level_in_tier - 1) * 500
                    _category_index, _level_number, _category, title, _level_symbol, _plays = _game_level_details_from_plays(plays)
                    item_lines.append(f"مستوى {level_in_tier}: {title}")
            headings = ["📚 مستويات تقدم الألعاب", "كل مستوى يحتاج 500 لعبة"]
            packet_type = "chat_message" if is_private else "room_message"
            route_fields = {"to": str(sender or "")} if is_private else {"room": str(room or "")}
            pages = _safe_text_pages_from_items(
                packet_type, route_fields, headings, item_lines,
                max_items_per_page=12,
                language="ar", marker_first=True,
            )
            self._send_paginated_text_pages(sender, room, packet_type, pages, route_fields, language="ar")
            return True

        if self._room_list_commands(room, text, sender, is_private):
            return True

        money_toggle_commands = {
            "فتح المليون": ("million", True, "المليون"),
            "غلق المليون": ("million", False, "المليون"),
            "فتح مليار": ("billion", True, "المليار"),
            "غلق مليار": ("billion", False, "المليار"),
            "فتح المليار": ("billion", True, "المليار"),
            "غلق المليار": ("billion", False, "المليار"),
        }
        money_toggle = money_toggle_commands.get(low)
        if money_toggle:
            game_name, enabled, label = money_toggle
            if not _is_master_name(sender):
                self.send_private_text(sender, "🔒 أوامر فتح وإغلاق المليون والمليار مخصصة للماستر فقط.")
                return True
            _set_money_game_open(game_name, enabled)
            self.send_private_text(
                sender,
                f"✅ تم فتح {label}. نسبة الربح الآن {MONEY_GAME_OPEN_WIN_CHANCE_PERCENT}%."
                if enabled else
                f"✅ تم غلق {label}. عادت نسبة الربح إلى {MONEY_GAME_WIN_CHANCE_PERCENT}%.",
            )
            return True

        departure_toggle = re.fullmatch(r"(?:ds|توديع|وداع|farewell)@(on|off)", text, re.I)
        if departure_toggle:
            if not _is_master_name(sender):
                self.send_private_text(sender, "🔒 تشغيل أو إيقاف توديع المغادرين مخصص للماستر.")
                return True
            self.departure_farewell_enabled = departure_toggle.group(1).casefold() == "on"
            self._save_departure_replies()
            self.send_private_text(
                sender,
                "✅ تم تشغيل رسالة الوداع الافتراضية." if self.departure_farewell_enabled
                else "⛔ تم إيقاف رسالة الوداع الافتراضية؛ مفاتيح ب/ح/ق والردود المخصصة ما زالت تعمل.",
            )
            return True

        # Master-only recovery command. Use @ separators so decorated names
        # remain intact: إضافة العاب@اسم_النك@3890
        m_restore_games = re.fullmatch(
            r"(?:إضافة|اضافة|استرجاع|استعادة)\s+(?:العاب|ألعاب)@([^@]+)@(\d+)",
            text, re.I,
        )
        m_increase_games = re.fullmatch(
            r"(?:زيادة|زود)\s+(?:العاب|ألعاب)@([^@]+)@(\d+)",
            text, re.I,
        )
        if m_restore_games or m_increase_games:
            if not _is_primary_master(sender):
                self.send_private_text(sender, "🔒 أمر استعادة عدد الألعاب مخصص للماستر الأساسي فقط.")
                return True
            match = m_restore_games or m_increase_games
            target = match.group(1).strip().lstrip("@")
            amount = int(match.group(2))
            current, total = _restore_game_play_count(
                target, amount, increment=bool(m_increase_games)
            )
            action = "زيادة" if m_increase_games else "استعادة"
            self.send_private_text(
                sender,
                f"✅ تمت {action} ألعاب @{target}\n"
                f"🎮 العدد السابق: {current}\n"
                f"🎮 العدد الحالي: {total}\n"
                f"🏅 المستوى الحالي: {_game_level_info(target)[0]}"
            )
            return True

        # Master runtime cleanup: تنضيف / تنظيف. This never deletes persistent
        # JSON state; it only removes disposable files and prunes memory caches.
        if low in ("تنضيف", "تنظيف", "cleanup", "clean"):
            if not _is_master_name(sender):
                self.send_private_text(sender, "🔒 أمر التنضيف مخصص للماستر فقط.")
                return True
            stats = _cleanup_runtime_files(bot=self) or {}
            self.send_private_text(
                sender,
                "🧹 تم تنفيذ التنضيف الآمن.\n"
                f"📁 ملفات محذوفة: {int(stats.get('files', 0) or 0)}\n"
                f"📂 مجلدات محذوفة: {int(stats.get('dirs', 0) or 0)}\n"
                f"🧠 تنظيف الذاكرة: {'تم' if stats.get('memory') else 'لا يوجد ما يحتاج تنظيفاً'}\n"
                "🔒 بيانات البوت الدائمة لم يتم لمسها."
            )
            return True

        # Protection status: report actual persisted room protection switches.
        if low in ("حالة الحماية", "حاله الحمايه", "حالة حماية", "حاله حماية", "protection status", "room protection status"):
            target_room = str(room or self.room or "").strip()
            if not target_room:
                self.send_private_text(sender, "⚠️ لا توجد غرفة محددة لفحص الحماية.")
                return True
            if not (_is_master_name(sender) or _room_manager(self, target_room, sender)):
                return True
            pcfg = _room_protection_cfg(target_room)
            mcfg = _room_moderation_config(target_room)
            bot_prot = bool(getattr(self, "bot_protection_enabled", _bot_protection_enabled()))
            if self._effective_room_language(target_room) == "en":
                protection_state = lambda enabled: "🟢 Enabled" if enabled else "🔴 Disabled"
                status_text = (
                    f"🛡️ Protection status — {target_room}\n"
                    f"• Room protection: {protection_state(mcfg.get('enabled'))}\n"
                    f"• Profanity filter: {protection_state(pcfg.get('swear'))}\n"
                    f"• Repeated-message protection: {protection_state(pcfg.get('repeat'))}\n"
                    f"• Join/leave protection: {protection_state(pcfg.get('flood'))}\n"
                    f"• Repetition limit: {int(mcfg.get('repeat_limit', pcfg.get('repeat_limit', 11)) or 11)}\n"
                    f"• Internal bot protection: {protection_state(bot_prot)}"
                )
            else:
                status_text = (
                    f"🛡️ حالة الحماية — {target_room}\n"
                    f"• حماية الغرفة: {'🟢 شغالة' if mcfg.get('enabled') else '🔴 متوقفة'}\n"
                    f"• فلتر الكلمات: {'🟢 شغال' if pcfg.get('swear') else '🔴 متوقف'}\n"
                    f"• حماية التكرار: {'🟢 شغالة' if pcfg.get('repeat') else '🔴 متوقفة'}\n"
                    f"• حماية الدخول/الخروج: {'🟢 شغالة' if pcfg.get('flood') else '🔴 متوقفة'}\n"
                    f"• حد التكرار: {int(mcfg.get('repeat_limit', pcfg.get('repeat_limit', 11)) or 11)}\n"
                    f"• حماية البوت الداخلية: {'🟢 شغالة' if bot_prot else '🔴 متوقفة'}"
                )
            self.send_private_text(sender, status_text)
            return True

        # Overall bot health/status for the master. This is diagnostic, not a
        # generic success message, and reports the live state the bot knows.
        if low in ("حالة البوت", "حاله البوت", "status", "bot status"):
            ws_state = "🟢 متصل" if getattr(self, "ws", None) is not None and getattr(self.ws, "sock", None) is not None else "🔴 غير متصل"
            master_state = "🟢 مشغل" if _master_process_running() else "🔴 متوقف"
            db_state = "🟢 متصل" if getattr(self, "db", None) is not None and getattr(self.db, "client", None) is not None else "🔴 غير متاح"
            self.send_private_text(sender, f"📊 حالة البوت\n• WebSocket: {ws_state}\n• خدمة الماستر: {master_state}\n• قاعدة البيانات: {db_state}\n• تنظيف تلقائي: {'🟢 شغال' if CLEANUP_MEMORY_ENABLED else '🔴 متوقف'}\n• حماية البوت: {'🟢 شغالة' if getattr(self, 'bot_protection_enabled', False) else '🔴 متوقفة'}")
            return True

        if low in ("رام", "ram", "cpu", "الموارد", "حالة الموارد", "حاله الموارد", "resource status"):
            metrics = _runtime_resource_status()
            ram_scope = "حد الحاوية" if metrics["ram_scope"] == "container" else "المضيف؛ حد الحاوية غير متاح"
            cpu_scope = "حد الحاوية" if metrics["cpu_scope"] == "container" else "الأنوية الظاهرة للنظام"
            self.send_private_text(
                sender,
                "🖥️ موارد بيئة تشغيل البوت\n"
                f"• RAM الكلية ({ram_scope}): {metrics['ram_total_mb']:.0f} MB\n"
                f"• RAM المستخدمة: {metrics['ram_used_mb']:.0f} MB ({metrics['ram_used_pct']:.1f}%)\n"
                f"• RAM المتاحة: {metrics['ram_available_mb']:.0f} MB\n"
                f"• CPU المتاح ({cpu_scope}): {metrics['cpu_count']:g} نواة\n"
                f"• حمل المضيف (1/5/15 دقيقة): {metrics['load_1']:.2f} / {metrics['load_5']:.2f} / {metrics['load_15']:.2f}\n"
                f"• حمل المضيف لكل نواة: {metrics['load_pct_per_core']:.1f}%\n"
                f"• CPU داخل البوت: {getattr(self, '_cpu_last_percent', 0.0):.1f}%\n"
                f"• التبريد التلقائي: {'🟡 فعّال' if getattr(self, '_cpu_cooling', False) else '🟢 جاهز'}\n"
                f"• رسائل السجل المخفّضة: {getattr(self, '_cpu_log_suppressed', 0)}"
            )
            return True

        # Master-only room broadcast: رسالهغرف@النص / رسالةغرف@النص.
        m_room_broadcast = re.fullmatch(r"(?:رسالهغرف|رسالةغرف|رساله\s+غرفه|رسالة\s+غرفه|room\s+message)@(.+)", text, re.I | re.S)
        if m_room_broadcast:
            if not _is_primary_master(sender):
                self.send_private_text(sender, "🔒 هذا الأمر مخصص للماستر الأساسي فقط.")
                return True
            message_text=m_room_broadcast.group(1).strip()
            if not message_text:
                self.send_private_text(sender, "❌ الصيغة: رساله غرفه@نص الرسالة")
                return True
            english = text.casefold().startswith("room message@")
            if self._queue_room_broadcast(message_text, sender):
                ack = ("⏳ Your request is being handled in the background; a delivery report will follow."
                       if english else "⏳ جاري تلبية طلبك... سيتم إبلاغك بعد انتهاء الإرسال إلى كل الغرف.")
                self.send_private_text(sender, ack)
            else:
                busy = ("⏳ The room broadcast queue is full; please retry shortly."
                        if english else "⏳ طابور بث الغرف ممتلئ حاليًا؛ أعد الأمر بعد قليل.")
                self.send_private_text(sender, busy)
            return True

        # Ordinary-user private message: رساله@اسم_المستخدم نص الرسالة
        # or: رساله اسم_المستخدم نص الرسالة
        # The message itself is NEVER echoed to the room. The recipient gets
        # a private notification containing the sender, room and message,
        # while the requester gets a private success/failure result.
        m_user_private = re.fullmatch(r"(?:رساله|رسالة)(?:@|\s+)@?([^\s@]+)\s+(.+)", text, re.I | re.S)
        if m_user_private:
            target = m_user_private.group(1).strip().lstrip("@")
            message_text = m_user_private.group(2).strip()
            if not target or not message_text:
                return True

            self._deliver_user_private_message(sender, target, message_text)
            return True

        # Master-only private broadcast: خاص@النص / رسالة@النص / broadcast@النص.
        # Also accept: رساله خاص@النص / رسالة خاص@النص.
        m_broadcast = re.fullmatch(r"(?:خاص|رسالة|رساله|broadcast)@(.+)", text, re.I | re.S)
        if not m_broadcast:
            m_broadcast = re.fullmatch(r"(?:رساله|رسالة)\s+خاص@(.+)", text, re.I | re.S)
        if m_broadcast:
            if not _is_primary_master(sender):
                self.send_private_text(sender, "🔒 هذا الأمر مخصص للماستر الأساسي فقط.")
                return True
            message_text = m_broadcast.group(1).strip()
            if not message_text:
                self.send_private_text(sender, "❌ الصيغة: خاص@نص الرسالة")
                return True
            recipients = _all_known_usernames(self)
            if not recipients:
                self.send_private_text(sender, "⚠️ لم أجد مستخدمين معروفين لإرسال الرسالة لهم.")
                return True
            if self._queue_private_broadcast(recipients, message_text, sender):
                self.send_private_text(
                    sender,
                    "⏳ جاري تلبية طلبك... سيتم إبلاغك بعد انتهاء إرسال الرسالة الخاصة."
                )
            else:
                self.send_private_text(sender, "⏳ يوجد بث خاص قيد التنفيذ؛ أعد المحاولة بعد قليل.")
            return True

        # The protection menu and all of its numbered settings are private and
        # restricted to the configured primary master.
        protection_match = re.fullmatch(r"(?:حماية|حمايه|حماية الغرفة|حمايه الغرفه|protection|room protection)(?:@([^@]+)|\s+(.+))?", text, re.I)
        if protection_match:
            room_manager = _room_protection_manager(self, room, sender)

            if not ((_is_primary_master(sender) and is_private) or room_manager):
                if room and not is_private:
                    self.send_room_text(room, "🚫 قائمة الحماية لصانع الغرفة أو الماستر فقط.")
                else:
                    self.send_private_text(sender, "🚫 قائمة الحماية للماستر الأساسي في الخاص أو لصانع الغرفة داخلها.")
                return True
            # When the menu is opened inside a room, that room is authoritative.
            # A stale last_joined_room must never redirect options 1..9 elsewhere.
            target_room = str(protection_match.group(1) or protection_match.group(2) or
                              room or self.room or getattr(self, "last_joined_room", "") or "").strip()
            if not target_room:
                self.send_private_text(sender, "⚠️ أرسل حماية@اسم_الغرفة من خاص البوت لفتح إعداداتها.")
                return True
            self._pending_protection_number = getattr(self, "_pending_protection_number", {})
            self._pending_protection_number[_norm_user(sender)] = {"room":target_room,"created":time.time(),"is_private":bool(is_private)}
            send_menu = self.send_private_text if is_private else self.send_room_text
            send_menu(sender if is_private else target_room,
                "🛡️ قائمة حماية الغرفة\n"
                "━━━━━━━━━━━━\n"
                "1️⃣ تشغيل حماية الغرفة من السب\n"
                "2️⃣ إيقاف حماية الغرفة من السب\n"
                "3️⃣ تشغيل حماية الفلود (هجوم الدخول المتزامن + حظر IP)\n"
                "4️⃣ إيقاف حماية الفلود\n"
                "5️⃣ تشغيل حماية الدخول والخروج (تكرار الدخول)\n"
                "6️⃣ إيقاف حماية الدخول والخروج\n"
                "7️⃣ تعيين حد رسائل الفلود\n"
                "8️⃣ تشغيل حظر الحسابات بلا صورة\n"
                "9️⃣ إيقاف حظر الحسابات بلا صورة\n"
                "━━━━━━━━━━━━\n"
                "📌 أرسل رقم الخيار الآن.")
            return True

        # Option 7 has priority over numeric menu choices: otherwise a limit
        # such as 3 would accidentally be interpreted as option 3.
        protection_key = _norm_user(sender)
        pending_protection = getattr(self, "_pending_protection_number", None)
        if not isinstance(pending_protection, dict):
            pending_protection = self._pending_protection_number = {}
        st=pending_protection.get(protection_key,{})
        if st.get("awaiting_number") and re.fullmatch(r"\d+",low):
            room_manager = _room_protection_manager(self, room, sender)

            if not ((is_private and _is_primary_master(sender)) or (not is_private and room_manager)):
                return True
            try:
                limit=int(low)
            except Exception:
                limit=0
            if not 2 <= limit <= 50:
                (self.send_private_text if is_private else self.send_room_text)(sender if is_private else room,"⚠️ أرسل رقماً من 2 إلى 50 فقط.")
                return True
            target_room=str(st.get("room") or room or self.room or "").strip()
            _save_room_protection(target_room,repeat_limit=limit)
            _save_room_moderation(target_room,repeat_limit=limit)
            self._pending_protection_number.pop(protection_key,None)
            (self.send_private_text if is_private else self.send_room_text)(sender if is_private else room,f"✅ تم اعتماد حد الفلود: {limit} رسائل متكررة في الغرفة: {target_room}")
            return True

        if protection_key in pending_protection and low.isdigit():
            st=pending_protection.get(protection_key,{})
            room_manager = _room_protection_manager(self, room, sender)

            if not ((is_private and _is_primary_master(sender)) or (not is_private and room_manager)):
                if is_private and _is_primary_master(sender):
                    self.send_private_text(sender, "🔒 أرسل رقم الخيار من خاص البوت أو داخل الغرفة بصفتك صانعاً.")
                return True
            if time.time()-float(st.get("created",0))>180:
                self._pending_protection_number.pop(protection_key,None)
            else:
                n=int(low); target_room=str(st.get("room") or room or self.room or "").strip()
                if n in range(1,10) and n != 7:
                    names={1:("swear",True,"🛡️ تم تشغيل حماية الغرفة من السب."),2:("swear",False,"⛔ تم إيقاف حماية الغرفة من السب."),3:("flood",True,"🛡️ تم تشغيل حماية الغرفة من الفلود."),4:("flood",False,"⛔ تم إيقاف حماية الغرفة من الفلود."),5:("joinleave",True,"🛡️ تم تشغيل حماية الغرفة من الدخول والخروج."),6:("joinleave",False,"⛔ تم إيقاف حماية الغرفة من الدخول والخروج."),8:("no_photo",True,"🛡️ تم تشغيل حظر الحسابات بلا صورة."),9:("no_photo",False,"⛔ تم إيقاف حظر الحسابات بلا صورة.")}[n]
                    _save_room_protection(target_room, **{names[0]:names[1]})
                    self._pending_protection_number.pop(protection_key,None)
                    (self.send_private_text if is_private else self.send_room_text)(sender if is_private else room,names[2]+f"\n🏠 الغرفة: {target_room}")
                    return True
                if n==7:
                    self._pending_protection_number[protection_key]={"room":target_room,"created":time.time(),"awaiting_number":True}
                    (self.send_private_text if is_private else self.send_room_text)(sender if is_private else room,"🔢 أرسل عدد الرسائل المتكررة المسموح بها قبل الحظر (من 2 إلى 50).")
                    return True
                (self.send_private_text if is_private else self.send_room_text)(sender if is_private else room,"⚠️ اختر رقماً من 1 إلى 9.")
                return True
        # Filter exceptions: amf@username adds, -amf@username removes, and
        # l@amf lists the accounts that the word filter must never ban.
        m_amf=re.fullmatch(r"([+-]?)amf@(.+)",text,re.I)
        if m_amf:
            if not _is_master_name(sender): return True
            action, target = m_amf.group(1), m_amf.group(2).strip().lstrip("@")
            if target:
                if action == "-":
                    self.filter_exceptions = _save_filter_exception_users(
                        self.filter_exceptions - {_norm_user(target)}
                    )
                    self.send_private_text(sender,f"✅ تمت إزالة @{target} من استثناءات الفلتر.")
                else:
                    self.filter_exceptions=_save_filter_exception_users(self.filter_exceptions|{target})
                    self.send_private_text(sender,f"✅ تمت إضافة @{target} إلى استثناءات الفلتر.")
            return True
        if low == "l@amf":
            if not _is_master_name(sender): return True
            users = sorted(self.filter_exceptions)
            self.send_private_text(sender, "🛡️ مستثنو الفلتر:\n" + ("\n".join(f"@{x}" for x in users) if users else "لا يوجد مستثنون حالياً."))
            return True
        if low == "l@mfb":
            if not _is_master_name(sender): return True
            rows=_filter_bans_list()
            if not rows:
                self.send_private_text(sender,"📭 لا يوجد محظورون من الفلتر.")
            else:
                lines=["🚫 المحظورون من الفلتر:"]
                for i,r in enumerate(rows[-100:],1):
                    lines.append(f"{i}. @{r.get('username','')} | السبب: {r.get('reason','كلمة مسيئة')} | الغرفة: {r.get('room','')}")
                self.send_private_text(sender,"\n".join(lines))
            return True

        if low in ("تشغيل الحماية", "تشغيل الحمايه", "الحماية تشغيل", "الحمايه تشغيل", "enable protection", "enable room protection"):
            if not _room_protection_manager(self, room, sender):

                return True
            cfg = _save_room_moderation(room, enabled=True)
            _save_room_protection(room, swear=True, flood=True)
            self.send_room_text(room, f"🛡️ حماية الغرفة شغالة. حد التكرار: {cfg['repeat_limit']} رسائل.")
            return True
        if low in ("إيقاف الحماية", "ايقاف الحماية", "إيقاف الحمايه", "ايقاف الحمايه", "الحماية إيقاف", "الحمايه ايقاف", "disable protection", "disable room protection"):
            if not _room_protection_manager(self, room, sender):

                return True
            _save_room_moderation(room, enabled=False)
            _save_room_protection(room, swear=False, flood=False)
            self.send_room_text(room, "⛔ حماية الغرفة متوقفة.")
            return True
        m_repeat = re.fullmatch(r"mr@(\d+)", text, re.I)
        if m_repeat:
            if not _room_protection_manager(self, room, sender):

                return True
            limit = max(2, min(50, int(m_repeat.group(1))))
            _save_room_moderation(room, repeat_limit=limit)
            self.send_room_text(room, f"✅ تم ضبط حماية التكرار في {room} على {limit} رسائل متتالية.")
            return True
        if low in ("نسخ احتياطي", "نسخه احتياطيه", "backup", "full backup"):
            queued = _request_github_full_backup(self, sender)
            if queued:
                self.send_private_text(
                    sender,
                    f"⏳ بدأ النسخ الاحتياطي في الخلفية. سيتم تأكيد النجاح بعد رفع {queued} ملفاً إلى GitHub (Talkin4)."
                )
            else:
                self.send_private_text(sender, "⚠️ تعذر بدء النسخ الاحتياطي؛ تأكد من تفعيل GITHUB_SYNC وإعداد مستودع Talkin4.")
            return True
        # Master-only self restart. The reply stays in the same channel where
        # the master issued the command, then the current Python process is replaced.
        if low in (
            "اعاده تشغيل البوت", "اعادة تشغيل البوت", "إعاده تشغيل البوت", "إعادة تشغيل البوت",
            "اعاده تشغيل المنصه", "اعادة تشغيل المنصه", "إعاده تشغيل المنصه", "إعادة تشغيل المنصة",
            "restart bot", "restart", "restart platform",
        ):
            platform_restart = "منصه" in low or "منصة" in low or low == "restart platform"
            reply = "🔄 جاري إعادة تشغيل المنصة..." if platform_restart else "🔄 جاري إعادة تشغيل البوت..."
            if is_private:
                self.send_private_text(sender, reply)
            elif room:
                self.send_room_text(room, reply)
            else:
                self.send_private_text(sender, reply)

            def _restart_process():
                try:
                    time.sleep(1.2)
                    self.stop_event.set()
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                except Exception as exc:
                    self.log("[BOT] restart failed:", repr(exc))
                    try:
                        self.stop_event.clear()
                        if is_private:
                            self.send_private_text(sender, f"❌ تعذر إعادة تشغيل البوت: {exc}")
                        elif room:
                            self.send_room_text(room, f"❌ تعذر إعادة تشغيل البوت: {exc}")
                    except Exception:
                        pass

            threading.Thread(target=_restart_process, name="bot-restart", daemon=True).start()
            return True
        # `اوامر` shows the organized menu only.
        if low in ("اوامر الماستر", "اوامر_الماستر"):
            if not _is_primary_master(sender):
                return False
            target = sender if is_private else None
            menu = _command_menu_for(True, is_private=is_private)
            if target:
                self.send_private_text(target, menu)
            else:
                self.send_room_text(room, menu)
            return True
        if low in ("اوامر","الاوامر","help","مساعدة"):
            target = sender if is_private else None
            menu = _command_menu_for(_is_primary_master(sender), is_private=is_private)
            if target:
                self.send_private_text(target, menu)
            else:
                self.send_room_text(room, menu)
            return True
        m_help = re.fullmatch(r"a([1-6])", low)
        if m_help:
            page=int(m_help.group(1))
            # Everyone may display a2..a6. Only a1 is restricted.
            if page == 1 and (not _is_primary_master(sender) or not is_private):
                return True
            key=(("__private__" if is_private else str(room)), _norm_user(sender))
            self.help_pages[key]=page
            self.help_page_part[key]=1
            self.help_game_part[key]=1
            self._send_help(room=room, private_to=sender if is_private else None, page=page, game_part=1)
            return True
        if low in ("ns","n","التالي","القائمة التالية","next"):
            key=(("__private__" if is_private else str(room)), _norm_user(sender))
            transfer_key = _norm_user(sender)
            transfer_pages = getattr(self, "_transfer_log_pages", {})
            if _is_master_name(sender) and transfer_key in transfer_pages:
                current = max(1, int(transfer_pages.get(transfer_key, 1) or 1))
                english = bool(getattr(self, "_transfer_log_languages", {}).get(transfer_key, False))
                next_page = current + 1
                _rows, _total, page_count = _all_point_transfer_history(
                    page=1, page_size=_POINT_TRANSFER_HISTORY_PAGE_SIZE
                )
                if next_page > page_count:
                    transfer_pages.pop(transfer_key, None)
                    getattr(self, "_transfer_log_languages", {}).pop(transfer_key, None)
                    message = "📒 هذه آخر قائمة في سجل التحويل." if not english else "📒 This is the last transfer-log list."
                else:
                    transfer_pages[transfer_key] = next_page
                    message, _page_count = _format_all_point_transfer_history_page(next_page, english)
                self.send_private_text(sender, message)
                return True
            # Continue paginated non-help results (room lists, saved member
            # lists, and other result sets) before falling back to help menus.
            packet_type = "chat_message" if is_private else "room_message"
            result_key = (packet_type, "" if is_private else str(room or ""), str(sender or ""))
            result_pages = getattr(self, "_result_pages", {})
            result_state = result_pages.get(result_key)
            if not isinstance(result_state, dict) and not is_private:
                # Some commands (including personal logs) deliberately answer
                # in DM even when issued in a room. Let `ns` sent from that room
                # advance the user's private result list instead of losing it.
                private_key = ("chat_message", "", str(sender or ""))
                private_state = result_pages.get(private_key)
                if isinstance(private_state, dict):
                    result_key, result_state, packet_type = private_key, private_state, "chat_message"
            if isinstance(result_state, dict):
                pages = result_state.get("pages") or []
                current = max(1, int(result_state.get("part", 1) or 1))
                next_part = current + 1
                language = result_state.get("language", "ar")
                if next_part <= len(pages):
                    result_state["part"] = next_part
                    message = pages[next_part - 1]
                    if next_part < len(pages):
                        message += "\n\n📌 اكتب ns للقائمة التالية." if language == "ar" else "\n\n📌 Type ns for the next list."
                    else:
                        result_pages.pop(result_key, None)
                        message += "\n\n✅ انتهت القوائم." if language == "ar" else "\n\n✅ End of list."
                else:
                    result_pages.pop(result_key, None)
                    message = "✅ انتهت القوائم." if language == "ar" else "✅ End of list."
                self._result_pages = result_pages
                kwargs = result_state.get("kwargs") or ({"to": str(sender or "")} if is_private else {"room": str(room or "")})
                self._send_text_packets(packet_type, message, **kwargs)
                return True
            # Do not assume a1 when ns is sent without opening a category.
            if key not in self.help_pages:
                target = "a1 إلى a6" if (_is_primary_master(sender) and is_private) else "a2 إلى a6"
                msg=f"📌 اكتب {target} أولًا لفتح قائمة الأوامر، ثم استخدم ns للتالي."
                if is_private:
                    self.send_private_text(sender,msg)
                elif room:
                    self.send_room_text(room,msg)
                return True
            sections_map = _help_sections_from_messages()
            current_page=int(self.help_pages.get(key,1) or 1)
            current_page=max(1,min(6,current_page))
            sections=sections_map.get(current_page, [])
            total=max(1,len(sections))
            part=int(self.help_page_part.get(key, self.help_game_part.get(key,1)) or 1)

            # ns moves only inside the currently opened category.
            # Do not jump from a3 to a2/a4 or between any other categories.
            if part < total:
                part += 1
                self.help_pages[key]=current_page
                self.help_page_part[key]=part
                self.help_game_part[key]=part
                if current_page == 3:
                    self._send_game_help_section(room=room, private_to=sender if is_private else None, part=part)
                else:
                    self._send_help(room=room, private_to=sender if is_private else None, page=current_page, game_part=part)
            else:
                self.help_pages[key]=current_page
                self.help_page_part[key]=part
                self.help_game_part[key]=part
                label={1:"الإدارة",2:"الموسيقى والتفاعلات",3:"الألعاب",
                       4:"الهدايا والنشر",5:"النقاط",6:"الغرف"}.get(current_page,"القائمة")
                msg=f"✅ انتهت قوائم {label}.\n📌 للانتقال إلى قائمة أخرى اكتب a1 إلى a6."
                if is_private:
                    self.send_private_text(sender,msg)
                else:
                    self.send_room_text(room,msg)
            return True
        if low in ("نقاطي","points"):
            self.send_private_text(sender, _points_summary_text(sender))
            return True
        recipient_log = re.fullmatch(
            r"(?:سجل\s+(?:التحويل|تحويل)|transfer\s+(?:history|log))\s*@\s*(.+)",
            text, re.I,
        )
        if recipient_log:
            english = _detect_text_language(text) == "en"
            if not _is_master_name(sender):
                message = "🔒 سجل تحويلات المستخدمين مخصص للماستر فقط." if not english else "🔒 User transfer logs are for the master only."
                self.send_private_text(sender, message)
                return True
            requested = recipient_log.group(1).strip().lstrip("@").strip()
            resolved = _resolve_points_account(_points_data(), requested)
            if not resolved:
                message = (f"❌ لم أجد @{_transfer_log_safe_field(requested)} في ملف النقاط؛ لم يُعرض أي سجل."
                           if not english else f"❌ @{_transfer_log_safe_field(requested)} was not found in the points file; no log was shown.")
                self.send_private_text(sender, message)
                return True
            target = str(resolved[1].get("username") or requested).strip()
            message, pages = _format_all_point_transfer_history_page(1, english, recipient=target)
            transfer_key = _norm_user(sender)
            if not isinstance(getattr(self, "_transfer_log_pages", None), dict):
                self._transfer_log_pages = {}
            if not isinstance(getattr(self, "_transfer_log_languages", None), dict):
                self._transfer_log_languages = {}
            if not isinstance(getattr(self, "_transfer_log_targets", None), dict):
                self._transfer_log_targets = {}
            self._transfer_log_pages[transfer_key] = 1
            self._transfer_log_languages[transfer_key] = english
            self._transfer_log_targets[transfer_key] = target
            self.send_private_text(sender, message)
            return True
        transfer_log = re.fullmatch(r"(?:سجل التحويل|transfer history|transfer log)(?:\s+(\d+))?", text, re.I)
        if transfer_log:
            english = _detect_text_language(text) == "en"
            if not _is_master_name(sender):
                message = "🔒 سجل تحويلات جميع المستخدمين مخصص للماستر فقط." if not english else "🔒 The bot-wide transfer log is for the master only."
                self.send_private_text(sender, message)
                return True
            page = int(transfer_log.group(1) or "1")
            message, pages = _format_all_point_transfer_history_page(page, english)
            if page <= pages:
                transfer_key = _norm_user(sender)
                if not hasattr(self, "_transfer_log_pages") or not isinstance(self._transfer_log_pages, dict):
                    self._transfer_log_pages = {}
                if not hasattr(self, "_transfer_log_languages") or not isinstance(self._transfer_log_languages, dict):
                    self._transfer_log_languages = {}
                if not isinstance(getattr(self, "_transfer_log_targets", None), dict):
                    self._transfer_log_targets = {}
                self._transfer_log_pages[transfer_key] = page
                self._transfer_log_languages[transfer_key] = english
                self._transfer_log_targets.pop(transfer_key, None)
            # The complete cross-user audit log is always private, even when
            # the master invokes it from a room.
            self.send_private_text(sender, message)
            return True
        if low in ("سجل تحويلاتي", "my transfer history"):
            rows = _point_transfer_history(sender, 10)
            english = _detect_text_language(text) == "en"
            if not rows:
                message = "📒 لا توجد تحويلات مسجلة لحسابك حتى الآن." if not english else "📒 No transfers are recorded for your account yet."
            else:
                title = "📒 Transfer history (latest 10)" if english else "📒 سجل تحويلاتك (آخر 10)"
                lines = [title, "━━━━━━━━━━━━"]
                for entry in rows:
                    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(entry.get("timestamp", 0) or 0)))
                    if _norm_user(entry.get("sender")) == _norm_user(sender):
                        detail = f"To @{entry.get('recipient')}" if english else f"إلى @{entry.get('recipient')}"
                    else:
                        detail = f"From @{entry.get('sender')}" if english else f"من @{entry.get('sender')}"
                    lines.append(f"{when} — {detail}: {_fmt_points(entry.get('amount'))}")
                message = "\n".join(lines)
            self.send_private_text(sender, message)
            return True
        game_record_aliases = {
            "سجل المليار": ("billion", "المليار"), "سجل مليار": ("billion", "المليار"),
            "billion record": ("billion", "Billion"), "billion history": ("billion", "Billion"),
            "سجل المليون": ("million", "المليون"), "سجل مليون": ("million", "المليون"),
            "million record": ("million", "Million"), "million history": ("million", "Million"),
        }
        if low in game_record_aliases:
            game_key, label = game_record_aliases[low]
            stats = _game_stats(sender, game_key)
            english = _detect_text_language(text) == "en"
            if english:
                message = (f"📊 {label} game record for @{sender}\n━━━━━━━━━━━━\n"
                           f"🎮 Plays: {stats['plays']}\n💰 Net points: {_fmt_points(stats['points'])}\n"
                           f"🎟️ Staked: {_fmt_points(stats['staked'])}")
            else:
                message = (f"📊 سجل لعبة {label} لـ @{sender}\n━━━━━━━━━━━━\n"
                           f"🎮 عدد اللعب: {stats['plays']}\n💰 صافي النقاط: {_fmt_points(stats['points'])}\n"
                           f"🎟️ إجمالي الرهانات: {_fmt_points(stats['staked'])}")
            (self.send_private_text if is_private else self.send_room_text)(sender if is_private else room, message)
            return True
        balance_top_aliases = {
            "ملياري": "billion", "مليارات": "billion", "مليارت": "billion",
            "مليوني": "million", "ملايين": "million",
        }
        if low in balance_top_aliases:
            english = _detect_text_language(text) == "en"
            message = _money_game_top_text(balance_top_aliases[low], english=english)
            if is_private:
                reserve = "\n📄 القائمة 999/999\n\n📌 اكتب ns للقائمة التالية."
                def _fits(value):
                    return len(encode_query("chat_message", type_="text", to=str(sender), body=value + reserve)) <= min(_ws_payload_limit(), SAFE_COMMAND_REPLY_BYTES)
                raw_pages, current = [], []
                for line in message.splitlines():
                    candidate = "\n".join(current + [line])
                    if current and not _fits(candidate):
                        raw_pages.append("\n".join(current)); current = [line]
                    else:
                        current.append(line)
                if current:
                    raw_pages.append("\n".join(current))
                pages = [f"{page}\n📄 القائمة {i}/{len(raw_pages)}" for i, page in enumerate(raw_pages, 1)]
                result_key = ("chat_message", "", str(sender))
                result_pages = getattr(self, "_result_pages", {})
                if len(pages) > 1:
                    result_pages[result_key] = {"pages": pages, "part": 1, "kwargs": {"to": str(sender)}, "language": "ar"}
                    self._result_pages = result_pages
                    message = pages[0] + "\n\n📌 اكتب ns للقائمة التالية."
                else:
                    result_pages.pop(result_key, None)
                    self._result_pages = result_pages
                    message = pages[0] + "\n\n✅ انتهت القوائم."
                self.send_private_text(sender, message)
            else:
                self.send_room_text(room, message)
            return True
        game_top_aliases = {
            "توب المليار": ("billion", "المليار"), "توب مليار": ("billion", "المليار"),
            "top b": ("billion", "Billion"), "topb": ("billion", "Billion"),
            "توب المليون": ("million", "المليون"), "توب مليون": ("million", "المليون"),
            "top m": ("million", "Million"), "topm": ("million", "Million"),
        }
        if low in game_top_aliases:
            game_key, label = game_top_aliases[low]
            rows = _game_top(game_key, limit=15)[:15]
            english = _detect_text_language(text) == "en"
            title = f"🏆 Top {label}" if english else f"🏆 توب {label}"
            lines = [title, "━━━━━━━━━━━━"]
            def _top_medal(i):
                return {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}️⃣")
            lines.extend(
                f"{_top_medal(i)} @{username} — {_fmt_points(points)} points | {plays} plays"
                if english else
                f"{_top_medal(i)} @{username} — {_fmt_points(points)} نقطة | {plays} لعب"
                for i, (points, _staked, plays, username) in enumerate(rows, 1)
            )
            if not rows:
                lines.append("No results yet." if english else "لا توجد نتائج بعد.")
            message = "\n".join(lines)
            if is_private:
                self.send_private_text(sender, message)
            else:
                self.send_room_text(room, message)
            return True
        if low in ("توب الألعاب", "توب الالعاب", "top games", "games top"):
            message = _game_top10_message()
            if is_private:
                self.send_private_text(sender, message)
            else:
                self.send_room_text(room, message)
            return True
        mtop=re.fullmatch(r"توب\s*(رهان|مضاربة|حظي|حظ|استثمار|حظ يا نصيب|بورصة|بورصه)?", low)
        points_top_command = low in ("توب النقاط", "توب النقاطي", "top points", "points top")
        if low in ("توب","top") or mtop or points_top_command:
            game_label=mtop.group(1) if mtop and not points_top_command else None
            game_map={"رهان":"bet","مضاربة":"duel","حظي":"luck","حظ":"luck","استثمار":"investment","حظ يا نصيب":"investment","بورصة":"stock","بورصه":"stock"}
            if game_label:
                rows=_game_top(game_map[game_label])
                def _top_medal(i):
                    return {1:"🥇",2:"🥈",3:"🥉"}.get(i, f"{i}️⃣")
                msg=f"🏆 توب {game_label}\n━━━━━━━━━━━━\n" + ("\n".join(f"{_top_medal(i)} {str(u).strip().lstrip('@')} — {_fmt_points(p)} نقطة | {pl} لعب" for i,(p,st,pl,u) in enumerate(rows,1)) if rows else "لا توجد نتائج بعد.")
            else:
                rows = _points_leaderboard_rows()[:POINTS_LEADERBOARD_LIMIT]
                def _top_medal(i):
                    return {1:"🥇",2:"🥈",3:"🥉"}.get(i, f"{i}️⃣")
                msg="🏆 توب النقاط\n━━━━━━━━━━━━\n"+"\n".join(f"{_top_medal(i)} {str(u).strip().lstrip('@')} — {_fmt_points(p)}" for i,(p,u) in enumerate(rows,1)) if rows else "🏆 لا توجد نقاط بعد."
            if is_private: self.send_private_text(sender,msg)
            else: self.send_room_text(room,msg)
            return True
        # Game switch:
        # - Any configured master can stop/start games in the room where the command is issued.
        # - The primary master (BOT_MASTER) controls the global game switch for all rooms.
        if low in ("ايقاف الالعاب", "إيقاف الالعاب", "ايقاف الألعاب", "إيقاف الألعاب"):
            # Primary master, verification masters, and room creators/managers can control games.
            if not _is_verification_manager(sender) and not (room and _room_manager(self, room, sender)):
                return True
            if _is_primary_master(sender):
                _set_games_global(False)
                self.send_private_text(sender, "🛑 تم إيقاف الألعاب في جميع الغرف.")
            elif room:
                _disable_games_room(room)
                self.send_room_text(room, "🛑 تم إيقاف الألعاب في هذه الغرفة فقط.")
            else:
                self.send_private_text(sender, "⚠️ نفّذ الأمر داخل الغرفة لإيقاف الألعاب فيها.")
            return True

        if low in ("تشغيل الالعاب", "تشغيل الألعاب"):
            # Primary master, verification masters, and room creators/managers can control games.
            if not _is_verification_manager(sender) and not (room and _room_manager(self, room, sender)):
                return True
            if _is_primary_master(sender):
                data = _game_control_data()
                data["global_enabled"] = True
                data["disabled_rooms"] = []
                _save_game_control(data)
                self.send_private_text(sender, "✅ تم تشغيل الألعاب في جميع الغرف.")
            elif room:
                _enable_games_room(room)
                self.send_room_text(room, "✅ تم تشغيل الألعاب في هذه الغرفة فقط.")
            else:
                self.send_private_text(sender, "⚠️ نفّذ الأمر داخل الغرفة لتشغيل الألعاب فيها.")
            return True

        # Invitation switch: only the configured master account can control it.
        if low in ("تشغيل الدعوات", "ايقاف الدعوات", "إيقاف الدعوات"):
            if not _is_primary_master(sender):
                return True
            if low == "تشغيل الدعوات":
                self.invites_enabled = True
                _save_local_json(DATA_DIR / "invite_settings.json", {"enabled": True, "updated_at": int(time.time())})
                self.send_private_text(sender, "✅ تم تشغيل الدعوات.")
            else:
                self.invites_enabled = False
                _save_local_json(DATA_DIR / "invite_settings.json", {"enabled": False, "updated_at": int(time.time())})
                self.send_private_text(sender, "🛑 تم إيقاف الدعوات.")
            return True

        # s@username: report the rooms where the user is currently present.
        m_is = re.fullmatch(r"s@(.+)", text.strip(), re.I)
        if m_is:
            target = m_is.group(1).strip().lstrip("@")
            if not target:
                self.send_private_text(sender, "❌ الصيغة: s@اسم المستخدم")
                return True
            if not _is_primary_master(sender):
                return True
            key = _norm_user(target)
            # Only server-confirmed rooms are eligible; persisted rosters and
            # the current command context are not proof of live presence.
            active_rooms = {str(r).strip() for r in getattr(self, "connected_rooms", set()) if str(r).strip()}
            matches = []
            for active_room in sorted(active_rooms):
                users = getattr(self, "room_users", {}).get(active_room, {}) or {}
                for username in users:
                    if _norm_user(username) == key:
                        matches.append((active_room, username))
                        break
            if matches:
                lines = [f"🔎 نتيجة البحث عن @{target}", f"🟢 أكد الخادم اتصال @{target} في {len(matches)} غرفة:"]
                lines.extend(f"🏠 {room_name} — @{username}" for room_name, username in matches)
                self.send_private_text(sender, "\n".join(lines))
            else:
                self.send_private_text(sender, f"🔴 أكد الخادم أن @{target} غير متصل حالياً في أي غرفة متصلة بالبوت.")
            return True

        # Word-filter controls are master-only and persist in moderation.json.
        if low == "mf@on" or low == "mf@off" or low.startswith("+mf@") or low.startswith("-mf@") or low == "l@mf" or low == "clear@mf":
            if not _is_master_name(sender):
                return True
            if low == "mf@on":
                self.moderation_enabled = True
                _save_moderation_config(True, sorted(self.banned_words))
                return True
            if low == "mf@off":
                self.moderation_enabled = False
                _save_moderation_config(False, sorted(self.banned_words))
                return True
            if low.startswith("+mf@"):
                word = text[4:].strip()
                if _valid_filter_word(word):
                    duplicate = any(_norm_filter_text(saved) == _norm_filter_text(word) for saved in self.banned_words)
                    self.banned_words.add(word)
                    _save_moderation_config(self.moderation_enabled, sorted(self.banned_words))
                    _save_mf_config(self.moderation_enabled, sorted(self.banned_words))
                    self.log("[FILTER] saved to mf.json:", word)
                    notice = "ℹ️ الكلمة موجودة مسبقًا في قائمة المنع." if duplicate else "✅ تمت إضافة الكلمة إلى قائمة المنع وحفظها."
                else:
                    notice = "❌ كلمة غير صالحة. أرسل نصًا من 1 إلى 80 حرفًا بعد +mf@."
                self.send_private_text(sender, notice)
                return True
            if low.startswith("-mf@"):
                word = text[4:].strip()
                target_norm = _norm_filter_text(word)
                self.banned_words = {w for w in self.banned_words if _norm_filter_text(w) != target_norm}
                _save_moderation_config(self.moderation_enabled, sorted(self.banned_words))
                _save_mf_config(self.moderation_enabled, sorted(self.banned_words))
                return True
            if low == "clear@mf":
                self.banned_words.clear()
                _save_moderation_config(self.moderation_enabled, [])
                _save_mf_config(self.moderation_enabled, [])
                return True
            if low == "l@mf":
                words = sorted(self.banned_words, key=lambda x: _norm_filter_text(x))
                if not hasattr(self, "_filter_list_state"):
                    self._filter_list_state = {}
                key = self._filter_list_key(room, sender)
                pages = self._build_filter_list_pages(words)
                self._filter_list_state[key] = {"pages": pages, "part": 1, "created": time.time()}
                self._send_filter_list_page(room, sender, is_private, 1)
                return True

        # Pending multi-room language choice: one choice applies to all requested rooms.
        if low in ("1","2") and self._complete_join_rooms_language(sender, low):
            return True

        # Add one or more room names to tracked_rooms.json without joining them yet.
        if low in ("اضف لملف الغرف", "أضف لملف الغرف", "اضف للملف الغرف", "أضف للملف الغرف"):
            if not _is_primary_master(sender):
                return True
            if not hasattr(self,"_pending_add_rooms"):
                self._pending_add_rooms={}
            self._pending_add_rooms[_norm_user(sender)]={"created":time.time()}
            self.send_private_text(
                sender,
                "📁 أضف الغرف إلى ملف الغرف\n"
                "📨 أرسل أسماء الغرف مفصولة بمسافة.\n"
                "مثال: مشاعر ادم نبض قلوب سوالف"
            )
            return True

        # Consume the next plain message after "اضف لملف الغرف" as room names.
        pending_add=getattr(self,"_pending_add_rooms",{}).get(_norm_user(sender))
        if isinstance(pending_add,dict) and low not in ("1","2"):
            if time.time()-float(pending_add.get("created",0) or 0) <= 180:
                # Do not steal another explicit bot command.
                if not _looks_like_bot_command(text):
                    self._pending_add_rooms.pop(_norm_user(sender),None)
                    return self._add_rooms_to_saved_file(sender, text)
            else:
                self._pending_add_rooms.pop(_norm_user(sender),None)

        # Global startup/join policy controls. These are master-only and affect
        # only the startup policy; they do not erase the saved room list.
        if low in ("تشغيل التوثيق", "start verification"):
            if not _is_primary_master(sender):
                return True
            VERIFICATION_ENABLED = True
            self.send_private_text(sender, "✅ تم تشغيل التوثيق. سيُطلب توثيق المستخدمين للأوامر التي تتطلبه.")
            return True
        if low in ("إيقاف التوثيق", "ايقاف التوثيق", "stop verification"):
            if not _is_primary_master(sender):
                return True
            VERIFICATION_ENABLED = False
            self.send_private_text(sender, "🛑 تم إيقاف التوثيق مؤقتاً. لن يتم طلب التوثيق من المستخدمين.")
            return True

        # Join every room saved in tracked_rooms.json. This is intentionally
        # explicit and does not change the startup variable.
        if low in (".دخول غرفي", "دخول غرفي", "دخولغرفي", "my rooms"):
            if not _is_primary_master(sender):
                self.send_private_text(sender, "🔒 أمر دخول غرفي مخصص للماستر الأساسي فقط.")
                return True
            saved_rooms = _persistent_rooms()
            if self.room and _norm_room(self.room) not in {_norm_room(x) for x in saved_rooms}:
                saved_rooms.append(self.room)
            saved_rooms = sorted({_norm_room(x) for x in saved_rooms if _norm_room(x)})
            if not saved_rooms:
                self.send_private_text(sender, "📭 لا توجد غرف محفوظة في ملف الغرف.")
                return True
            threading.Thread(
                target=self._join_rooms_serially, args=(saved_rooms, sender),
                daemon=True, name="manual-join-my-rooms",
            ).start()
            self.send_private_text(sender, f"⏳ جاري دخول غرفي ({len(saved_rooms)} غرفة) بالتتابع...")
            return True

        # Bulk room join: enter every room currently saved in tracked_rooms.json.
        if low in ("دخول الكل", "دخولكل", "join all"):
            if not _is_primary_master(sender):
                self.send_private_text(sender, "🔒 أمر دخول الكل مخصص للماستر الأساسي فقط.")
                return True
            saved_rooms = _persistent_rooms()
            if not saved_rooms:
                self.send_private_text(sender, "📭 ملف الغرف المحفوظة فارغ حالياً.\n📌 أضف غرفة أولاً عبر دخول@اسم_الغرفة.")
                return True
            # Do not burst all room_join packets. The serial worker waits for
            # each server ACK and prevents the visible enter/leave loop.
            threading.Thread(
                target=self._join_rooms_serially, args=(saved_rooms, sender),
                daemon=True, name="serial-room-join-all",
            ).start()
            sent = len(saved_rooms)
            skipped = 0
            self.send_private_text(
                sender,
                f"🏠 دخول الكل\n━━━━━━━━━━━━\n📋 الغرف المحفوظة: {len(saved_rooms)}\n"
                f"📨 أُرسلت طلبات الدخول: {sent}\n⚠️ تخطّي/فشل: {skipped}\n"
                "⏳ انتظر تأكيد الخادم للغرف التي تم إرسال طلبها."
            )
            return True

        # إدارة صانعي الغرفة
        m_add_creator = re.fullmatch(r"(?:\+صانع|اضف صانع|أضف صانع|صانع)\s*@\s*([^\s@]+)", text.strip(), re.I)
        if m_add_creator:
            if not room:
                self.send_private_text(sender, "⚠️ نفّذ الأمر داخل الغرفة لإضافة صانع لها.")
                return True
            if not _room_manager(self, room, sender):
                self.send_room_text(room, f"🔒 @{sender} هذا الأمر مخصص لصانع الغرفة أو الماستر.")
                return True
            target = m_add_creator.group(1).strip()
            data = _room_creators_data()
            rnorm = _norm_room(room)
            c_list = data.get(rnorm, [])
            if not isinstance(c_list, list): c_list = []
            if _norm_user(target) not in [_norm_user(x) for x in c_list]:
                c_list.append(target)
                data[rnorm] = c_list
                _save_room_creators(data)
                self.send_room_text(room, f"👑 تم تعيين @{target} صانعاً للغرفة بنجاح.")
            else:
                self.send_room_text(room, f"ℹ️ @{target} مضاف بالفعل كصانع لهذه الغرفة.")
            return True

        m_del_creator = re.fullmatch(r"(?:-صانع|حذف صانع|ازالة صانع|إزالة صانع)\s*@\s*([^\s@]+)", text.strip(), re.I)
        if m_del_creator:
            if not room:
                self.send_private_text(sender, "⚠️ نفّذ الأمر داخل الغرفة لحذف صانع منها.")
                return True
            if not _room_manager(self, room, sender):
                self.send_room_text(room, f"🔒 @{sender} هذا الأمر مخصص لصانع الغرفة أو الماستر.")
                return True
            target = m_del_creator.group(1).strip()
            data = _room_creators_data()
            rnorm = _norm_room(room)
            c_list = data.get(rnorm, [])
            if isinstance(c_list, list):
                new_list = [x for x in c_list if _norm_user(x) != _norm_user(target)]
                data[rnorm] = new_list
                _save_room_creators(data)
                self.send_room_text(room, f"🗑️ تم حذف @{target} من صانعي الغرفة.")
            else:
                self.send_room_text(room, "ℹ️ قائمة صناع الغرفة فارغة.")
            return True

        if low in ("صانعي", "صناع", "صانعي الغرفة", "صناع الغرفة", "صانع الغرفة", "creators"):
            if not room:
                self.send_private_text(sender, "⚠️ نفّذ الأمر داخل الغرفة لعرض صانعيها.")
                return True
            data = _room_creators_data()
            rnorm = _norm_room(room)
            c_list = data.get(rnorm, [])
            if c_list:
                names = " • ".join(f"@{x}" for x in c_list)
                msg = f"👑 صانعو الغرفة ({room}):\n━━━━━━━━━━━━\n{names}"
            else:
                msg = f"ℹ️ لم يتم تعيين صانعين مخصصين لغرفة {room} بعد (أو يعتمد على رتبة الروم)."
            if is_private: self.send_private_text(sender, msg)
            else: self.send_room_text(room, msg)
            return True

        # Joining one or multiple rooms: Arabic and English forms.
        m_join_en = re.fullmatch(r"join\s*@\s*(.+)", text, re.I | re.S)
        if m_join_en:
            rooms = [x.strip() for x in m_join_en.group(1).strip().split() if x.strip()]
            if rooms:
                self._begin_join_rooms_language(sender, rooms, response_room=room if not is_private else "", is_private=is_private)
            return True
        # Joining one or multiple rooms: دخول@مشاعر ادم نبض ... (متاح لأي عضو بالغرفة)
        m_join = re.fullmatch(r"دخول\s*@\s*(.+)", text, re.I | re.S)
        if m_join:
            raw_rooms = m_join.group(1).strip()
            rooms = [x.strip() for x in raw_rooms.split() if x.strip()]
            if not rooms:
                msg = "❌ الصيغة: دخول@اسم_الغرفة أو دخول@غرفة1 غرفة2"
                if is_private: self.send_private_text(sender, msg)
                elif room: self.send_room_text(room, msg)
                return True
            # Persist explicit manual join targets immediately. The actual
            # room_join packets still run on the serial background worker, so
            # saving here prevents a newly requested room from being lost
            # across a reconnect while its server acknowledgement is pending.
            saved_rooms = _persistent_rooms()
            saved_keys = {_norm_room(value).casefold() for value in saved_rooms}
            for target in rooms:
                key = _norm_room(target).casefold()
                if key and key not in saved_keys:
                    saved_rooms.append(target)
                    saved_keys.add(key)
            _save_persistent_rooms(saved_rooms)
            if not isinstance(getattr(self, "known_rooms", None), set):
                self.known_rooms = set()
            self.known_rooms.update(rooms)
            for target in rooms:
                blocked = _norm_room(target) in getattr(self, "blocked_rooms", set())
                if blocked:
                    self.blocked_rooms.discard(_norm_room(target))
                    self._blocked_room_reasons.pop(_norm_room(target), None)
                    self._blocked_room_notices.discard(_norm_room(target))
                    self.connected_rooms = {r for r in self.connected_rooms if _norm_room(r) != _norm_room(target)}
                    self.known_rooms = {r for r in self.known_rooms if _norm_room(r) != _norm_room(target)}
            self._save_blocked_rooms()
            _save_persistent_rooms(self.known_rooms)
            response_room = str(room or getattr(self, "last_joined_room", "") or getattr(self, "room", "") or "").strip()

            # Accept immediately, but send one room_join at a time.
            if not hasattr(self, "room_languages"):
                self.room_languages = {}
            for target in rooms:
                self.room_languages[_norm_room(target)] = "ar"
            _save_local_json(DATA_DIR / "room_languages.json", self.room_languages)
            threading.Thread(
                target=self._join_rooms_serially, args=(rooms, sender),
                daemon=True, name="serial-room-join-command",
            ).start()
            confirm_msg = f"✅ تم قبول طلب دخول الغرفة: {', '.join(rooms)}\n⏳ جاري الانضمام بالتتابع وانتظار تأكيد الخادم لكل غرفة..."
            if response_room and not is_private:
                self.send_room_text(response_room, confirm_msg)
            else:
                self.send_private_text(sender, confirm_msg)

            return True
        if text.casefold().startswith("ts@"):
            if not _is_primary_master(sender):
                self.send_private_text(
                    sender,
                    "🚫 أمر ts مخصص للماستر الأساسي المحدد في BOT_MASTER فقط.",
                )
                return True
            if not is_private:
                if room:
                    self.send_room_text(room, "🚫 أرسل أمر ts في خاص البوت فقط.")
                return True
            match = re.fullmatch(r"ts@([^@]+)@([^@]+)", text, re.I)
            if not match:
                self.send_private_text(sender, "❌ الصيغة: ts@اسم المستخدم@عدد النقاط")
                return True
            target = match.group(1).strip().lstrip("@")
            raw_amount = match.group(2).strip()
            zero_token = raw_amount.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")).replace(",", "")
            amount = 0 if zero_token in {"0", "0.0", "0.00"} else _parse_compact_amount(raw_amount)
            if not target or amount is None or amount < 0:
                self.send_private_text(sender, "❌ المبلغ غير صالح. استخدم عددًا صحيحًا غير سالب، مثل 0 أو 1000000 أو 10m.")
                return True
            result = _set_points_balance(target, amount, actor=sender)
            if not result.get("ok"):
                if result.get("error") == "target_not_found":
                    message = f"❌ لم أجد @{target} في ملف النقاط؛ لم يتغير أي رصيد."
                elif result.get("error") == "ambiguous_target":
                    message = f"⚠️ وجدت أكثر من سجل مطابق لـ@{target} في ملف النقاط؛ لم يتغير أي رصيد. استخدم الاسم الكامل لتمييز الحساب."
                elif result.get("error") == "invalid_amount":
                    message = "❌ يجب أن يكون عدد النقاط صفرًا أو أكبر."
                else:
                    message = "⚠️ تعذر حفظ التسوية بأمان؛ لم يتغير الرصيد. حاول لاحقًا."
                self.send_private_text(sender, message)
                return True
            if result.get("changed"):
                message = (
                    f"✅ تمت تسوية نقاط @{result['username']}.\n"
                    f"قبل: {int(result['old_balance']):,} نقطة\n"
                    f"بعد: {int(result['balance']):,} نقطة"
                )
            else:
                message = f"ℹ️ رصيد @{result['username']} هو بالفعل {_fmt_points(result['balance'])} نقطة؛ لم يتغير."
            self.send_private_text(sender, message)
            return True

        if text.casefold().startswith("-sb@"):
            if not _is_primary_master(sender):
                message = "🚫 أمر -sb مخصص للماستر المحدد في BOT_MASTER فقط."
                if is_private:
                    self.send_private_text(sender, message)
                elif room:
                    self.send_room_text(room, message)
                return True
            if not is_private:
                if room:
                    self.send_room_text(room, "🚫 أرسل أمر -sb في خاص البوت فقط.")
                return True
            match = re.fullmatch(r"-sb@([^@]+)@([^@]+)", text, re.I)
            if not match:
                self.send_private_text(sender, "❌ الصيغة: -sb@اسم المستخدم@عدد النقاط")
                return True
            target = match.group(1).strip().lstrip("@")
            amount = _parse_compact_amount(match.group(2))
            if amount is None or amount <= 0:
                self.send_private_text(sender, "❌ مبلغ غير صالح. استخدم رقمًا مثل 1000000 أو 10m10b.")
                return True
            result = _deduct_points(target, amount, actor=sender)
            english = _detect_text_language(text) == "en"
            if not result.get("ok"):
                error = result.get("error")
                if error == "target_not_found":
                    message = ("❌ لم يتم الخصم: اكتب اسم العميل المطابق تماماً للاسم المحفوظ في ملف النقاط. لم يتغير أي رصيد."
                               if not english else "❌ Deduction failed: use the exact username saved in the points file. No balance changed.")
                elif error == "insufficient":
                    balance = _fmt_points(result.get("balance", 0))
                    message = (f"❌ رصيد @{target} غير كافٍ. رصيده الحالي: {balance} نقطة. لم يتم الخصم."
                               if not english else f"❌ @{target} has insufficient points. Current balance: {balance}. Nothing was deducted.")
                elif error == "invalid_amount":
                    message = ("❌ يجب أن يكون عدد النقاط أكبر من صفر."
                               if not english else "❌ The amount must be greater than zero.")
                else:
                    message = ("⚠️ تعذر حفظ الخصم بأمان؛ لم يتغير الرصيد. حاول لاحقاً."
                               if not english else "⚠️ The deduction could not be saved safely; no balance changed. Try again later.")
                self.send_private_text(sender, message)
                return True
            balance = _fmt_points(result["balance"])
            message = (f"✅ تم سحب {_fmt_points(amount)} نقطة من @{result['username']}. الرصيد الجديد: {balance} نقطة."
                       if not english else f"✅ Deducted {_fmt_points(amount)} points from @{result['username']}. New balance: {balance}.")
            self.send_private_text(sender, message)
            return True

        m_transfer = re.fullmatch(r"sb@([^@]+)@([^@]+)", text, re.I)
        if m_transfer and _is_verified_user(sender):
            target = m_transfer.group(1).strip().lstrip("@")
            amount = _parse_compact_amount(m_transfer.group(2))
            if not target or amount is None or amount <= 0:
                self.send_private_text(sender, "❌ الصيغة: sb@اسم المستخدم@عدد النقاط مثل 10m10b")
                return True
            result = _transfer_points(sender, target, amount)
            if not result.get("ok"):
                error = result.get("error")
                if error in {"target_not_found", "sender_not_found"}:
                    message = "❌ فشل التحويل: اكتب اسم المستخدم الصحيح كما هو محفوظ في ملف النقاط. لم يتم خصم أي نقاط."
                elif error == "insufficient":
                    message = f"❌ رصيدك غير كافٍ. رصيدك الحالي: {_fmt_points(result.get('sender_balance'))} نقطة. لم يتم خصم شيء."
                elif error == "same_account":
                    message = "❌ لا يمكن تحويل النقاط إلى الحساب نفسه. لم يتم خصم أي نقاط."
                else:
                    message = "⚠️ تعذر حفظ التحويل بأمان؛ لم يتم تأكيده. أعد المحاولة لاحقاً."
                if _detect_text_language(text) == "en":
                    message = {
                        "target_not_found": "❌ Transfer failed: enter the exact username saved in the points file. No points were deducted.",
                        "sender_not_found": "❌ Transfer failed: your points account was not found. No points were deducted.",
                        "insufficient": f"❌ Insufficient balance: {_fmt_points(result.get('sender_balance'))} points. Nothing was deducted.",
                        "same_account": "❌ You cannot transfer points to the same account. No points were deducted.",
                        "persistence_failed": "⚠️ The transfer could not be safely saved; it was not confirmed. Please retry later.",
                    }.get(error, message)
                self.send_private_text(sender, message)
                return True
            recipient = result["recipient"]
            sender_reply = f"✅ تم تحويل {_fmt_points(amount)} نقطة إلى @{recipient}. رصيدك: {_fmt_points(result['sender_balance'])}"
            recipient_reply = f"💰 استلمت {_fmt_points(amount)} نقطة من @{result['sender']}. رصيدك الحالي: {_fmt_points(result['recipient_balance'])}"
            if _detect_text_language(text) == "en":
                sender_reply = f"✅ Transferred {_fmt_points(amount)} points to @{recipient}. Your balance: {_fmt_points(result['sender_balance'])}"
                recipient_reply = f"💰 You received {_fmt_points(amount)} points from @{result['sender']}. Balance: {_fmt_points(result['recipient_balance'])}"
            self.send_private_text(sender, sender_reply)
            if _norm_user(recipient) != _norm_user(sender):
                self.send_private_text(recipient, recipient_reply)
            return True

        # VIP users may publish images; the actual image is handled by _handle_publish_media.
        if _is_publish_command(text) and _is_vip_user(sender):
            desc = _publish_description(text)
            hit = self._find_publish_filter_hit(desc)
            if hit:
                self.send_private_text(sender,
                    f"🚫 تم منع أمر النشر: الوصف يحتوي كلمة محظورة ({hit}).\n"
                    "⛔ تم منع حسابك من النشر حتى فك المنع.")
                _record_filter_ban(sender, room, "محاولة نشر كلمة مسيئة في الوصف", hit)
                _record_publish_ban(sender, room, hit)
                return True
            self.publish_pending[_norm_user(sender)]={"description":desc,"source_room":str(room or ""),"created_at":time.time(),"silent":_is_master_name(sender)}
            self.send_private_text(sender,"🖼️ تم استلام أمر النشر. أرسل الصورة الآن خلال دقيقتين في الروم أو الخاص، وسيتم نشرها في جميع الغرف." + (f"\n📝 الوصف: {desc}" if desc else ""))
            return True

        if re.match(r"^bl@", text, re.I) and not _is_master_name(sender):
            denied = "🔒 bl@ مخصص للماستر فقط."
            self.send_private_text(sender, denied) if is_private else self.send_room_text(room, denied)
            return True
        shop_command_text = str(text or "").strip()
        shop_command_digits = shop_command_text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        public_shop_command = bool(
            shop_command_text.casefold() in {"متجر", "shop", "store", "story"}
            or re.fullmatch(r"(?:sby|buy|purchase|شراء|اشتر|اشتري)(?:\s*@?\s*)[1-6]", shop_command_digits, re.I)
        )
        public_welcome_command = bool(
            re.fullmatch(r"(?:ترحيب|welcome)@.+", shop_command_text, re.I | re.S)
            or shop_command_text.casefold() in {"ترحيب_صورة", "welcome_image"}
            or shop_command_text.casefold() in {"حذف الترحيب", "حذف ترحيب", "إلغاء الترحيب", "الغاء الترحيب", "إلغاء شراء الترحيب", "الغاء شراء الترحيب", "delete welcome", "remove welcome"}
        )
        pending_welcome_text = _norm_user(sender) in getattr(self, "custom_welcome_text_pending", {})
        verified_shop_action = _is_verified_user(sender) and (public_shop_command or public_welcome_command or pending_welcome_text)
        if not _is_master_name(sender) and not (_is_mvip_master(sender) and _is_verification_manager_command(text)) and not verified_shop_action:
            if _looks_like_admin_command(text):
                self.send_private_text(sender, "🚫 هذا الأمر مخصص للماستر والإدارة فقط.")
            return False
        # Personal bot protection: it never calls native room moderation.
        protection_state = getattr(self, "_bot_protection_menu_state", None)
        if protection_state and (protection_state == _norm_user(sender) or protection_state == _norm_user(sender) + ":delete") and low not in ("حمايه البوت", "حماية البوت"):
            if low in ("1", "تشغيل"):
                self.bot_protection_enabled = True
                _save_bot_protection_enabled(True)
                self._bot_protection_menu_state = None
                self.send_private_text(sender, "✅ تم تشغيل حماية البوت.")
                return True
            if low in ("2", "إيقاف", "ايقاف"):
                self.bot_protection_enabled = False
                _save_bot_protection_enabled(False)
                self._bot_protection_menu_state = None
                self.send_private_text(sender, "🛑 تم إيقاف حماية البوت.")
                return True
            if low in ("3", "المحظورين", "عرض المحظورين"):
                blocked = sorted(self.bot_blocked_users)
                self.send_private_text(sender, "🛡️ المحظورون داخل البوت:\n" + ("\n".join(f"{i}. @{x}" for i, x in enumerate(blocked, 1)) if blocked else "لا يوجد مستخدمون محظورون."))
                return True
            if low in ("4", "حذف محظور", "حذف المحظورين"):
                self._bot_protection_menu_state = _norm_user(sender) + ":delete"
                self.send_private_text(sender, "✍️ أرسل اسم المستخدم المراد حذفه من قائمة المحظورين.")
                return True
            if protection_state.endswith(":delete"):
                target = _norm_user(text)
                if target:
                    self.bot_blocked_users.discard(target)
                    _save_bot_blocked_users(self.bot_blocked_users)
                self._bot_protection_menu_state = None
                self.send_private_text(sender, f"✅ تم حذف @{target} من قائمة المحظورين.")
                return True
        if low in ("حمايه البوت", "حماية البوت", "bot protection", "bot_protection"):
            self._bot_protection_menu_state = _norm_user(sender)
            self.send_private_text(sender, "🛡️ حماية البوت\n1. تشغيل\n2. إيقاف\n3. عرض المحظورين\n4. حذف محظور\n\nأرسل رقم العملية.")
            return True
        if low in ("المحظورين", "المحظورون", "قائمة المحظورين", "bot blocked"):
            blocked = sorted(self.bot_blocked_users)
            self.send_private_text(sender, "🛡️ المحظورون داخل البوت:\n" + ("\n".join("• @" + x for x in blocked) if blocked else "لا يوجد مستخدمون محظورون."))
            return True
        unban = re.fullmatch(r"(?:فك حظر|unblock)@(.+)", text.strip(), re.I)
        if unban:
            target = _norm_user(unban.group(1))
            self.bot_blocked_users.discard(target)
            _save_bot_blocked_users(self.bot_blocked_users)
            self.send_private_text(sender, f"✅ تم فك الحظر الداخلي عن @{target}.")
            return True
        # "غرفي" must include the complete restored catalog. connected_rooms
        # can be temporarily smaller while the fast reconnect ACKs are still
        # arriving, so use the persistent room file as well.
        if low in ("جلب غرف السيرفر", "جلب الغرف من السيرفر", "قائمة غرف السيرفر", "get server rooms"):
            if not is_private:
                self.send_room_text(room, "⏳ جاري طلب قائمة غرف السيرفر... سأرسل النتيجة في الخاص.")
            self._request_full_room_sync(sender, room, language=_detect_text_language(text) or self._effective_room_language(room), join_after=False)
            return True
        if low in ("دخول الغرف", "دخول غرف السيرفر", "دخول كل غرف السيرفر", "join server rooms"):
            # Match Talkin1: fetch the authoritative catalog and then join it
            # serially in one operation. The old two-command flow could report
            # "no catalog" even though the server had already returned rooms.
            if not is_private:
                self.send_room_text(room, "⏳ جاري جلب غرف السيرفر ثم دخولها بالتتابع؛ سأرسل النتيجة الفعلية في الخاص.")
            self._request_full_room_sync(
                sender, room,
                language=_detect_text_language(text) or self._effective_room_language(room),
                join_after=True,
            )
            return True
        if low in ("غرفي", "غرفيّ", "myrooms", "my rooms"):
            blocked = getattr(self, "blocked_rooms", set())
            live = sorted({
                str(r).strip() for r in getattr(self, "connected_rooms", set())
                if str(r).strip() and _norm_room(r) not in blocked
            }, key=str.casefold)
            if not is_private:
                language = self._effective_room_language(room)
                self.send_room_text(room, "📡 أعرض الغرف المتصلة الآن فعليًا." if language == "ar" else "📡 Showing rooms currently connected to this session.")
            self._send_room_names_pages(
                sender, room, live, live_only=True,
                language=self._effective_room_language(room) if room else (_detect_text_language(text) or self._effective_room_language(room)),
            )
            return True

        m_all = re.fullmatch(r"تحويل للكل@(\d+)", text, re.I)
        if m_all:
            amount = int(m_all.group(1))
            if amount <= 0:
                self.send_private_text(sender, "❌ عدد النقاط يجب أن يكون أكبر من صفر.")
                return True
            users = {}
            active_rooms = {str(r).strip() for r in self.known_rooms if str(r).strip()}
            if self.room: active_rooms.add(str(self.room).strip())
            for active_room in active_rooms:
                for username in self.room_users.get(active_room, {}):
                    if username and _norm_user(username) != _norm_user(BOT_ID):
                        users[_norm_user(username)] = username
                try:
                    for item in self.db.room_users(active_room) or []:
                        username = str(item.get("username") or "").strip() if isinstance(item, dict) else ""
                        if username and _norm_user(username) != _norm_user(BOT_ID):
                            users[_norm_user(username)] = username
                except Exception as exc:
                    self.log("[POINTS-ALL] room roster failed:", active_room, repr(exc))
            for username in users.values():
                _add_points(username, amount)
                self.send_private_text(username, f"💰 إشعار تحويل جماعي: استلمت {_fmt_points(amount)} نقطة من الماستر @{sender}. رصيدك الحالي: {_fmt_points(_get_points(username))}")
            return True
            return True
        # Master commands are accepted from both private chat and rooms.
        # Room moderation acts on the room where the command was received.
        # Confirmations and diagnostics are sent privately to the master.
        if low in ("توثيق الكل", "وثق الكل", "verifyall", "verify_all", "vi@all", "vi@الكل"):
            users = {}
            active_rooms = {str(r).strip() for r in self.known_rooms if str(r).strip()}
            if self.room:
                active_rooms.add(str(self.room).strip())
            for active_room in active_rooms:
                for username in self.room_users.get(active_room, {}):
                    if username and _norm_user(username) != _norm_user(BOT_ID):
                        users[_norm_user(username)] = username
                try:
                    for item in self.db.room_users(active_room) or []:
                        username = str(item.get("username") or "").strip() if isinstance(item, dict) else ""
                        if username and _norm_user(username) != _norm_user(BOT_ID):
                            users[_norm_user(username)] = username
                except Exception as exc:
                    self.log("[VERIFY-ALL] room roster failed:", active_room, repr(exc))
            data = _verified_data()
            now = int(time.time())
            for username in users.values():
                data[_norm_user(username)] = {"username": username, "verified_by": sender, "created_at": now}
            _save_local_json(VERIFIED_FILE, data)
            self.send_private_text(sender, f"✅ تم توثيق {len(users)} مستخدم.")
            return True
        # Verification masters (persisted separately from management masters).
        if low.startswith("mvip@"):
            if not _is_primary_master(sender):
                self.send_private_text(sender, "🚫 إضافة ماستر توثيق مسموحة للمالك الأساسي فقط.")
                return True
            target = text[5:].strip().lstrip("@")
            if not target:
                self.send_private_text(sender, "❌ الصيغة: mvip@اسم المستخدم")
                return True
            data = _mvip_master_list()
            if any(_norm_user(x) == _norm_user(target) for x in data) or _is_master_name(target):
                self.send_private_text(sender, f"⚠️ @{target} لديه صلاحية ماستر توثيق بالفعل.")
                return True
            data.append(target)
            _save_local_json(MVIP_MASTERS_FILE, data)
            self.send_private_text(sender, f"✅ تم إضافة @{target} إلى ماسترات التوثيق.")
            return True
        if low.startswith("umvip@"):
            if not _is_primary_master(sender):
                self.send_private_text(sender, "🚫 إزالة ماستر توثيق مسموحة للمالك الأساسي فقط.")
                return True
            target = text[6:].strip().lstrip("@")
            data = [x for x in _mvip_master_list() if _norm_user(x) != _norm_user(target)]
            _save_local_json(MVIP_MASTERS_FILE, data)
            self.send_private_text(sender, f"✅ تم إزالة @{target} من ماسترات التوثيق.")
            return True
        if low == "l@mvip":
            if not _is_verification_manager(sender):
                return True
            msg = _format_saved_accounts("👑 ماسترات التوثيق", { _norm_user(x): {"username": x} for x in _mvip_master_list() }, "📭 لا توجد ماسترات توثيق.")
            self.send_private_text(sender, msg)
            return True
        if low == "l@mas":
            if not _is_master_name(sender):
                return True
            masters = list(_master_list())
            if BOT_MASTER and not any(_norm_user(x) == _norm_user(BOT_MASTER) for x in masters):
                masters.insert(0, BOT_MASTER)
            msg = _format_saved_accounts("👑 ماسترات الإدارة", { _norm_user(x): {"username": x} for x in masters }, "📭 لا توجد ماسترات إدارة.")
            self.send_private_text(sender, msg)
            return True

        # Add/remove master. Only the owner from BOT_MASTER may alter master list.
        if low.startswith("mas@") or low.startswith("إضافة ماستر@") or low.startswith("اضافة ماستر@"):
            if _norm_user(sender) != _norm_user(BOT_MASTER):
                self.log("[MASTER] add-master denied", sender); return True
            prefix = "mas@" if low.startswith("mas@") else ("إضافة ماستر@" if low.startswith("إضافة ماستر@") else "اضافة ماستر@")
            target=text[len(prefix):].strip().lstrip("@");
            if not target: self.log("[MASTER] invalid add-master", sender); return True
            masters=_master_list()
            if any(_norm_user(x)==_norm_user(target) for x in masters) or _norm_user(target) == _norm_user(BOT_MASTER):
                self.send_private_text(sender, f"⚠️ @{target} لديه صلاحية ماستر بالفعل.")
                return True
            masters.append(target); _save_local_json(MASTERS_FILE,masters)
            self.send_private_text(sender, f"✅ تم إضافة @{target} إلى الماسترز.")
            return True
        if low.startswith("umas@") or low.startswith("umas ") or low.startswith("سحب ماستر@") or low.startswith("إزالة ماستر@"):
            if _norm_user(sender) != _norm_user(BOT_MASTER):
                self.log("[MASTER] remove-master denied", sender); return True
            prefix = "umas@" if low.startswith("umas@") else ("umas " if low.startswith("umas ") else ("سحب ماستر@" if low.startswith("سحب ماستر@") else "إزالة ماستر@"))
            target=text[len(prefix):].strip().lstrip("@"); masters=[x for x in _master_list() if _norm_user(x)!=_norm_user(target)]; _save_local_json(MASTERS_FILE,masters)
            self.send_private_text(sender, f"✅ تم سحب الماستر من @{target}.")
            return True
        if low.startswith("sb@"):
            if not _is_master_name(sender):
                self.send_private_text(sender,"🚫 أمر النقاط للماستر فقط."); return True
            m=re.match(r"^sb@([^@]+)@(-?\d+)$",text,re.I)
            if not m: self.send_private_text(sender,"❌ الصيغة: sb@اسم المستخدم@عدد النقاط"); return True
            target,amount=m.group(1).strip(),int(m.group(2)); new=_add_points(target,amount)
            action = "تحويل" if amount >= 0 else "خصم"
            return True
            return True
        # Persistent account lists. These commands are master-only because
        # they expose the bot's saved verification records. They are read
        # directly from disk so replacing bot.py does not reset the lists.
        if low in ("vi", "الموثقين", "الموثقون", "الموثقين؟"):
            count = len(_verified_data())
            self.send_private_text(sender, f"📋 عدد الحسابات الموثقة: {count}")
            return True
        if low in ("vip", "vips", "حسابات vip", "قائمة vip"):
            count = len(_vip_data())
            self.send_private_text(sender, f"👑 عدد أعضاء VIP: {count}")
            return True
        if low in ("اعضاء", "أعضاء", "الاعضاء", "الأعضاء", "members"):
            users = _persistent_all_roster_users()
            self.send_private_text(sender, f"👥 عدد الأعضاء المحفوظين: {len(users)}")
            return True

        if low.startswith("vi@"):
            if not _is_verification_manager(sender):
                return True
            target=text[2:].strip().lstrip("@");
            if not target: self.send_private_text(sender,"❌ الصيغة: vi@اسم المستخدم"); return True
            data=_verified_data()
            if _saved_identity_matches(data, target):
                self.send_private_text(sender, f"⚠️ @{target} لديه توثيق عادي بالفعل.")
                return True
            if _saved_identity_matches(_vip_data(), target):
                self.send_private_text(sender, f"⚠️ @{target} لديه توثيق VIP بالفعل.")
                return True
            key=_norm_user(target)
            data[key]={"username":target,"verified_by":sender,"created_at":int(time.time())}; _save_local_json(VERIFIED_FILE,data)
            # Keep the bot's normal verification notice for the verified user,
            # while the master process separately relays the result to whoever
            # requested the verification.
            self.send_private_text(target, f"✅ تم توثيق حسابك @{target} بنجاح.\n🎉 يمكنك الآن استخدام أوامر البوت.")
            self.send_private_text(sender, f"✅ تم توثيق @{target}.")
            if room:
                self.send_room_text(room, f"✅ تم توثيق @{target} بنجاح.")
            return True
        if low.startswith("ازالة توثيق@") or low.startswith("إزالة توثيق@") or low.startswith("سحب التوثيق@") or low.startswith("uns@"):
            if not _is_verification_manager(sender):
                return True
            prefix = "uns@" if low.startswith("uns@") else ("سحب التوثيق@" if low.startswith("سحب التوثيق@") else text.split("@",1)[0]+"@")
            target=text[len(prefix):].strip().lstrip("@"); data=_verified_data(); data.pop(_norm_user(target),None); _save_local_json(VERIFIED_FILE,data)
            self.send_private_text(sender, f"✅ تم إلغاء توثيق @{target}.")
            return True
        if low.startswith("vip@"):
            if not _is_verification_manager(sender):
                return True
            target=text[4:].strip().lstrip("@");
            if not target: self.send_private_text(sender,"❌ الصيغة: Vip@اسم المستخدم"); return True
            data=_vip_data()
            if _saved_identity_matches(data, target):
                self.send_private_text(sender, f"⚠️ @{target} لديه توثيق VIP بالفعل.")
                return True
            key=_norm_user(target)
            data[key]={"username":target,"granted_by":sender,"created_at":int(time.time())}; _save_local_json(VIP_FILE,data)
            self.custom_welcomes[key] = {
                "username": target,
                "message": "👑 عضو Vip\n👤 {username}\n🏠 الغرفة: {room}"
            }
            self.custom_welcome_enabled = True
            self._save_social_features()
            for active_room in self._active_rooms():
                present = any(_norm_user(u) == key for u in self.room_users.get(active_room, {}))
                if present:
                    self.send_room_text(active_room, f"👑 عضو Vip\n👤 {target}\n🏠 الغرفة: {active_room}")
            self.send_private_text(target, f"✅ تم توثيق حسابك @{target}\n بنجاح.\n🎉 ويمكنك الان النشر وارسال الهدايا\n👑 تم تفعيل الترحيب المخصص تلقائياً.\nمن قبل @{sender}")
            self.send_private_text(sender, f"✅ تم منح VIP لـ @{target}.")
            return True
        if low.startswith("unvip@") or low.startswith("un vip@"):
            if not _is_verification_manager(sender):
                return True
            target=text[text.casefold().find("vip@")+4:].strip().lstrip("@"); data=_vip_data(); data.pop(_norm_user(target),None); _save_local_json(VIP_FILE,data)
            self.send_private_text(sender, f"✅ تم إلغاء VIP عن @{target}.")
            return True
        # Room/admin commands accepted in both room and private master chat.
        m=re.match(r"^(k@|kick\s+)(@?[^\s]+)$", text, re.I)
        if m:
            target=m.group(2).lstrip("@").strip()
            if not room:
                self.send_private_text(sender,"❌ لا توجد غرفة لتنفيذ الطرد فيها."); return True
            self.request_admin_action(room,target,"kick",sender,announce_room=True)
            return True
        m=re.match(r"^(b@|ban\s+)(@?[^\s]+)$", text, re.I)
        if m:
            target=m.group(2).lstrip("@").strip()
            if not room:
                self.send_private_text(sender,"❌ لا توجد غرفة لتنفيذ الحظر فيها."); return True
            self.request_admin_action(room,target,"ban",sender,announce_room=True)
            return True
        # Global ban: حظر بكل الغرف @username / حظر بكل الغرف username
        m_global_ban = re.fullmatch(r"حظر\s+بكل\s+الغرف\s+@?([^\s@]+)", text.strip(), re.I)
        if m_global_ban:
            target = m_global_ban.group(1).strip()
            if not _is_master_name(sender):
                return True
            if not target:
                self.send_private_text(sender, "❌ الصيغة: حظر بكل الغرف @اسم المستخدم")
                return True
            active_rooms = sorted({str(r).strip() for r in getattr(self, "connected_rooms", set()) if str(r).strip()})
            if self.room and str(self.room).strip() not in active_rooms:
                active_rooms.append(str(self.room).strip())
            active_rooms = sorted(set(active_rooms))
            if not active_rooms:
                self.send_private_text(sender, "❌ البوت غير موجود في أي غرفة حالياً.")
                return True
            delay = max(0.25, float(os.getenv("GLOBAL_BAN_DELAY_SECONDS", "0.8") or 0.8))

            def _ban_all_rooms_ar():
                total = len(active_rooms)
                self.log(f"[MOD-ALL] starting Arabic global ban target=@{target} rooms={total}")
                with self._room_bulk_lock:
                    for index, active_room in enumerate(active_rooms, 1):
                        try:
                            self.request_admin_action(active_room, target, "ban", sender, announce_room=True)
                            self.log(f"[MOD-ALL] ban sent {index}/{total}: {active_room} -> @{target}")
                        except Exception as exc:
                            self.log(f"[MOD-ALL] ban failed room={active_room} target=@{target}: {exc!r}")
                        if index < total:
                            time.sleep(delay)
                    self.log(f"[MOD-ALL] global ban finished target=@{target} rooms={total}")
                    self.send_private_text(sender, f"✅ تم تنفيذ حظر @{target} في {total} غرفة بدون خروج البوت من أي غرفة.")

            threading.Thread(target=_ban_all_rooms_ar, daemon=True, name="global-ban-all-rooms-ar").start()
            return True

        m=re.match(r"^bl@(.+)$", text, re.I)
        if m:
            if not _is_master_name(sender):
                denied = "🔒 bl@ مخصص للماستر فقط."
                self.send_private_text(sender, denied) if is_private else self.send_room_text(room, denied)
                return True
            target=m.group(1).strip().lstrip("@")
            if not target:
                self.send_private_text(sender,"❌ الصيغة: bl@اسم المستخدم") if is_private else self.send_room_text(room,"❌ الصيغة: bl@اسم المستخدم")
                return True

            # IMPORTANT: use only rooms that are actually connected in the
            # current WebSocket session. ``known_rooms`` may contain old/stale
            # rooms; sending moderation packets to those rooms can make the
            # server close the socket and trigger the reconnect loop.
            active_rooms={str(r).strip() for r in getattr(self, "connected_rooms", set()) if str(r).strip()}
            active_rooms=sorted({r for r in active_rooms if r})
            if not active_rooms:
                self.send_private_text(sender,"❌ البوت غير موجود في أي غرفة حالياً.")
                return True

            # Do not flood one WebSocket with ban requests for every room at
            # once. Talkin accepts the same operation, but rapid back-to-back
            # moderation packets can cause the server to disconnect the bot.
            # Process them serially with a small configurable delay.
            delay=max(0.25, float(os.getenv("GLOBAL_BAN_DELAY_SECONDS", "0.8") or 0.8))

            def _ban_all_rooms():
                total=len(active_rooms)
                sent=0
                failed=0
                self.log(f"[MOD-ALL] starting global ban target=@{target} rooms={total} delay={delay}s")
                with self._room_bulk_lock:
                    for index, active_room in enumerate(active_rooms, 1):
                        try:
                            if self.request_admin_action(active_room, target, "ban", sender, announce_room=True):
                                sent += 1
                            else:
                                failed += 1
                            self.log(f"[MOD-ALL] ban sent {index}/{total}: {active_room} -> @{target}")
                        except Exception as exc:
                            failed += 1
                            self.log(f"[MOD-ALL] ban failed room={active_room} target=@{target}: {exc!r}")
                        if index < total:
                            time.sleep(delay)
                    self.log(f"[MOD-ALL] global ban requests finished target=@{target} sent={sent} failed={failed}")
                self.send_private_text(
                    sender,
                    f"✅ أُرسلت طلبات حظر @{target} إلى {sent} غرفة متصلة.\n"
                    f"⚠️ تعذّر الإرسال إلى {failed} غرفة.\n"
                    "🛡️ لا يُستثنى المستهدف بسبب رتبته، والبوت لا يغادر الغرف."
                )

            threading.Thread(
                target=_ban_all_rooms,
                daemon=True,
                name="global-ban-all-rooms",
            ).start()
            return True
        if low == ".u":
            if not _is_master_name(sender):
                return True
            return self._undo_last_bot_action(sender, room)

        m=re.match(r"^(u@|ub@|unban\s+)(@?[^\s]+)$", text, re.I)
        if m:
            target=m.group(2).lstrip("@").strip()
            if not room:
                self.send_private_text(sender,"❌ لا توجد غرفة لتنفيذ فك الحظر فيها."); return True
            self.request_admin_action(room,target,"member",sender,announce_room=True)
            return True
        m=re.match(r"^(m@|member\s+)(@?[^\s]+)$", text, re.I)
        if m:
            target=m.group(2).lstrip("@").strip()
            if not room:
                self.send_private_text(sender,"❌ لا توجد غرفة لمنح العضوية فيها."); return True
            self.request_admin_action(room,target,"member",sender,announce_room=True)
            return True
        m=re.match(r"^(a@|admin\s+)(@?[^\s]+)$", text, re.I)
        if m:
            target=m.group(2).lstrip("@").strip()
            if not room:
                self.send_private_text(sender,"❌ لا توجد غرفة لتعيين المشرف فيها."); return True
            self.request_admin_action(room,target,"admin",sender,announce_room=True)
            return True
        m=re.match(r"^(o@|owner\s+)(@?[^\s]+)$", text, re.I)
        if m:
            target=m.group(2).lstrip("@").strip()
            if not room:
                self.send_private_text(sender,"❌ لا توجد غرفة لتعيين المالك فيها."); return True
            self.request_admin_action(room,target,"owner",sender,announce_room=True)
            return True
        if low in ("دخول الكل", "دخولكل", "join all"):
            if not _is_primary_master(sender):
                self.send_private_text(sender, "🔒 أمر دخول الكل مخصص للماستر الأساسي فقط.")
                return True
            saved_rooms = _persistent_rooms()
            if not saved_rooms:
                self.send_private_text(sender, "📭 ملف الغرف المحفوظة فارغ حالياً.\n📌 أضف غرفة أولاً عبر دخول@اسم_الغرفة.")
                return True
            threading.Thread(
                target=self._join_rooms_serially, args=(saved_rooms, sender),
                daemon=True, name="serial-room-join-all-private",
            ).start()
            sent = len(saved_rooms)
            skipped = 0
            self.send_private_text(
                sender,
                f"🏠 دخول الكل\n━━━━━━━━━━━━\n📋 الغرف المحفوظة: {len(saved_rooms)}\n"
                f"📨 أُرسلت طلبات الدخول: {sent}\n⚠️ تخطّي/فشل: {skipped}\n"
                "⏳ انتظر تأكيد الخادم للغرف التي تم إرسال طلبها."
            )
            return True

        m_join = re.fullmatch(r"دخول@(.+)", text, re.I)
        if m_join:
            target=m_join.group(1).strip()
            if not target:
                self.send_private_text(sender,"❌ الصيغة: دخول@اسم_الغرفة"); return True
            if _norm_room(target) in getattr(self, "blocked_rooms", set()):
                blocked_key = _norm_room(target)
                self.blocked_rooms.discard(blocked_key)
                self._blocked_room_reasons.pop(blocked_key, None)
                self._blocked_room_notices.discard(blocked_key)
                self.known_rooms = {r for r in self.known_rooms if _norm_room(r) != blocked_key}
                self.connected_rooms = {r for r in self.connected_rooms if _norm_room(r) != blocked_key}
                self._save_blocked_rooms()
                _save_persistent_rooms(self.known_rooms)
            # A manual دخول@ request is also an explicit save request. Store
            # the room before asking for language so it survives reconnects
            # even when the server ACK arrives asynchronously.
            saved_rooms = _persistent_rooms()
            saved_keys = {_norm_room(value).casefold() for value in saved_rooms}
            if _norm_room(target).casefold() not in saved_keys:
                saved_rooms.append(target)
                _save_persistent_rooms(saved_rooms)
            if not isinstance(getattr(self, "known_rooms", None), set):
                self.known_rooms = set()
            self.known_rooms.add(target)
            # Ask for the language before sending room_join. The previous
            # room-command path joined first and showed the prompt afterward.
            self._begin_join_rooms_language(
                sender,
                [target],
                response_room=room if not is_private else "",
                is_private=is_private,
            )
            return True
        if low in ("خروج", "leave", "exit", "غادر", "leave room") and room and not is_private:
            if not _is_primary_master(sender):
                self.send_room_text(room, "🔒 هذا الأمر مخصص للماستر الأساسي فقط.")
                return True
            target = room
            self.send_room_text(target, "وداعاً نلتقي مرة أخرى")
            def _cycle_current_room():
                try:
                    self.leave_room(target)
                    time.sleep(max(0.5, float(os.getenv("ROOM_CYCLE_DELAY_SECONDS", "1.0"))))
                    self.join_room(target, force=True, requested_by=sender)
                except Exception as exc:
                    self.log("[ROOM] master cycle failed:", target, repr(exc))
            threading.Thread(target=_cycle_current_room, daemon=True, name="master-room-cycle").start()
            return True
        if low in ("خروج","leave","exit") or low.startswith(("خروج ","leave ","exit ")):
            parts=text.split(None,1); target=parts[1].strip() if len(parts)==2 else ""
            if target:
                ok=self.leave_room(target)
                self.send_private_text(sender,f"{'✅ خرجت من الغرفة' if ok else '❌ تعذر الخروج'}: {target}")
            else:
                rooms=self.leave_all_rooms()
                self.send_private_text(sender,f"✅ خرجت من جميع الغرف. العدد: {len(rooms)}")
            return True
        if (low.startswith("invmsg") or low.startswith("رسالةدعوة")
                or low.startswith("invite message") or low.startswith("invmsg@")):
            if "@" in text:
                template = text.split("@", 1)[1].strip()
            else:
                parts=text.split(None,1); template=parts[1].strip() if len(parts)==2 else "يوجد معجب مخفي في {room}"
            if "{room}" not in template:
                template = template.rstrip() + " {room}"
            self.invite_message_template=template
            self.send_private_text(sender,f"✅ تم تغيير نص الدعوة إلى: {template}"); return True
        # .r = historical room entrants.
        # The persistent room roster is append/update-only, so users who
        # entered the room and later left remain visible in this list.
        if low == ".r":
            if not room:
                self.send_private_text(sender, "⚠️ نفّذ .r داخل الغرفة المطلوبة.")
                return True
            # IMPORTANT: .r must use the dedicated entry history, not the
            # general room roster. occupants_list/room_users refreshes can touch
            # last_seen for every member and would incorrectly make old members
            # appear as if they entered after the bot.
            entry_room = _norm_room(room)
            entry_times = getattr(self, "_room_entry_times", {})
            entry_ts = float(entry_times.get(entry_room, 0) or 0)
            entry_map = getattr(self, "_room_entry_users", {}).get(entry_room, {})
            users = list(entry_map.values()) if isinstance(entry_map, dict) else []
            if not entry_ts:
                users = []
            # .r is a paginated result: send ONLY the first page now.
            # The next page is delivered when the same user sends `ns`.
            rows=[]; seen=set()
            for u in users or []:
                name=str(u or "").strip().lstrip("@")
                key=_norm_user(name)
                if name and key and key != _norm_user(BOT_ID) and key not in seen:
                    seen.add(key); rows.append(name)
            rows.sort(key=_norm_user)
            if not rows:
                self.send_private_text(sender, f"📭 لا توجد أسماء محفوظة لأعضاء دخلوا الغرفة {room}.")
                return True
            limit=int(os.getenv("WS_MAX_MESSAGE_BYTES", "1008"))
            title=f"👥 الأعضاء الذين دخلوا الغرفة — {room} ({len(rows)})"
            pages=[]; current=[title]
            for index,name in enumerate(rows,1):
                line=f"{index}. @{name}"
                candidate="\n".join(current+[line])
                probe=encode_query("chat_message", type_="text", to=str(sender or ""), body=candidate)
                if len(probe)>max(120,limit-80) and len(current)>1:
                    pages.append("\n".join(current)); current=[title,line]
                else:
                    current.append(line)
            if len(current)>1:
                pages.append("\n".join(current))
            total=len(pages)
            # Store under a private-chat key so the generic `ns` handler can
            # continue this list even though .r was issued from a room.
            result_pages=getattr(self,"_result_pages",{})
            result_key=("chat_message","",str(sender or ""))
            result_pages[result_key]={
                "pages": pages, "part": 1,
                "kwargs": {"to": str(sender or "")}
            }
            self._result_pages=result_pages
            first=pages[0]
            if total>1:
                first += "\n\n📌 للقائمة التالية اكتب Ns"
            self.send_private_text(sender, first)
            return True
        if low == "inv" or low.startswith("inv ") or low in ("دعوات","invite") or low.startswith(("دعوات ","invite ")):
            if not getattr(self, "invites_enabled", True):
                self.send_private_text(sender, "🛑 الدعوات متوقفة حالياً. أرسل: تشغيل الدعوات")
                return True
            # Invitations are executed only from the room where the command is sent.
            if is_private or not room:
                self.send_private_text(sender, "⚠️ نفّذ inv داخل الغرفة المطلوبة.")
                return True
            target_room = str(room).strip()
            # inv is a room workflow: announce progress and final status in
            # the room. The actual invitation packets remain private invites.
            self.request_occupants(
                target_room,
                silent_master=False,
                response_room=target_room,
                response_to="",
            )
            return True
        m_single_invite = re.fullmatch(r"(?:i@|دعوه?\s+|دعوة\s+)(.+)", text.strip(), re.I)
        if m_single_invite:
            target = m_single_invite.group(1).strip().lstrip("@").strip()
            target_room = str(room or self.room or "").strip()
            if not target or not target_room:
                self.send_private_text(sender, "❌ الصيغة: i@اسم_المستخدم داخل غرفة.")
                return True
            if not getattr(self, "invites_enabled", True):
                self.send_private_text(sender, "🛑 الدعوات متوقفة حالياً. أرسل: تشغيل الدعوات")
                return True
            # Direct invite must use the same normal private-invite sender as the
            # automatic `inv` system. The previous code referenced an undefined
            # variable `sent` and therefore never actually sent the invitation.
            role_ok = self._inv_bot_owner_allowed(target_room)
            if role_ok is False:
                reply = "⚠️ ارفع البوت أونر في الغرفة ثم أعد الأمر i@اسم_المستخدم."
                if is_private:
                    self.send_private_text(sender, reply)
                else:
                    self.send_room_text(target_room, reply)
                return True
            if role_ok is None:
                self.send_private_text(sender, "⏳ جاري التحقق من رتبة البوت. أعد i@اسم_المستخدم بعد لحظات إذا لزم الأمر.")
                return True
            try:
                sent = self.send_private_invite(target, target_room, inviter=sender, allow_repeat=True)
                msg = (f"✅ تم إرسال الدعوة الخاصة إلى @{target} للغرفة {target_room}."
                       if sent else
                       f"❌ تعذر إرسال الدعوة الخاصة إلى @{target} للغرفة {target_room}؛ تحقق من الاتصال واسم المستخدم.")
                if is_private:
                    self.send_private_text(sender, msg)
                else:
                    self.send_room_text(target_room, msg)
            except Exception as exc:
                msg = f"❌ تعذر إرسال الدعوة إلى @{target}: {exc}"
                if is_private:
                    self.send_private_text(sender, msg)
                else:
                    self.send_room_text(target_room, msg)
            return True
        if low.startswith("say ") or low.startswith("قل "):
            parts=text.split(None,1); msg=parts[1].strip() if len(parts)==2 else ""
            if room and msg: self.send_room_text(room,msg)
            else: self.send_private_text(sender,"❌ استخدم say نص داخل غرفة.")
            return True
        # Departure replies use a dedicated file and never modify existing +sr replies.
        m_ds = re.match(r"^\+ds@([^@]+)@(.+)$", text.strip(), re.I)
        if m_ds:
            if not _is_master_name(sender):
                self.send_private_text(sender, "🔒 إضافة ردود المغادرة مخصصة للماستر.")
                return True
            trigger, reply = _departure_trigger_key(m_ds.group(1)), m_ds.group(2).strip()
            if not trigger or not reply:
                self.send_private_text(sender, "❌ الصيغة: +ds@أي_حرف@الرد (مثال: +ds@س@الرد)")
                return True
            replies = getattr(self, "departure_replies", {})
            variants = list(replies.get(trigger, []))
            if reply not in variants:
                variants.append(reply)
            replies[trigger] = variants
            self.departure_replies = replies
            self._save_departure_replies()
            self.send_private_text(sender, f"✅ حُفظ رد المغادرة للمفتاح «{trigger}» مستقلاً عن الردود السابقة.")
            return True

        # Auto replies: +sr@وصف@الرد / Sr@on / Sr@off
        m_sr = re.match(r"^\+sr@([^@]+)@(.+)$", text.strip(), re.I)
        if m_sr and _is_master_name(sender):
            trigger, reply = m_sr.group(1).strip(), m_sr.group(2).strip()
            if trigger and reply:
                key_sr=_auto_reply_key(trigger)
                variants=self._auto_reply_variants(key_sr)
                if reply not in variants:
                    variants.append(reply)
                self.auto_replies[key_sr]={"trigger":trigger,"replies":variants}
                self.auto_replies_enabled=True
                self._save_social_features()
                self.send_private_text(sender, f"✅ تمت إضافة رد تلقائي\n📌 الوصف: {trigger}\n💬 عدد الردود: {len(variants)}")
            return True
        if low == "l@sr":
            if not _is_master_name(sender):
                return True
            rows=[]
            for key_sr in sorted(self.auto_replies):
                item=self.auto_replies.get(key_sr)
                variants=self._auto_reply_variants(key_sr)
                trigger=str(item.get("trigger") or key_sr) if isinstance(item,dict) else key_sr
                if variants:
                    rows.append(f"💬 {trigger} — {len(variants)} رد")
                    rows.extend(f"   {i}. {v}" for i,v in enumerate(variants,1))
            if not rows:
                self.send_private_text(sender,"📭 لا توجد ردود تلقائية حالياً.\n➕ أضفها عبر +sr@الكلمة@الرد")
            else:
                self._send_text_packets("chat_message", "📋 الردود التلقائية\n━━━━━━━━━━━━\n"+"\n".join(rows), to=sender)
            return True
        if re.match(r"^sr@(?:on|off)$", text.strip(), re.I) and _is_master_name(sender):
            self.auto_replies_enabled = text.strip().lower() == "sr@on"
            self._save_social_features()
            self.send_private_text(sender, "✅ تم تشغيل الردود التلقائية." if self.auto_replies_enabled else "⛔ تم إيقاف الردود التلقائية.")
            return True
        if low in {"متجر", "shop", "store", "story"}:
            self.send_private_text(sender, member_shop.render_catalog())
            return True
        buy_text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
        buy = re.fullmatch(r"(?:sby|buy|purchase|شراء|اشتر|اشتري)(?:\s*@?\s*)([1-6])", buy_text, re.I)
        if buy:
            item_id = int(buy.group(1))
            item = member_shop.ITEMS[item_id]
            target_room = str(room or getattr(self, "room", "") or "").strip()
            if item["kind"].startswith("room_") and not target_room:
                self.send_private_text(sender, "⚠️ اشترِ حماية الغرفة من داخل الغرفة المطلوبة.")
                return True
            account = _resolve_points_account(_points_data(), sender)
            balance = _points_balance(account[1]) if account else 0
            if balance < int(item["cost"]):
                self.send_private_text(sender, f"❌ رصيدك غير كافٍ. السعر {item['cost']:,} نقطة، رصيدك {balance:,}.")
                return True
            try:
                _add_points(sender, -int(item["cost"]))
            except (PointsInsufficientError, PointsPersistenceError):
                self.send_private_text(sender, "❌ تعذر خصم النقاط؛ لم يتم الشراء.")
                return True
            success, error = member_shop.purchase(
                sender, item_id, target_room,
                user_id=_stored_user_id(sender, room),
                aliases=_stored_user_aliases(sender, room),
            )
            if not success:
                try:
                    _add_points(sender, int(item["cost"]))
                except Exception as exc:
                    self.log("[SHOP] refund failed after rejected purchase", repr(exc))
                self.send_private_text(sender, error)
                return True
            _github_sync_after_local_save(member_shop.SHOP_PATH, None)
            if item["kind"] == "custom_welcome":
                if not isinstance(getattr(self, "custom_welcome_text_pending", None), dict):
                    self.custom_welcome_text_pending = {}
                self.custom_welcome_text_pending[_norm_user(sender)] = {"username": sender, "created_at": time.time()}
                extra = "\n✍️ أرسل نص الترحيب الآن (خلال دقيقتين)."
            elif item["kind"] == "image_welcome":
                if not isinstance(getattr(self, "custom_welcome_image_pending", None), dict):
                    self.custom_welcome_image_pending = {}
                self.custom_welcome_image_pending[_norm_user(sender)] = {"username": sender, "created_at": time.time()}
                extra = "\n🖼️ أرسل صورة الترحيب الآن (خلال دقيقتين)؛ سيُكتب اسمك عليها عند الترحيب بك."
            else:
                extra = "\n⏱️ تنتهي الحماية بعد ساعة."
            self.send_private_text(sender, f"✅ تم شراء {item['icon']} {item['name']} مقابل {item['cost']:,} نقطة.{extra}")
            return True
        custom_welcome = re.fullmatch(r"(?:ترحيب|welcome)@(.+)", text, re.I | re.S)
        if custom_welcome:
            if not member_shop.active(
                    "custom_welcome", username=sender,
                    user_id=_stored_user_id(sender, room),
                    aliases=_stored_user_aliases(sender, room)):
                self.send_private_text(sender, "🔒 افتح ميزة الترحيب المخصص من المتجر أولاً: sby 5.")
                return True
            welcome = custom_welcome.group(1).strip()[:300]
            if welcome:
                welcome_key = _norm_user(sender)
                current = getattr(self, "custom_welcomes", {}).get(welcome_key)
                if not isinstance(current, dict):
                    current = {"username": sender}
                current["username"] = sender
                current["message"] = welcome
                self.custom_welcomes[welcome_key] = current
                getattr(self, "custom_welcome_text_pending", {}).pop(welcome_key, None)
                self.custom_welcome_enabled = True
                self._save_social_features()
                self.send_private_text(sender, "✅ تم حفظ ترحيبك المخصص. للحذف: حذف ترحيب النص، أو حذف ترحيب لحذف النص والصورة.")
            return True
        if low in {"ترحيب_صورة", "welcome_image"}:
            if not member_shop.active(
                    "image_welcome", username=sender,
                    user_id=_stored_user_id(sender, room),
                    aliases=_stored_user_aliases(sender, room)):
                self.send_private_text(sender, "🔒 افتح ميزة الترحيب بصورة من المتجر أولاً: sby 6.")
                return True
            if not isinstance(getattr(self, "custom_welcome_image_pending", None), dict):
                self.custom_welcome_image_pending = {}
            self.custom_welcome_image_pending[_norm_user(sender)] = {"username": sender, "created_at": time.time()}
            self.send_private_text(sender, "🖼️ أرسل صورة الترحيب الآن في الخاص أو أي غرفة خلال دقيقتين؛ سيُكتب اسمك عليها عند الترحيب بك.")
            return True

        # Custom welcome: swc+@اسم@الترحيب and on/off.
        m_sw = re.match(r"^(?:swc\+|ترحيب\+)@([^@]+)@(.+)$", text.strip(), re.I)
        if m_sw and _is_master_name(sender):
            user, welcome = m_sw.group(1).strip().lstrip("@"), m_sw.group(2).strip()
            if user and welcome:
                self.custom_welcomes[_norm_user(user)] = {"username": user, "message": welcome}
                self.custom_welcome_enabled = True
                self._save_social_features()
                self.send_private_text(sender, f"✅ تم حفظ الترحيب المخصص لـ @{user}.\n💬 {welcome}")
            return True
        if re.match(r"^swc@(?:on|off)$", text.strip(), re.I) and _is_master_name(sender):
            self.custom_welcome_enabled = text.strip().lower() == "swc@on"
            self._save_social_features()
            self.send_private_text(sender, "✅ تم تشغيل الترحيب المخصص." if self.custom_welcome_enabled else "⛔ تم إيقاف الترحيب المخصص.")
            return True
        # Publishing ban management.
        if low == "l@mbp":
            if not _is_master_name(sender):
                return True
            rows=_publish_ban_rows()
            if not rows:
                self.send_private_text(sender,"📭 لا يوجد مستخدمون ممنوعون من النشر.")
            else:
                lines=["🚫 الممنوعون من النشر","━━━━━━━━━━━━"]
                for i,row in enumerate(rows[-100:],1):
                    username=str(row.get("username") if isinstance(row,dict) else row).strip().lstrip("@")
                    word=str(row.get("word") or "") if isinstance(row,dict) else ""
                    at=str(row.get("at") or "") if isinstance(row,dict) else ""
                    extra=f" | الكلمة: {word}" if word else ""
                    lines.append(f"{i}. {username}{extra}" + (f" | {at}" if at else ""))
                self._send_text_packets("chat_message","\n".join(lines),to=sender)
            return True
        m_unpublish_ban=re.fullmatch(r"mbp@(.+)",text.strip(),re.I)
        if m_unpublish_ban:
            if not _is_master_name(sender):
                if is_private:
                    self.send_private_text(sender, "🔒 فك منع النشر متاح للماستر فقط. أرسل الأمر من حساب الماستر المسجل.")
                return True
            target=m_unpublish_ban.group(1).strip().lstrip("@")
            if not target:
                self.send_private_text(sender,"❌ الصيغة: mbp@اسم المستخدم")
                return True
            removed = _remove_publish_ban(target)
            if _is_publish_banned(target):
                self.send_private_text(sender, f"❌ تعذر فك منع النشر عن {target}; بقي الحظر فعالًا. تحقق من صلاحية الكتابة على ملف بيانات البوت.")
            elif removed:
                self.send_private_text(sender, f"✅ تم فك منع النشر عن {target} وإزالة {removed} سجل(ات).")
            else:
                self.send_private_text(sender, f"ℹ️ {target} غير موجود في قائمة منع النشر.")
            return True

        # Publishing: master or verified user says نشر / انشر (optionally @description), then sends an image.
        if _is_publish_command(text):
            desc = _publish_description(text)
            if _is_publish_banned(sender):
                self.send_private_text(sender,"🚫 حسابك ممنوع من النشر حالياً.\n📌 لفك المنع راجع الماستر.")
                return True
            hit = self._find_publish_filter_hit(desc)
            if hit:
                self.send_private_text(sender,
                    f"🚫 تم منع أمر النشر: الوصف يحتوي كلمة محظورة ({hit}).\n"
                    "⛔ تم منع حسابك من النشر حتى فك المنع.")
                self.publish_pending.pop(_norm_user(sender), None)
                _record_filter_ban(sender, room, "محاولة نشر كلمة مسيئة في الوصف", hit)
                _record_publish_ban(sender, room, hit)
                return True
            # The image may be sent later in a room or in private chat.
            # Key the pending publish by sender, not by the command room, so
            # sending the image from another room still completes the publish.
            self.publish_pending[_norm_user(sender)]={"description":desc,"source_room":str(room or ""),"created_at":time.time(),"silent":False}
            self.send_private_text(sender,"🖼️ تم استلام أمر النشر. أرسل الصورة الآن خلال دقيقتين في الروم أو الخاص، وسيتم نشرها في جميع الغرف." + (f"\n📝 الوصف: {desc}" if desc else ""))
            return True
        return False
