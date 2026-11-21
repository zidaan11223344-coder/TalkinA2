"""Incoming event deduplication, room event handlers, message routing and worker lanes."""

class EventRoutingSection:
    def _is_duplicate_incoming(self, kind, values, event_id=""):
        """Return True when the server has replayed an inbound event.

        Event id 41 is preferred. Some Talkin server versions omit it, so a
        short-lived content signature is used as a fallback. The cache is
        intentionally in memory: it is transport de-duplication state, not
        bot data that should be persisted to GitHub.
        """
        now = time.time()
        if not hasattr(self, "_incoming_seen"):
            self._incoming_seen = {}
        if not hasattr(self, "_incoming_seen_lock"):
            self._incoming_seen_lock = threading.Lock()
        event_id = str(event_id or "").strip()
        if event_id:
            # Some server versions recycle event ids for separate messages.
            # Bind the id to the message content so a later بنك/roll is not
            # suppressed as a replay of an earlier command.
            raw = "\x1f".join(str(v or "") for v in values)
            signature = hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()
            key = (str(kind), "id", event_id, signature)
            ttl = 3.0
        else:
            raw = "\x1f".join(str(v or "") for v in values)
            key = (str(kind), "sig", hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest())
            # Without a server event id, the signature can only protect against
            # the same frame being replayed immediately. A long content TTL
            # incorrectly swallowed legitimate repeated commands such as roll/بنك.
            ttl = 0.45
        with self._incoming_seen_lock:
            previous = self._incoming_seen.get(key, 0.0)
            self._incoming_seen[key] = now
            if len(self._incoming_seen) > 2000:
                cutoff = now - 300.0
                self._incoming_seen = {k: ts for k, ts in self._incoming_seen.items() if ts >= cutoff}
        return bool(previous and now - previous < ttl)

    def _handle_reaction_command(self, room, command, sender):
        """Route reactions privately and acknowledge them in the originating room."""
        value = str(command or "").strip()
        repost = re.fullmatch(r"x@([A-Za-z0-9]{4})", value, re.I)
        if repost:
            code = repost.group(1).lower()
            info = getattr(self, "reaction_targets", {}).get(code)
            if not isinstance(info, dict) or info.get("kind") != "explore":
                return False
            if time.time() - float(info.get("created_at", 0) or 0) > 86400:
                return False
            media_url = str(info.get("media_url") or "")
            repost_media_type = str(info.get("media_type") or "image").strip().lower()
            if repost_media_type not in {"image", "video"}:
                repost_media_type = "image"
            if not media_url or self.send_room_media(room, media_url, repost_media_type) is False:
                self.send_room_text(room, "تعذر إعادة نشر هذا المنشور الآن.")
                return True
            original = str(info.get("caption") or "")
            original = re.sub(r"\n🔁 أعاد النشر:.*$", "", original, flags=re.S)
            self.send_room_text(room, original + f"\n🔁 أعاد النشر: @{str(sender or '').lstrip('@')}")
            return True
        reaction = re.fullmatch(r"(lk|lv|dl|cm|report)@([A-Za-z0-9]{4})(?:\s+(.*))?", value, re.I)
        if not reaction:
            return False
        action, code = reaction.group(1).lower(), reaction.group(2).lower()
        extra = (reaction.group(3) or "").strip()
        info = getattr(self, "reaction_targets", {}).get(code)
        if not isinstance(info, dict) or time.time() - float(info.get("created_at", 0) or 0) > 86400:
            return False
        publisher = str(info.get("publisher") or "").strip()
        source_kind = str(info.get("kind") or "").lower()
        source_desc = str(info.get("description") or info.get("title") or "").strip()
        labels = {"lk": "👍 إعجاب", "lv": "❤️ حب", "dl": "👎 عدم إعجاب", "cm": "💬 تعليق", "report": "🚨 بلاغ"}
        owner_label = "صاحب الأغنية" if source_kind == "music" else "ناشر الصورة"
        notice = f"{labels[action]} على المحتوى المنشور\n🆔 الرمز: {code}\n👤 المتفاعل: {sender}\n📌 {owner_label}: {publisher}"
        if source_desc: notice += f"\n📝 المحتوى: {source_desc}"
        if extra: notice += f"\n💬 رسالة التفاعل: {extra}"
        notice += f"\n🏠 الغرفة: {room}"
        english_room = self._configured_room_language(room) == "en"
        room_ack = ({"lk": "You liked the post", "dl": "You disliked the post", "lv": "You loved the post", "report": "Report sent", "cm": "Your comment was sent"}[action]
                    if english_room else
                    {"lk": "لقد أعجبت بمنشور", "dl": "لم تعجب", "lv": "أحببت", "report": "تم إرسال البلاغ", "cm": "تم إرسال تعليقك"}[action])
        self.send_room_text(room, f"@{str(sender or '').lstrip('@')} {room_ack}")
        if action == "report":
            master = str(BOT_MASTER or "").strip()
            if master: self.send_private_text(master, notice)
            return True
        if publisher:
            self.send_private_text(publisher, notice)
        return True

    def _translate_text_external(self, text, target_language):
        value = str(text or "").strip()
        if not value: return ""
        target = "en" if str(target_language).casefold() == "en" else "ar"
        response = requests.get("https://translate.googleapis.com/translate_a/single",
            params={"client":"gtx","sl":"auto","tl":target,"dt":"t","q":value[:1800]},
            headers={"User-Agent":"Mozilla/5.0"}, timeout=(5, 15))
        response.raise_for_status()
        data = response.json(); parts = data[0] if isinstance(data, list) and data else []
        return "".join(str(x[0]) for x in parts if isinstance(x, list) and x and x[0]).strip()

    def _handle_translation_subscription(self, room, body, sender):
        command = str(body or "").strip()
        stop_match = re.fullmatch(
            r"(?:إيقاف|ايقاف|وقف)\s*الترجم(?:ة|ه)(?:\s*@?([^\s@]+))?|(?:stoptranslation|stoptranslate|stop\s+translation)(?:@|\s+)?([^\s@]+)?",
            command, re.I,
        )
        if stop_match:
            target = (stop_match.group(1) or stop_match.group(2) or "").strip().lstrip("@")
            subscriptions = getattr(self, "translation_subscriptions", {})
            removed = 0
            if isinstance(subscriptions, dict):
                if target:
                    target_key = _norm_user(target)
                    entries = subscriptions.get(target_key, {})
                    if isinstance(entries, dict) and _norm_user(sender) in entries:
                        entries.pop(_norm_user(sender), None); removed += 1
                    if isinstance(entries, dict) and not entries:
                        subscriptions.pop(target_key, None)
                else:
                    requester_key = _norm_user(sender)
                    for target_key in list(subscriptions):
                        entries = subscriptions.get(target_key, {})
                        if isinstance(entries, dict) and requester_key in entries:
                            entries.pop(requester_key, None); removed += 1
                        if isinstance(entries, dict) and not entries:
                            subscriptions.pop(target_key, None)
            message = "✅ تم إيقاف الترجمة." if removed else "ℹ️ لا يوجد اشتراك ترجمة لإيقافه."
            self.send_room_text(room, message) if room else self.send_private_text(sender, message)
            return True
        match = re.fullmatch(r"(انجليزي|عربي)@([^\s@]+)", command, re.I)
        if not match: return False
        requested, target = match.group(1), match.group(2).strip().lstrip("@")
        language = "en" if requested.casefold() == "انجليزي" else "ar"
        subscriptions = getattr(self, "translation_subscriptions", None)
        if not isinstance(subscriptions, dict): subscriptions = self.translation_subscriptions = {}
        subscription_room = str(room or getattr(self, "room", "") or getattr(self, "last_joined_room", "") or "").strip()
        subscriptions.setdefault(_norm_user(target), {})[_norm_user(sender)] = {"requester": str(sender or ""), "language": language, "room": subscription_room, "created_at": time.time()}
        msg = (f"✅ ستصل ترجمة رسائل @{target} إلى الغرفة باللغة الإنجليزية." if language == "en" else f"✅ ستصل ترجمة رسائل @{target} إلى الغرفة باللغة العربية.")
        self.send_private_text(sender, msg) if not room else self.send_room_text(room, msg)
        return True

    def _translate_subscribed_message(self, sender, body, room=""):
        entries = (getattr(self, "translation_subscriptions", {}) or {}).get(_norm_user(sender), {})
        if not isinstance(entries, dict): return
        def worker():
            for requester_key, item in list(entries.items()):
                try:
                    translated = self._translate_text_external(body, item.get("language", "en"))
                    if translated:
                        label = "English" if item.get("language") == "en" else "العربية"
                        target_room = str(item.get("room") or "").strip()
                        if target_room:
                            self.send_room_text(target_room, f"🌐 ترجمة ({label}) من @{sender}:\n{translated}")
                except Exception as exc:
                    self.log("[TRANSLATE] failed:", type(exc).__name__, str(exc)[:160])
        threading.Thread(target=worker, name="message-translation", daemon=True).start()

    def _send_personal_download_link(self, room, sender, design_kind, is_private=False):
        """Publish a browser-downloadable link for the user's own image."""
        sender = str(sender or "").strip().lstrip("@")
        key = _norm_user(sender)
        if not key:
            return True
        base = _public_base_url()
        if not base:
            message = "❌ رابط الوسائط العام PUBLIC_BASE_URL غير مضبوط."
            self.send_private_text(sender, message) if is_private or not room else self.send_room_text(room, message)
            return True

        filename = ""
        if design_kind == "name":
            designs = getattr(self, "personal_designs", {})
            user_designs = designs.get(key) if isinstance(designs, dict) else None
            design = user_designs.get("name") if isinstance(user_designs, dict) else None
            filename = Path(str((design or {}).get("filename") or "")).name if isinstance(design, dict) else ""
            if not filename.startswith("name3d_") or not filename.lower().endswith((".gif", ".jpg")):
                message = "ℹ️ أنشئ بطاقة الاسم أولاً بالأمر: اسم <اسم المستخدم>"
                self.send_private_text(sender, message) if is_private or not room else self.send_room_text(room, message)
                return True
            source_path = BASE_DIR / "generated_publish" / filename
            media_path = _prepare_name_design_for_download(source_path)
        else:
            photo_url = (getattr(self, "user_photos", {}).get(key, "")
                         or self._lookup_profile_photo(sender))
            media_path = _store_profile_photo_for_download(photo_url) if photo_url else None
            if media_path is None:
                try:
                    self._queue_profile_roster_refresh(room, sender)
                except Exception:
                    pass
                message = "⏳ تعذر تحميل صورة حسابك الآن. أعد الأمر بعد لحظات."
                self.send_private_text(sender, message) if is_private or not room else self.send_room_text(room, message)
                return True
            filename = media_path.name

        if media_path is None or not media_path.is_file():
            message = "❌ الصورة غير موجودة على الخادم. أعد إنشاءها ثم حاول مرة أخرى."
            self.send_private_text(sender, message) if is_private or not room else self.send_room_text(room, message)
            return True
        media_url = f"{base}/publish/{quote(filename)}"
        try:
            self._verify_public_media_url(media_url, "image")
        except Exception as exc:
            self.log("[PERSONAL-DOWNLOAD] media verification failed:", repr(exc))
            message = "❌ تعذر تجهيز رابط التحميل حالياً. حاول مرة أخرى."
            self.send_private_text(sender, message) if is_private or not room else self.send_room_text(room, message)
            return True

        label = "بطاقة اسمك" if design_kind == "name" else "صورتك الشخصية"
        message = (f"📥 رابط تحميل {label}:\n{media_url}\n"
                   "🌐 افتح الرابط في المتصفح ثم اختر تنزيل/حفظ الصورة.")
        self.send_private_text(sender, message) if is_private or not room else self.send_room_text(room, message)
        return True

    def handle_room_event(self, result):
        event = (result.get("room_event") or {}) if isinstance(result, dict) else {}
        event_type = str(event.get(1, ""))
        room = str(event.get(13, getattr(self, "room", "")) or "")
        body = str(event.get(6, "") or "")
        if event_type != "text" or not body:
            return self._handle_room_event_impl(result)

        detected = _detect_text_language(body)
        key = (_norm_room(room), _norm_user(event.get(22, "") or event.get(2, "")))
        recent = getattr(self, "_recent_room_command_languages", None)
        if recent is None:
            recent = self._recent_room_command_languages = {}
        if detected and _looks_like_bot_command(body):
            recent[key] = (detected, time.monotonic())
            if len(recent) > 2048:
                cutoff = time.monotonic() - 600.0
                self._recent_room_command_languages = {
                    saved_key: entry for saved_key, entry in recent.items()
                    if entry[1] >= cutoff
                }
                recent = self._recent_room_command_languages
        language = detected
        if language is None and re.fullmatch(r"[0-9٠-٩]", body.strip()):
            previous = recent.get(key)
            if previous and time.monotonic() - previous[1] <= 180.0:
                language = previous[0]

        token = self._push_room_language_override(room, language)
        try:
            return self._handle_room_event_impl(result)
        finally:
            self._restore_room_language_override(token)

    def _handle_room_event_impl(self, result):
        event = result.get("room_event") or {}
        if not hasattr(self, "blocked_rooms"):
            self.blocked_rooms = set()
        if not hasattr(self, "_blocked_room_notices"):
            self._blocked_room_notices = set()
        event_type = str(event.get(1, ""))
        frm = str(event.get(2, ""))
        to = str(event.get(3, ""))
        body = str(event.get(6, ""))
        room = str(event.get(13) or self.room or "").strip()
        # Talkin gateways do not all use the same event spelling for a
        # moderation removal. Convert the explicit variants to the canonical
        # role_changed/kicked path; user_left alone remains intentionally
        # ambiguous and does not trigger a rejoin.
        event_type_alias = event_type.casefold().strip()
        if event_type_alias in {"user_kicked", "member_kicked", "room_kicked", "kicked", "removed", "member_removed"}:
            event_type = "role_changed"
            event = dict(event)
            event.setdefault(31, "kicked")
        # Receiving an actual room text event proves this session can see that
        # room, even if the server omitted/reordered the explicit join ack.
        if room and event_type == "text":
            if not hasattr(self, "connected_rooms") or not isinstance(self.connected_rooms, set):
                self.connected_rooms = set()
            self.connected_rooms.add(room)
        secret_pending = getattr(self, "_secret_export_auth_pending", {})
        is_secret_password_reply = (
            isinstance(secret_pending, dict) and _norm_user(frm) in secret_pending
        )
        if body and not is_secret_password_reply:
            self._remember_inflight_command(room, body, frm, is_private=False)
        if room and room != BOT_MASTER and _norm_room(room) not in getattr(self, "blocked_rooms", set()):
            # This handler runs for every room message.  Rewriting the JSON
            # file (and possibly enqueueing DB/GitHub persistence) on every
            # event blocks the WebSocket reader and makes commands feel slow.
            # Persist only when a genuinely new room is discovered.
            known_before = len(self.known_rooms)
            self.known_rooms.add(room)
            if len(self.known_rooms) != known_before:
                _save_persistent_rooms(self.known_rooms)
        event_id = str(event.get(41, ""))
        event_user_17 = str(event.get(17, "") or "").strip()
        event_user_22 = str(event.get(22, "") or "").strip()
        if event_type in ("user_joined", "user_left"):
            username = event_user_17 or event_user_22
            if event_type == "user_left" and _norm_user(username) == _norm_user(BOT_ID):
                # A leave packet immediately following a ban can identify the
                # bot as actor/initiator (in either native field), not as the
                # account removed. Do not let that ambiguous echo schedule a
                # leave/rejoin cycle when this room's pending moderation named
                # a different target. A moderation request targeting the bot
                # itself is never suppressed.
                marker = getattr(self, "_recent_room_moderation", {}).get(_norm_room(room))
                if (isinstance(marker, dict)
                        and float(marker.get("created_at", 0) or 0) >= time.time() - 20
                        and _norm_user(marker.get("target")) != _norm_user(BOT_ID)):
                    self.log(f"[MOD_EVENT] ignored ambiguous bot user_left after moderation of another account in {room}")
                    username = ""
        else:
            username = event_user_22
        monitored_username = username or frm or str(event.get(17, "") or "").strip()
        self._report_monitored_event(room, event_type, monitored_username, body)
        # Some server builds return L@a/L@m/L@o as a RoomEvent text `User List:`.
        # Consume that native response before ordinary room-command handling.
        if self._complete_pending_room_list_text(room, body):
            return
        if event_type == "text" and body:
            if self._handle_translation_subscription(room, body, frm): return
            self._translate_subscribed_message(frm, body, room)
        if event_type == "text" and self._handle_telegram_log_command(body, frm):
            return
        # NS is a navigation command. Every newly received NS must be accepted
        # immediately; do not let the transport replay/duplicate cache suppress
        # rapid NS presses.
        is_ns_navigation = event_type == "text" and _is_ns_command(body)
        role_field_present = 8 in event or "role" in event
        role = str(event.get(8, event.get("role", "")) or "").strip().lower()
        count = str(event.get(23, "") or "").strip()
        reconnected = str(event.get(24, "") or "").strip()
        # Do not log room message contents, usernames, room names, or media events.
        _skip_room_text_signature_dedup = (not event_id and event_type == "text")
        if (
            not is_ns_navigation
            and not _skip_room_text_signature_dedup
            and self._is_duplicate_incoming(
                "room",
                (event_type, room, frm, to, body, str(event.get(7, "") or ""),
                 username if event_type in ("user_joined", "user_left") else "",
                 role if event_type in ("user_joined", "user_left") else ""),
                event_id,
            )
        ):
            if ROOM_EVENT_DEBUG:
                self.log("[DEDUP] ignored repeated room event")
            return

        # Keep the live membership state in sync.  The APK itself uses these
        # exact event names and RoomEvent fields.
        if event_type in ("user_joined", "user_left") and username:
            pcfg = _room_protection_cfg(room)
            now = time.time()
            norm_u = _norm_user(username)
            norm_r = _norm_room(room)

            # --- حماية الفلود: ترقية سريعة (نكّان خلال 1.25 ثانية) أو
            # مستمرة (3 خلال 5 ثوانٍ)، ثم حظر الحسابات اللاحقة في نفس الموجة. ---
            if (event_type == "user_joined" and pcfg.get("flood")
                    and norm_u != _norm_user(BOT_ID)):
                candidates = _join_flood_candidates(
                    self, room, username, role if role_field_present else None, now=now
                )
                if candidates:
                    self.log(f"[FLOOD] كشف دخول سريع/جماعي في {room}: {len(candidates)} حسابات مستهدفة")
                    banned_names = []
                    for u_flood in candidates:
                        try:
                            # Send the native IP ban synchronously before this
                            # room event handler proceeds to later messages.
                            self.send_admin(room, u_flood, "ban_ip")
                            self._join_flood_banned[norm_r].add(_norm_user(u_flood))
                            _record_filter_ban(u_flood, room, "حماية الفلود", "دخول سريع/جماعي (حظر IP)")
                            banned_names.append(f"@{u_flood}")
                        except Exception as f_err:
                            self.log("[FLOOD] ban_ip error:", repr(f_err))
                    if banned_names:
                        if self._configured_room_language(room) == "en":
                            notice = f"🚫 The flooders have been rounded up! 😋😁 ({len(banned_names)} account(s) IP-banned)"
                        else:
                            notice = f"🚫 تم جمع الفلود إلى الزريبة😋😁 (حظر IP لـ {len(banned_names)} حساب)"
                        self.send_room_text(room, notice)

            # --- حماية الدخول والخروج: كشف تكرار الدخول والخروج لنفس النك وحظره ---
            if (pcfg.get("joinleave") and norm_u != _norm_user(BOT_ID)
                    and _auto_ban_candidate(self, room, username, role if role_field_present else None)):
                st = self._joinleave_state[norm_r][norm_u]
                evs = st.setdefault("events", [])
                evs[:] = [x for x in evs if now - float(x[0]) <= 300]
                evs.append((now, event_type))
                # 3 حركات دخول/خروج خلال 5 دقائق
                if len(evs) >= 3 and now >= float(st.get("banned_until", 0) or 0):
                    try:
                        self.send_admin(room, username, "ban_ip")
                        # This is a real moderation ban. Do not schedule an
                        # automatic unban after two minutes; only .u/member
                        # or an explicit admin action may restore the role.
                        st["banned_until"] = float("inf")
                        _record_filter_ban(username, room, "حماية الدخول والخروج", "تكرار الدخول والخروج (حظر IP)")
                        self.send_room_text(room, f"🚫 تم حظر @{username} (حظر IP) بسبب تكرار الدخول والخروج.")
                    except Exception as exc:
                        self.log("[JOINLEAVE] ban failed", repr(exc))

        if event_type == "user_joined" and username:
            # Numeric RoomEvent field 3 is `to`, not a profile photo. Only
            # trust explicitly named avatar fields; UserItem field 3 is handled
            # separately by the roster decoder.
            joined_photo = _extract_room_event_profile_photo(event)
            if joined_photo.startswith("//"):
                joined_photo = "https:" + joined_photo
            if joined_photo.startswith(("http://", "https://")):
                if not isinstance(getattr(self, "user_photos", None), dict):
                    self.user_photos = {}
                self.user_photos[username.casefold()] = joined_photo
                self.user_photos[_norm_user(username)] = joined_photo
            else:
                # Join notifications often omit the profile URL; refresh the
                # full roster asynchronously so game cards can use it later.
                self._queue_profile_roster_refresh(room, username)

            # Optional room protection: reject accounts with no profile photo.
            # The cache above is used as a fallback when a join event omits it.
            if (_room_protection_cfg(room).get("no_photo")
                    and _norm_user(username) != _norm_user(BOT_ID)
                    and _auto_ban_candidate(self, room, username, role if role_field_present else None)):
                joined_photo = _extract_room_event_profile_photo(event)
                joined_photo = joined_photo or str(getattr(self, "user_photos", {}).get(_norm_user(username), "") or "").strip()
                if not joined_photo:
                    # A join event commonly omits the avatar even when the
                    # account has one. Let the native roster request above
                    # populate the cache before enforcing this room rule.
                    def verify_joined_photo():
                        deadline = time.monotonic() + 8.0
                        while time.monotonic() < deadline:
                            photos = getattr(self, "user_photos", {})
                            photo = (photos.get(_norm_user(username), "") or
                                     photos.get(username.casefold(), "")) if isinstance(photos, dict) else ""
                            if str(photo or "").startswith(("http://", "https://")):
                                return
                            stop_event = getattr(self, "stop_event", None)
                            if stop_event is not None and stop_event.is_set():
                                return
                            time.sleep(0.25)
                        try:
                            self.send_admin(room, username, "ban")
                            _record_filter_ban(username, room, "حساب بلا صورة", "حماية الصورة")
                            self.send_room_text(room, f"🚫 تم حظر @{username} لعدم وجود صورة حساب.")
                        except Exception as exc:
                            self.log("[NO-PHOTO] deferred ban failed", repr(exc))

                    threading.Thread(
                        target=verify_joined_photo,
                        name="no-profile-photo-check",
                        daemon=True,
                    ).start()
            room_members = self.room_users.setdefault(room, {})
            already_present = any(_norm_user(member) == norm_u for member in room_members)
            room_members[username] = role or "none"
            _remember_roster(room, [{"username": username, "role": role or "none"}])
            # Keep a separate history for .r. Only events received after the
            # bot's own you_joined/you_rejoined timestamp are recorded.
            entry_room = _norm_room(room)
            entry_ts = float(getattr(self, "_room_entry_times", {}).get(entry_room, 0) or 0)
            if entry_ts and _norm_user(username) != _norm_user(BOT_ID) and time.time() >= entry_ts:
                entry_map = getattr(self, "_room_entry_users", {}).setdefault(entry_room, {})
                entry_map.setdefault(_norm_user(username), username)
            self.last_joined_room = room
            if _norm_user(username) == _norm_user(BOT_MASTER):
                self._master_online_rooms.add(_norm_room(room))
                self.master_online = True
                self.master_last_seen = time.time()
            # Welcome the master using the exact configured BOT_MASTER account.
            welcome = None
            cw = None
            if _norm_user(username) == _norm_user(BOT_MASTER):
                welcome = f"👑 لقد أتاكم الزعيم\n👤 {username}\n🏠 الغرفة: {room}"
            elif username and _norm_user(username) != _norm_user(BOT_ID):
                welcomes_enabled = bool(getattr(self, "custom_welcome_enabled", True))
                cw = getattr(self, "custom_welcomes", {}).get(_norm_user(username)) if welcomes_enabled else None
                welcome = _join_welcome_message(username, room, welcomes_enabled, cw)
            if username and _norm_user(username) != _norm_user(BOT_ID) and self._is_dragon_hunter(username):
                if self._configured_room_language(room) == "en":
                    hunter_welcome = f"🐉 Welcome back, Dragon Hunter @{username}!"
                else:
                    hunter_welcome = f"🐉 أهلاً بعودة صائد التنين @{username}!"
                welcome = f"{welcome}\n{hunter_welcome}" if welcome else hunter_welcome
            is_fresh_join = not already_present and reconnected.casefold() not in {"1", "true", "yes", "on"}
            if welcome and is_fresh_join:
                self.send_room_text(room, welcome)
            self._send_saved_custom_welcome_image(
                room, username, cw, fresh_join=is_fresh_join,
            )
        if event_type in ("you_joined", "you_rejoined"):
            if room:
                # Non-blocking native roster refresh: UserItem responses fill
                # user_photos in the event workers, never on the socket reader.
                self._queue_profile_roster_refresh(room)
                entry_room = _norm_room(room)
                entry_time = time.time()
                getattr(self, "_room_entry_times", {}).update({entry_room: entry_time})
                # Start a fresh history for this bot entry/re-entry. Members
                # who were already in the room are NOT part of .r.
                getattr(self, "_room_entry_users", {}).pop(entry_room, None)

        elif event_type == "user_left" and username:
            if _norm_user(username) in {_norm_user(BOT_ID), _norm_user(PRIMARY_BOT_ID)}:
                self._handle_unconfirmed_bot_user_left(room)
                reason_raw = str(event.get("reason", "") or event.get(31, "") or event.get("status", ""))
                reason = reason_raw.casefold()
                explicit_removal = any(token in reason for token in ("kick", "remove", "ban", "طرد", "إزالة", "حظر"))
                bot_is_target = _norm_user(event_user_17) in {
                    _norm_user(BOT_ID), _norm_user(PRIMARY_BOT_ID)
                }
                actor = str(event.get(22, "") or event.get("actor", "") or "").strip()
                actor_is_other_account = bool(actor) and _norm_user(actor) not in {
                    _norm_user(BOT_ID), _norm_user(PRIMARY_BOT_ID)
                }
                intentional = _norm_room(room) in getattr(self, "_intentional_leaves", set())
                # When the packet identifies the bot as the removed account,
                # the executor is irrelevant: admin, owner, master, or any
                # other room manager must all lead to automatic rejoin. The
                # actor-only packet (field 22 == bot, with another target or
                # no target) remains ignored to avoid rejoining after a ban
                # issued by the bot against someone else.
                if (not intentional and (explicit_removal or
                        (bot_is_target and actor_is_other_account))):
                    pending_notices = getattr(self, "_pending_kick_notices", None)
                    if not isinstance(pending_notices, set):
                        pending_notices = self._pending_kick_notices = set()
                    pending_notices.add(_norm_room(room))
                    self.log(f"[KICK_DETECTED] bot left as removed target from {room}; rejoining automatically...")
                    self._schedule_auto_rejoin(room)
            else:
                self.room_users.get(room, {}).pop(username, None)
                departed = getattr(self, "_last_departed_user_by_room", None)
                if departed is None:
                    departed = self._last_departed_user_by_room = {}
                departed[_norm_room(room)] = username
            if _norm_user(username) == _norm_user(BOT_MASTER):
                self._master_online_rooms.discard(_norm_room(room))
                self.master_online = bool(self._master_online_rooms)
        elif event_type == "role_changed":
            # In native RoomEvent packets field 17 is the affected account and
            # field 31 is its resulting role. Field 22 can identify the actor
            # who issued the moderation command, so never use it as a fallback:
            # doing so can falsely mark the bot as outcast when it bans a member.
            changed_role = str(
                event.get(31, "") or event.get("role", "") or event.get("value", "")
            ).strip().lower()
            changed_role = {
                "kick": "kicked", "remove": "kicked", "removed": "kicked",
                "evicted": "kicked", "ejected": "kicked", "forced_out": "kicked",
                "ban": "outcast", "banned": "outcast", "blocked": "outcast",
            }.get(changed_role, changed_role)
            explicit_candidates = [
                str(event.get(name, "") or "").strip()
                for name in ("target", "to", "username", "user")
                if str(event.get(name, "") or "").strip()
            ]
            field_candidates = [value for value in (event_user_17, event_user_22) if value]
            candidates = list(dict.fromkeys(explicit_candidates + field_candidates))
            changed_user = explicit_candidates[0] if explicit_candidates else event_user_17
            if not changed_user and event.get("affected_user"):
                changed_user = str(event.get("affected_user") or "").strip()
                candidates.append(changed_user)
            bot_keys = {_norm_user(BOT_ID), _norm_user(PRIMARY_BOT_ID), _norm_user(getattr(self, "bot_id", ""))}
            bot_keys.discard("")
            pending_target_is_nonbot = False
            if changed_role in ("kicked", "outcast"):
                for pending_action in getattr(self, "pending_admin_actions", {}).values():
                    target_key = _norm_user(pending_action.get("target"))
                    if (str(pending_action.get("role", "")).casefold() == changed_role
                            and target_key and target_key not in bot_keys
                            and any(_norm_user(candidate) == target_key for candidate in candidates)):
                        pending_target_is_nonbot = True
                        break
                # For outcast/ban packets field 22 is commonly the admin
                # who issued the ban, not the affected account. Only infer
                # the bot target from that ambiguous field for a kick event;
                # a real bot ban must carry an explicit target or pending
                # moderation action.
                bot_candidate = next((candidate for candidate in candidates if _norm_user(candidate) in bot_keys), "")
                if bot_candidate and not pending_target_is_nonbot and changed_role == "kicked":
                    changed_user = bot_candidate
            # A pending action is authoritative when the native packet carries
            # both actor and target but their field positions vary by server.
            if changed_role:
                with self.pending_admin_lock:
                    for pending_action in self.pending_admin_actions.values():
                        if (_norm_room(pending_action.get("room")) == _norm_room(room)
                                and str(pending_action.get("role", "")).lower() == changed_role):
                            target_key = _norm_user(pending_action.get("target"))
                            match = next((candidate for candidate in candidates
                                          if _norm_user(candidate) == target_key), None)
                            if match:
                                changed_user = match
                                break
            if _norm_user(changed_user) == _norm_user(BOT_ID):
                recent = getattr(self, "_recent_room_moderation", {}).get(_norm_room(room))
                if (isinstance(recent, dict)
                        and float(recent.get("created_at", 0) or 0) >= time.time() - 20
                        and _norm_user(recent.get("target")) != _norm_user(BOT_ID)):
                    target_key = _norm_user(recent.get("target"))
                    actual_target = next((candidate for candidate in candidates
                                          if _norm_user(candidate) == target_key), None)
                    if actual_target:
                        changed_user = actual_target
                    else:
                        self.log(f"[MOD_EVENT] ignored ambiguous bot role_changed after moderation of another account in {room}")
                        changed_user = ""
            if changed_user and changed_role:
                if changed_role in ("kicked", "outcast"):
                    self.room_users.get(room, {}).pop(changed_user, None)
                    if _norm_user(changed_user) == _norm_user(BOT_ID):
                        room_key = _norm_room(room)
                        was_connected = any(
                            _norm_room(connected).casefold() == room_key.casefold()
                            for connected in getattr(self, "connected_rooms", set())
                        )
                        recent_departures = getattr(self, "_recent_bot_room_departures", {})
                        departed_recently = (
                            isinstance(recent_departures, dict)
                            and 0 <= time.time() - float(recent_departures.get(room_key, 0) or 0) <= 120
                        )
                        was_in_room = was_connected or departed_recently
                        if isinstance(recent_departures, dict):
                            recent_departures.pop(room_key, None)
                        self.connected_rooms = {
                            connected for connected in self.connected_rooms
                            if _norm_room(connected).casefold() != _norm_room(room).casefold()
                        }
                        intentional = _norm_room(room) in getattr(self, "_intentional_leaves", set())
                        if changed_role in ("kicked", "outcast") and was_in_room and not intentional:
                            pending_notices = getattr(self, "_pending_kick_notices", None)
                            if not isinstance(pending_notices, set):
                                pending_notices = self._pending_kick_notices = set()
                            pending_notices.add(_norm_room(room))
                            self.log(f"[KICK_DETECTED] Bot was removed from {room}; rejoining automatically...")
                            self._schedule_auto_rejoin(room)
                        elif intentional:
                            self.log(f"[ROLE_CHANGED] Ignoring rejoin for intentionally left room {room}.")
                else:
                    self.room_users.setdefault(room, {})[changed_user] = changed_role
                key = (room.casefold(), changed_user.casefold(), changed_role)
                with self.pending_admin_lock:
                    pending = self.pending_admin_actions.pop(key, None)
                if pending:
                    requester = str(pending.get("requester") or "").strip()
                    if changed_role == "outcast":
                        _record_bot_ban(
                            pending.get("target") or changed_user,
                            pending.get("room") or room,
                            requester,
                            "حظر إداري",
                        )
                        last_bans = getattr(self, "last_bot_bans", None)
                        if not isinstance(last_bans, dict):
                            last_bans = {}
                        last_bans[_norm_room(room)] = {
                            "room": str(pending.get("room") or room),
                            "target": str(pending.get("target") or changed_user).lstrip("@"),
                            "banned_by": requester,
                            "created_at": int(time.time()),
                            "active": True,
                            "undo_requested": False,
                            "undone": False,
                        }
                        self.last_bot_bans = last_bans
                        _save_local_json(LAST_BOT_BANS_FILE, last_bans)
                    elif changed_role == "member":
                        last_bans = getattr(self, "last_bot_bans", None)
                        if isinstance(last_bans, dict):
                            latest_ban = last_bans.get(_norm_room(room))
                            if (isinstance(latest_ban, dict)
                                    and _norm_user(latest_ban.get("target")) == _norm_user(changed_user)):
                                latest_ban["active"] = False
                                latest_ban["undo_requested"] = False
                                latest_ban["undone"] = True
                                latest_ban["unbanned_at"] = int(time.time())
                                _save_local_json(LAST_BOT_BANS_FILE, last_bans)
                    inverse = {
                        "kicked": "member",
                        "outcast": "member",
                        "member": pending.get("previous_role") or "outcast",
                        "admin": pending.get("previous_role") or "member",
                        "owner": pending.get("previous_role") or "member",
                    }.get(changed_role, "member")
                    if requester and _is_master_name(requester):
                        self.last_admin_actions[_norm_user(requester)] = {
                            "room": pending.get("room") or room,
                            "target": pending.get("target") or changed_user,
                            "operation": pending.get("role"),
                            "confirmed_role": changed_role,
                            "inverse": inverse,
                            "created_at": time.time(),
                        }
                    labels = {
                        "kicked": f"✅ تم طرد @{changed_user} من الغرفة.",
                        "outcast": f"🚫 تم حظر @{changed_user} من الغرفة.",
                        "member": f"✅ تم فك حظر @{changed_user}.",
                        "admin": f"🛡️ تم رفع @{changed_user} إلى مشرف.",
                        "owner": f"👑 تم رفع @{changed_user} إلى أونر.",
                    }
                    if pending.get("announce_room") and room and labels.get(changed_role):
                        self.send_room_text(room, labels[changed_role])
                    self.log(f"[MOD] server confirmed room={room} target=@{changed_user} role={changed_role}")
        if event_type in ("you_joined", "you_rejoined"):
            # The server's join acknowledgement is the source of truth for
            # the session-only connected-room list.
            if room:
                self.connected_rooms.add(room)
                room_key = _norm_room(room)
                getattr(self, "_recent_bot_room_departures", {}).pop(room_key, None)
                getattr(self, "_intentional_leaves", set()).discard(room_key)
            self.last_joined_room = room
            kick_notices = getattr(self, "_pending_kick_notices", set())
            if room and _norm_room(room) in kick_notices:
                kick_notices.discard(_norm_room(room))
                master = str(BOT_MASTER or "").strip().lstrip("@")
                if master:
                    try:
                        self.send_private_text(master, f"✅ تم طرد البوت فعليًا من الغرفة: {room}\n✅ عاد البوت إلى الغرفة بنجاح.")
                    except Exception as exc:
                        self.log("[ROOM] kick return notice failed:", repr(exc))
            rnorm = _norm_room(room)
            waiting_ack = getattr(self, "_auto_rejoin_waiting_ack", None)
            if waiting_ack is not None:
                waiting_ack.difference_update(
                    item for item in tuple(waiting_ack)
                    if _norm_room(item).casefold() == rnorm.casefold()
                )
            rejoin_state = getattr(self, "_auto_rejoin_state", {}).get(rnorm)
            if rejoin_state is not None:
                rejoin_state["joined_at"] = time.monotonic()
            auto_pending = getattr(self, "_auto_rejoin_pending", None)
            if auto_pending is not None:
                lock = getattr(self, "_auto_rejoin_lock", None)
                if lock is None:
                    auto_pending.discard(rnorm)
                else:
                    with lock:
                        auto_pending.discard(rnorm)
            pending_join = self._pending_room_joins.pop(rnorm, None)
            if pending_join and pending_join.get("timer"):
                pending_join["timer"].cancel()
            if rnorm in getattr(self, "blocked_rooms", set()):
                self.blocked_rooms.discard(rnorm)
                self._save_blocked_rooms()
            self._blocked_room_reasons.pop(rnorm, None)
            self._blocked_room_notices.discard(rnorm)
            if room:
                self.known_rooms.add(room)
                _save_persistent_rooms(self.known_rooms)
            requester = str((pending_join or {}).get("requested_by", "") or "").strip()
            # Confirm immediately when the server ACK arrives. Roster loading
            # can involve a slow database/WebSocket round trip and must never
            # delay the user's entry confirmation.
            if requester:
                self.send_private_text(requester, f"✅ أكد الخادم دخول البوت إلى الغرفة: {room}")
            if room:
                try:
                    self.request_room_occupants(room)
                except Exception as exc:
                    self.log("[ROOM] occupants refresh after join failed:", repr(exc))
            if requester:
                self.send_room_text(room, "مرحبا لقد عدت ادري وحشتكم🙂😋")
        elif event_type in (
            "room_full", "room_unauthorized", "room_wrong_password",
            "room_needs_captcha", "room_needs_password", "room_membership_required",
            "room_full_rejoin", "room_unauthorized_rejoin", "room_wrong_password_rejoin",
            "room_needs_captcha_rejoin", "room_needs_password_rejoin",
            "room_membership_required_rejoin",
        ):
            # IMPORTANT: do not immediately send room_join here.  These events
            # can be emitted repeatedly by the server when a room rejects a
            # join.  The old code answered every event with another room_join,
            # creating the visible leave/join loop.  A real reconnect is left
            # to run_once(), while a rejoin is attempted at most once after a
            # long cooldown and never recursively from this event handler.
            self.log("[ROOM] server requested rejoin; delayed reconnect")
            failure_events = {
                "room_unauthorized", "room_membership_required", "room_full",
                "room_wrong_password", "room_needs_password", "room_needs_captcha",
                "room_unauthorized_rejoin", "room_membership_required_rejoin",
                "room_full_rejoin", "room_wrong_password_rejoin",
                "room_needs_password_rejoin", "room_needs_captcha_rejoin",
            }
            if event_type in failure_events:
                blocked_room = _norm_room(room)
                getattr(self, "_room_restore_candidates", set()).discard(blocked_room)
                if any(_norm_room(item) == blocked_room for item in getattr(self, "connected_rooms", set())):
                    # A delayed/replayed rejection can arrive after the
                    # server has already emitted you_joined. The live ACK is
                    # authoritative; do not remove the room or tell the user
                    # that entry failed.
                    self.log("[ROOM] ignored stale join failure after confirmed entry:", room, event_type)
                    pending_join = self._pending_room_joins.pop(blocked_room, None)
                    if pending_join and pending_join.get("timer"):
                        pending_join["timer"].cancel()
                    if ACK_ROOM_EVENTS and result.get("uid"):
                        try:
                            self.ack(result["uid"])
                        except Exception as exc:
                            self.log("[ACK] stale join-failure ack failed:", exc)
                    return
                reason_text = self._room_failure_message(room, event_type)
                pending_join = self._pending_room_joins.pop(blocked_room, None)
                if pending_join and pending_join.get("timer"):
                    pending_join["timer"].cancel()
                normalized_event = event_type.replace("_rejoin", "")
                self._mark_room_blocked(
                    room, reason_text,
                    persistent=(normalized_event == "room_unauthorized"),
                )
                if normalized_event == "room_unauthorized":
                    advice = "ارفع البوت إشرافاً أو أونر ثم أعد المحاولة."
                elif normalized_event == "room_membership_required":
                    advice = "الغرفة للأعضاء فقط؛ أضف البوت للغرفة ثم أعد المحاولة."
                elif normalized_event == "room_full":
                    advice = "الغرفة ممتلئة؛ فرّغ مقعداً ثم أعد المحاولة."
                elif normalized_event in ("room_wrong_password", "room_needs_password"):
                    advice = "تأكد من كلمة مرور الغرفة ثم أعد المحاولة."
                else:
                    advice = "الغرفة تطلب تحققاً لا يستطيع البوت إكماله آلياً."
                notice=f"{reason_text}: {room}\n💡 {advice}"
                requested_by = str((pending_join or {}).get("requested_by", "") or "").strip()
                # Notify only the user who explicitly requested دخول@اسم_الغرفة.
                # Automatic room restoration after a restart has no requester,
                # so it must stay silent and must never notify BOT_MASTER.
                if requested_by and blocked_room not in self._blocked_room_notices:
                    self.send_private_text(requested_by, notice)
                    self._blocked_room_notices.add(blocked_room)

        if ACK_ROOM_EVENTS and result.get("uid"):
            try:
                self.ack(result["uid"])
            except Exception as e:
                self.log("[ACK] failed:", e)

        # لا نعتبر صورة الحساب في field 10 صورة نشر. هذا الحقل موجود أيضاً
        # في أحداث النص، ولذلك كان أمر «انشر» ينشر صورة صاحب الأمر فوراً.
        media_url = ""
        if event_type in {"image", "photo", "picture", "media", "file"}:
            media_url = next(
                (str(event.get(key, "") or "").strip()
                 for key in (7, 6, 9, 11, 12, 13, 14, 15,
                             "url", "media_url", "image_url", "file_url",
                             "photo", "attachment")
                 if str(event.get(key, "") or "").strip().startswith(("http://", "https://"))),
                "",
            ) or _extract_media_url_universal(event, body) or first_http_url(event)
        if not media_url and body and ("http://" in body or "https://" in body):
            image_match = re.search(
                r"https?://\S+\.(?:png|jpg|jpeg|webp|gif)(?:\?\S*)?",
                body, re.I,
            )
            if image_match:
                media_url = image_match.group(0).rstrip(",.;)]}")

        media_senders = []
        for candidate in (frm, event.get(22, ""), event.get(17, ""), event.get(2, ""),
                          event.get("sender", ""), event.get("username", ""), event.get("from", "")):
            candidate = str(candidate or "").strip()
            if candidate and candidate not in media_senders and _norm_user(candidate) != _norm_user(BOT_ID):
                media_senders.append(candidate)

        # A purchased personal welcome-image request owns the next image from
        # its sender and takes precedence over generic publishing.
        if media_url and getattr(self, "custom_welcome_image_pending", {}):
            if self._try_custom_welcome_image_media(room, media_url, media_senders):
                return

        # sav@ owns the next image from the same sender and has priority over
        # the generic public-image publisher.
        if media_url and getattr(self, "custom_gift_pending", {}):
            if self._try_custom_gift_media(room, media_url, media_senders):
                return

        # استخدم المسار المباشر نفسه الموجود في Talkin1 حتى لا تضيع أخطاء
        # معالجة الصورة داخل خيط خلفي.
        if getattr(self, "publish_pending", {}):
            if media_url or event_type in {"image", "photo", "picture", "media", "file"}:
                handled = bool(media_url and self._try_publish_pending_media(room, media_url, media_senders))
                if handled:
                    return
                if not media_url:
                    self.log("[PUBLISH] image event ignored: no usable media")
                return

        if event_type in {"image", "photo", "picture", "media", "file"} or (media_url and event_type not in {"text", "user_joined", "user_left"}):
            if media_url:
                self._try_publish_pending_media(room, media_url, media_senders)
            return

        if event_type != "text" or not body:
            return
        if frm == BOT_ID:
            return

        # Complete an in-room `دخول@اسم_الغرفة` language selection before
        # admin/verification gates.  This is intentionally available to normal
        # users because the original join request itself is public.
        _norm_ch_room = _normalize_lang_choice(body)
        if _norm_ch_room in ("1", "2"):
            response_room = str(room or getattr(self, "last_joined_room", "") or getattr(self, "room", "") or "").strip()
            pending_key, pending_join = self._find_pending_join_for_message(frm, response_room)
            if isinstance(pending_join, dict):
                if self._complete_join_rooms_language(frm, _norm_ch_room, response_room=response_room, is_private=False):
                    return

        # Direct private message for everyone:
        #   رساله اسم_المستخدم نص الرسالة
        #   رساله@اسم_المستخدم نص الرسالة
        # This MUST be intercepted before the admin-command gate so it is
        # never echoed to the room. No public confirmation is sent either.
        m_direct_public = re.fullmatch(r"(?:رساله|رسالة)\s*@?([^\s@]+)\s+(.+)", body.strip(), re.I | re.S)
        if m_direct_public:
            target = m_direct_public.group(1).strip()
            message_text = m_direct_public.group(2).strip()
            if target and message_text:
                self._deliver_user_private_message(frm, target, message_text)
                return

        # Personal bot ban: silently ignore subsequent traffic except for a
        # rate-limited acknowledgement. This is deliberately not a room ban.
        self.bot_blocked_users = getattr(self, "bot_blocked_users", _bot_blocked_users())
        if _norm_user(frm) in self.bot_blocked_users and not _is_master_name(frm):
            now_blocked = time.time()
            last_notice = float(getattr(self, "_bot_block_notice_at", {}).get(_norm_user(frm), 0.0) or 0.0)
            if now_blocked - last_notice >= 30.0:
                self.send_room_text(room, f"🚫 @{frm} تم حظرك اشتباه فلود")
                self._bot_block_notice_at[_norm_user(frm)] = now_blocked
            return
        # Administrative commands are private to the configured master. Do
        # not send an authorization message to other users and do not allow
        # verified/VIP users to reach the management handlers accidentally.
        is_publish_command = _is_publish_command(body)
        is_creator_allowed = bool(room and _room_manager(self, room, frm) and (
            body.strip().casefold() in ("تشغيل الحماية", "تشغيل الحمايه", "ايقاف الحماية", "إيقاف الحماية",
                                        "تشغيل الالعاب", "تشغيل الألعاب", "ايقاف الالعاب", "إيقاف الالعاب",
                                        "صانعي", "صناع", "صانعي الغرفة", "صناع الغرفة")
            or re.match(r"^[+-]?صانع(?:ي| الغرفة| الغرفه)?(?:@.+)?$", body.strip(), re.I)
        ))
        verified_admin_identity = (
            _verified_identity_candidate(username, frm, event.get(17, ""))
            if (_looks_like_admin_command(body) or is_publish_command) else ""
        )
        if (_looks_like_admin_command(body)
                and not _is_master_name(frm)
                and not is_creator_allowed
                and not (_is_mvip_master(frm) and _is_verification_manager_command(body))
                and not (re.fullmatch(r"sb@([^@]+)@(\d+)", body.strip(), re.I) and verified_admin_identity)
                and not (is_publish_command and verified_admin_identity)
                and body.strip().casefold() not in {"توب", "top", "توب الألعاب", "توب الالعاب", "top games", "games top", "ملياري", "مليارات", "مليوني", "ملايين"}):
            if not verified_admin_identity:
                self.send_room_text(room, f"🔒 @{frm} طلب توثيق لاستخدام أوامر البوت.\n{_verification_notice()}")
            return

        # Word filter runs before games/normal commands. It uses the same native
        # room ban operation as b@, with Arabic normalization and no public reply.
        room_cfg = _room_moderation_config(room)
        protection_cfg = _room_protection_cfg(room)
        if protection_cfg["flood"] and _auto_ban_candidate(self, room, frm, role if role_field_present else None):
            now = time.time()
            state = self._room_repeat_state[room]
            last_sender = str(state.get("sender", ""))
            last_text = str(state.get("text", ""))
            last_at = float(state.get("at", 0.0) or 0.0)
            same_sender = _norm_user(last_sender) == _norm_user(frm)
            same_text = _norm_filter_text(last_text) == _norm_filter_text(body.strip())
            within_window = now - last_at <= 30.0
            # Both counters are consecutive counters. A different sender
            # breaks the sender sequence; a different text breaks the text
            # sequence. Either counter can independently cause a ban.
            state["sender_count"] = int(state.get("sender_count", 0) or 0) + 1 if same_sender and within_window else 1
            state["text_count"] = int(state.get("text_count", 0) or 0) + 1 if same_text and within_window else 1
            state["sender"] = frm
            state["text"] = body.strip()
            state["at"] = now
            sender_count = int(state.get("sender_count", 0) or 0)
            text_count = int(state.get("text_count", 0) or 0)
            limit = room_cfg["repeat_limit"]
            if sender_count >= limit or text_count >= limit:
                try:
                    self.send_admin(room, frm, "ban")
                except Exception as exc:
                    self.log("[FLOOD] native ban failed", repr(exc))
                _record_filter_ban(frm, room, "تكرار مشبوه", "تكرار الرسائل")
                self.send_room_text(room, f"🚫 @{frm} تم حظره بسبب تكرار مشبوه.")
                state.clear()
                return
            if sender_count == limit - 1 or text_count == limit - 1:
                self.send_room_text(room, f"⚠️ تحذير @{frm}: الرسالة مكررة، الرسالة التالية ستؤدي إلى الحظر.")
                return
        # The word filter is controlled ONLY by the room's "حماية الغرفة من السب"
        # switch.  The legacy mf@on/mf@off setting manages the word list but must
        # never activate filtering by itself. This keeps the filter completely
        # inactive until the master explicitly enables protection option 1.
        filter_enabled = bool(protection_cfg.get("swear", False))
        room_words = room_cfg.get("words") or []
        if isinstance(room_words, dict):
            room_words = list(room_words.keys())
        elif not isinstance(room_words, (list, tuple, set)):
            room_words = [room_words] if isinstance(room_words, str) else []
        filter_words = sorted({str(word).strip() for word in room_words if str(word).strip()} | set(self.banned_words))
        normalized_body = _norm_filter_text(body)
        hit = (
            next((w for w in filter_words
                  if _norm_filter_text(w) and _norm_filter_text(w) in normalized_body), None)
            if filter_enabled else None
        )
        if hit and _profanity_ban_candidate(self, room, frm, role if role_field_present else None):
            try:
                exempt = _norm_user(frm) in getattr(self,"filter_exceptions",set())
                if exempt:
                    # The exception account may report a target after @; ban only that named target.
                    mentions=re.findall(r"@([^\s@]+)", body)
                    target=mentions[-1].strip() if mentions else ""
                    if (target and _norm_user(target)!=_norm_user(frm)
                            and _profanity_ban_candidate(self, room, target)):
                        self.send_admin(room,target,"ban")
                        _record_filter_ban(target,room,f"إبلاغ من @{frm}: كلمة الفلتر",hit)
                        self.send_room_text(room,f"🚫 تم حظر @{target}\nالسبب: بلاغ فلتر من @{frm}")
                    return
                self.send_admin(room, frm, "ban")
                _record_filter_ban(frm,room,"كلمة مسيئة",hit)
                self.send_room_text(room, f"🚫 تم حظر @{frm}\nالسبب: كلمة مسيئة")
                self.log("[WORD-FILTER] native room ban", frm, "word=", hit, "room=", room)
            except Exception as exc:
                self.log("[WORD-FILTER] failed:", repr(exc))
            return

        gift_request = re.fullmatch(r"sav@([^@\s]+)", body.strip(), re.I)
        if gift_request:
            self._request_custom_gift_image(room, frm, gift_request.group(1))
            return

        # أوامر الكمين وتوب المراهنين والصور الشخصية/العشوائية.
        if self._handle_kemin_command(room, body, frm):
            return
        if self._handle_top_wagerers(room, body):
            return
        # In some rooms field 2 is a display label or numeric sender ID while
        # field 22 is the canonical Talkin username. Prefer the canonical field
        # so «صورتي» can resolve a member who has not appeared in the primary
        # room's roster cache.
        profile_sender = username or event_user_22 or event_user_17 or frm
        profile_sender_candidates = [
            username, event_user_22, event_user_17, frm,
            str(event.get("username", "") or ""),
        ]
        if self._handle_personal_design_command(room, body, profile_sender):
            return
        if self._handle_internet_picture_command(room, body, profile_sender):
            return
        if self._handle_random_picture_command(
            room, body, profile_sender, sender_candidates=profile_sender_candidates
        ):
            return

        # شبيه: متاح كأمر غرفة مستقل ولا يحتاج توثيقاً.
        if self._handle_lookalike_command(room, body, frm):
            return

        # Explore publication/repost and reactions are handled before ordinary command gates.
        if self._handle_explore_command(room, body, profile_sender):
            return
        if self._handle_reaction_command(room, body, frm):
            return

        # نقاطي متاح للجميع ولا يحتاج توثيقاً.
        if body.strip().casefold() in ("نقاطي", "points"):
            self.send_room_text(room, _points_summary_text(frm))
            if not getattr(self, "_replaying_bot_action", False):
                self._remember_bot_action(room, body, frm, is_private=False)
            return
        named_points = re.fullmatch(r"(?:نقاط|points)\s+@?(.+?)", body.strip(), re.I)
        if named_points:
            self.send_room_text(room, _named_points_text(named_points.group(1)))
            if not getattr(self, "_replaying_bot_action", False):
                self._remember_bot_action(room, body, frm, is_private=False)
            return

        # Cricket handles its own verified-member gate and turn flow before the
        # normal bot-command verification gate. Some Talkin builds put the
        # canonical username in field 22 while field 2 contains a display/id;
        # prefer the canonical field so verified Join requests are recognized.
        verified_sender = _verified_identity_candidate(username, frm, event.get(17, ""))
        is_verified = bool(verified_sender)
        command_sender = verified_sender or username or frm or str(event.get(17, "") or "").strip()
        owner_live_music_allowed = _owner_live_music_allowed(self, room, command_sender, body)
        cricket_sender = command_sender
        # Bomb replies use digits that Cricket also accepts, so the shared
        # router claims active bomb wires before routing any board-game input.
        if self._route_room_game_input(
                room, cricket_sender, body,
                username_aliases=(username, frm, event.get(17, ""), event.get(22, ""))):
            return

        # Verified users may use normal bot commands; administration remains
        # restricted to masters. Unverified command attempts receive one clear
        # notice instead of being silently ignored.
        if (not is_verified
                and not owner_live_music_allowed
                and _looks_like_bot_command(body)
                and not re.match(r"^دخول\s*@\s*.+$", body.strip(), re.I)
                and body.strip().casefold() not in {"توب", "top", "توب الألعاب", "توب الالعاب", "top games", "games top", "ملياري", "مليارات", "مليوني", "ملايين"}):
            self.send_room_text(room, f"🔒 @{frm} طلب توثيق لاستخدام أوامر البوت.\n{_verification_notice()}")
            return
        # Music/gifts require verification; masters are always allowed.
        if re.match(r"^sa@[^@]+@.+$", body.strip(), re.I):
            if not is_verified:
                self.send_room_text(room, f"🔒 @{frm} يحتاج توثيقاً لاستخدام الهدايا.\n{_verification_notice()}")
                return
            if self.handle_gift_command(room, body, command_sender):
                if not getattr(self, "_replaying_bot_action", False):
                    self._remember_bot_action(room, body, frm, is_private=False)
                return
        if body.strip().casefold() in ("هدايا", "gifts", "gv"):
            self.gift_help(room, frm)
            if not getattr(self, "_replaying_bot_action", False):
                self._remember_bot_action(room, body, frm, is_private=False)
            return
        # Live music controls. These operate only on the current room's live session.
        low_body = body.strip().casefold()
        if low_body in (
            "إيقاف الأغنية", "ايقاف الأغنية", "إيقاف الاغنية", "ايقاف الاغنية",
            "وقف الأغنية", "وقف الاغنية", "stop song", "stop music"
        ):
            if not is_verified:
                self.send_room_text(room, f"🔒 @{frm} غير موثّق لإيقاف أغنية البث.\n{_verification_notice()}")
                return
            self._stop_live_song(room)
            self.send_room_text(room, "⏹️ تم إيقاف الأغنية في البث، والبوت ما زال على المايك.")
            return

        if low_body in (
            "إيقاف البث", "ايقاف البث", "وقف البث", "إيقاف بث", "ايقاف بث",
            "stop live", "stop broadcast"
        ):
            if not is_verified:
                self.send_room_text(room, f"🔒 @{frm} غير موثّق لإيقاف البث.\n{_verification_notice()}")
                return
            self._stop_live_broadcast(room)
            self.send_room_text(room, "🛑 تم إيقاف البث ومغادرة البوت للمايك في هذه الغرفة.")
            return

        # Live-seat commands:
        #   اصعد/اصعد للبوت -> request a seat for the bot.
        #   صعدني -> invite only the user who issued the command.
        # Keep the existing verification requirement and native invitation flow.
        live_command = body.strip().casefold()
        if live_command in ("صعود", "اصعد", "إصعد", ".صعود", "اصعد للبوت", "اصعد البوت", "live", "join live", "صعدني", "صعدني للبث"):
            if not is_verified:
                self.send_room_text(room, f"🔒 @{frm} غير موثّق لاستخدام البث.\n{_verification_notice()}")
                return

            if live_command in ("صعدني", "صعدني للبث"):
                target = str(frm or "").strip().lstrip("@")
                if target and target != BOT_ID:
                    self.send_live_invitation_to_user(target, room)
            else:
                self.request_live_room(room)
            return
        m_share_ar = re.fullmatch(r"(?:مشاركه|مشاركة)(?:\s+|@)(?!اسم(?:\s|$))@?([^\s@]+)", body.strip(), re.I)
        if m_share_ar:
            self.share_last_music(frm, m_share_ar.group(1), room)
            if not getattr(self, "_replaying_bot_action", False):
                self._remember_bot_action(room, body, frm, is_private=False)
            return
        m_share_photo = re.fullmatch(
            r"(?:مشاركه|مشاركة)\s+(?:صورتي|صورتى|الصورة|الصوره|صورة)\s*@?([^\s@]+)",
            body.strip(), re.I,
        )
        if m_share_photo:
            self.share_personal_design_private(frm, m_share_photo.group(1), "portrait", room)
            return
        m_share_name = re.fullmatch(
            r"(?:مشاركه|مشاركة)\s+(?:اسمي|اسمى|الاسم)\s*@?([^\s@]+)",
            body.strip(), re.I,
        )
        if m_share_name:
            self.share_personal_design_private(frm, m_share_name.group(1), "name", room)
            return
        m_share = re.fullmatch(r"(?:sher|share)@(.+)", body.strip(), re.I)
        if m_share:
            self.share_last_music(frm, m_share.group(1), room)
            if not getattr(self, "_replaying_bot_action", False):
                self._remember_bot_action(room, body, frm, is_private=False)
            return
        if (body.strip().startswith((".تشغيل ", "بث "))
                or re.match(r"^(?:play|music|song)\s+.+$", body.strip(), re.I)):
            if not is_verified and not owner_live_music_allowed:
                self.send_room_text(room, f"🔒 @{frm} غير موثّق لتشغيل الأغاني.\n{_verification_notice()}")
                return
            is_room_broadcast = body.strip().startswith("بث ")
            if re.match(r"^(?:play|music|song)\s+", body.strip(), re.I):
                command = ".sa " + re.split(r"\s+", body.strip(), maxsplit=1)[1]
            else:
                command = body.replace(".تشغيل ", ".sa ", 1) if not is_room_broadcast else body.replace("بث ", ".sa ", 1)
            # بث means broadcast to every room connected to this bot;
            # .تشغيل remains local to the room where it was requested.
            if self.handle_music_command(
                    room, command, frm,
                    # .تشغيل = this room only.
                    # بث = explicit global/live broadcast.
                    broadcast_all=is_room_broadcast,
                    with_reactions=False,
                    room_output=True,
                    live_stream=is_room_broadcast):
                if not getattr(self, "_replaying_bot_action", False):
                    self._remember_bot_action(room, body, frm, is_private=False)
                return
        youtube_direct_test = re.fullmatch(r"\.aax\s*(.+)", body.strip(), re.I)
        if youtube_direct_test:
            if not is_verified:
                self.send_room_text(room, f"🔒 @{frm} غير موثّق لاختبار تشغيل الأغاني.\n{_verification_notice()}")
                return
            if self.handle_youtube_direct_test_command(room, youtube_direct_test.group(1), frm):
                if not getattr(self, "_replaying_bot_action", False):
                    self._remember_bot_action(room, body, frm, is_private=False)
                return
        youtube_test = re.fullmatch(r"\.aa\s*(.+)", body.strip(), re.I)
        if youtube_test:
            if not is_verified:
                self.send_room_text(room, f"🔒 @{frm} غير موثّق لاختبار تشغيل الأغاني.\n{_verification_notice()}")
                return
            if self.handle_youtube_test_command(room, youtube_test.group(1), frm):
                if not getattr(self, "_replaying_bot_action", False):
                    self._remember_bot_action(room, body, frm, is_private=False)
                return
        if body.strip().lower().startswith(".sa "):
            if not is_verified:
                self.send_room_text(room, f"🔒 @{frm} غير موثّق لاستخدام الأغاني.\n{_verification_notice()}")
                return
            if self.handle_music_command(room, body, frm, broadcast_all=True, with_reactions=True):
                if not getattr(self, "_replaying_bot_action", False):
                    self._remember_bot_action(room, body, frm, is_private=False)
                return

        # Keep a small per-room message history for diagnostics.
        self.last_messages[room].append((frm, body, event_id))
        self.last_messages[room] = self.last_messages[room][-50:]

        # Wallet catalog/holdings pagination owns `ns` while this user has an
        # active wallet page. Route it before the generic management pager,
        # whose fallback otherwise consumes `ns` and leaves wallet page 1 stuck.
        if self._wallet_handle_next_page(room, command_sender, body):
            return

        if self._handle_management_command(room, body, frm):
            return

        if self._run_game_command_async(room, body, command_sender):
            if not getattr(self, "_replaying_bot_action", False):
                self._remember_bot_action(room, body, command_sender, is_private=False)
            return

        if self._handle_departure_reply_command(room, body):
            return

        # Exact-match automatic replies are intentionally evaluated LAST so
        # they never steal a real bot command such as بنك/roll/نشر. One normal
        # text message produces one response from the saved reply cycle.
        if (self.auto_replies_enabled
                and not _looks_like_bot_command(body)
                and not _looks_like_admin_command(body)):
            reply=self._choose_auto_reply(body.strip().casefold(),frm,room)
            if reply:
                self.send_room_text(room,reply)
                return

        if body.lower().strip() in ("!help", "مساعدة") and AUTO_HELP:
            self.send_room_text(room, "أوامر البوت: k@ اسم، b@ اسم، a@ اسم، o@ اسم، دخول@اسم_الغرفة، خروج [اسم_الغرفة]، inv، invmsg نص الدعوة لدعوة مستخدمي الغرفة")

    def _event_lane_key(self, message):
        """Return a serialization lane without fully processing the frame."""
        try:
            top = decode_message(message)
            room_event = top.get("room_event") if isinstance(top, dict) else None
            if isinstance(room_event, dict):
                room = str(room_event.get(13, "") or "").strip()
                if room:
                    return "room:" + _norm_room(room)
            chat = top.get("chat_message") if isinstance(top, dict) else None
            if isinstance(chat, dict):
                room = str(chat.get(4, "") or chat.get(13, "") or "").strip()
                if room:
                    return "room:" + _norm_room(room)
                sender = str(chat.get(3, "") or chat.get(22, "") or "").strip()
                if sender:
                    return "private:" + _norm_user(sender)
        except Exception:
            pass
        return "global"

    def _event_worker_loop(self):
        while not self.stop_event.is_set():
            try:
                _priority, _seq, ws, message = self._event_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            lane_key = self._event_lane_key(message)
            with self._event_lane_guard:
                lane_lock = self._event_lane_locks.setdefault(lane_key, threading.Lock())
            try:
                # Independent rooms/private senders can proceed in parallel;
                # commands in one lane remain ordered and state-safe.
                with lane_lock:
                    self._process_message(ws, message)
            except Exception as exc:
                self.last_error = str(exc)
                self.log("[EVENT-WORKER] processing failed:", repr(exc))
            finally:
                try:
                    self._event_queue.task_done()
                except Exception:
                    pass

    def on_message(self, ws, message):
        """Queue inbound frames so expensive decoding/commands never block recv()."""
        if isinstance(message, str):
            return
        try:
            # One shallow protobuf pass is enough to prioritize chat/room text
            # above low-value roster/stream traffic. The full decode happens
            # once, inside the worker.
            top = decode_message(message)
            priority = 2
            if 9 in top or 10 in top:
                priority = 0
            elif 8 in top:
                priority = 1
            with self._event_queue_lock:
                self._event_queue_seq += 1
                seq = self._event_queue_seq
            try:
                self._event_queue.put_nowait((priority, seq, ws, bytes(message)))
            except queue.Full:
                # Never let a burst of presence/stream frames freeze command
                # reception. If the queue is full, make one immediate worker
                # pass for command-bearing frames; otherwise drop the oldest
                # low-priority pressure rather than blocking the socket.
                if priority == 0:
                    try:
                        self._process_message(ws, bytes(message))
                    except Exception as exc:
                        self.log("[EVENT-WORKER] urgent frame failed:", repr(exc))
                else:
                    if ROOM_EVENT_DEBUG:
                        self.log("[EVENT-QUEUE] dropped low-priority frame; queue full")
        except Exception as exc:
            self.last_error = str(exc)
            self.log("[EVENT-QUEUE] enqueue failed:", repr(exc))

    def _process_message(self, ws, message):
        try:
            if isinstance(message, str):
                self.log("[WS] unexpected text frame received")
                return
            result = decode_result_message(message)
            self._handle_stream_result_ack(result)
            self._cache_user_photos_from_result(result)
            # Room join outcomes are emitted as top-level ResultMessage types
            # by some TalkinChat builds, not as nested RoomEvent packets.
            join_result_types = {
                "success", "room_unauthorized", "room_membership_required",
                "room_full", "room_wrong_password", "room_needs_password",
                "room_needs_captcha", "room_unauthorized_rejoin",
                "room_membership_required_rejoin", "room_full_rejoin",
                "room_wrong_password_rejoin", "room_needs_password_rejoin",
                "room_needs_captcha_rejoin",
            }
            result_type = str(result.get("type") or "").strip()
            result_room = str(result.get("value") or "").strip()

            # Some TalkinChat server versions return a join failure without
            # putting the room name in ResultMessage.value.  In that case the
            # room is still known from _pending_room_joins, and that pending
            # record also contains the exact user who sent دخول@اسم_الغرفة.
            # Use it so the failure notification is delivered to the requester
            # instead of falling back to BOT_MASTER.
            if result_type in join_result_types and not result_room:
                pending_rooms = list(getattr(self, "_pending_room_joins", {}).keys())
                if len(pending_rooms) == 1:
                    result_room = str(
                        getattr(self, "_pending_room_joins", {}).get(pending_rooms[0], {}).get("room", "")
                        or ""
                    ).strip()

            if (
                result_type in join_result_types
                and result_room
                and (result_type != "success" or _norm_room(result_room) in self._pending_room_joins)
            ):
                self.handle_room_event({
                    "room_event": {1: "you_joined" if result_type == "success" else result_type, 13: result_room},
                    "uid": result.get("uid", ""),
                })
            if "room_event" in result:
                self.handle_room_event(result)
            if result.get("rooms"):
                self._process_room_list(result.get("rooms"))
            if result.get("users") or result.get("room_admin"):
                # Occupants responses do not always echo the room name. When a
                # native list request is pending, bind either users or
                # RoomAdmin responses to that requested room. If multiple
                # room-less responses are pending, correlate FIFO by request
                # time instead of dropping every response until timeout.
                if not result.get("_occupants_room"):
                    pending_map = getattr(self, "_pending_room_lists", {})
                    pending_rooms = list(pending_map.keys())
                    if pending_rooms:
                        pending_rooms.sort(
                            key=lambda key: float(
                                (pending_map.get(key) or {}).get("requested_at", 0) or 0
                            )
                        )
                        result["_occupants_room"] = pending_rooms[0]
                        if len(pending_rooms) > 1:
                            self.log(
                                "[ROOM-LIST] response omitted room; FIFO-correlated",
                                "room=", pending_rooms[0],
                                "pending=", len(pending_rooms),
                            )
                _inv_collecting = bool(
                    getattr(self, "_inv_roster_pending", None)
                    and isinstance(getattr(self, "_inv_roster_pending", None), dict)
                    and _norm_room(result.get("_occupants_room", "")) == _norm_room(getattr(self, "_inv_roster_pending", {}).get("room", ""))
                )
                if _inv_collecting:
                    self._complete_pending_room_list(result)
                else:
                    self.process_occupants_for_invite(result)
                    self._complete_pending_room_list(result)
            # Different Talkin builds wrap the live invitation as StreamEvent,
            # CallInfo, or (less commonly) a RoomEvent. Try all wrappers.
            for stream_event in tuple(
                    x for x in (result.get("stream_event"), result.get("call_info"), result.get("room_event"))
                    if isinstance(x, dict)):
                if ROOM_EVENT_DEBUG:
                    self.log("[STREAM]", stream_event)
                # Some deployments expose member identity in STREAM records
                # rather than the RoomAdmin roster. Persist that identity so
                # inv/.r can use it even when the member is currently offline.
                self._cache_stream_member_event(stream_event)
                self._handle_stream_event(stream_event)
            # The native app sends publish_stream after the invitation is
            # accepted. It is the authoritative success signal and may be
            # direct or nested inside a StreamEvent/CallInfo/RoomEvent.
            publish_handled = self._handle_publish_stream_confirmation(result)
            if not publish_handled:
                self._handle_stream_result_ack(result)

            top_type = str(result.get("type", "") or "").strip().casefold()
            if "invite" in top_type or "invited" in top_type:
                if ROOM_EVENT_DEBUG:
                    self.log("[STREAM] top-level invitation", result)
                self._handle_stream_event(result)
            if result.get("room_admin"):
                if ROOM_EVENT_DEBUG:
                    self.log("[ROOM_ADMIN]", result["room_admin"])
            if result.get("chat_message"):
                cm = result["chat_message"]
                # Some server versions wrap room chat as ChatMessage. Route it
                # through the same room handler using the embedded room field;
                # never infer a room for genuine private messages.
                if not result.get("room_event") and isinstance(cm, dict):
                    cm_room = str(cm.get(4, "") or cm.get(13, "") or "").strip()
                    live_rooms = {
                        _norm_room(value) for value in getattr(self, "connected_rooms", set())
                        if str(value or "").strip()
                    }
                    # An inbound user message can arrive before the explicit
                    # join acknowledgement; a known room identity confirms
                    # this session can see that room.
                    candidate_rooms = set(live_rooms)
                    candidate_rooms.update(
                        _norm_room(value) for value in getattr(self, "known_rooms", set())
                        if str(value or "").strip()
                    )
                    try:
                        candidate_rooms.update(
                            _norm_room(value) for value in self._active_rooms()
                            if str(value or "").strip()
                        )
                    except Exception:
                        pass
                    if cm_room and _norm_room(cm_room) in candidate_rooms:
                        connected = getattr(self, "connected_rooms", None)
                        if not isinstance(connected, set):
                            connected = set()
                            self.connected_rooms = connected
                        connected.add(cm_room)
                        self.handle_room_event({"room_event": {
                            1: "text", 2: str(cm.get(3, "") or ""),
                            6: str(cm.get(5, "") or ""), 13: cm_room,
                            41: str(cm.get(41, "") or ""),
                        }})
                        return
                # Private master commands are also accepted as ChatMessage frames.
                try:
                    frm = str(cm.get(3, "") or "").strip()
                    body = str(cm.get(5, "") or "").strip()
                    private_username = str(
                        cm.get(22, "") or cm.get("username", "") or cm.get("sender", "") or ""
                    ).strip()
                    secret_pending = getattr(self, "_secret_export_auth_pending", {})
                    is_secret_password_reply = (
                        isinstance(secret_pending, dict) and _norm_user(frm) in secret_pending
                    )
                    if body and not is_secret_password_reply:
                        self._remember_inflight_command(self.room, body, frm, is_private=True)
                    media_url = next(
                        (str(cm.get(key, "") or "").strip() for key in (6, 7, 8, 9, 10, 11, 12, 13, 14, 15, "url", "media_url", "image_url", "file_url")
                         if str(cm.get(key, "") or "").strip().startswith(("http://", "https://"))),
                        "",
                    ) or _extract_media_url_universal(cm, body) or first_http_url(cm) or first_http_url(body)
                    if not media_url and body and ("http://" in body or "https://" in body):
                        img_m = re.search(r'https?://\S+(?:\?\S*)?', body, re.I)
                        if img_m:
                            media_url = img_m.group(0).rstrip(",.;)]}")
                    # Some deployments deliver a room image as ChatMessage rather
                    # than RoomEvent. Use the room field when it matches a room
                    # currently tracked by the bot, then consume the pending publish.
                    message_room = str(cm.get(4, "") or cm.get(13, "") or "").strip()
                    tracked_rooms = {_norm_room(r) for r in self._active_rooms()}
                    media_room = message_room if _norm_room(message_room) in tracked_rooms else self.room
                    private_media_candidates = [frm, cm.get(22, ""), cm.get("sender", ""), cm.get("username", "")]
                    if media_url and getattr(self, "custom_welcome_image_pending", {}):
                        if self._try_custom_welcome_image_media(media_room, media_url, private_media_candidates):
                            return
                    if media_url and getattr(self, "custom_gift_pending", {}):
                        if self._try_custom_gift_media(media_room, media_url, private_media_candidates):
                            return
                    if media_url and getattr(self, "publish_pending", {}):
                        media_candidates = [frm, cm.get(22, ""), cm.get("sender", ""), cm.get("username", "")]
                        if self._try_publish_pending_media(media_room, media_url, media_candidates):
                            return
                    if body and self._handle_explore_command(self.room, body, frm):
                        return
                    if body and self._handle_reaction_command(self.room, body, frm):
                        return
                    if body and self._handle_telegram_log_command(body, frm, is_private=True):
                        return
                    # Every NS is a fresh navigation request. Do not suppress
                    # rapid NS commands with the normal transport de-dup cache.
                    # Direct private message is a user command, not an admin
                    # command. Intercept it before any management/broadcast
                    # routing so the message body never appears in a room.
                    m_direct_private = re.fullmatch(r"(?:رساله|رسالة)\s*@?([^\s@]+)\s+(.+)", body, re.I | re.S)
                    if m_direct_private:
                        target = m_direct_private.group(1).strip()
                        message_text = m_direct_private.group(2).strip()
                        if target and message_text:
                            self._deliver_user_private_message(frm, target, message_text)
                            return

                    _private_event_id = str(cm.get(41, "") or result.get("uid", "") or "")
                    # A missing ChatMessage event id means content-signature
                    # de-duplication is not safe for legitimate repeated text.
                    # Keep signature de-duplication only for media messages.
                    _skip_text_signature_dedup = (not _private_event_id and not media_url)
                    if (
                        not _is_ns_command(body)
                        and not _skip_text_signature_dedup
                        and self._is_duplicate_incoming(
                            "private",
                            (frm, body, media_url),
                            _private_event_id,
                        )
                    ):
                        self.log("[DEDUP] ignored repeated private message")
                        return
                    if MASTER_SERVICE_ENABLED:
                        self._relay_private_to_owner(frm, body, media_url)
                    if _norm_user(frm) == _norm_user(BOT_MASTER):
                        self.master_online = True
                        self.master_last_seen = time.time()
                    if body and self._handle_master_process_command(frm, body, is_private=True):
                        return
                    private_design_sender = (
                        _verified_identity_candidate(private_username, frm) or private_username or frm
                    )
                    if body and self._handle_personal_design_command(
                            self.room, body, private_design_sender, is_private=True):
                        return
                    # The standalone master account is a four-option support
                    # service, not a second games/music/invitations bot. Route
                    # every external private message through that menu before
                    # the ordinary bot feature handlers below.
                    if MASTER_SERVICE_ENABLED and self._handle_master_account_service(frm, body):
                        return
                    private_verified_sender = (
                        _verified_identity_candidate(private_username, frm) if body else ""
                    )
                    private_is_verified = bool(private_verified_sender)
                    private_command_sender = private_verified_sender or private_username or frm
                    cricket_room = (
                        self.room
                        or getattr(self, "last_joined_room", "")
                        or next(iter(getattr(self, "connected_rooms", set())), "")
                    )
                    if body and self._handle_auction_private_command(private_command_sender, body):
                        return
                    if body and self._cricket.handle(cricket_room, private_command_sender, body, is_private=True):
                        return
                    if body and self._monitor_command(frm, body):
                        return
                    # The primary bot does not show the master-service menu.
                    # That menu belongs exclusively to the standalone master
                    # account when MASTER_SERVICE_ENABLED is active.
                    if media_url:
                        # A pending publish request is the authorization for
                        # the image; let the media handler validate it rather
                        # than rejecting it on a second private-message role
                        # check.
                        private_media_senders = [
                            frm, cm.get(22, ""), cm.get("sender", ""), cm.get("username", "")
                        ]
                        if self._try_publish_pending_media(self.room, media_url, private_media_senders):
                            return
                    # Silently ignore master-only commands from everyone else.
                    is_publish_command = _is_publish_command(body)
                    if (body and _looks_like_admin_command(body)
                            and not _is_master_name(frm)
                            and not (_is_mvip_master(frm) and _is_verification_manager_command(body))
                            and not (re.fullmatch(r"sb@([^@]+)@(\d+)", body.strip(), re.I) and private_is_verified)
                            and not (is_publish_command and private_is_verified)
                            and body.strip().casefold() not in {"توب", "top", "توب الألعاب", "توب الالعاب", "top games", "games top", "ملياري", "مليارات", "مليوني", "ملايين"}
                            and not re.match(r"^دخول\s*@\s*.+$", body, re.I)):
                        if not private_is_verified:
                            self.send_private_text(frm, f"🔒 @{frm} طلب توثيق لاستخدام أوامر البوت.\n{_verification_notice()}")
                        return
                    if (body
                            and not private_is_verified
                            and _looks_like_bot_command(body)
                            and not re.match(r"^دخول\s*@\s*.+$", body.strip(), re.I)
                            and body.strip().casefold() not in {"توب", "top", "توب الألعاب", "توب الالعاب", "top games", "games top", "ملياري", "مليارات", "مليوني", "ملايين"}):
                        self.send_room_text(self.room, f"🔒 @{frm} طلب توثيق لاستخدام أوامر البوت.\n{_verification_notice()}")
                        return
                    if body and body.casefold() in ("نقاطي", "points"):
                        self.send_private_text(frm, _points_summary_text(frm))
                        return
                    named_points = re.fullmatch(r"(?:نقاط|points)\s+@?(.+?)", body.strip(), re.I)
                    if named_points:
                        self.send_private_text(frm, _named_points_text(named_points.group(1)))
                        return
                    if body and self._handle_translation_subscription("", body, frm):
                        return
                    if body:
                        # A pending multi-room join language choice has priority
                        # over game commands; otherwise "1" starts Ludo and the
                        # bot may join/respond in the wrong context.
                        _pm_choice = _normalize_lang_choice(body)
                        if _pm_choice in ("1", "2") and self._complete_join_rooms_language(frm, _pm_choice, response_room=self.room, is_private=True):
                            return
                        if self._handle_management_command(self.room, body, frm, is_private=True):
                            return
                    m_share_ar = re.fullmatch(r"(?:مشاركه|مشاركة)(?:\s+|@)(?!اسم(?:\s|$))@?([^\s@]+)", body.strip(), re.I)
                    if m_share_ar:
                        self.share_last_music(frm, m_share_ar.group(1))
                        return
                    m_share = re.fullmatch(r"(?:sher|share)@(.+)", body.strip(), re.I)
                    if m_share:
                        self.share_last_music(frm, m_share.group(1))
                        return
                    if (body.strip().startswith((".تشغيل ", "بث "))
                            or re.match(r"^(?:play|music|song)\s+.+$", body.strip(), re.I)):
                        is_room_broadcast = body.strip().startswith("بث ")
                        if re.match(r"^(?:play|music|song)\s+", body.strip(), re.I):
                            command = ".sa " + re.split(r"\s+", body.strip(), maxsplit=1)[1]
                        else:
                            command = body.replace(".تشغيل ", ".sa ", 1) if not is_room_broadcast else body.replace("بث ", ".sa ", 1)
                        if self.handle_music_command(
                                self.room, command, frm,
                                broadcast_all=not is_room_broadcast,
                                with_reactions=False,
                                room_output=not is_room_broadcast,
                                live_stream=is_room_broadcast):
                            return
                    youtube_direct_test = re.fullmatch(r"\.aax\s*(.+)", body.strip(), re.I)
                    if youtube_direct_test:
                        if not private_is_verified:
                            self.send_private_text(frm, f"🔒 @{frm} غير موثّق لاختبار تشغيل الأغاني.\n{_verification_notice()}")
                            return
                        if self.handle_youtube_direct_test_command(self.room, youtube_direct_test.group(1), frm):
                            return
                    youtube_test = re.fullmatch(r"\.aa\s*(.+)", body.strip(), re.I)
                    if youtube_test:
                        if not private_is_verified:
                            self.send_private_text(frm, f"🔒 @{frm} غير موثّق لاختبار تشغيل الأغاني.\n{_verification_notice()}")
                            return
                        if self.handle_youtube_test_command(self.room, youtube_test.group(1), frm):
                            return
                    if body.strip().lower().startswith(".sa "):
                        if self.handle_music_command(self.room, body, frm, broadcast_all=True, with_reactions=True):
                            return
                    m_custom_gift = re.fullmatch(r"sav@([^@\s]+)", body.strip(), re.I)
                    if m_custom_gift:
                        if private_is_verified:
                            self._request_custom_gift_image(self.room, private_command_sender, m_custom_gift.group(1), private_to=frm)
                        else:
                            self.send_private_text(frm, f"🔒 @{frm} يحتاج توثيقًا لإرسال هدية صورة.\n{_verification_notice()}")
                        return
                    if re.match(r"^sa@[^@]+@.+$", body.strip(), re.I):
                        if private_is_verified:
                            if self.handle_gift_command(self.room, body, private_command_sender, private_to=frm):
                                return
                        else:
                            self.send_private_text(frm, f"🔒 @{frm} يحتاج توثيقاً لاستخدام الهدايا.\n{_verification_notice()}")
                            return
                    if body and private_is_verified and self._run_game_command_async(self.room, body, private_command_sender):
                        return
                    if _is_master_name(frm) and body:
                        # Reuse room command handling with the command-context room.
                        ctx_room = self.room
                        parts = body.split(None, 1)
                        cmd = parts[0].lower() if parts else ""
                        arg = parts[1].strip() if len(parts) == 2 else ""
                        pending_add = getattr(self, "_pending_add_rooms", {}).get(_norm_user(frm))
                        if isinstance(pending_add, dict) and body.strip().casefold() not in ("1", "2") and not _looks_like_bot_command(body.strip()):
                            if time.time() - float(pending_add.get("created", 0) or 0) <= 180:
                                self._pending_add_rooms.pop(_norm_user(frm), None)
                                self._add_rooms_to_saved_file(frm, body.strip())
                                return
                            self._pending_add_rooms.pop(_norm_user(frm), None)
                        if body.strip().casefold() in ("اضف لملف الغرف", "أضف لملف الغرف", "اضف للملف الغرف", "أضف للملف الغرف") and _is_primary_master(frm):
                            if not hasattr(self, "_pending_add_rooms"):
                                self._pending_add_rooms = {}
                            self._pending_add_rooms[_norm_user(frm)] = {"created": time.time()}
                            self.send_private_text(frm, "📁 أضف الغرف إلى ملف الغرف\n📨 أرسل أسماء الغرف مفصولة بمسافة.\nمثال: مشاعر ادم نبض قلوب سوالف")
                        elif body.strip().casefold() in ("1", "2") and self._complete_join_rooms_language(frm, body.strip(), response_room=ctx_room, is_private=True):
                            return
                        elif body.strip().casefold() in ("تشغيل الدعوات", "ايقاف الدعوات", "إيقاف الدعوات") and _is_primary_master(frm):
                            if body.strip().casefold() == "تشغيل الدعوات":
                                self.invites_enabled = True
                                _save_local_json(DATA_DIR / "invite_settings.json", {"enabled": True, "updated_at": int(time.time())})
                                self.send_private_text(frm, "✅ تم تشغيل الدعوات.")
                            else:
                                self.invites_enabled = False
                                _save_local_json(DATA_DIR / "invite_settings.json", {"enabled": False, "updated_at": int(time.time())})
                                self.send_private_text(frm, "🛑 تم إيقاف الدعوات.")
                        elif re.fullmatch(r"s@(.+)", body.strip(), re.I) and _is_primary_master(frm):
                            target = re.fullmatch(r"s@(.+)", body.strip(), re.I).group(1).strip().lstrip("@")
                            key = _norm_user(target)
                            active_rooms = {str(r).strip() for r in getattr(self, "connected_rooms", set()) if str(r).strip()}
                            matches = []
                            for active_room in sorted(active_rooms):
                                for username in (getattr(self, "room_users", {}).get(active_room, {}) or {}):
                                    if _norm_user(username) == key:
                                        matches.append((active_room, username))
                                        break
                            if matches:
                                lines = [f"🔎 نتيجة البحث عن @{target}", f"🟢 أكد الخادم اتصال @{target} في {len(matches)} غرفة:"]
                                lines.extend(f"🏠 {room_name} — @{username}" for room_name, username in matches)
                                self.send_private_text(frm, "\n".join(lines))
                            else:
                                self.send_private_text(frm, f"🔴 أكد الخادم أن @{target} غير متصل حالياً في أي غرفة متصلة بالبوت.")
                        elif cmd in ("inv", "دعوات", "invite"):
                            if not getattr(self, "invites_enabled", True):
                                self.send_private_text(frm, "🛑 الدعوات متوقفة حالياً. أرسل: تشغيل الدعوات")
                            elif not ctx_room:
                                self.send_private_text(frm, "⚠️ نفّذ inv داخل الغرفة المطلوبة.")
                            else:
                                self.request_occupants(ctx_room, silent_master=False, response_room=ctx_room)
                        elif re.fullmatch(r"دخول@(.+)", body.strip(), re.I | re.S):
                            raw_rooms = re.fullmatch(r"دخول@(.+)", body.strip(), re.I | re.S).group(1).strip()
                            rooms = [x.strip() for x in raw_rooms.split() if x.strip()]
                            if rooms:
                                for target_room in rooms:
                                    blocked_room = _norm_room(target_room)
                                    if blocked_room in self.blocked_rooms:
                                        self.blocked_rooms.discard(blocked_room)
                                        self._blocked_room_reasons.pop(blocked_room, None)
                                        self._blocked_room_notices.discard(blocked_room)
                                self._save_blocked_rooms()
                                self._begin_join_rooms_language(frm, rooms)
                            else:
                                self.send_private_text(frm, "❌ الصيغة: دخول@اسم_الغرفة أو دخول@غرفة1 غرفة2 غرفة3")
                        elif cmd in ("خروج", "leave", "exit"):
                            if arg:
                                ok = self.leave_room(arg)
                                self.send_private_text(BOT_MASTER, f"{'✅ خرجت من الغرفة' if ok else '❌ تعذر الخروج'}: {arg}")
                            else:
                                rooms = self.leave_all_rooms()
                                self.send_private_text(BOT_MASTER, f"✅ خرجت من جميع الغرف. العدد: {len(rooms)}")
                        elif (cmd in ("invmsg", "رسالةدعوة") and arg) or body.casefold().startswith("invmsg@"):
                            self.invite_message_template = (body.split("@", 1)[1].strip() if body.casefold().startswith("invmsg@") else arg)
                            if "{room}" not in self.invite_message_template:
                                self.invite_message_template = self.invite_message_template.rstrip() + " {room}"
                            self.send_private_text(frm, f"✅ تم تغيير رسالة الدعوة إلى: {self.invite_message_template}")
                        elif cmd in ("a@", "admin") and arg:
                            target = arg.lstrip("@").strip()
                            self.request_admin_action(ctx_room, target, "admin", frm, announce_room=True)
                        elif cmd in ("o@", "owner") and arg:
                            target = arg.lstrip("@").strip()
                            self.request_admin_action(ctx_room, target, "owner", frm, announce_room=True)
                        elif cmd in ("k@", "kick") and arg:
                            target = arg.lstrip("@").strip()
                            self.request_admin_action(ctx_room, target, "kick", frm, announce_room=True)
                        elif cmd in ("b@", "ban") and arg:
                            target = arg.lstrip("@").strip()
                            self.request_admin_action(ctx_room, target, "ban", frm, announce_room=True)
                        elif cmd in ("u@", "unban") and arg:
                            target = arg.lstrip("@").strip()
                            self.request_admin_action(ctx_room, target, "member", frm, announce_room=True)
                        elif cmd in ("say", "قل") and arg:
                            self.send_room_text(ctx_room, arg)
                except Exception as e:
                    self.log("[CHAT_MESSAGE] private command handling failed:", repr(e))
        except Exception as e:
            self.last_error = str(e)
            self.log("[WS] decode error:", repr(e))
            if DEBUG and isinstance(message, (bytes, bytearray)):
                self.log("[WS] raw:", bytes(message).hex()[:1000])
