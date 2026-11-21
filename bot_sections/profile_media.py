"""Profile/photo, gifts, publishing, generated cards and image-search methods."""

class ProfileMediaSection:
    def _cache_user_photos_from_result(self, result):
        """Cache Talkin profile URLs using the shared RoomAdmin/UserItem decoder."""
        try:
            if not isinstance(getattr(self, "user_photos", None), dict):
                self.user_photos = {}
            if not isinstance(getattr(self, "user_profile_ids", None), dict):
                self.user_profile_ids = {}
            result = result if isinstance(result, dict) else {}
            users = self._extract_room_list_users(result)
            profile = result.get("profile") or result.get("user_profile") or result.get("profile_data")
            if isinstance(profile, dict):
                profile_user = dict(profile)
                profile_user.setdefault("username", profile_user.get("user_name") or profile_user.get("name") or profile_user.get("jid"))
                profile_user.setdefault("photo", profile_user.get("avatar_url") or profile_user.get("photo_url") or profile_user.get("profile_photo_url") or profile_user.get("image_url") or profile_user.get("url") or profile_user.get("media"))
                profile_user.setdefault("user_id", profile_user.get("id") or profile_user.get("uid") or profile_user.get("user_id"))
                # Some Talkin gateways return only {photo/url} for get_profile.
                # First match ResultMessage.uid to Query.id_; only use a single
                # outstanding request as a compatibility fallback when uid is absent.
                matched_request = None
                pending = getattr(self, "_profile_lookup_pending", {})
                if not isinstance(pending, dict):
                    pending = {}
                now_mono = time.monotonic()
                live_pending = []
                for request_id, item in list(pending.items()):
                    if (not isinstance(item, dict)
                            or float(item.get("expires", 0) or 0) < now_mono):
                        pending.pop(request_id, None)
                    else:
                        live_pending.append((str(request_id), item))
                response_uid = str(result.get("uid") or "").strip()
                if response_uid:
                    matched_request = pending.pop(response_uid, None)
                if matched_request is None and not profile_user.get("username") and len(live_pending) == 1:
                    request_id, matched_request = live_pending[0]
                    pending.pop(request_id, None)
                if not profile_user.get("username") and isinstance(matched_request, dict):
                    profile_user["username"] = matched_request.get("username", "")
                if profile_user.get("username"):
                    users.append(profile_user)
            # Do not associate a bare URL from a generic ResultMessage with
            # the requester. Some gateways include a room/bot avatar in an
            # otherwise unrelated result; without a matching username or ID it
            # is impossible to prove that the URL belongs to this account.
            for user in users:
                username = str(user.get("username") or "").strip().lstrip("@")
                photo = str(user.get("photo") or "").strip()
                user_id = str(user.get("user_id") or "").strip()
                if photo.startswith("//"):
                    photo = "https:" + photo
                if username and user_id:
                    self.user_profile_ids[username.casefold()] = user_id
                    self.user_profile_ids[_norm_user(username)] = user_id
                status = str(user.get("status") or "").strip()
                if username and _norm_user(username) == _norm_user(BOT_ID):
                    if status:
                        with self._profile_status_lock:
                            self._profile_current_status = status
                            if status == getattr(self, "_profile_status_pending", ""):
                                self._profile_status_pending = ""
                            if self._profile_status_timer is None and not BOT_BASE_STATUS:
                                self._profile_base_status = status
                    # Keep the bot avatar separately: it must not be mixed
                    # with normal members' photo cache. This also lets us
                    # verify whether Talkin's room roster has the new avatar.
                    if photo.startswith(("http://", "https://")):
                        self.bot_profile_photo_url = photo
                        self.log("[PROFILE-PHOTO] bot avatar found in native room roster:", photo[:180])
                if (username and _norm_user(username) != _norm_user(BOT_ID)
                        and photo.startswith(("http://", "https://"))):
                    self.user_photos[username.casefold()] = photo
                    self.user_photos[_norm_user(username)] = photo
                    if user_id:
                        self.user_photos["id:" + user_id] = photo
                    # Explicitly named profile responses complete any outstanding
                    # request for this same user, so stale records cannot block
                    # photo association for a later room.
                    pending = getattr(self, "_profile_lookup_pending", {})
                    if isinstance(pending, dict):
                        wanted = _norm_user(username)
                        for request_id, pending_item in list(pending.items()):
                            if (isinstance(pending_item, dict)
                                    and _norm_user(pending_item.get("username", "")) == wanted):
                                pending.pop(request_id, None)
            photo_count = sum(
                1 for user in users
                if str(user.get("photo") or "").startswith(("http://", "https://"))
            )
            if users:
                self.log("[PROFILE-PHOTO] native roster parsed:", "users=", len(users), "photos=", photo_count)
        except Exception as exc:
            self.log("[GIFT] photo cache failed:", repr(exc))

    def _request_native_profile_roster(self, room, legacy=False):
        """Ask Talkin for native UserItems and profile photo URLs.

        Current APK builds use ``to=username`` for occupants_list. Older
        gateways may still require BOT_ID, so callers can request that legacy
        shape explicitly after the primary request returns no avatar.
        """
        room = str(room or "").strip()
        if not room:
            return False
        try:
            if not getattr(self, "ws", None):
                return False
            query_args = {
                "type_": "occupants_list", "room": room,
                "to": BOT_ID if legacy else "username", "value": "none",
            }
            if legacy:
                query_args["id_"] = secrets.token_urlsafe(12)[:16]
            sent = self.send_query(encode_query("room_admin", **query_args))
            self.log(
                "[PROFILE-PHOTO] native occupants request sent:",
                room, "mode=legacy" if legacy else "mode=app",
            )
            return sent is not False
        except Exception as exc:
            self.log("[PROFILE-PHOTO] native roster request failed:", room, type(exc).__name__, str(exc)[:160])
            return False

    def _upload_bot_profile_photo_official(self, image_path):
        """Upload a bot avatar using Talkinchat's native profile-image flow.

        The official Android client does not send the public asset URL directly
        to ``profile_update``.  It first POSTs the image as multipart data to
        ``https://talkinchat.com/upload`` with ``type=profile`` and then uses
        the returned URL as the photo value.  This is important because the
        server emits the room-avatar update as part of this upload flow.
        """
        image_path = Path(image_path)
        if not image_path.is_file():
            raise FileNotFoundError(str(image_path))
        username = str(BOT_ID or "").strip()
        if not username:
            raise RuntimeError("BOT_ID غير مضبوط")
        mime_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        fields = {
            "jid": username,
            "to_user": username,
            "is_private": "yes",
            "type": "profile",
            "device_id": "null",
            # The current Android client sends its session field `s`; in the
            # profile flow it is empty on the supported Talkinchat builds.
            "s": os.getenv("TALKINCHAT_UPLOAD_SESSION", ""),
        }
        with image_path.open("rb") as image_file:
            response = requests.post(
                "https://talkinchat.com/upload",
                data=fields,
                files={"file": (image_path.name, image_file, mime_type)},
                headers={"User-Agent": "Talkinchat/5.8.3", "Accept": "text/plain"},
                timeout=(10, 45),
            )
        response.raise_for_status()
        uploaded_url = str(response.text or "").strip().strip('"')
        if uploaded_url.startswith("{"):
            try:
                payload = response.json()
                uploaded_url = str(
                    payload.get("url") or payload.get("photo_url")
                    or payload.get("result") or ""
                ).strip()
            except Exception:
                uploaded_url = ""
        if not uploaded_url or uploaded_url.casefold() in {"failed", "error", "none", "null"}:
            raise RuntimeError("رفض خادم Talkinchat رفع صورة الملف الشخصي")
        if not uploaded_url.startswith(("http://", "https://")):
            raise RuntimeError("استجابة رفع الصورة ليست رابطًا صالحًا")
        self.log("[PROFILE] official avatar upload completed:", uploaded_url[:180])
        return uploaded_url

    def _queue_profile_roster_refresh(self, room, username=""):
        """Refresh Talkin's native avatar roster in the background per member."""
        room = str(room or "").strip()
        room_key = _norm_room(room)
        user_key = _norm_user(username) if username else "*"
        key = (room_key, user_key)
        if not room or not room_key:
            return False
        lock = getattr(self, "_profile_roster_refresh_lock", None)
        if lock is None:
            lock = self._profile_roster_refresh_lock = threading.Lock()
        with lock:
            recent = getattr(self, "_profile_roster_refresh_at", {})
            now = time.monotonic()
            if now - float(recent.get(key, 0) or 0) < 30:
                return False
            recent[key] = now
            self._profile_roster_refresh_at = recent

        def refresh():
            try:
                # Match the APK's independent get_profile request first. A
                # room roster is only a fallback because users may be offline
                # or absent from the current room.
                if username:
                    self._request_native_profile_lookup(username)
                # request_room_occupants() prefers Supabase and returns before
                # asking Talkin. Avatars are delivered in Talkin UserItems, so
                # explicitly request the native roster here as a fallback.
                self._request_native_profile_roster(room)
            except Exception as exc:
                self.log("[PROFILE-PHOTO] room roster refresh failed:", type(exc).__name__)

        threading.Thread(target=refresh, name="profile-roster-refresh", daemon=True).start()
        return True

    def _request_native_profile_lookup(self, username, alternate=False):
        """Request a user profile like TalkinChat's get_profile IQ.

        The gateway may return the profile as a single ``profile`` object or
        normalize it into ``users[]``; both are consumed by
        ``_cache_user_photos_from_result``. This request is deliberately
        best-effort so older gateways keep using the roster fallback.
        """
        username = str(username or "").strip().lstrip("@")
        if not username or not getattr(self, "ws", None):
            return False
        lock = getattr(self, "_profile_lookup_lock", None)
        if lock is None:
            lock = self._profile_lookup_lock = threading.Lock()
        with lock:
            recent = getattr(self, "_profile_lookup_at", {})
            key = _norm_user(username)
            now = time.monotonic()
            # An alternate spelling is permitted immediately after the primary
            # request; ordinary duplicate commands remain rate-limited.
            if not alternate and now - float(recent.get(key, 0) or 0) < 15.0:
                return False
            recent[key] = now
            self._profile_lookup_at = recent
        try:
            action = "get_profile" if alternate else PROFILE_LOOKUP_ACTION
            type_name = "profile" if alternate else PROFILE_LOOKUP_TYPE

            # Talkin ResultMessage.uid echoes Query.id_. Give every profile
            # lookup its own correlation token. The old username-keyed pending
            # map retained old requests for 20 seconds; simultaneous responses
            # without an embedded username then could not be attributed safely.
            request_id = secrets.token_urlsafe(15)[:20]
            pending = getattr(self, "_profile_lookup_pending", None)
            if not isinstance(pending, dict):
                pending = self._profile_lookup_pending = {}
            for old_id, old_item in list(pending.items()):
                if (not isinstance(old_item, dict)
                        or float(old_item.get("expires", 0) or 0) < now):
                    pending.pop(old_id, None)
            pending[request_id] = {
                "username": username,
                "username_key": key,
                "request_id": request_id,
                "expires": now + 20.0,
                "created": now,
            }
            sent = self.send_query(encode_query(
                action,
                type_=type_name,
                to=username,
                value="none",
                id_=request_id,
            ))
            if sent is False:
                pending.pop(request_id, None)
            self.log("[PROFILE-PHOTO] direct get_profile request sent:",
                     key, "alternate=" + str(bool(alternate)), "request_id=" + request_id)
            return sent is not False
        except Exception as exc:
            self.log("[PROFILE-PHOTO] direct get_profile request failed:", type(exc).__name__)
            return False

    def _lookup_profile_photo(self, username, preferred_room=""):
        """Resolve a profile photo from cache, any stored room ID, then username."""
        raw_username = str(username or "").strip().lstrip("@")
        normalized = raw_username.casefold()
        if not normalized or normalized in {
            _norm_user(BOT_ID), _norm_user(PRIMARY_BOT_ID),
            _norm_user(getattr(self, "bot_id", "")),
        }:
            return ""
        photos = getattr(self, "user_photos", {})
        for cache_key in (normalized, _norm_user(raw_username)):
            photo = str(photos.get(cache_key, "") or "").strip() if isinstance(photos, dict) else ""
            if photo.startswith(("http://", "https://", "//")):
                return "https:" + photo if photo.startswith("//") else photo
        client = getattr(getattr(self, "db", None), "client", None)
        profile_ids = getattr(self, "user_profile_ids", {})
        user_id = ""
        if isinstance(profile_ids, dict):
            user_id = str(profile_ids.get(normalized) or profile_ids.get(_norm_user(raw_username)) or "").strip()
        if not user_id:
            try:
                # The profile may have been learned in a different room. Use
                # the persistent roster index so the lookup works from every
                # room, not just the room whose occupants were loaded first.
                user_id = str(_stored_user_id(raw_username, preferred_room=preferred_room) or "").strip()
                if user_id:
                    if not isinstance(getattr(self, "user_profile_ids", None), dict):
                        self.user_profile_ids = {}
                    self.user_profile_ids[normalized] = user_id
                    self.user_profile_ids[_norm_user(raw_username)] = user_id
            except Exception as exc:
                self.log("[PROFILE-PHOTO] stored roster ID lookup failed:", type(exc).__name__)
        if user_id:
            cached_by_id = str(photos.get("id:" + user_id, "") or "").strip() if isinstance(photos, dict) else ""
            if cached_by_id.startswith(("http://", "https://", "//")):
                return "https:" + cached_by_id if cached_by_id.startswith("//") else cached_by_id
        if not client:
            return ""
        photo_fields = (
            "photo_url", "photo", "avatar_url", "avatar", "image_url", "picture",
            "profile_photo", "profile_photo_url", "profile_picture_url",
            "profile_image_url", "profile_image", "profile_picture", "profile_pic",
            "picture_url", "photoURL", "avatarUrl", "profilePicture", "image",
            "pictureUrl", "profile_photo_url", "photo_uri", "avatar_uri", "media",
        )
        queries = []
        if user_id:
            queries.append(("id", user_id))
        queries.extend(("username", candidate) for candidate in dict.fromkeys((raw_username, normalized)))
        for column, value in queries:
            try:
                response = (client.table("profiles").select("*")
                            .eq(column, value).limit(1).execute())
                rows = getattr(response, "data", None) or []
            except Exception as exc:
                self.log("[PROFILE-PHOTO] profiles lookup failed:", column, type(exc).__name__)
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                photo = ""
                for key in photo_fields:
                    value = row.get(key)
                    if value is None:
                        continue
                    candidate_url = _extract_media_url_universal({"photo": value})
                    if candidate_url:
                        photo = candidate_url
                        break
                if not photo:
                    continue
                if photo.startswith("//"):
                    photo = "https:" + photo
                if not isinstance(getattr(self, "user_photos", None), dict):
                    self.user_photos = {}
                self.user_photos[normalized] = photo
                self.user_photos[_norm_user(raw_username)] = photo
                row_id = str(row.get("id") or "").strip()
                if row_id:
                    if not isinstance(getattr(self, "user_profile_ids", None), dict):
                        self.user_profile_ids = {}
                    self.user_profile_ids[normalized] = row_id
                    self.user_profile_ids[_norm_user(raw_username)] = row_id
                return photo
        # Supabase username columns are often case-sensitive under `eq`; some
        # deployments preserve account-name casing differently from Talkin.
        ilike = getattr(client.table("profiles").select("*"), "ilike", None)
        if callable(ilike):
            for candidate in dict.fromkeys((raw_username, normalized)):
                try:
                    response = (client.table("profiles").select("*")
                                .ilike("username", candidate).limit(1).execute())
                    rows = getattr(response, "data", None) or []
                except Exception as exc:
                    self.log("[PROFILE-PHOTO] case-insensitive lookup failed:", type(exc).__name__)
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    photo = next((url for key in photo_fields
                                  if (url := _extract_media_url_universal({"photo": row.get(key)}))), "")
                    if photo:
                        if photo.startswith("//"):
                            photo = "https:" + photo
                        self.user_photos[normalized] = photo
                        self.user_photos[_norm_user(raw_username)] = photo
                        return photo
        return ""

    def share_personal_design_private(self, sender: str, target: str, design_kind: str, room: str = ""):
        """Send the sender's saved portrait/name design as a native private image."""
        sender = str(sender or "").strip().lstrip("@")
        target = str(target or "").strip().lstrip("@")
        design_kind = "portrait" if str(design_kind or "").casefold() == "portrait" else "name"
        designs = getattr(self, "personal_designs", {})
        user_designs = designs.get(_norm_user(sender)) if isinstance(designs, dict) else None
        design = user_designs.get(design_kind) if isinstance(user_designs, dict) else None
        filename = Path(str((design or {}).get("filename") or "")).name if isinstance(design, dict) else ""
        expected_prefix = "portrait3d_" if design_kind == "portrait" else "name3d_"
        valid_suffixes = (".gif", ".jpg", ".jpeg", ".png")
        media_path = BASE_DIR / "generated_publish" / filename
        allowed_prefixes = (expected_prefix, "light_" + expected_prefix)
        if (not target or not filename.startswith(allowed_prefixes)
                or not filename.lower().endswith(valid_suffixes) or not media_path.is_file()):
            notice = "⚠️ لا توجد صورة شخصية محفوظة للمشاركة؛ استخدم صمم صورتي أولاً." if design_kind == "portrait" else "⚠️ لا توجد بطاقة اسم محفوظة للمشاركة؛ استخدم اسم <اسم المستخدم> أولاً."
            self.send_room_text(room, notice) if room else self.send_private_text(sender, notice)
            return True
        media_url = ""
        try:
            label = "الصورة الشخصية" if design_kind == "portrait" else "بطاقة الاسم"
            text_sent = self.send_private_text(target, f"🖼️ تمت مشاركة {label} معك من @{sender}.")
            if text_sent is False:
                raise RuntimeError("تعذر إرسال رسالة المشاركة النصية")
            time.sleep(0.60)
            media_sent = self.send_private_media(
                target, media_url, "image", filename=filename, local_path=media_path
            )
            if media_sent is False:
                raise RuntimeError("خادم Talkin لم يؤكد إرسال الصورة الخاصة")
        except Exception as exc:
            self.log("[PERSONAL-MEDIA] private share failed:", repr(exc))
            notice = f"⚠️ تعذر مشاركة {label} مع @{target}. سبب الفشل: {str(exc)[:220]}"
            self.send_room_text(room, notice) if room else self.send_private_text(sender, notice)
            return True
        notice = f"✅ تمت مشاركة {label} مع @{target} في الخاص."
        self.send_room_text(room, notice) if room else self.send_private_text(sender, notice)
        return True

    def send_gift_native(self, room: str, gift_id: str, target_username: str):
        """Legacy/native packet kept for diagnostics only. Gift command now sends the real asset image."""
        kwargs = {"room": room}
        kwargs[GIFT_TARGET_FIELD] = target_username
        kwargs[GIFT_ID_FIELD] = gift_id
        return self.send_query(encode_query("gifts", type_=GIFT_PROTOCOL, **kwargs))

    def gift_help(self, room, sender=""):
        english = self._effective_room_language(room) == "en"
        gift_lines = []
        for key, (emoji, arabic_name) in GIFT_CATALOG.items():
            name = GIFT_CATALOG_EN.get(str(key), arabic_name) if english else arabic_name
            cost = GIFT_COSTS.get(str(key), 0)
            gift_lines.append(f"{key} {emoji} {name}={cost}")
        page_size = 30
        pages = [gift_lines[index:index + page_size] for index in range(0, len(gift_lines), page_size)]
        if not pages:
            pages = [[]]
        page_texts = []
        for index, page in enumerate(pages, 1):
            heading = (
                f"🎁 Gifts and prices in points — List {index}/{len(pages)}"
                if english else f"🎁 قائمة الهدايا — الأسعار بالنقاط — القائمة {index}/{len(pages)}"
            )
            if index == len(pages):
                command = "📌 Send: sa@id@username" if english else "📌 للإرسال: sa@رقم@اسم_المستخدم"
                page_texts.append(heading + "\n" + "\n".join(page) + "\n" + command)
            else:
                page_texts.append(heading + "\n" + "\n".join(page))
        return self._send_room_result_pages(
            room, sender, page_texts, "en" if english else "ar"
        )

    def _set_profile_status(self, status: str):
        """Update the bot profile status and restore it after a timed gift status.

        The primary bot's base status is configured with BOT_BASE_STATUS because
        Talkin does not expose the current profile text in the room bootstrap.
        A new gift cancels the previous timer and replaces its temporary status.
        PROFILE_STATUS_ACTION allows deployments using a different Talkin build
        to select the matching profile-update action without changing code.
        """
        status = str(status or "").strip()
        self._profile_status_pending = status
        if len(status) > PROFILE_STATUS_MAX_CHARS:
            # Never send a partial HTML block: a long profile update can make
            # Talkin close the socket and trigger the bot's leave/rejoin loop.
            status = '<b><font size="1" color="#5DE2E7">حماية وألعاب | a1-a6 | دخول@الغرفة</font></b>'
        try:
            sent = False
            # Send exactly one packet.  Some Talkin server builds close the
            # WebSocket when fallback profile actions are sent back-to-back.
            action = PROFILE_STATUS_ACTION
            try:
                payload = encode_query(action, type_="status", body=status, value=status)
                if len(payload) > _ws_payload_limit():
                    # Long HTML statuses must fit the hard 1008-byte Talkin
                    # frame limit. Talkin's profile_update protocol reads the
                    # status from Query.value (field 11); keep that canonical
                    # field and omit the redundant body copy.
                    payload = encode_query(action, type_="status", value=status)
                self.send_query(payload)
                self.log("[PROFILE] status update sent via", action)
                sent = True
                if PROFILE_STATUS_VERIFY:
                    check = threading.Timer(8.0, self._check_profile_status_delivery, args=(status,))
                    check.daemon = True
                    old_check = getattr(self, "_profile_status_check_timer", None)
                    if old_check is not None:
                        old_check.cancel()
                    self._profile_status_check_timer = check
                    check.start()
            except Exception as exc:
                self.log("[PROFILE] action failed", action, repr(exc))
            if not sent:
                self.log("[PROFILE] status update failed: no packet sent")
                self._notify_profile_status_failure("لم يتم إرسال حزمة تحديث الحالة.")
            return sent
        except Exception as exc:
            self.log("[PROFILE] status update failed:", repr(exc))
            self._notify_profile_status_failure(f"{type(exc).__name__}: {exc}")
            return False

    def _check_profile_status_delivery(self, expected: str):
        """Warn the master when the server accepts but does not echo a status."""
        pending = str(getattr(self, "_profile_status_pending", "") or "").strip()
        if pending == expected:
            self._notify_profile_status_failure(
                "تم إرسال الحزمة لكن الخادم لم يؤكد ظهور الحالة بعد 8 ثوانٍ."
            )

    def _notify_profile_status_failure(self, reason: str):
        """Notify the master once per cooldown when profile status cannot be sent."""
        if not BOT_MASTER:
            return
        now = time.time()
        last = float(getattr(self, "_last_profile_status_error_notice", 0.0) or 0.0)
        if now - last < 300.0:
            return
        self._last_profile_status_error_notice = now
        try:
            self.send_private_text(
                BOT_MASTER,
                "⚠️ تعذر تحديث حالة بروفايل البوت.\n"
                f"السبب: {str(reason)[:500]}\n"
                f"الفعل المستخدم: {PROFILE_STATUS_ACTION}",
            )
        except Exception as exc:
            self.log("[PROFILE] failed to notify master:", repr(exc))

    def _set_temporary_gift_status(self, sender: str, receiver: str, gift_name: str):
        sender = str(sender or "").strip().lstrip("@")
        receiver = str(receiver or "").strip().lstrip("@")
        gift_name = str(gift_name or "هدية").strip()
        with self._profile_status_lock:
            self._profile_status_token += 1
            token = self._profile_status_token
            timer = self._profile_status_timer
            if timer is not None:
                timer.cancel()

            # Keep the gift status short and independent from the base status.
            # This uses the exact same profile-status sending method, but avoids
            # the larger combined payload that previously caused WebSocket 1009.
            temporary = (
                f'<font color="#66D9FF">🎁 المرسل: {sender}</font>'
                f'<br><font color="#FF9ED8">🎁 المستقبل: {receiver}</font>'
                f'<br><font color="#6A1B9A">🎁 نوع الهدية: {gift_name}</font>'
            )
            self._set_profile_status(temporary)

            def restore():
                with self._profile_status_lock:
                    if token != self._profile_status_token:
                        return
                    self._profile_status_timer = None
                    self._set_profile_status(self._profile_base_status)
                    self._profile_current_status = self._profile_base_status

            self._profile_status_timer = threading.Timer(GIFT_STATUS_SECONDS, restore)
            self._profile_status_timer.daemon = True
            self._profile_status_timer.start()

    def handle_gift_command(self, room: str, text: str, sender_name: str = "", private_to: str = ""):
        raw=text.strip(); m=re.match(r"^sa@([^@]+)@(.+)$",raw,re.I)
        if not m: return False
        gift_id=m.group(1).strip(); target=m.group(2).strip(); item=GIFT_CATALOG.get(gift_id)
        if not item or not target:
            self.reply_text(room,"❌ الصيغة: sa@رقم_الهدية@اسم_المستخدم",private_to); return True
        try:
            sender_name = str(sender_name or BOT_ID)
            if not _is_verified_user(sender_name):
                self.reply_text(room, f"🔒 @{sender_name} يحتاج توثيق VIP لإرسال الهدايا.\n{_verification_notice()}", private_to)
                return True
            if not _is_verified_user(target):
                self.reply_text(
                    room,
                    f"🔒 المستلم @{target} غير موثق.\n📩 اطلب توثيق VIP للمستلم أولاً ثم أعد إرسال الهدية.",
                    private_to
                )
                return True
            # Giant Chat point costs; owner/masters have unlimited points.
            cost=int(GIFT_COSTS.get(str(gift_id),0)); charged=False
            if not _is_master_name(sender_name):
                balance=_get_points(sender_name)
                if balance < cost:
                    self.reply_text(room,f"❌ رصيدك غير كافٍ. الهدية تحتاج {_fmt_points(cost)} نقطة، ورصيدك {_fmt_points(balance)}.",private_to); return True
                _add_points(sender_name,-cost); charged=True
            # Render and send the real gift card image, then send the gift text.
            # The image is hosted by the bot media server under /gifts/.
            public_base = _public_base_url()
            if not public_base:
                if charged:
                    _add_points(sender_name, cost)
                raise RuntimeError("لا يوجد رابط عام لصور الهدايا؛ اضبط PUBLIC_MEDIA_URL أو PUBLIC_BASE_URL أو رابط النطاق العام للاستضافة")
            sender_key = sender_name.casefold().lstrip("@")
            receiver_key = target.casefold().lstrip("@")
            # Use each account's own cached/profile image beside its own name.
            sender_photo_url = self.user_photos.get(sender_key, "") or self._lookup_profile_photo(sender_name)
            receiver_photo_url = self.user_photos.get(receiver_key, "") or self._lookup_profile_photo(target)
            gift_path = render_gift_card(gift_id, sender_name, target, sender_photo_url, receiver_photo_url)
            gift_url = public_base + "/gifts/" + gift_path.name
            self._verify_public_media_url(gift_url, "image")
            if not gift_path.is_file() or gift_path.stat().st_size < 64:
                raise RuntimeError(f"ملف صورة الهدية غير صالح: {gift_path}")
            # Put the gift notice above the configured base status in one
            # bounded profile-status value, then restore the base status later.
            self._set_temporary_gift_status(
                sender_name,
                target,
                GIFT_CATALOG.get(str(gift_id), ("🎁", "هدية"))[1],
            )
            if private_to:
                self.send_private_media(private_to, gift_url, "image")
                self.send_private_text(private_to, f"🎁 {item[0]} {item[1]}\n📤 {sender_name} ➜ 📥 {target}\n💰 {cost} نقطة")
            else:
                gift_text = f"🎁 {item[0]} {item[1]}\n📤 {sender_name} ➜ 📥 {target}\n💰 {cost} نقطة"
                self._broadcast_gift_to_all_rooms(gift_url, gift_text, room)
        except Exception as e:
            self.report_master_error("إرسال صورة الهدية", e, room)
            self.reply_text(room, "❌ تعذر إرسال صورة الهدية. تم إرسال الخطأ الحقيقي للماستر.", private_to)
        return True

    def _broadcast_gift_to_all_rooms(self, gift_url: str, gift_text: str, exclude_room: str = ""):
        """Send the gift image and its message to every room the bot currently knows."""
        rooms = set()
        for source in (
            getattr(self, "known_rooms", set()) or set(),
            getattr(self, "rooms", {}) or {},
        ):
            try:
                rooms.update(str(r).strip() for r in source if str(r).strip())
            except Exception:
                pass
        current = str(getattr(self, "room", "") or "").strip()
        if current:
            rooms.add(current)

        # Send sequentially to avoid a burst of WebSocket packets.
        sent = 0
        for r in sorted(rooms):
            try:
                self.send_room_media(r, gift_url, "image")
                self.send_room_text(r, gift_text)
                sent += 1
                time.sleep(0.08)
            except Exception as exc:
                self.log("[GIFT] broadcast failed:", r, repr(exc))
        return sent

    def _send_rare_diamond_winner_card(self, room, winner_name):
        """Send a personalized rare-diamond winner image when its base artwork exists."""
        base = _public_base_url()
        if not base:
            self.log("[RARE-DIAMOND] winner image skipped: public media URL is not configured")
            return False
        source = _rare_diamond_asset_path()
        if not source:
            self.log("[RARE-DIAMOND] winner image skipped: rare-diamond artwork has not been added yet")
            return False
        try:
            winner_key = _norm_user(winner_name)
            winner_photo = getattr(self, "user_photos", {}).get(winner_key, "")
            if not winner_photo:
                winner_photo = self._lookup_profile_photo(winner_name)
            card = render_rare_diamond_winner_card(winner_name, winner_photo, source)
            url = f"{base}/games/{card.name}"
            self._verify_public_media_url(url, "image")
            sent = self.send_room_media(room, url, "image") is not False
            self.log("[RARE-DIAMOND] winner card", winner_name, "sent=" + str(sent))
            return sent
        except Exception as exc:
            self.log("[RARE-DIAMOND] winner card failed:", repr(exc))
            return False

    def _render_royal_winner_card(self, item, username, avatar_url=""):
        """Render winner identity into the artwork's gold frame (never a red overlay)."""
        if not PIL_AVAILABLE:
            return None
        item = "royal_dragon" if item == "royal_dragon" else "royal_diamond"
        asset = "royal_dragon_winner_3d.jpg" if item == "royal_dragon" else "royal_diamond_winner_3d.jpg"
        source = ASSETS_DIR / "treasures" / asset
        if not source.is_file():
            return None
        try:
            with Image.open(source) as raw:
                card = raw.convert("RGBA")
            card.thumbnail((960, 640), Image.Resampling.LANCZOS)
            width, height = card.size

            avatar = None
            if avatar_url:
                try:
                    response = requests.get(str(avatar_url), timeout=(3, 8),
                                            headers={"User-Agent": "Talkin1Bot/1.0"})
                    response.raise_for_status()
                    with Image.open(io.BytesIO(response.content)) as raw_avatar:
                        avatar = raw_avatar.convert("RGBA")
                    avatar.thumbnail((88, 88), Image.Resampling.LANCZOS)
                except Exception as exc:
                    self.log("[ROYAL-HUNT] winner avatar download failed:", repr(exc))

            font_candidates = [
                ASSETS_DIR / "NotoSansArabic-SemiBold.ttf",
                ASSETS_DIR / "DejaVuSans-Bold.ttf",
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            ]
            font_path = next((fp for fp in font_candidates if fp.is_file()), None)
            font_size = max(18, int(width * 0.052))
            font = ImageFont.truetype(str(font_path), font_size) if font_path else ImageFont.load_default()
            name = str(username or "").strip().lstrip("@")[:32]
            label = f"الفائز: @{name}"

            if item == "royal_dragon":
                # The dragon artwork already contains a gold nameplate near its bottom.
                # Fit the avatar and winner name inside that existing plate.
                x1, y1, x2, y2 = (int(width * .245), int(height * .755),
                                  int(width * .755), int(height * .895))
                avatar_size = max(38, int((y2 - y1) * .78))
                avatar_x = x1 + int((x2 - x1) * .07)
                avatar_y = y1 + (y2 - y1 - avatar_size) // 2
                text_left = avatar_x + avatar_size + int(width * .025)
                text_right = x2 - int(width * .025)
            else:
                # The diamond artwork has no built-in nameplate: extend the canvas
                # below the artwork and create a matching navy-and-gold footer there.
                footer_h = max(112, int(height * .22))
                expanded = Image.new("RGBA", (width, height + footer_h), (8, 13, 35, 255))
                expanded.alpha_composite(card, (0, 0))
                card = expanded
                draw_bg = ImageDraw.Draw(card)
                margin = int(width * .055)
                draw_bg.rounded_rectangle(
                    (margin, height + 12, width - margin, height + footer_h - 12),
                    radius=max(16, int(width * .025)),
                    fill=(12, 19, 48, 245),
                    outline=(218, 174, 75, 255),
                    width=max(3, int(width * .006)),
                )
                x1, y1, x2, y2 = margin, height + 12, width - margin, height + footer_h - 12
                avatar_size = min(88, int(footer_h * .66))
                avatar_x = x1 + int(width * .045)
                avatar_y = y1 + (y2 - y1 - avatar_size) // 2
                text_left = avatar_x + avatar_size + int(width * .035)
                text_right = x2 - int(width * .04)

            if avatar is not None:
                avatar = avatar.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
                mask = Image.new("L", (avatar_size, avatar_size), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, avatar_size - 1, avatar_size - 1), fill=255)
                card.paste(avatar, (avatar_x, avatar_y), mask)
                ImageDraw.Draw(card).ellipse(
                    (avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size),
                    outline=(255, 220, 130, 255), width=max(2, int(width * .004))
                )

            draw = ImageDraw.Draw(card)
            available = max(80, text_right - text_left)
            # Reduce font size when the master username is long.
            while font_size > 14 and draw.textbbox((0, 0), label, font=font)[2] > available:
                font_size -= 2
                font = ImageFont.truetype(str(font_path), font_size) if font_path else ImageFont.load_default()
            text_y = y1 + ((y2 - y1) - font_size) // 2
            draw.text((text_left, text_y), label, fill=(255, 239, 190, 255),
                      font=font, stroke_width=1, stroke_fill=(30, 20, 5, 220))

            out = BASE_DIR / "generated_publish" / f"royal_winner_{item}_{uuid.uuid4().hex}.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            card.convert("RGB").save(out, "JPEG", quality=90, optimize=True, progressive=True)
            return out
        except Exception as exc:
            self.log("[ROYAL-HUNT] winner card failed:", repr(exc))
            return None

    def _send_steal_image(self, room, game_key, winner_name=""):
        if winner_name:
            self._send_game_winner_card(game_key, winner_name, [room], respect_room_filter=False)
        else:
            base = _public_base_url()
            filename = GAME_IMAGE_FILES.get(game_key)
            image = ASSETS_DIR / filename if filename else None
            if base and image and image.is_file():
                self.send_room_media(room, f"{base}/assets/{filename}", "image")

    def _ocr_publish_image(self, media_url):
        """Extract visible text from a publish image for the same word filter.

        OCR is best-effort: if OCR is unavailable or the image has no readable
        text, publication continues normally. Arabic and English are both
        scanned.
        """
        if not TESSERACT_AVAILABLE or not PIL_AVAILABLE or not media_url:
            return ""
        try:
            from io import BytesIO
            r = requests.get(media_url, headers={"User-Agent":"Mozilla/5.0", "Accept":"image/*"}, timeout=(1.5, 2.5))
            r.raise_for_status()
            if len(r.content) > 12 * 1024 * 1024:
                return ""
            img = Image.open(BytesIO(r.content)).convert("RGB")
            # Keep OCR responsive on Railway while retaining enough detail for
            # Arabic text in normal phone screenshots/photos.
            max_side = 2200
            if max(img.size) > max_side:
                ratio = max_side / float(max(img.size))
                img = img.resize((max(1,int(img.width*ratio)), max(1,int(img.height*ratio))))
            try:
                return str(pytesseract.image_to_string(img, lang="ara+eng", config="--psm 6") or "").strip()
            except Exception:
                return str(pytesseract.image_to_string(img, lang="eng", config="--psm 6") or "").strip()
        except Exception as exc:
            self.log("[PUBLISH-OCR] skipped:", repr(exc))
            return ""

    def _handle_publish_media_legacy(self, room, sender, media_url, description=""):
        if not media_url: return False
        # Accept the pending image from ANY room (or private chat).
        key=_norm_user(sender); pending=self.publish_pending.get(key)
        if not pending: return False
        if time.time()-pending.get("created_at",0)>120:
            self.publish_pending.pop(key,None); self.send_private_text(sender,"⌛ انتهت مهلة النشر، أرسل أمر بوست أو انشر من جديد."); return True
        desc=_fit_text_to_packet("room_message", str(pending.get("description", description or ""))[:500], room=room)
        if _is_publish_banned(sender):
            self.send_private_text(sender,"🚫 حسابك ممنوع من النشر حالياً.\n📌 لفك المنع راجع الماستر.")
            self.publish_pending.pop(key,None)
            return True
        # Check both the written description and text visible inside the image.
        publish_hit = self._find_publish_filter_hit(desc)
        ocr_text = ""
        if not publish_hit:
            ocr_text = self._ocr_publish_image(media_url)
            publish_hit = self._find_publish_filter_hit(ocr_text)
        if publish_hit:
            source = "الوصف" if self._find_publish_filter_hit(desc) else "الصورة"
            self.send_private_text(sender, f"🚫 تم منع النشر: تم اكتشاف كلمة محظورة في {source}.\n⛔ تم منع حسابك من النشر حتى فك المنع.")
            self.publish_pending.pop(key,None)
            _record_filter_ban(sender,room,"محاولة نشر كلمة مسيئة",publish_hit)
            _record_publish_ban(sender,room,publish_hit)
            return True
        source_room=str(pending.get("source_room") or room or "")
        silent_publish=bool(pending.get("silent"))
        self.publish_pending.pop(key,None)
        # A room that rejected/banned the bot must not abort or receive this
        # publication; all other active rooms continue normally.
        rooms=self._active_rooms()
        # In rooms, the successful publish message contains ONLY the reaction
        # controls. The publish status/result is sent privately to the master.
        # One code identifies the whole image publication. The command prefix
        # (lk/lv/dl/cm/report) selects the reaction type.
        base_code=uuid.uuid4().hex[:4]
        reaction_codes={kind: base_code for kind in ("like", "love", "dislike", "comment", "report")}
        self.reaction_targets[base_code]={
            "publisher": sender, "kind": "image",
            "description": desc or "منشور صورة", "created_at": time.time(),
        }
        caption=_message_template(
            "publish", "broadcast",
            "🖼️ منشور صورة\n👤 {publisher}\n📝 {description}\n━━━━━━━━━━━━━\n👍 lk@{like}\n❤️ lv@{love}\n👎 dl@{dislike}\n💬 cm@{comment} msg\n🚨 report@{report} msg",
            publisher=_name_for_chat_text(sender), description=desc or "بدون وصف",
            source_label=source_room, code=base_code,
            like=reaction_codes["like"], love=reaction_codes["love"], dislike=reaction_codes["dislike"],
            comment=reaction_codes["comment"], report=reaction_codes["report"], room=source_room
        )
        # Build a fresh card for this publication: submitted image + current
        # publisher photo + username, using the same visual treatment as the
        # billion winner card. The generated URL is unique for every publish.
        publish_url = media_url
        try:
            publisher_key = _norm_user(sender)
            publisher_photo = self.user_photos.get(publisher_key, "")
            if not publisher_photo:
                publisher_photo = self._lookup_profile_photo(sender)
            card = render_publish_card(media_url, sender, publisher_photo)
            base = _public_base_url()
            if base:
                publish_url = f"{base}/publish/{card.name}"
                self._verify_public_media_url(publish_url, "image")
            else:
                self.log("[PUBLISH] public base URL unavailable; using original media URL")
        except Exception as exc:
            self.log("[PUBLISH] template render failed; using original image:", repr(exc))

        ok=0
        errors=[]
        for target in rooms:
            try:
                media_result = self.send_room_media(target,publish_url,"image")
                if media_result is False:
                    errors.append((target, "تم رفض حزمة الصورة أو تجاوزت الحد المسموح"))
                    continue
                self.send_room_text(target,caption)
                ok+=1
            except Exception as e:
                errors.append((target,str(e)))
                self.log("[PUBLISH] room delivery failed")
        # Count one publication, not one copy per room, and persist it.
        if ok:
            _record_media_publication("image", desc or "منشور صورة", sender, source_room or room)
        # إشعار بنجاح النشر في الروم وفي الخاص
        confirm_text = f"✅ تم نشر الصورة بنجاح في {ok} غرفة." + (f"\n❌ أخطاء: {len(errors)}" if errors else "")
        if source_room:
            self.send_room_text(source_room, confirm_text)
        elif room:
            self.send_room_text(room, confirm_text)
        if not silent_publish and sender:
            self.send_private_text(sender, confirm_text)
            if errors:
                self.send_private_text(sender, "❌ أخطاء النشر: " + " | ".join(f"{r}: {e[:60]}" for r,e in errors))
        return True

    def _try_publish_pending_media(self, room, media_url, candidates=()):
        """Consume a pending publish request from any Talkin media wrapper."""
        media_url = str(media_url or "").strip()
        if not media_url:
            return False
        pending = getattr(self, "publish_pending", {})
        if not pending:
            return False
        tried = set()
        # فقط صاحب طلب النشر من يتم قبول صورته، وتجاهل أي صورة يرسلها شخص آخر بالغرفة
        for sender in (candidates or []):
            sender = str(sender or "").strip()
            key = _norm_user(sender)
            if not key or key in tried:
                continue
            tried.add(key)
            if key in pending:
                if self._handle_publish_media(room, sender, media_url):
                    return True
        return False

    def _ocr_publish_image(self, media_url):
        """Extract visible text from a publish image for the same word filter.

        OCR is best-effort: if OCR is unavailable or the image has no readable
        text, publication continues normally. Arabic and English are both
        scanned.
        """
        if not TESSERACT_AVAILABLE or not PIL_AVAILABLE or not media_url:
            return ""
        try:
            from io import BytesIO
            r = requests.get(media_url, headers={"User-Agent":"Mozilla/5.0", "Accept":"image/*"}, timeout=(4,8))
            r.raise_for_status()
            if len(r.content) > 12 * 1024 * 1024:
                return ""
            img = Image.open(BytesIO(r.content)).convert("RGB")
            # Keep OCR responsive on Railway while retaining enough detail for
            # Arabic text in normal phone screenshots/photos.
            max_side = 2200
            if max(img.size) > max_side:
                ratio = max_side / float(max(img.size))
                img = img.resize((max(1,int(img.width*ratio)), max(1,int(img.height*ratio))))
            try:
                return str(pytesseract.image_to_string(img, lang="ara+eng", config="--psm 6") or "").strip()
            except Exception:
                return str(pytesseract.image_to_string(img, lang="eng", config="--psm 6") or "").strip()
        except Exception as exc:
            self.log("[PUBLISH-OCR] skipped:", repr(exc))
            return ""

    def _publish_media_worker(self, room, sender, media_url, desc, pending):
        """Run OCR, image rendering and room delivery off the receive thread."""
        if not media_url or not isinstance(pending, dict):
            return
        if _is_publish_banned(sender):
            self.send_private_text(sender,"🚫 حسابك ممنوع من النشر حالياً.\n📌 لفك المنع راجع الماستر.")
            return
        # Check both the written description and text visible inside the image.
        publish_hit = self._find_publish_filter_hit(desc)
        ocr_text = ""
        if not publish_hit:
            ocr_text = self._ocr_publish_image(media_url)
            publish_hit = self._find_publish_filter_hit(ocr_text)
        if publish_hit:
            source = "الوصف" if self._find_publish_filter_hit(desc) else "الصورة"
            self.send_private_text(sender, f"🚫 تم منع النشر: تم اكتشاف كلمة محظورة في {source}.\n⛔ تم منع حسابك من النشر حتى فك المنع.")
            _record_filter_ban(sender,room,"محاولة نشر كلمة مسيئة",publish_hit)
            _record_publish_ban(sender,room,publish_hit)
            return
        source_room=str(pending.get("source_room") or room or "")
        silent_publish=bool(pending.get("silent"))
        # A room that rejected/banned the bot must not abort or receive this
        # publication; all other active rooms continue normally.
        rooms=self._active_rooms()
        # In rooms, the successful publish message contains ONLY the reaction
        # controls. The publish status/result is sent privately to the master.
        # One code identifies the whole image publication. The command prefix
        # (lk/lv/dl/cm/report) selects the reaction type.
        base_code=uuid.uuid4().hex[:4]
        reaction_codes={kind: base_code for kind in ("like", "love", "dislike", "comment", "report")}
        self.reaction_targets[base_code]={
            "publisher": sender, "kind": "image",
            "description": desc or "منشور صورة", "created_at": time.time(),
        }
        caption=_message_template(
            "publish", "broadcast",
            "🖼️ منشور صورة\n👤 {publisher}\n📝 {description}\n━━━━━━━━━━━━━\n👍 lk@{like}\n❤️ lv@{love}\n👎 dl@{dislike}\n💬 cm@{comment} msg\n🚨 report@{report} msg",
            publisher=_name_for_chat_text(sender), description=desc or "بدون وصف",
            source_label=source_room, code=base_code,
            like=reaction_codes["like"], love=reaction_codes["love"], dislike=reaction_codes["dislike"],
            comment=reaction_codes["comment"], report=reaction_codes["report"], room=source_room
        )
        # Build a fresh card for this publication: submitted image + current
        # publisher photo + username, using the same visual treatment as the
        # billion winner card. The generated URL is unique for every publish.
        publish_url = media_url
        try:
            publisher_key = _norm_user(sender)
            publisher_photo = self.user_photos.get(publisher_key, "")
            if not publisher_photo:
                publisher_photo = self._lookup_profile_photo(sender)
            card = render_publish_card(media_url, sender, publisher_photo)
            base = _public_base_url()
            if base:
                publish_url = f"{base}/publish/{card.name}"
                self._verify_public_media_url(publish_url, "image")
            else:
                self.log("[PUBLISH] public base URL unavailable; using original media URL")
        except Exception as exc:
            self.log("[PUBLISH] template render failed; using original image:", repr(exc))

        ok=0
        errors=[]
        room_delay=max(0.02, float(os.getenv("ROOM_BULK_DELAY_SECONDS", "0.08") or 0.08))
        with self._room_bulk_lock:
            for room_index, target in enumerate(rooms):
                try:
                    media_result = self.send_room_media(target,publish_url,"image")
                    if media_result is False:
                        errors.append((target, "تم رفض حزمة الصورة أو تجاوزت الحد المسموح"))
                        continue
                    self.send_room_text(target,caption)
                    ok+=1
                except Exception as e:
                    errors.append((target,str(e)))
                    self.log("[PUBLISH] room delivery failed")
                if room_index + 1 < len(rooms):
                    time.sleep(room_delay)
        # Count one publication, not one copy per room, and persist it.
        if ok:
            _record_media_publication("image", desc or "منشور صورة", sender, source_room or room)
        # إشعار بنجاح النشر في الروم وفي الخاص
        confirm_text = f"✅ تم نشر الصورة بنجاح في {ok} غرفة." + (f"\n❌ أخطاء: {len(errors)}" if errors else "")
        if source_room:
            self.send_room_text(source_room, confirm_text)
        elif room:
            self.send_room_text(room, confirm_text)
        if not silent_publish and sender:
            self.send_private_text(sender, confirm_text)
            if errors:
                self.send_private_text(sender, "❌ أخطاء النشر: " + " | ".join(f"{r}: {e[:60]}" for r,e in errors))
        return True

    def _request_custom_gift_image(self, room, sender, target, *, private_to=""):
        sender = str(sender or "").strip().lstrip("@")
        target = str(target or "").strip().lstrip("@")
        if not sender or not target:
            return False
        if not _is_verified_user(sender):
            english = getattr(self, "_configured_room_language", lambda _room: "ar")(room) == "en"
            notice = (f"🔒 @{sender} must be verified to send a gift image.\n{_verification_notice()}" if english else f"🔒 @{sender} يحتاج توثيقًا لإرسال هدية صورة.\n{_verification_notice()}")
            self.send_private_text(private_to, notice) if private_to else self.send_room_text(room, notice)
            return True
        lock = getattr(self, "_custom_gift_pending_lock", None)
        if lock is None:
            self._custom_gift_pending_lock = lock = threading.RLock()
        with lock:
            pending = getattr(self, "custom_gift_pending", {})
            pending[_norm_user(sender)] = {
                "sender": sender, "receiver": target, "room": str(room or ""),
                "created_at": time.time(), "private_to": str(private_to or ""),
            }
            self.custom_gift_pending = pending
        english = getattr(self, "_configured_room_language", lambda _room: "ar")(room) == "en"
        prompt = "🎁 Send the gift image now." if english else "🎁 أرسل صورة الهدية الآن"
        self.send_private_text(private_to, prompt) if private_to else self.send_room_text(room, prompt)
        return True

    def _try_custom_welcome_image_media(self, room, media_url, candidates=()):
        """Save a verified user's pending welcome image URL to their profile."""
        media_url = str(media_url or "").strip()
        if not media_url.startswith(("http://", "https://")):
            return False
        pending_map = getattr(self, "custom_welcome_image_pending", {})
        if not isinstance(pending_map, dict) or not pending_map:
            return False
        for candidate in candidates:
            sender_key = _norm_user(candidate)
            if not sender_key:
                continue
            request = pending_map.get(sender_key)
            if not isinstance(request, dict):
                continue
            if time.time() - float(request.get("created_at") or 0) > 120:
                pending_map.pop(sender_key, None)
                continue
            username = str(request.get("username") or candidate).strip().lstrip("@")
            if not member_shop.active(
                "image_welcome", username=username,
                user_id=_stored_user_id(username, room),
                aliases=_stored_user_aliases(username, room),
            ):
                pending_map.pop(sender_key, None)
                self.send_private_text(username, "🔒 ميزة الترحيب بالصورة غير مفعّلة لهذا الحساب؛ اشترِها من المتجر أولًا.")
                return True
            welcomes = getattr(self, "custom_welcomes", None)
            if not isinstance(welcomes, dict):
                welcomes = self.custom_welcomes = {}
            entry = welcomes.get(sender_key)
            if not isinstance(entry, dict):
                entry = {"username": username}
            entry["username"] = username
            entry["image_url"] = media_url
            entry["image_updated_at"] = time.time()
            welcomes[sender_key] = entry
            pending_map.pop(sender_key, None)
            self.custom_welcome_enabled = True
            self._save_social_features()
            self.send_private_text(username, "✅ تم حفظ صورة الترحيب. ستظهر عند دخولك الجديد للغرفة. للحذف: حذف ترحيب الصورة، أو حذف ترحيب لحذف النص والصورة.")
            return True
        return False

    def _send_saved_custom_welcome_image(self, room, username, custom_welcome, *, fresh_join):
        """Send a saved personal welcome image only on a genuinely new join."""
        if not fresh_join or not isinstance(custom_welcome, dict):
            return False
        if _norm_user(username) in {_norm_user(BOT_MASTER), _norm_user(BOT_ID)}:
            return False
        image_url = str(custom_welcome.get("image_url") or "").strip()
        if not image_url.startswith(("http://", "https://")):
            return False
        threading.Thread(
            target=self._render_and_send_personal_welcome_image,
            args=(str(room or ""), str(username or ""), image_url),
            daemon=True,
            name="personal-welcome-image",
        ).start()
        return True

    def _render_and_send_personal_welcome_image(self, room, username, image_url):
        """Download the user's chosen art, stamp their exact username, and send it."""
        try:
            if not PIL_AVAILABLE:
                raise RuntimeError("Pillow غير مثبت")
            response = requests.get(image_url, timeout=(5, 15), stream=True)
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise ValueError("صورة الترحيب أكبر من 8 MB")
                chunks.append(chunk)
            if not chunks:
                raise ValueError("ملف صورة الترحيب فارغ")
            raw = b"".join(chunks)
            with Image.open(io.BytesIO(raw)) as probe:
                width, height = probe.size
                if width < 1 or height < 1 or width * height > 24_000_000:
                    raise ValueError("أبعاد صورة الترحيب غير مناسبة")
                image = probe.convert("RGBA")
            target_w = min(1200, image.width)
            if image.width > target_w:
                target_h = max(1, round(image.height * target_w / image.width))
                image = image.resize((target_w, target_h), Image.Resampling.LANCZOS)
            w, h = image.size
            banner_h = max(112, int(h * 0.20))
            banner_y = h - banner_h
            overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            draw.rectangle((0, banner_y, w, h), fill=(4, 20, 30, 225))
            draw.line((int(w * .06), banner_y + 2, int(w * .94), banner_y + 2),
                      fill=(244, 196, 92, 255), width=max(2, int(w * .004)))
            _draw_centered(
                draw, (w / 2, banner_y + banner_h * .34), "أهلاً وسهلاً",
                max(20, int(banner_h * .23)), (255, 224, 145, 255), int(w * .88),
            )
            _draw_name_centered(
                draw, (w / 2, banner_y + banner_h * .70), str(username or "").lstrip("@"),
                max(20, int(banner_h * .25)), (255, 255, 255, 255), int(w * .88),
                apply_correction=True, force_ltr=True,
            )
            image = Image.alpha_composite(image, overlay).convert("RGB")
            out_dir = BASE_DIR / "generated_welcomes"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"welcome_{uuid.uuid4().hex}.jpg"
            image.save(out, "JPEG", quality=88, optimize=True)
            base = _public_base_url()
            if not base:
                raise RuntimeError("رابط الوسائط العامة غير مضبوط")
            url = f"{base}/welcomes/{out.name}"
            self._verify_public_media_url(url, "image")
            self.send_room_media(room, url, "image")
        except Exception as exc:
            self.log("[WELCOME] personalized image send failed:", username, repr(exc))

    def _try_custom_gift_media(self, room, media_url, candidates=()):
        media_url = str(media_url or "").strip()
        sender = next((str(value or "").strip() for value in candidates if str(value or "").strip()), "")
        if not media_url or not sender:
            return False
        lock = getattr(self, "_custom_gift_pending_lock", None)
        if lock is None:
            return False
        with lock:
            pending = getattr(self, "custom_gift_pending", {})
            key = _norm_user(sender)
            request = pending.get(key)
            if not isinstance(request, dict):
                return False
            if time.time() - float(request.get("created_at") or 0) > 120:
                pending.pop(key, None)
                return False
            pending.pop(key, None)
        threading.Thread(
            target=self._custom_gift_image_worker,
            args=(str(request.get("room") or room), str(request.get("sender") or sender),
                  str(request.get("receiver") or ""), media_url, str(request.get("private_to") or "")),
            name="custom-gift-image", daemon=True,
        ).start()
        return True

    def _custom_gift_image_worker(self, room, sender, receiver, media_url, private_to=""):
        """Make a tall exact-text gift card from the submitted artwork, then fan it out safely."""
        try:
            if not PIL_AVAILABLE:
                raise RuntimeError("Pillow غير مثبت")
            base = _public_base_url()
            if not base:
                raise RuntimeError("لا يوجد رابط عام لإرسال صورة الهدية")
            response = requests.get(media_url, timeout=(5, 15), stream=True)
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > 12 * 1024 * 1024:
                    raise ValueError("حجم الصورة أكبر من 12 ميغابايت")
                chunks.append(chunk)
            source = Image.open(io.BytesIO(b"".join(chunks))).convert("RGB")
            max_w, max_h = 1100, 1000
            scale = min(max_w / source.width, max_h / source.height, 1.0)
            source = source.resize((max(1, round(source.width * scale)), max(1, round(source.height * scale))), Image.Resampling.LANCZOS)
            width, photo_h = source.size
            panel_h = 390
            card = Image.new("RGB", (width, photo_h + panel_h), (13, 16, 28))
            card.paste(source, (0, 0))
            draw = ImageDraw.Draw(card)
            draw.rounded_rectangle((24, photo_h + 18, width - 24, photo_h + panel_h - 18), radius=28,
                                   fill=(16, 21, 37), outline=(244, 196, 92), width=4)
            _draw_centered(draw, (width / 2, photo_h + 65), "🎁 Gift / هدية", 40, (255, 222, 155), width - 80)
            _draw_centered(draw, (width / 2, photo_h + 145), f"Sender / المرسل: {sender}", 32, (126, 226, 255), width - 80)
            _draw_centered(draw, (width / 2, photo_h + 225), f"Recipient / المستلم: {receiver}", 32, (255, 166, 218), width - 80)
            _draw_centered(draw, (width / 2, photo_h + 310), f"Room / الغرفة: {room}", 29, (230, 230, 238), width - 80)
            generated = GENERATED_GIFTS_DIR / f"usergift_{uuid.uuid4().hex}.jpg"
            generated.parent.mkdir(parents=True, exist_ok=True)
            card.save(generated, "JPEG", quality=90, optimize=True, progressive=False)
            media = f"{base.rstrip('/')}/gifts/{generated.name}"
            self._verify_public_media_url(media, "image")
            rooms = list(self._active_rooms())
            if room and all(_norm_room(r) != _norm_room(room) for r in rooms):
                rooms.append(room)
            sent, failed = 0, 0
            delay = max(0.02, float(os.getenv("ROOM_BULK_DELAY_SECONDS", "0.08") or 0.08))
            bulk_lock = getattr(self, "_room_bulk_lock", None)
            if bulk_lock is None:
                bulk_lock = threading.RLock()
            with bulk_lock:
                for index, target_room in enumerate(rooms):
                    try:
                        english = getattr(self, "_configured_room_language", lambda _room: "ar")(target_room) == "en"
                        caption = (f"🎁 Gift image\n📤 Sender: @{sender}\n📥 Recipient: @{receiver}\n🏠 Room: {room}" if english else f"🎁 هدية مصوّرة\n📤 المرسل: @{sender}\n📥 المستلم: @{receiver}\n🏠 الغرفة: {room}")
                        if self.send_room_media(target_room, media, "image") is False:
                            failed += 1
                        else:
                            self.send_room_text(target_room, caption)
                            sent += 1
                    except Exception as exc:
                        failed += 1
                        self.log("[CUSTOM-GIFT] room delivery failed:", str(target_room), repr(exc))
                    if index + 1 < len(rooms):
                        time.sleep(delay)
            english = getattr(self, "_configured_room_language", lambda _room: "ar")(room) == "en"
            result = (f"✅ Gift image shared in {sent} room(s)." + (f" Failed: {failed}." if failed else "")) if english else (f"✅ نُشرت صورة الهدية في {sent} غرفة." + (f" تعذر النشر في {failed} غرفة." if failed else ""))
            if private_to:
                self.send_private_text(private_to, result)
            else:
                self.send_room_text(room, result)
        except Exception as exc:
            self.log("[CUSTOM-GIFT] image processing failed:", repr(exc))
            english = getattr(self, "_configured_room_language", lambda _room: "ar")(room) == "en"
            if private_to:
                self.send_private_text(private_to, "❌ Could not prepare or share the gift image; send the command and image again." if english else "❌ تعذر تجهيز صورة الهدية أو نشرها؛ أعد إرسال الأمر والصورة.")
            else:
                self.send_room_text(room, "❌ Could not prepare or share the gift image; send the command and image again." if english else "❌ تعذر تجهيز صورة الهدية أو نشرها؛ أعد إرسال الأمر والصورة.")

    def _handle_publish_media(self, room, sender, media_url, description=""):
        """Claim an incoming image and schedule publishing without blocking commands."""
        if not media_url:
            return False
        sender = str(sender or "").strip()
        key = _norm_user(sender)
        if not key:
            return False
        if not hasattr(self, "publish_pending"):
            self.publish_pending = {}
        claim_lock = getattr(self, "_publish_claim_lock", None)
        if claim_lock is None:
            claim_lock = threading.RLock()
            self._publish_claim_lock = claim_lock
        with claim_lock:
            pending = self.publish_pending.pop(key, None)
        if not pending:
            return False
        if time.time() - pending.get("created_at", 0) > 120:
            self.send_private_text(sender, "⌛ انتهت مهلة النشر، أرسل أمر بوست أو انشر من جديد.")
            return True
        if _is_publish_banned(sender):
            self.send_private_text(sender, "🚫 حسابك ممنوع من النشر حالياً.\n📌 لفك المنع راجع الماستر.")
            return True
        desc = _fit_text_to_packet(
            "room_message", str(pending.get("description", description or ""))[:500], room=room
        )
        if self._find_publish_filter_hit(desc):
            hit = self._find_publish_filter_hit(desc)
            self.send_private_text(sender, f"🚫 تم منع النشر: الوصف يحتوي كلمة محظورة ({hit}).\n⛔ تم منع حسابك من النشر حتى فك المنع.")
            _record_filter_ban(sender, room, "محاولة نشر كلمة مسيئة في الوصف", hit)
            _record_publish_ban(sender, room, hit)
            return True
        self.send_private_text(sender, "⏳ بدأ فحص الصورة؛ سيستمر البوت في استقبال الأوامر أثناء نشرها في الغرف.")
        def worker():
            try:
                self._publish_media_worker(room, sender, media_url, desc, pending)
            except Exception as exc:
                self.log("[PUBLISH] background media handling failed:", repr(exc))
                try:
                    self.send_private_text(sender, "❌ تعذر إكمال نشر الصورة. أعد المحاولة بعد التحقق من اتصال البوت.")
                except Exception:
                    pass
        threading.Thread(target=worker, name="publish-image-worker", daemon=True).start()
        return True

    def _try_publish_pending_media(self, room, media_url, candidates=()):
        """Consume a pending publish request from any Talkin media wrapper."""
        media_url = str(media_url or "").strip()
        if not media_url:
            return False
        lock = getattr(self, "_publish_media_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._publish_media_lock = lock
        if not lock.acquire(blocking=False):
            return True
        try:
            tried = set()
            # The first candidate is the authoritative event sender (field 2 / frm).
            # Never fall through to other event fields: they may contain metadata
            # usernames and could let another user claim the pending publication.
            pending = getattr(self, "publish_pending", {})
            authoritative_sender = next(
                (str(item or "").strip() for item in (candidates or []) if str(item or "").strip()),
                "",
            )
            for sender in ((authoritative_sender,) if authoritative_sender else ()):
                sender = str(sender or "").strip()
                key = _norm_user(sender)
                if not key or key in tried:
                    continue
                tried.add(key)
                if key in pending:
                    try:
                        if self._handle_publish_media(room, sender, media_url):
                            return True
                    except Exception as exc:
                        self.log("[PUBLISH] media handling failed:", repr(exc))
            # Never consume another user's pending publish request. The image
            # must belong to the same account that issued بوست/انشر.
            return False
        finally:
            lock.release()

    def _publish_personal_design(self, room, sender, design_kind, is_private=False):
        """Queue personal-design publication and immediately release the room event loop."""
        sender = str(sender or "").strip().lstrip("@")
        key = (_norm_user(sender), str(design_kind or "portrait"))
        lock = getattr(self, "_personal_design_publish_lock", None)
        if lock is None:
            lock = self._personal_design_publish_lock = threading.RLock()
        pending = getattr(self, "_personal_design_publish_pending", None)
        if not isinstance(pending, set):
            pending = self._personal_design_publish_pending = set()
        with lock:
            if key in pending:
                self.send_private_text(sender, "⏳ نشر تصميمك جارٍ بالفعل؛ يمكنك متابعة استخدام بقية الأوامر.")
                return True
            pending.add(key)
        def worker():
            try:
                self._publish_personal_design_worker(room, sender, design_kind, is_private=is_private)
            except Exception as exc:
                self.log("[PERSONAL-DESIGN] background publish failed:", repr(exc))
                try:
                    self.send_private_text(sender, "❌ تعذر إكمال نشر التصميم؛ راجع سجل البوت.")
                except Exception:
                    pass
            finally:
                with lock:
                    pending.discard(key)
        try:
            threading.Thread(target=worker, name="personal-design-publish", daemon=True).start()
            self.send_private_text(sender, "⏳ بدأ نشر تصميمك في الخلفية؛ يمكنك إرسال أوامر أخرى الآن.")
        except Exception as exc:
            with lock:
                pending.discard(key)
            self.log("[PERSONAL-DESIGN] worker start failed:", repr(exc))
            self.send_private_text(sender, "❌ تعذر بدء نشر التصميم؛ حاول مرة أخرى.")
        return True

    def _publish_personal_design_worker(self, room, sender, design_kind, is_private=False):
        """Broadcast the user's last generated portrait/name card with standard publish reactions."""
        sender = str(sender or "").strip().lstrip("@")
        key = _norm_user(sender)
        if not key:
            return True
        if _is_publish_banned(sender):
            self.send_private_text(sender, "🚫 حسابك ممنوع من النشر حالياً. راجع الماستر لفك المنع.")
            return True
        # Personal designs are publishable by verified and VIP users (and the
        # master through _is_verified_user), but are not a master-only action.
        if not _is_verified_user(sender):
            self.send_private_text(sender, f"🔒 النشر متاح للموثقين وVIP فقط.\n{_verification_notice()}")
            return True

        designs = getattr(self, "personal_designs", {})
        user_designs = designs.get(key) if isinstance(designs, dict) else None
        # Keep portrait and name art independently; creating one must not overwrite the other.
        design = user_designs.get(design_kind) if isinstance(user_designs, dict) else None
        if not isinstance(design, dict) or str(design.get("kind") or "") != design_kind:
            needed = "صمم صورتي" if design_kind == "portrait" else "اسم <اسم المستخدم>"
            self.send_private_text(sender, f"ℹ️ لا يوجد تصميم محفوظ لهذا النوع. أنشئه أولاً بالأمر: {needed}")
            return True

        filename = Path(str(design.get("filename") or "")).name
        expected_prefix = "portrait3d_" if design_kind == "portrait" else "name3d_"
        valid_suffixes = (".gif",) if design_kind == "portrait" else (".gif", ".jpg")
        allowed_prefixes = (expected_prefix, "light_" + expected_prefix)
        if (not filename.startswith(allowed_prefixes)
                or not filename.lower().endswith(valid_suffixes)):
            self.send_private_text(sender, "❌ ملف التصميم المحفوظ غير صالح. أعد إنشاء التصميم ثم انشره.")
            return True
        media_path = BASE_DIR / "generated_publish" / filename
        if not media_path.is_file():
            self.send_private_text(sender, "❌ ملف التصميم لم يعد موجوداً على الخادم. أعد إنشاءه بالأمر الخاص به ثم انشره.")
            return True

        display_name = str(design.get("name") or "").strip()
        description = (f"بطاقة الاسم: {display_name}" if design_kind == "name" else "الصورة الشخصية بتصميم 3D متحرك")
        filter_text = display_name if design_kind == "name" else ""
        hit = self._find_publish_filter_hit(filter_text) if filter_text else None
        if hit:
            self.send_private_text(sender, "🚫 تم إيقاف نشر بطاقة الاسم لاحتوائها على لفظ محظور.")
            _record_filter_ban(sender, room, "محاولة نشر بطاقة اسم مسيئة", hit)
            _record_publish_ban(sender, room, hit)
            return True

        base = _public_base_url()
        if not base:
            self.send_private_text(sender, "❌ رابط الوسائط العام PUBLIC_BASE_URL غير مضبوط.")
            return True
        media_url = f"{base}/publish/{quote(filename)}"
        try:
            self._verify_public_media_url(media_url, "image")
            # Apply the same best-effort image-text moderation used by ordinary image publishing.
            ocr_text = self._ocr_publish_image(media_url)
            hit = self._find_publish_filter_hit(ocr_text) if ocr_text else None
            if hit:
                self.send_private_text(sender, "🚫 تم إيقاف نشر التصميم بعد اكتشاف لفظ محظور داخل الصورة.")
                _record_filter_ban(sender, room, "محاولة نشر صورة شخصية مخالفة", hit)
                _record_publish_ban(sender, room, hit)
                return True
        except Exception as exc:
            self.log("[PERSONAL-DESIGN] media verification failed:", repr(exc))
            self.send_private_text(sender, "❌ تعذر التحقق من رابط التصميم العام. تحقق من خدمة الوسائط ثم أعد المحاولة.")
            return True

        rooms = self._active_rooms() or ([str(room)] if room else [])
        if not rooms:
            self.send_private_text(sender, "ℹ️ لا توجد غرف نشطة متاحة للنشر حالياً.")
            return True

        code = uuid.uuid4().hex[:4]
        reaction_codes = {name: code for name in ("like", "love", "dislike", "comment", "report")}
        if not hasattr(self, "reaction_targets") or not isinstance(self.reaction_targets, dict):
            self.reaction_targets = {}
        self.reaction_targets[code] = {
            "publisher": sender,
            "kind": "image",
            "description": description,
            "media_url": media_url,
            "media_type": "image",
            "created_at": time.time(),
        }
        title = "🖼️ صورة شخصية بتصميم 3D" if design_kind == "portrait" else "✍️ بطاقة اسم بتصميم 3D"
        caption = (
            f"{title}\n"
            f"👤 {_name_for_chat_text(sender)}\n"
            + (f"🏷️ {display_name}\n" if display_name else "")
            + "━━━━━━━━━━━━━\n"
            f"👍 lk@{reaction_codes['like']}   ❤️ lv@{reaction_codes['love']}   👎 dl@{reaction_codes['dislike']}\n"
            f"💬 cm@{reaction_codes['comment']} نص\n"
            f"🚨 report@{reaction_codes['report']} نص"
        )

        ok = 0
        errors = []
        delay = max(0.02, float(os.getenv("ROOM_BULK_DELAY_SECONDS", "0.08") or 0.08))
        bulk_lock = getattr(self, "_room_bulk_lock", None)
        if bulk_lock is None:
            bulk_lock = threading.Lock()
            self._room_bulk_lock = bulk_lock
        with bulk_lock:
            for index, target in enumerate(rooms):
                try:
                    result = self.send_room_media(target, media_url, "image")
                    if result is False:
                        errors.append((target, "رفض إرسال صورة التصميم"))
                    else:
                        text_result = self.send_room_text(target, caption)
                        if text_result is False:
                            errors.append((target, "تعذر إرسال أزرار التفاعل"))
                        else:
                            ok += 1
                except Exception as exc:
                    errors.append((target, str(exc)[:100]))
                    self.log("[PERSONAL-DESIGN] room delivery failed:", target, repr(exc))
                if index + 1 < len(rooms):
                    time.sleep(delay)

        if ok:
            _record_media_publication("image", description, sender, str(room or ""))
        confirmation = f"✅ تم نشر التصميم في {ok} غرفة من أصل {len(rooms)} غرفة."
        if errors:
            confirmation += f"\n⚠️ تعذر الإرسال إلى {len(errors)} غرفة."
        self.send_private_text(sender, confirmation)
        if errors:
            self.log("[PERSONAL-DESIGN] room errors:", errors[:20])
        return True

    def _handle_personal_design_command(self, room, command, sender, is_private=False):
        """Create personal 3D art, save the latest design, and publish it on request."""
        raw = str(command or "").strip()
        low = raw.casefold()

        if low in {"حمل اسمي", "تحميل اسمي", "احفظ اسمي", "download my name"}:
            return self._send_personal_download_link(room, sender, "name", is_private=is_private)
        if low in {"حمل صورتي", "تحميل صورتي", "احفظ صورتي", "download my photo"}:
            return self._send_personal_download_link(room, sender, "portrait", is_private=is_private)

        if low in {"انشر صورتي", "انشر صورتى", "publish my photo"}:
            return self._publish_personal_design(room, sender, "portrait", is_private=is_private)
        if low in {"انشر اسمي", "انشر اسمى", "publish my name"}:
            return self._publish_personal_design(room, sender, "name", is_private=is_private)

        if low in {"صمم صورتي", "صمم صورتى", "صمم صورتي 3d", "صمم صورتي ثلاثي الابعاد"}:
            photo_url = (getattr(self, "user_photos", {}).get(_norm_user(sender), "")
                         or getattr(self, "user_photos", {}).get(str(sender or "").casefold(), "")
                         or self._lookup_profile_photo(sender))
            if not photo_url:
                try:
                    self._queue_profile_roster_refresh(room, sender)
                except Exception:
                    pass
                notice = "⏳ لم تصل صورة حسابك بعد. حدّث صورة الحساب أو أعد الأمر بعد لحظات."
                self.send_private_text(sender, notice) if is_private else self.send_room_text(room, notice)
                return True
            try:
                portrait_indices = getattr(self, "_portrait_template_indices", None)
                if not isinstance(portrait_indices, dict):
                    portrait_indices = self._portrait_template_indices = {}
                portrait_key = _norm_user(sender)
                template_index = (int(portrait_indices.get(portrait_key, -1)) + 1) % 5
                portrait_indices[portrait_key] = template_index
                image_path = render_user_3d_portrait_gif(photo_url, sender, variant=template_index)
                media_path, media_type = _prepare_talkin_animation_media(image_path)
                media_path = _optimize_social_media_asset(media_path)
                base = _public_base_url()
                if not base:
                    raise RuntimeError("PUBLIC_BASE_URL غير مضبوط")
                media_url = f"{base}/publish/{quote(media_path.name)}"
                self._verify_public_media_url(media_url, "image")
                key = _norm_user(sender)
                with getattr(self, "_personal_design_lock", threading.RLock()):
                    designs = getattr(self, "personal_designs", None)
                    if not isinstance(designs, dict):
                        designs = self.personal_designs = {}
                    user_designs = designs.get(key)
                    if not isinstance(user_designs, dict):
                        user_designs = {}
                    user_designs["portrait"] = {
                        "kind": "portrait", "filename": media_path.name,
                        "media_type": media_type, "name": "",
                        "created_at": time.time(), "username": str(sender),
                    }
                    designs[key] = user_designs
                    _save_local_json(DATA_DIR / "personal_designs.json", designs)
                sent = (self.send_private_media(sender, media_url, media_type, filename=media_path.name)
                        if is_private else self.send_room_media(room, media_url, media_type))
                if sent is False:
                    raise RuntimeError("رفض الاتصال إرسال الصورة")
                notice = "✨ تم تصميم صورتك وحفظها. للمعاينة استخدمها الآن، وللنشر في كل الغرف أرسل: انشر صورتي"
                self.send_private_text(sender, notice) if is_private else self.send_room_text(room, notice)
            except Exception as exc:
                self.log("[DESIGN] portrait failed:", repr(exc))
                notice = "❌ تعذر تصميم الصورة. تحقق من رابط الوسائط العام وتوفر Pillow."
                self.send_private_text(sender, notice) if is_private else self.send_room_text(room, notice)
            return True

        name_match = re.fullmatch(r"اسم\s+(.+)", raw, re.I | re.S)
        if name_match:
            name = name_match.group(1).strip()
            if not name or len(name) > 48:
                notice = "❌ الاسم يجب أن يكون من 1 إلى 48 حرفاً. مثال: اسم محمد"
                self.send_private_text(sender, notice) if is_private else self.send_room_text(room, notice)
                return True
            hit = self._find_publish_filter_hit(name)
            if hit:
                notice = "🚫 لا يمكن تصميم اسم يحتوي على لفظ محظور."
                self.send_private_text(sender, notice) if is_private else self.send_room_text(room, notice)
                return True
            busy_lock = getattr(self, "_personal_name_card_lock", None)
            if busy_lock is None:
                busy_lock = self._personal_name_card_lock = threading.Lock()
            busy = getattr(self, "_personal_name_card_busy", set())
            key = _norm_user(sender)
            with busy_lock:
                if key in busy:
                    notice = "⏳ بطاقة اسمك قيد التجهيز بالفعل؛ انتظر ظهورها ثم أرسل اسماً جديداً."
                    self.send_private_text(sender, notice) if is_private else self.send_room_text(room, notice)
                    return True
                busy.add(key)
                self._personal_name_card_busy = busy

            def name_card_worker():
                try:
                    image_path = render_3d_name_card(name)
                    base = _public_base_url()
                    if not base:
                        raise RuntimeError("PUBLIC_BASE_URL غير مضبوط")
                    media_url = f"{base}/publish/{quote(image_path.name)}"
                    self._verify_public_media_url(media_url, "image")
                    with getattr(self, "_personal_design_lock", threading.RLock()):
                        designs = getattr(self, "personal_designs", None)
                        if not isinstance(designs, dict):
                            designs = self.personal_designs = {}
                        user_designs = designs.get(key)
                        if not isinstance(user_designs, dict):
                            user_designs = {}
                        user_designs["name"] = {
                            "kind": "name", "filename": image_path.name,
                            "media_type": "image", "name": name,
                            "created_at": time.time(), "username": str(sender),
                        }
                        designs[key] = user_designs
                        _save_local_json(DATA_DIR / "personal_designs.json", designs)
                    sent = (self.send_private_media(sender, media_url, "image", filename=image_path.name)
                            if is_private else self.send_room_media(room, media_url, "image"))
                    if sent is False:
                        raise RuntimeError("رفض الاتصال إرسال الصورة")
                    notice = f"✨ تم تصميم بطاقة الاسم «{name}» وحفظها. لنشرها في كل الغرف أرسل: انشر اسمي"
                    self.send_private_text(sender, notice) if is_private else self.send_room_text(room, notice)
                except Exception as exc:
                    self.log("[DESIGN] name card failed:", repr(exc))
                    notice = "❌ تعذر إنشاء صورة الاسم. تحقق من Pillow ورابط الوسائط العام."
                    self.send_private_text(sender, notice) if is_private else self.send_room_text(room, notice)
                finally:
                    with busy_lock:
                        busy.discard(key)

            self.send_private_text(sender, "⏳ جاري تجهيز بطاقة الاسم في الخلفية؛ يمكنك إرسال أوامر أخرى الآن.") if is_private else self.send_room_text(room, "⏳ جاري تجهيز بطاقة الاسم في الخلفية؛ يمكنك إرسال أوامر أخرى الآن.")
            try:
                threading.Thread(target=name_card_worker, name="personal-name-card", daemon=True).start()
            except Exception as exc:
                with busy_lock:
                    busy.discard(key)
                self.log("[DESIGN] name card worker start failed:", type(exc).__name__)
                notice = "❌ تعذر بدء تجهيز بطاقة الاسم. أعد المحاولة."
                self.send_private_text(sender, notice) if is_private else self.send_room_text(room, notice)
            return True

        if low in {"تغيير صورتك", "غير صورتك", "تغيير صورة البوت", "غير صورة البوت"}:
            if not _is_master_name(sender):
                self.send_room_text(room, "🔒 تغيير صورة البوت مخصص للماستر.")
                return True
            try:
                assets = sorted(ASSETS_DIR.glob("bot_avatar_*.jpg")) + sorted(ASSETS_DIR.glob("bot_avatar_*.png"))
                if not assets:
                    raise RuntimeError("لا توجد صور bot_avatar داخل assets")
                last = str(getattr(self, "_last_bot_avatar_asset", "") or "")
                choices = [p for p in assets if str(p) != last] or assets
                chosen = random.choice(choices)
                base = _public_base_url()
                if not base:
                    raise RuntimeError("PUBLIC_BASE_URL غير مضبوط")
                public_photo_url = f"{base}/assets/{quote(chosen.name)}"
                self._verify_public_media_url(public_photo_url, "image")
                # Match the official Android client: upload the local image
                # first, then send the URL returned by Talkinchat.  Sending
                # our public asset URL directly updates the profile page but
                # does not trigger the room UserItem/avatar update.
                photo_url = self._upload_bot_profile_photo_official(chosen)
                action = _PROFILE_ACTION_ALIASES.get(
                    os.getenv("BOT_PROFILE_PHOTO_ACTION", "profile_update").strip(),
                    os.getenv("BOT_PROFILE_PHOTO_ACTION", "profile_update").strip(),
                ) or "profile_update"
                photo_type = os.getenv("BOT_PROFILE_PHOTO_TYPE", "photo").strip() or "photo"
                packet = encode_query(action, type_=photo_type, value=photo_url)
                if len(packet) > _ws_payload_limit():
                    raise RuntimeError("حزمة تحديث الصورة تجاوزت حد WebSocket")
                sent = self.send_query(packet)
                if sent is False:
                    raise RuntimeError("لم يقبل اتصال WebSocket طلب تغيير الصورة")
                self._last_bot_avatar_asset = str(chosen)
                self.log("[PROFILE] bot photo update request sent:", chosen.name, "action=", action, "type=", photo_type)
                # A profile_update alone can update the profile page while
                # room occupants keep a stale/empty UserItem avatar. Refresh
                # the native roster in every currently connected room too.
                refresh_queued = self._queue_bot_avatar_room_refresh(photo_url)
                self.send_private_text(
                    sender,
                    "📨 أُرسل طلب تغيير صورة البوت. "
                    + ("بدأت مزامنة قوائم الأعضاء في الغرف المتصل بها البوت؛ انتظر قليلاً ثم حدّث الغرفة." if refresh_queued
                       else "تم تغيير طلب الصورة، لكن لم أجد غرفاً متصلة لطلب مزامنة صورتها.")
                    + " إذا بقيت الصورة فارغة، أرسل سجل [PROFILE-PHOTO] بعد التغيير حتى نعرف هل الخادم أعاد الصورة في قائمة أعضاء الغرف."
                )
            except Exception as exc:
                self.log("[PROFILE] bot photo update failed:", repr(exc))
                self.send_private_text(sender, f"❌ تعذر إرسال طلب تغيير صورة البوت: {str(exc)[:180]}")
            return True
        return False

    def _publish_explore_command_sync(self, room, command, sender):
        match = re.fullmatch(r"(aax2|ax2|ax1|aax|ax)@(.+)", str(command or "").strip(), re.I | re.S)
        if not match:
            return False
        command_mode = match.group(1).casefold()
        # ax1 replaces the old aax workflow: it is now the premium template broadcast.
        if command_mode == "aax":
            command_mode = "ax1"
        text = match.group(2).strip()[:150]
        if not text:
            usage = {
                "aax2": "استخدم aax2@النص لمعاينة القالب الملكي في الغرفة الحالية.",
                "ax2": "استخدم ax2@النص لنشر القالب الملكي الجديد في كل الغرف.",
                "ax1": "استخدم ax1@النص لنشر قالب اكسبلور الملكي المتحرك في كل الغرف.",
                "ax": "استخدم ax@النص لنشر اكسبلور الأساسي في كل الغرف.",
            }
            self.send_room_text(room, usage.get(command_mode, "استخدم ax1@النص لنشر قالب اكسبلور الملكي في كل الغرف."))
            return True
        if _is_publish_banned(sender):
            self.send_private_text(sender, "🚫 حسابك ممنوع من النشر حالياً. راجع الماستر لفك المنع.")
            return True
        filter_hit = self._find_publish_filter_hit(text)
        if filter_hit:
            self.send_room_text(room, "🚫 توقف النشر: النص يحتوي إساءة أو لفظاً محظوراً.")
            self.send_private_text(sender, "🚫 تم إيقاف النشر بسبب استخدام لفظ مسيء أو قذف. لن تتمكن من النشر حتى يفك الماستر المنع.")
            _record_filter_ban(sender, room, "محاولة نشر إساءة في اكسبلور", filter_hit)
            _record_publish_ban(sender, room, filter_hit)
            return True
        try:
            key = _norm_user(sender)
            avatar_url = getattr(self, "user_photos", {}).get(key, "") or self._lookup_profile_photo(sender)
            english = self._configured_room_language(room) == "en"
            language = "en" if english else "ar"
            secondary_text = ""
            premium_template = command_mode in {"aax2", "ax1", "ax2"}
            if premium_template:
                mode = "animated"
            else:
                mode = os.getenv("EXPLORE_TEMPLATE_MODE", "cycle").strip().casefold()
                if mode not in {"static", "animated", "cycle"}:
                    mode = "cycle"
                if mode == "cycle":
                    mode = "static"
            explore_variant = (int(getattr(self, "_explore_template_index", -1)) + 1) % 4
            self._explore_template_index = explore_variant
            if premium_template:
                if command_mode == "ax1":
                    card = render_explore_premium_animated_card(
                        text, sender, avatar_url, language=language, variant=explore_variant
                    )
                else:
                    card = render_explore_premium_card(
                        text, sender, avatar_url, language=language, variant=explore_variant
                    )
            else:
                card = (render_explore_animated_card(text, sender, avatar_url, language=language,
                                                     secondary_text=secondary_text)
                        if mode == "animated" else
                        render_explore_card(text, sender, avatar_url, language=language,
                                            secondary_text=secondary_text))
            media_type = "image"
            if mode == "animated" and not premium_template:
                card, media_type = _prepare_talkin_animation_media(card)
                self.log("[EXPLORE] animated media:", card.name, "type=", media_type)
            card = _optimize_social_media_asset(card)
            base = _public_base_url()
            if not base:
                raise RuntimeError("رابط الوسائط العام غير مضبوط")
            media_url = f"{base}/publish/{quote(card.name)}"
            self._verify_public_media_url(media_url, "image")
            code = uuid.uuid4().hex[:4]
            if english:
                prefix = ("🧪 Luxury 3D Explore preview" if command_mode == "aax2"
                          else ("☕ Royal 3D Explore" if command_mode in {"ax1", "ax2"} else "✨ Explore"))
                caption = (f"{prefix}\n👤 Publisher: @{str(sender or '').lstrip('@')}\n"
                           f"📝 Explore text: {text}\n🏠 Room: {room}\n━━━━━━━━━━━━\n"
                           f"👍 lk@{code}\n❤️ lv@{code}\n👎 dl@{code}\n"
                           f"💬 cm@{code} msg\n🚨 report@{code} msg\n🔁 x@{code} — Repost")
            else:
                prefix = ("🧪 معاينة القالب الملكي 3D" if command_mode == "aax2"
                          else ("☕ اكسبلور كوفي الملكي 3D" if command_mode in {"ax1", "ax2"} else "✨ اكسبلور"))
                caption = (f"{prefix}\n👤 الناشر: @{str(sender or '').lstrip('@')}\n"
                           f"📝 نص الاكسبلور: {text}\n🏠 الغرفة: {room}\n━━━━━━━━━━━━\n"
                           f"👍 lk@{code}\n❤️ lv@{code}\n👎 dl@{code}\n"
                           f"💬 cm@{code} نص\n🚨 report@{code} نص\n🔁 x@{code} — إعادة نشر")
            self.reaction_targets[code] = {"publisher": str(sender), "kind": "explore", "description": text,
                                            "media_url": media_url, "media_type": media_type,
                                            "caption": caption, "created_at": time.time()}
            # Only aax2 remains a local preview. ax1 is the new all-room command.
            if command_mode == "aax2":
                rooms = [str(room)] if room else ([str(getattr(self, "room", ""))] if getattr(self, "room", "") else [])
            else:
                rooms = self._active_rooms() or ([str(room)] if room else [])
            delivered = 0
            for target in rooms:
                if self.send_room_media(target, media_url, media_type) is not False:
                    self.send_room_text(target, caption)
                    delivered += 1
            confirmation = (f"✅ تمت معاينة القالب الملكي في الغرفة الحالية." if command_mode == "aax2"
                            else (f"✅ تم نشر قالب اكسبلور الملكي المتحرك في {delivered} غرفة." if command_mode in {"ax1", "ax2"}
                                  else f"✅ تم نشر اكسبلور في {delivered} غرفة."))
            self.send_private_text(sender, confirmation)
        except Exception as exc:
            self.log("[EXPLORE] publish failed:", repr(exc))
            notice = ("تعذر تجهيز القالب الملكي GIF. تحقق من رابط الوسائط العام وخدمة عرض GIF في الاستضافة."
                      if command_mode in {"aax2", "ax1", "ax2"}
                      else "تعذر تجهيز منشور اكسبلور؛ تحقق من إعداد رابط الوسائط العام.")
            self.send_private_text(sender, notice)
        return True

    def _handle_explore_command(self, room, command, sender):
        """Queue royal Explore broadcasts while preserving local preview behavior."""
        mode_match = re.match(r"^(aax2|ax2|ax1|aax|ax)@", str(command or "").strip(), re.I)
        mode = mode_match.group(1).casefold() if mode_match else ""
        if not mode:
            return False
        # Existing ax/aax2 previews remain synchronous for compatibility; only
        # all-room royal publication is moved off the receive loop.
        if mode in {"ax", "aax2"}:
            return self._publish_explore_command_sync(room, command, sender)
        lock = getattr(self, "_explore_publish_lock", None)
        if lock is None:
            lock = self._explore_publish_lock = threading.Lock()
        if not lock.acquire(blocking=False):
            self.send_room_text(room, "⏳ يوجد نشر اكسبلور جارٍ الآن؛ سيستمر البوت باستقبال بقية الأوامر.")
            return True
        def worker():
            try:
                self._publish_explore_command_sync(room, command, sender)
            finally:
                lock.release()
        self.send_room_text(room, "⏳ جاري تجهيز اكسبلور في الخلفية؛ يمكنك إرسال أوامر أخرى الآن.")
        threading.Thread(target=worker, name="explore-publish", daemon=True).start()
        return True

    def _classify_picture_name(self, name):
        """Guess whether a display/user name is commonly masculine or feminine.
        This is only a playful name-based guess; it does not identify the real
        person's gender. Unknown names fall back to a mixed search.
        """
        raw = str(name or "").strip().lstrip("@").strip()
        compact = re.sub(r"[\s_.-]+", "", raw.casefold())
        male = {
            "احمد", "محمد", "محمود", "علي", "عمر", "عثمان", "خالد", "وليد",
            "مازن", "ياسر", "يحيى", "عبدالله", "عبد الله", "عبدالرحمن", "عبد الرحمن",
            "ابراهيم", "إبراهيم", "اسماعيل", "إسماعيل", "سعيد", "سالم", "حسن", "حسين",
            "حامد", "رامي", "سامر", "سامي", "طارق", "فهد", "فيصل", "بدر", "بشار",
            "زياد", "زكريا", "أنس", "انس", "معاذ", "مصعب", "هيثم", "كريم", "نبيل",
            "ahmed", "mohammed", "mohamed", "mohamad", "ali", "omar", "khaled",
            "waleed", "walid", "mazen", "yasser", "yaser", "yahia", "abdullah",
            "ibrahim", "ismail", "said", "salem", "hassan", "hussein", "rami",
            "samer", "sami", "tariq", "fahad", "faisal", "badr", "ziad", "anas",
            "muaz", "moath", "musab", "karim", "nabil"
        }
        female = {
            "اميرة", "أميرة", "اميره", "أميره", "سارة", "ساره", "نور", "ريم", "رنا",
            "رؤى", "روى", "ليان", "ليلى", "ليلا", "مريم", "مها", "منى", "منال",
            "هدى", "هبة", "هبه", "دعاء", "دينا", "رانيا", "رغد", "شهد", "شيماء",
            "سلمى", "سما", "سمر", "حنان", "وفاء", "إيمان", "ايمان", "أروى", "اروى",
            "جنى", "جنان", "تالا", "لارا", "لينا", "ياسمين", "اسيل", "أسيل",
            "amerah", "amira", "sarah", "sara", "noor", "reem", "rana", "roya",
            "layan", "layla", "leila", "maryam", "mariam", "maha", "mona", "manal",
            "huda", "heba", "hiba", "doaa", "dina", "rania", "raghad", "shahd",
            "shimaa", "salma", "sama", "samar", "hanan", "wafa", "eman", "arwa",
            "jana", "janna", "tala", "lara", "lina", "yasmin", "yasmine", "aseel"
        }
        explicit_male = {"هيبه", "الهيبه", "الهيبة", "ملك", "الملك", "ابو", "ابو الشباب"}
        explicit_female = {"اميره بحجابي", "اميرة بحجابي", "اميره", "اميرة", "ملكة", "الملكة", "بنت", "بنوتة"}
        if raw in explicit_male or compact in {re.sub(r"[\s_.-]+", "", x.casefold()) for x in explicit_male}:
            return "male"
        if raw in explicit_female or compact in {re.sub(r"[\s_.-]+", "", x.casefold()) for x in explicit_female}:
            return "female"
        if compact in {re.sub(r"[\s_.-]+", "", x.casefold()) for x in male}:
            return "male"
        if compact in {re.sub(r"[\s_.-]+", "", x.casefold()) for x in female}:
            return "female"
        # Common Arabic feminine endings; useful for names not in the lists.
        if raw.endswith(("ة", "ه", "ى")) and len(raw) >= 3:
            return "female"
        return "unknown"

    def _picture_gender_hint(self, name):
        """Use explicit roster profile metadata when available, otherwise name hints.
        This is a playful search category, never a claim about a person's identity.
        """
        key = _norm_user(name)
        for roster in getattr(self, "room_users", {}).values():
            value = roster.get(name) if isinstance(roster, dict) else None
            if isinstance(value, dict):
                gender = str(value.get("gender") or value.get("sex") or value.get("profile_gender") or "").casefold()
                if gender in ("male", "m", "ذكر", "ولد"): return "male"
                if gender in ("female", "f", "أنثى", "انثى", "بنت"): return "female"
            if isinstance(roster, dict):
                for username, item in roster.items():
                    if _norm_user(username) == key and isinstance(item, dict):
                        gender = str(item.get("gender") or item.get("sex") or "").casefold()
                        if gender in ("male", "m", "ذكر", "ولد"): return "male"
                        if gender in ("female", "f", "أنثى", "انثى", "بنت"): return "female"
        return self._classify_picture_name(name)

    def _picture_search_queries_for_name(self, name, gender=None):
        """Pick 20% playful monkey results and otherwise an adult SFW gender-matched portrait."""
        gender = gender or self._picture_gender_hint(name)
        if secrets.randbelow(100) < 20:
            return ("قرود لطيفة", "قرود مضحكة", "monkeys safe for work", "قرود في الطبيعة")
        if gender == "male":
            return (
                "handsome adult men portrait safe for work",
                "شباب بالغون وسيمون بملابس محتشمة",
                "handsome Arab adult man portrait, fully clothed",
            )
        if gender == "female":
            return (
                "beautiful adult women portrait safe for work",
                "نساء بالغات بملابس محتشمة portrait",
                "beautiful Arab adult woman portrait, fully clothed",
            )
        return (
            "adult person portrait safe for work",
            "شخص بالغ portrait بملابس محتشمة",
            "fully clothed adult portrait, safe for work",
        )

    def _random_internet_picture_query(self, requested, gender=None):
        """Return 20% monkey images and otherwise adult male/female portraits.

        A one-token username (or explicit @username) uses its known room/name
        gender when available. Unknown usernames and the no-argument command
        alternate randomly between adult men and women. Multi-word subjects
        remain normal SafeSearch web searches.
        """
        text = str(requested or "").strip()
        normalized = _norm_user(text)
        category = ""
        if not text:
            if secrets.randbelow(100) < 20:
                category = "monkey"
            else:
                category = secrets.choice(("men", "women"))
        elif any(word in normalized for word in ("قرد", "قرود", "شمبانزي", "monkey", "chimp")):
            category = "monkey"
        elif any(word in normalized for word in ("شاب", "شباب", "رجل", "رجال", "وسيم", "وسيمين")):
            category = "men"
        elif any(word in normalized for word in ("بنت", "بنات", "فتاه", "فتاة", "نساء", "امراه", "مراه", "حلوه", "حلوة")):
            category = "women"
        else:
            # A single token is treated as a Talkin username (including a
            # name-only handle such as Ahmed); multi-word values remain topics.
            is_username = text.startswith("@") or not re.search(r"\s", text) or gender in ("male", "female")
            if is_username:
                if secrets.randbelow(100) < 20:
                    category = "monkey"
                else:
                    category = gender if gender in ("male", "female") else secrets.choice(("men", "women"))
            else:
                return f"{text} portrait safe for work", text, False

        if category == "monkey":
            return "monkeys wildlife", "قرود", True
        if category == "men" or category == "male":
            return "handsome adult men portraits fully clothed", "شباب وسيمين بالغين", False
        return "beautiful adult women portraits fully clothed", "نساء بالغات", False

    def _handle_internet_picture_command(self, room, body, sender):
        """Search the web for a random image; this is separate from account avatars."""
        text = str(body or "").strip()
        if text.casefold() == ".صوره":
            requested = ""
        else:
            match = re.fullmatch(r"\.صوره(?:@|\s+)(.+)", text, re.I)
            if not match:
                return False
            requested = match.group(1).strip()
        if _is_explicit_picture_query(requested):
            return True
        if not room:
            return True

        busy = getattr(self, "_internet_picture_busy", set())
        key = (_norm_user(sender), _norm_room(room))
        if key in busy:
            return True
        busy.add(key)
        self._internet_picture_busy = busy

        def worker():
            try:
                target_gender = self._picture_gender_hint(requested) if requested else "unknown"
                search_query, label, is_monkey = self._random_internet_picture_query(
                    requested, target_gender
                )
                recent = getattr(self, "_internet_picture_recent", {})
                query_key = _norm_user(search_query)
                excluded = set(recent.get(query_key, []))
                image_url = (
                    _search_monkey_image(exclude_urls=excluded)
                    if is_monkey
                    else _search_lookalike_image(search_query, exclude_urls=excluded)
                )
                if not image_url and excluded:
                    image_url = _search_monkey_image() if is_monkey else _search_lookalike_image(search_query)
                if not image_url:
                    self.log("[RANDOM-PICTURE] no matching image for query:", search_query[:120])
                    self.send_room_text(room, "❌ لم أجد صورة مناسبة من نتائج البحث الآن؛ جرّب بعد قليل.")
                    return

                local = _download_lookalike_image(image_url, f"internet_{uuid.uuid4().hex}")
                if not local:
                    self.log("[RANDOM-PICTURE] image download/processing returned no file")
                    self.send_room_text(room, "❌ ظهرت صورة في البحث لكن تعذّر تنزيلها أو معالجتها؛ تأكد من تثبيت Pillow.")
                    return
                base = _public_base_url()
                if not base:
                    self.log("[RANDOM-PICTURE] public media URL is not configured")
                    self.send_room_text(room, "❌ اضبط PUBLIC_BASE_URL أو PUBLIC_MEDIA_URL في متغيرات الاستضافة لعرض الصور داخل Talkin.")
                    return
                public_url = f"{base.rstrip('/')}/lookalikes/{local.name}"
                try:
                    self._verify_public_media_url(public_url, "image")
                except Exception as exc:
                    self.log("[RANDOM-PICTURE] public image check failed:", type(exc).__name__)
                    self.send_room_text(room, "❌ رابط الصور العام لا يمكن الوصول إليه من الإنترنت؛ راجع PUBLIC_BASE_URL وPORT.")
                    return

                sent = self.send_room_media(room, public_url, "image") is not False
                if not sent:
                    self.send_room_text(room, "❌ تعذر إرسال الصورة إلى Talkin؛ راجع سجل WebSocket.")
                    return
                history = list(recent.get(query_key, []))
                history.append(image_url)
                recent[query_key] = history[-8:]
                self._internet_picture_recent = recent
                self.send_room_text(room, f"📸 صورتك هي: {label}")
            except Exception as exc:
                self.log("[RANDOM-PICTURE] search failed:", type(exc).__name__, str(exc)[:200])
                try:
                    self.send_room_text(room, "❌ تعذر البحث عن الصورة أو إرسالها الآن؛ راجع سجل الاستضافة لمعرفة السبب.")
                except Exception:
                    pass
            finally:
                busy.discard(key)

        try:
            threading.Thread(target=worker, name="internet-picture-search", daemon=True).start()
        except Exception as exc:
            busy.discard(key)
            self.log("[RANDOM-PICTURE] worker start failed:", type(exc).__name__)
        return True

    def _handle_random_picture_command(self, room, body, sender, sender_candidates=None):
        """Send the actual room-command sender's Talkin profile image to the room."""
        if not room:
            return True
        text = str(body or "").strip()
        if text.casefold() not in {"صورتي", "صورتك", "my picture", "my photo", "picture me"}:
            return False

        candidates = []
        seen_candidates = set()
        bot_names = {
            _norm_user(BOT_ID), _norm_user(PRIMARY_BOT_ID),
            _norm_user(getattr(self, "bot_id", "")),
        }
        bot_names.discard("")
        for candidate in [sender, *(sender_candidates or [])]:
            name = str(candidate or "").strip().lstrip("@")
            key_name = _norm_user(name)
            if (name and key_name and key_name not in bot_names
                    and key_name not in seen_candidates):
                seen_candidates.add(key_name)
                candidates.append(name)
        if not candidates:
            self.send_room_text(room, "❌ لم أتمكن من تحديد اسم حسابك لجلب الصورة.")
            return True

        primary = candidates[0]
        busy = getattr(self, "_profile_picture_busy", set())
        key = (_norm_room(room), _norm_user(primary))
        if key in busy:
            return True
        busy.add(key)
        self._profile_picture_busy = busy
        # Keep the last avatar while requesting a fresh native profile.  A
        # secondary room can answer the roster request late or omit UserItem
        # photos entirely; deleting the known URL first caused «صورتي» to
        # fail there even though the same account worked in the base room.
        # The request below still refreshes the cache whenever the gateway
        # returns a newer URL.
        photos = getattr(self, "user_photos", {})
        previous_photos = {}
        if isinstance(photos, dict):
            for candidate in candidates:
                previous = photos.get(_norm_user(candidate)) or photos.get(str(candidate).casefold())
                if str(previous or "").startswith(("http://", "https://", "//")):
                    previous_photos[_norm_user(candidate)] = str(previous)

        def cached_photo(candidate):
            photos = getattr(self, "user_photos", {})
            if not isinstance(photos, dict):
                return ""
            raw_candidate = str(candidate or "").strip().lstrip("@")
            keys = [raw_candidate.casefold(), _norm_user(raw_candidate)]
            # Depending on the room gateway, RoomEvent.from can be the numeric
            # Talkin account ID instead of the visible username.
            if raw_candidate.isdigit():
                keys.insert(0, "id:" + raw_candidate)
            profile_ids = getattr(self, "user_profile_ids", {})
            if isinstance(profile_ids, dict):
                uid = str(profile_ids.get(_norm_user(raw_candidate)) or
                          profile_ids.get(raw_candidate.casefold()) or "").strip()
                if uid.isdigit():
                    keys.insert(0, "id:" + uid)
            for cache_key in dict.fromkeys(keys):
                value = str(photos.get(cache_key) or "").strip()
                if value.startswith(("http://", "https://", "//")):
                    return "https:" + value if value.startswith("//") else value
            return ""

        def find_cached():
            for candidate in candidates:
                value = cached_photo(candidate)
                if value:
                    return candidate, value
            return "", ""

        def wait_for_photo(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                stop_event = getattr(self, "stop_event", None)
                if stop_event is not None and stop_event.is_set():
                    break
                found_name, found_url = find_cached()
                if found_url:
                    return found_name, found_url
                time.sleep(0.25)
            return find_cached()

        def profile_photo_worker():
            lookup_lock = getattr(self, "_profile_picture_lookup_lock", None)
            if lookup_lock is None:
                lookup_lock = self._profile_picture_lookup_lock = threading.Lock()
            try:
                # Do not fire multiple get_profile requests at once: some Talkin
                # gateways return a profile photo without its username. A single
                # in-flight lookup lets the response be safely tied to the right
                # account. This lock is in a worker, never in the room event loop.
                with lookup_lock:
                    found_name, photo_url = find_cached()

                    # First use the profile table / any persisted room roster ID.
                    if not photo_url:
                        for candidate in candidates:
                            try:
                                photo_url = self._lookup_profile_photo(candidate, preferred_room=room)
                            except Exception as lookup_exc:
                                self.log("[PROFILE-PICTURE] DB lookup failed:", _norm_user(candidate), type(lookup_exc).__name__)
                            if photo_url:
                                found_name = candidate
                                break

                    # Query Talkin directly, one canonical candidate at a time.
                    # This works even when the account is not in the primary room.
                    if not photo_url:
                        for candidate in candidates:
                            self._request_native_profile_lookup(candidate)
                            found_name, photo_url = wait_for_photo(2.0)
                            if photo_url:
                                break
                            self._request_native_profile_lookup(candidate, alternate=True)
                            found_name, photo_url = wait_for_photo(2.0)
                            if photo_url:
                                break

                    # Only as a final fallback, refresh the roster for the exact
                    # room where the user typed «صورتي» (never broadcast elsewhere).
                    if not photo_url:
                        self._request_native_profile_roster(room, legacy=False)
                        found_name, photo_url = wait_for_photo(3.0)
                    if not photo_url:
                        self._request_native_profile_roster(room, legacy=True)
                        found_name, photo_url = wait_for_photo(2.0)

                if not photo_url:
                    # A few room gateways do not return UserItems for a
                    # refresh request. Do not turn that transport miss into a
                    # user-facing failure when a known avatar is available.
                    for candidate in candidates:
                        fallback = previous_photos.get(_norm_user(candidate), "")
                        if fallback:
                            found_name, photo_url = candidate, fallback
                            self.log("[PROFILE-PICTURE] refresh missed; using last known avatar:", _norm_user(candidate))
                            break
                if not photo_url:
                    cached_count = len(getattr(self, "user_photos", {}) or {})
                    self.log(
                        "[PROFILE-PICTURE] avatar not found:",
                        "candidates=", [_norm_user(x) for x in candidates],
                        "cached_photos=", cached_count,
                    )
                    self.send_room_text(room, "❌ لم أتمكن من جلب صورة حسابك من Talkin. تأكد أن لحسابك صورة ظاهرة ثم أعد أمر «صورتي».")
                    return

                # «صورتي» يجب أن يحمل صورة الحساب الأصلية مباشرة إلى الغرفة.
                # لا ننشئ نسخة JPEG ولا نطلب PUBLIC_BASE_URL هنا؛ رابط CDN الذي
                # أعاده Talkin هو المرفق الذي يرسله العميل مباشرة.
                media_url = photo_url
                sent = False
                send_errors = []
                try:
                    result = self.send_room_media(room, media_url, "image")
                    if result is not False:
                        sent = True
                    else:
                        send_errors.append("send_query returned False")
                except Exception as media_exc:
                    send_errors.append(f"{type(media_exc).__name__}: {str(media_exc)[:100]}")
                if not sent:
                    self.log("[PROFILE-PICTURE] avatar send failed:", " | ".join(send_errors))
                    self.send_room_text(room, "❌ وجدت صورة حسابك لكن Talkin لم يقبل إرسالها إلى الغرفة.")
                    return

                self.log("[PROFILE-PICTURE] avatar sent directly:", _norm_user(found_name or primary), "via=profile-url")
                self.send_room_text(room, "صورتك هي الصورة")
            except Exception as exc:
                self.log("[PROFILE-PICTURE] worker failed:", type(exc).__name__, str(exc)[:240])
                try:
                    self.send_room_text(room, "❌ تعذر جلب صورة الحساب الآن؛ راجع سجل الاستضافة [PROFILE-PICTURE].")
                except Exception:
                    pass
            finally:
                busy.discard(key)

        try:
            threading.Thread(target=profile_photo_worker, name="profile-photo-lookup", daemon=True).start()
        except Exception as exc:
            busy.discard(key)
            self.log("[PROFILE-PICTURE] worker start failed:", type(exc).__name__)
        return True

    def _handle_kemin_command(self, room, body, sender):
        """Two-player trap challenge. The player who avoids the trap wins 1B."""
        if str(body or "").strip().casefold() not in {"كمين", "الكمين"}:
            return False
        if not room or not str(sender or "").strip():
            return True
        amount = 1_000_000_000
        lock = _KEMIN_STATE_LOCK
        pending = getattr(self, "_kemin_pending", {})
        now = time.time()
        with lock:
            # Expire an abandoned challenge after two minutes.
            old = pending.get(str(room))
            if old and now - float(old.get("created", 0)) > 120:
                pending.pop(str(room), None)
                old = None
            if old is None:
                balance = _points_balance(_resolve_points_account(_points_data(), sender)[1]) if _resolve_points_account(_points_data(), sender) else 0
                if balance < amount:
                    self.send_room_text(room, f"❌ @{sender} تحتاج إلى مليار نقطة على الأقل لدخول لعبة الكمين.")
                    return True
                pending[str(room)] = {"owner": str(sender), "created": now, "id": uuid.uuid4().hex}
                self._kemin_pending = pending
                self.send_room_text(
                    room,
                    f"🪤 @{sender} نصب كميناً!\\n"
                    f"🎯 من يجرؤ على الدخول؟ أرسل «كمين» لتواجه الفخ.\\n"
                    f"💰 الرهان: 1,000,000,000 نقطة | تنتهي الدعوة خلال دقيقتين."
                )
                return True
            owner = str(old.get("owner") or "")
            if _norm_user(owner) == _norm_user(sender):
                self.send_room_text(room, "⏳ أنت صاحب الكمين. انتظر لاعباً آخر ليدخل الفخ.")
                return True
            pending.pop(str(room), None)
            self._kemin_pending = pending

        # Both accounts must be able to cover the full transfer before the draw.
        accounts = _points_data()
        owner_account = _resolve_points_account(accounts, owner)
        challenger_account = _resolve_points_account(accounts, sender)
        owner_balance = _points_balance(owner_account[1]) if owner_account else 0
        challenger_balance = _points_balance(challenger_account[1]) if challenger_account else 0
        if owner_balance < amount or challenger_balance < amount:
            self.send_room_text(room, "❌ أُلغي الكمين: يجب أن يمتلك اللاعبان مليار نقطة على الأقل.")
            return True

        # Avoiding the trap wins; falling into it loses.
        challenger_escaped = bool(secrets.randbelow(2))
        winner, loser = (sender, owner) if challenger_escaped else (owner, sender)
        transfer = _transfer_points(loser, winner, amount, reason="kemin_game")
        if not transfer.get("ok"):
            self.send_room_text(room, "❌ تعذر تنفيذ تحويل نقاط الكمين؛ لم تُعلن نتيجة الجولة.")
            return True
        _record_game(owner, "kemin", -amount if loser == owner else amount, amount)
        _record_game(sender, "kemin", -amount if loser == sender else amount, amount)
        loser_name = loser
        winner_name = winner
        title = "🪤 وقع في الكمين!" if loser == sender else "🛡️ تفادى الكمين!"
        result_text = (
            f"{title}\\n"
            f"🏆 الفائز: @{winner_name}\\n"
            f"💥 الخاسر: @{loser_name}\\n"
            f"💰 تم تحويل 1,000,000,000 نقطة إلى الفائز."
        )
        try:
            card = render_kemin_card(owner, sender, loser, self._lookup_profile_photo(owner),
                                     self._lookup_profile_photo(sender))
            card_url = self._game_public_image(card)
            if card_url:
                self.send_room_media(room, card_url, "image")
        except Exception as exc:
            self.log("[KEMIN] card render failed:", repr(exc))
        self.send_room_text(room, result_text)
        return True

    def _handle_lookalike_command(self, room, body, sender):
        """Handle شبيه@username / شبيه username in a background worker."""
        if not room:
            return True
        text = _normalize_game_command_text(body)
        m = re.fullmatch(r"(?:شبيه|شبيهك)@(.+)", text, re.I)
        if not m:
            m = re.fullmatch(r"(?:شبيه|شبيهك)\s+(.+)", text, re.I)
        if not m:
            return False
        target = m.group(1).strip().lstrip("@").strip()
        if not target:
            self.send_room_text(room, "❌ الصيغة: شبيه@اسم_المستخدم")
            return True
        # Avoid several expensive web searches by the same user at once.
        # Also remember a small rolling set of the last images used for this
        # target, so the same person does not keep receiving the same image.
        busy = getattr(self, "_lookalike_busy", set())
        key = (_norm_user(sender), str(room))
        if key in busy:
            self.send_room_text(room, f"⏳ @{sender} جاري البحث عن شبيه @{target}...")
            return True
        busy.add(key)
        self._lookalike_busy = busy
        self.send_room_text(room, f"🔎 جاري البحث عن شبيه @{target}...")
        response_language = self._capture_room_response_language(room, text)

        def worker():
            try:
                # This command is intentionally a playful random-image game.
                # Do not try to infer the person's real appearance or identity.
                # Instead, search public images using lighthearted animal/funny
                # themes, then send one result to the room.
                recent = getattr(self, "_lookalike_recent", {})
                target_key = _norm_user(target)
                excluded = set(recent.get(target_key, []))
                # Randomize the theme so repeated commands can return different
                # images. Keep the queries non-explicit and suitable for chat.
                variants = self._picture_search_queries_for_name(target, self._picture_gender_hint(target))
                # All lookalike themes are non-explicit and the search layer
                # applies strict SafeSearch before any URL is downloaded.
                variants = tuple(q for q in variants if not _is_explicit_picture_query(q)) or ("adult portrait safe for work",)
                query = secrets.choice(variants)
                image_url = (_search_monkey_image(exclude_urls=excluded)
                             if any(token in query.casefold() for token in ("قرود", "قرد", "monkey"))
                             else _search_lookalike_image(query, exclude_urls=excluded))
                if image_url and image_url in excluded:
                    # If all current results were previously used, clear the
                    # rolling history and allow a genuinely new search result.
                    excluded.clear()
                    image_url = (_search_monkey_image()
                                 if any(token in query.casefold() for token in ("قرود", "قرد", "monkey"))
                                 else _search_lookalike_image(query))
                if not image_url:
                    self.send_room_text(room, f"❌ لم أجد صورة مناسبة لـ @{target}.")
                    return
                # Record the source URL only after we have a successfully
                # downloaded image. Keep a short history per target.
                history = list(recent.get(target_key, []))
                history.append(image_url)
                recent[target_key] = history[-8:]
                self._lookalike_recent = recent
                local = _download_lookalike_image(image_url, target)
                if not local:
                    self.send_room_text(room, f"❌ وجدت نتيجة لكن تعذر تحميل الصورة لـ @{target}.")
                    return
                base = _public_base_url()
                if not base:
                    self.send_room_text(room, "❌ رابط الصور العام غير مضبوط في إعدادات البوت.")
                    return
                public_url = f"{base}/lookalikes/{local.name}"
                # The phrase is intentionally a playful result, not an identity claim.
                self.send_room_text(room, f"👤 شبيه @{target} هو")
                self.send_room_media(room, public_url, "image")
            except Exception as exc:
                self.log("[LOOKALIKE] failed:", repr(exc))
                try:
                    self.send_room_text(room, "❌ تعذر البحث عن صورة الشبيه حالياً.")
                except Exception:
                    pass
            finally:
                try:
                    busy.discard(key)
                except Exception:
                    pass

        threading.Thread(target=self._localized_room_worker(room, response_language, worker),
                         name="lookalike-search", daemon=True).start()
        return True
