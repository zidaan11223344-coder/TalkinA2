"""Ordinary private invitations and room-roster invitation workflow."""

class InvitationSection:
    def request_room_occupants(self, room: str):
        """Load and persist the complete room roster without a huge WebSocket frame.

        Supabase is used first because it can return the room members in small
        HTTP batches. Only when the database cannot provide the roster do we fall
        back to Talkin's occupants_list WebSocket response.
        """
        room = _norm_room(room)
        if not room:
            return False
        try:
            db_users = self.db.room_users(room) if getattr(self, "db", None) else []
        except Exception as exc:
            db_users = []
            self.log("[ROOM] DB roster refresh failed", room, repr(exc))
        if db_users:
            users_info = []
            for user in db_users:
                if not isinstance(user, dict):
                    continue
                username = str(user.get("username") or "").strip()
                if username and _norm_user(username) != _norm_user(BOT_ID):
                    users_info.append({
                        "username": username,
                        "role": str(user.get("role") or "none").strip().lower() or "none",
                        "user_id": str(user.get("user_id") or ""),
                    })
            if users_info:
                if not isinstance(getattr(self, "user_profile_ids", None), dict):
                    self.user_profile_ids = {}
                if not isinstance(getattr(self, "user_photos", None), dict):
                    self.user_photos = {}
                for user in db_users:
                    if not isinstance(user, dict):
                        continue
                    username = str(user.get("username") or "").strip().lstrip("@")
                    user_id = str(user.get("user_id") or "").strip()
                    if username and user_id:
                        self.user_profile_ids[username.casefold()] = user_id
                        self.user_profile_ids[_norm_user(username)] = user_id
                    photo_value = next((user.get(key) for key in (
                        "photo_url", "photo", "avatar_url", "avatar", "image_url",
                        "picture", "profile_photo_url", "profile_picture_url",
                    ) if user.get(key) is not None), None)
                    photo = _extract_media_url_universal({"photo": photo_value}) if photo_value is not None else ""
                    if username and photo:
                        self.user_photos[username.casefold()] = photo
                        self.user_photos[_norm_user(username)] = photo
                self.room_users[room] = {u["username"]: u.get("role", "none") for u in users_info}
                _remember_roster(room, users_info)
                self.last_joined_room = room
                self.log("[ROOM] complete roster saved", room, "users=", len(users_info))
                return True
        if not self.ws:
            return False
        try:
            self.send_query(encode_query(
                "room_admin", type_="occupants_list", room=room,
                to=BOT_ID, value="none"
            ))
            self.last_joined_room = room
            self.log("[ROOM] occupants refresh requested (WebSocket fallback)", room)
            return True
        except Exception as exc:
            self.log("[ROOM] occupants refresh failed", room, repr(exc))
            return False

    def _inv_bot_owner_allowed(self, room):
        """Prepare `inv` for authoritative owner verification from the server.

        Do not reject from the local cache: after rejoin/cache refresh the role
        can be empty or stale even when the bot is really the room owner.
        `inv` performs the same room-role discovery used by the live/music
        path, then the owners_list response is the final permission check.
        """
        role = self._bot_room_role(room)
        if role in {
            "owner", "creator", "room_owner", "room_creator", "host",
            "مالك", "اونر", "أونر", "صانع", "صانع_الغرفة",
        }:
            return True
        # Unknown/stale/member/admin cache is deliberately not treated as a
        # definitive failure. The native owners_list request below decides.
        return True

    def _start_inv_roster_collection(self, room, response_room="", response_to=""):
        """Merge the complete room-membership roster with native role lists.
        Database membership preserves offline users; owners_list independently
        verifies the bot's current owner permission before any invite is sent.
        """
        room = str(room or "").strip()
        if not room:
            return False
        key = _norm_room(room)
        # Seed the invitation set from the room's complete membership table.
        # In several Talkin builds owners_list/admins_list/members_list are
        # presence-filtered and omit offline accounts. The native owners_list
        # request below is STILL required to authorize the bot as room owner;
        # DB/persisted membership is used only to avoid losing offline targets.
        seeded = {}
        try:
            for item in self._settings_room_users(room):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("username") or "").strip().lstrip("@")
                key_user = _norm_user(name)
                if not name or not key_user or key_user == _norm_user(BOT_ID):
                    continue
                seeded[key_user] = {
                    "username": name,
                    "role": str(item.get("role") or "member").strip().lower() or "member",
                    "user_id": str(item.get("user_id") or ""),
                }
        except Exception as exc:
            self.log("[INV] complete room roster seed failed:", room, repr(exc))
        state = {
            "room": room,
            "response_room": str(response_room or "").strip(),
            "response_to": str(response_to or "").strip(),
            "queue": ["o", "a", "m"],
            "current": "",
            "users": seeded,
            "bot_is_owner": False,
            "started_at": time.time(),
        }
        self.log("[INV] seeded complete room roster (offline-capable):", room, len(seeded))
        self._inv_roster_pending = state
        return self._request_next_inv_roster_category()

    def request_occupants(self, room: str = "", silent_master: bool = False, response_room: str = "", response_to: str = ""):

        """Load invitation candidates ONLY from the room specified by inv.

        The source roster is restricted to that single room.  This prevents
        inv issued in one room from inviting users saved from other rooms.
        Owners, admins and normal members of the selected room are all eligible.
        """
        if not getattr(self, "invites_enabled", True):
            target = str(response_to or BOT_MASTER or "").strip()
            if target:
                self.send_private_text(target, "🛑 الدعوات متوقفة حالياً. أرسل: تشغيل الدعوات")
            elif response_room:
                self.send_room_text(response_room, "🛑 الدعوات متوقفة حالياً.")
            return
        with self.invite_lock:
            if self.invite_pending:
                msg = "⏳ ما زلت أجمع معلومات الغرف، انتظر حتى تكتمل العملية."
                if response_room:
                    self.send_room_text(response_room, msg)
                elif response_to:
                    self.send_private_text(response_to, msg)
                else:
                    self.send_private_text(BOT_MASTER, msg)
                return
            self.invite_pending = True
            self.invite_silent_master = bool(silent_master)
            self.invite_room = str(room or self.room or "").strip()
            self.invite_sent.clear()

        command_room = self.invite_room
        response_room = str(response_room or "").strip()
        response_to = str(response_to or "").strip()

        # `inv` is strictly owner-only, but the cached role is NOT used
        # to reject the bot because it can be stale/wrong. The native
        # owners_list response below is the authoritative check.
        # inv progress is always announced in the same room where the command
        # was issued.  Mark the management command as replied so the generic
        # fallback message is not emitted.
        if response_room:
            self.send_room_text(response_room, "⏳ جاري جمع القوائم...\n📋 الأونرات والمشرفين والأعضاء، وبعد اكتمالها ستبدأ الدعوات.")
        if getattr(self, "_master_reply_local", None) is not None:
            try:
                self._master_reply_local.replied = True
                self._master_reply_local.private_replied = True
            except Exception:
                pass

        self._inv_cached_role = self._bot_room_role(command_room)
        self._inv_response_room = response_room or command_room
        self._inv_response_to = response_to if not response_room else ""
        self._inv_roster_pending = None
        self._start_inv_roster_collection(command_room, self._inv_response_room, self._inv_response_to)
        return
        # Keep the destination available to the async invite worker even when
        # users are loaded immediately from the persistent/DB roster.
        self._inv_response_room = response_room
        self._inv_response_to = response_to
        # IMPORTANT: invitations are scoped to the room where inv was requested.
        # Never merge the persistent rosters of the bot's other rooms.
        active_rooms = [command_room] if command_room else []
        self.log("[INV] loading users ONLY from requested room:", active_rooms)
        try:
            progress = (
                f"⏳ جاري جمع أعضاء الغرفة المطلوبة فقط...\n"
                f"📌 نص الدعوة سيكون باسم الغرفة التي نُفّذ فيها inv: {command_room}"
            )
            if response_room:
                self.send_room_text(response_room, progress)
            elif response_to:
                self.send_private_text(response_to, progress)
            else:
                self.send_private_text(BOT_MASTER, progress)
        except Exception as e:
            self.log("[INV] progress message failed:", repr(e))

        # INV uses the COMPLETE room-settings roster, not only currently online users.
        # This is intentional: the room settings/member list contains offline
        # members as well, and the native invitation can be sent to those users.
        settings_users = []
        try:
            # Primary source: room settings roster (owners + admins + members),
            # including offline users. _settings_room_users() falls back to the
            # saved members file when room settings are unavailable.
            settings_rows = self._settings_room_users(command_room)
            seen_settings = set()
            for item in (settings_rows or []):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("username") or "").strip().lstrip("@")
                key = _norm_user(name)
                if name and key and key != _norm_user(BOT_ID) and key not in seen_settings:
                    seen_settings.add(key)
                    settings_users.append(name)
        except Exception as exc:
            self.log("[INV] room settings/member-file roster failed:", command_room, repr(exc))

        if settings_users:
            self.log("[INV] using COMPLETE ROOM SETTINGS roster:", command_room, len(settings_users))
            threading.Thread(
                target=self._finish_invites,
                args=(command_room, settings_users),
                name="talkin-room-settings-invites",
                daemon=True,
            ).start()
            return

        # Database roster unavailable: ask the server for the room-settings
        # occupants list. Do NOT filter by online/presence; offline members
        # returned by room settings are valid invitation targets.
        self._inv_expected_rooms = set(active_rooms)
        self._inv_live_users = []
        self._inv_live_seen = set()
        self._inv_command_room = command_room
        self._inv_response_room = response_room
        self._inv_response_to = response_to

        if not active_rooms:
            with self.invite_lock:
                self.invite_pending = False
                self.invite_silent_master = False
            msg = "⚠️ لا توجد غرف نشطة حالياً. استخدم: دخول@اسم_الغرفة"
            if response_room:
                self.send_room_text(response_room, msg)
            elif response_to:
                self.send_private_text(response_to, msg)
            else:
                self.send_private_text(BOT_MASTER, msg)
            return

        for source_room in active_rooms:
            try:
                self.log("[INV] requesting CURRENT room settings roster:", source_room)
                self.send_query(encode_query(
                    "room_admin", type_="occupants_list", room=source_room,
                    to=BOT_ID, value="none"
                ))
            except Exception as e:
                self.log("[INV] occupants request failed", source_room, repr(e))
                self._inv_expected_rooms.discard(source_room)

        if not self._inv_expected_rooms:
            with self.invite_lock:
                self.invite_pending = False
                self.invite_silent_master = False

    def send_private_invite(self, username: str, room: str, inviter: str = "", allow_repeat: bool = False):
        if not getattr(self, "invites_enabled", True):
            return False
        """Send a NORMAL private chat invitation, not a system/RPC invitation.

        The room name is always the exact room in which the `inv` command was
        executed (or the room explicitly supplied to a master private `inv`
        command). This keeps the invitation text tied to the command room.
        """
        username = str(username or "").strip()
        room = str(room or "").strip()
        if not username or username == BOT_ID or not room:
            return False
        with self.invite_lock:
            if not allow_repeat and username in self.invite_sent:
                return False

        # IMPORTANT: normal TalkinChat private message, deliberately NOT
        # room_invite_username / native system invitation.
        text = self.invite_message_template or "يوجد معجب مخفي في {room}"
        if "{room}" not in text:
            text = text.rstrip() + " {room}"
        try:
            text = text.format(sender=(inviter or INVITE_SENDER_NAME), room=room, username=username)
        except Exception:
            text = f"يوجد معجب مخفي في {room}"
        result = self.send_query(encode_query("chat_message", type_="text", to=username, body=text))
        if result is False:
            self.log("[INV] private invitation packet was not sent:", username, "room=", room)
            return False

        with self.invite_lock:
            self.invite_sent.add(username)
        self.log("[INV] normal private invite sent:", username, "room=", room)
        # Do NOT announce each invitation inside the room. Invitations are a
        # background action: only the invited user receives the private invite,
        # while progress/result is reported privately to the command sender.
        return True

    def _decode_user_item_candidate(self, item, depth=0):
        """Decode one APK UserItem without mistaking wrapper fields for users."""
        if depth > 6 or item is None:
            return []
        if isinstance(item, bytes):
            try:
                return self._decode_user_item_candidate(decode_message(item), depth + 1)
            except Exception:
                return []
        if isinstance(item, (list, tuple)):
            out=[]
            for x in item:
                out.extend(self._decode_user_item_candidate(x, depth + 1))
            return out
        if not isinstance(item, dict):
            return []

        # APK UserItem schema: 1 username, 2 user_id, 3 photo, 4 status,
        # 5 online, 6 role. Some gateways convert protobuf numeric keys to
        # strings, while others return ordinary username/photo_url objects.
        def field(number, *aliases):
            for key in (number, str(number), *aliases):
                if key in item and item.get(key) is not None:
                    value = item.get(key)
                    if isinstance(value, list):
                        value = value[0] if value else ""
                    return as_text(value).strip()
            return ""

        username = field(1, "username", "user_name", "nick", "name").lstrip("@")
        photo_keys = (
            3, "3", 7, "7", 8, "8", 9, "9", 10, "10", 11, "11", 12, "12",
            "photo", "photo_url", "profile_photo", "profile_photo_url",
            "avatar", "avatar_url", "profile_picture", "profile_picture_url",
            "image", "image_url", "picture", "picture_url", "media",
        )
        photo = ""
        for photo_key in photo_keys:
            if photo_key not in item or item.get(photo_key) is None:
                continue
            photo_value = item.get(photo_key)
            photo = _extract_media_url_universal({"photo": photo_value})
            if not photo and isinstance(photo_value, str):
                # decode_generic() represents nested non-UTF8 protobuf as hex.
                hex_value = photo_value.strip()
                if len(hex_value) >= 16 and len(hex_value) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", hex_value):
                    try:
                        photo = _extract_media_url_universal({"photo": bytes.fromhex(hex_value)})
                    except Exception:
                        photo = ""
            if photo:
                break
        if not photo:
            # Some app versions moved the avatar URL to a later UserItem field.
            photo = _extract_media_url_universal(item)
        if photo.startswith("//"):
            photo = "https:" + photo
        status = field(4, "status", "signature")
        user_id = field(2, "user_id", "uid", "id")
        role = field(6, "role", "user_role", "rank", "affiliation", "muc_affiliation", "affiliation_role")
        if not role:
            role = field(7, "rank", "affiliation", "muc_affiliation", "affiliation_role")
        online = field(5, "online", "is_online", "is_present")
        has_metadata = any(k in item or str(k) in item for k in (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12))
        explicit_profile = bool(photo or role or status or user_id or online)
        if username and (has_metadata or explicit_profile or set(item.keys()).issubset({1, "1"})):
            return [{
                "username": username, "role": role.lower() or "none",
                "user_id": user_id, "photo": photo, "status": status,
                "online": online,
            }]

        # The server may wrap users one level deeper. Walk nested protobuf
        # fields, but only accept the strict UserItem shape above.
        out=[]
        for v in item.values():
            out.extend(self._decode_user_item_candidate(v, depth + 1))
        return out

    def _extract_room_list_users(self, result):
        """Extract actual users[]/RoomAdmin UserItem records from the APK response."""
        candidates=[]
        # ResultMessage.users[] is the primary source for members_list/
        # admins_list/owners_list on the APK protocol.
        rv=result.get("users") or []
        if isinstance(rv, dict):
            candidates.append(rv)
        elif isinstance(rv, (list, tuple)):
            candidates.extend(rv)
        else:
            candidates.append(rv)
        # Some server builds wrap users under RoomAdmin. Keep this as a
        # fallback only; it does not change the request protocol.
        ra=result.get("room_admin") or {}
        if isinstance(ra, dict):
            for field_no in (11, 10):
                raw=ra.get(field_no) or []
                candidates.extend(raw if isinstance(raw, list) else [raw])
            # Also inspect every bytes/list field because older server builds
            # used a different RoomAdmin field number for the repeated users.
            for k,v in ra.items():
                if k in (10, 11): continue
                if isinstance(v, (bytes, list, tuple, dict)):
                    candidates.append(v)
        out=[]; seen=set()
        for item in candidates:
            for u in self._decode_user_item_candidate(item):
                key=_norm_user(u.get("username"))
                if key and key not in seen:
                    seen.add(key); out.append(u)
        return out

    def _users_from_room_admin(self, room_admin):
        # Kept for inv/presence compatibility; use the same strict APK parser.
        if not isinstance(room_admin, dict):
            return []
        return self._extract_room_list_users({"room_admin": room_admin})

    def _usernames_from_room_admin(self, room_admin):
        return [u["username"] for u in self._users_from_room_admin(room_admin)]

    def _finish_invites(self, room, usernames):
        """Send invitations in a worker so the main receive loop stays alive."""
        silent_master = bool(self.invite_silent_master)
        response_room = str(getattr(self, "_inv_response_room", "") or "").strip()
        response_to = str(getattr(self, "_inv_response_to", "") or "").strip()
        count = 0
        try:
            for username in usernames:
                try:
                    if not getattr(self, "invites_enabled", True):
                        self.log("[INV] stopped: invitations disabled by master")
                        break
                    if self.send_private_invite(username, room):
                        count += 1
                    # Small pacing gap, but never blocks the WebSocket reader.
                    # Avoid a burst of private-message packets for large rooms;
                    # aggressive invitation loops can trigger gateway disconnects.
                    time.sleep(max(0.1, float(os.getenv("INVITE_SEND_INTERVAL_SECONDS", "0.4"))))
                except Exception as e:
                    self.log("[INV] failed for", username, repr(e))

            self.log(f"[INV] occupants loaded: {len(usernames)}, invitations sent: {count}")
            if not silent_master:
                try:
                    if count == 0:
                        msg = (
                            f"⚠️ لم تُرسل أي دعوة. DB client={'نعم' if self.db.client else 'لا'} | "
                            f"Supabase room_id={self.db.last_room_id or 'غير موجود'} | "
                            f"room_members={self.db.last_member_count} | profiles={self.db.last_profile_count} | "
                            f"آخر خطأ={self.db.last_error or 'راجع سجل البوت'}"
                        )
                        if response_room:
                            self.send_room_text(response_room, msg)
                        elif response_to:
                            self.send_private_text(response_to, msg)
                        else:
                            self.send_private_text(BOT_MASTER, msg)
                except Exception:
                    pass
                try:
                    msg = (
                        f"✅ تم جمع معلومات الغرفة. عدد المستخدمين: {len(usernames)}\n"
                        f"📨 تم إرسال الدعوة العادية على الخاص إلى: {count} مستخدم."
                    )
                    if response_room:
                        self.send_room_text(response_room, msg)
                    elif response_to:
                        self.send_private_text(response_to, msg)
                    else:
                        self.send_private_text(BOT_MASTER, msg)
                except Exception as e:
                    self.log("[INV] final result failed:", repr(e))
        finally:
            with self.invite_lock:
                self.invite_pending = False
                self.invite_silent_master = False

    def process_occupants_for_invite(self, result):
        room = (result.get("_occupants_room") or self.invite_room or
                self.last_joined_room or self.room)
        room = str(room or "").strip()
        # An occupants response proves only that the roster was readable; it
        # does not prove that the bot is a member. Only you_joined/you_rejoined
        # events add rooms to connected_rooms.

        # Fallback live responses are tagged by the room they came from.
        # Accumulate all room responses before sending the final invitation batch.
        source_room = str(result.get("_occupants_room") or "").strip()
        if source_room and hasattr(self, "_inv_expected_rooms"):
            for u in self._users_from_room_admin(result.get("room_admin") or {}):
                username = str(u.get("username") or "").strip()
                # Room settings is the source of truth for inv. Include offline
                # members too; only exclude the bot itself and duplicates.
                if (username and _norm_user(username) != _norm_user(BOT_ID)
                        and _norm_user(username) not in getattr(self, "_inv_live_seen", set())):
                    self._inv_live_seen.add(_norm_user(username))
                    self._inv_live_users.append(username)
            self._inv_expected_rooms.discard(source_room)
            if self._inv_expected_rooms:
                return
            result = {"db_users": [{"username": u} for u in self._inv_live_users]}

        # `inv` uses the complete room-settings roster. A db_users payload may
        # therefore contain offline members and must not be filtered by presence.
        users_info = []
        for user in (result.get("db_users") or []):
            if isinstance(user, dict):
                username = str(user.get("username") or "").strip()
                if username:
                    users_info.append({
                        "username": username,
                        "role": str(user.get("role") or "none").strip().lower() or "none",
                        "user_id": str(user.get("user_id") or ""),
                    })

        # Some server builds return ResultMessage.users directly.
        # IMPORTANT: read the bot's own role BEFORE excluding the bot from
        # the invitation roster. Otherwise an owner bot is always seen as
        # having an unknown role and `inv` gets stuck on "جاري التحقق".
        for user in (result.get("users") or []):
            if not isinstance(user, dict):
                continue
            username = str(user.get(1, "") or "").strip()
            role = str(user.get(6, "") or "none").strip().lower()
            if username and _norm_user(username) == _norm_user(BOT_ID):
                if role:
                    self.bot_room_roles[_norm_room(room)] = role
                continue
            if username:
                users_info.append({"username": username, "role": role or "none"})

        # Actual occupants_list response: RoomAdmin field 10 contains the
        # repeated UserItem protobuf messages.
        if not users_info and result.get("room_admin"):
            users_info = self._users_from_room_admin(result["room_admin"])

        # Cache the bot's own room role before removing it from the invitation roster.
        for u in users_info:
            if _norm_user(u.get("username")) == _norm_user(BOT_ID):
                bot_role = str(u.get("role") or "").casefold().strip()
                if bot_role:
                    self.bot_room_roles[_norm_room(room)] = bot_role
                break

        # The bot itself belongs to the room settings list but must not receive
        # an invitation and must not be counted as an ordinary member.
        filtered_users = []
        for u in users_info:
            if _norm_user(u.get("username")) == _norm_user(BOT_ID):
                continue
            filtered_users.append(u)
        users_info = filtered_users

        pending_role = getattr(self, "_pending_inv_role_check", {}).pop(_norm_room(room), None)
        if pending_role is not None:
            bot_role = self._bot_room_role(room)
            if bot_role not in {"owner", "creator", "room_owner", "room_creator"}:
                self.send_room_text(room, "⚠️ ارفع البوت أونر ثم أعد المحاولة.")
                return

        # Cache the complete room list, including role categories. The disk
        # roster is append/update-only: a transient leave event never erases history.
        if users_info:
            self.room_users[room] = {u["username"]: u.get("role", "none") for u in users_info}
            _remember_roster(room, users_info)

        # A roster refresh requested on room entry is only for persistence.
        # It must never start invitations unless the master explicitly used inv.
        if not self.invite_pending:
            if pending_role is not None:
                usernames = [u["username"] for u in users_info if _norm_user(u.get("username")) != _norm_user(BOT_ID)]
                if not usernames:
                    self.send_room_text(room, "📭 لا يوجد أعضاء لإرسال الدعوات لهم.")
                    return
                self.invite_pending = True
                self.invite_silent_master = False
                self.invite_room = room
                self.invite_sent.clear()
                response_room = str((pending_role or {}).get("room") or room).strip()
                threading.Thread(target=self._finish_invites, args=(room, usernames), name="talkin-invites", daemon=True).start()
            return

        if not users_info:
            self.log("[INV] occupants response received but no usernames decoded")
            try:
                self.send_private_text(BOT_MASTER, "⚠️ وصلت بيانات إعدادات الغرفة لكن لم أستطع استخراج أسماء المستخدمين.")
            except Exception:
                pass
            with self.invite_lock:
                self.invite_pending = False
                self.invite_silent_master = False
            return

        # These counts come from the CURRENT room UserItem role field, not from
        # Supabase or any saved roster. Keep the three categories separate.
        owner_roles = {"owner", "creator", "room_owner", "room_creator"}
        admin_roles = {"admin", "moderator", "mod"}
        member_roles = {"member", "user", "none", ""}
        owners = [u["username"] for u in users_info if str(u.get("role") or "").casefold() in owner_roles]
        admins = [u["username"] for u in users_info if str(u.get("role") or "").casefold() in admin_roles]
        members = [u["username"] for u in users_info if str(u.get("role") or "").casefold() in member_roles]
        unknown = [u["username"] for u in users_info if str(u.get("role") or "").casefold() not in owner_roles | admin_roles | member_roles]
        self.log(f"[INV] CURRENT ROOM SETTINGS: room={room} total={len(users_info)} owners={len(owners)} admins={len(admins)} members={len(members)} unknown={len(unknown)}")
        if unknown:
            self.log("[INV] unknown role values:", sorted({str(u.get("role") or "") for u in users_info if str(u.get("role") or "").casefold() not in owner_roles | admin_roles | member_roles}))

        # Master gets the exact live room-settings counts privately.
        try:
            summary = (
                f"📋 إعدادات الغرفة الحالية\n"
                f"━━━━━━━━━━━━\n"
                f"👥 إجمالي الأعضاء: {len(users_info)}\n"
                f"👑 الأونرات: {len(owners)}\n"
                f"🛡️ المشرفين: {len(admins)}\n"
                f"👤 الأعضاء: {len(members)}\n"
                f"━━━━━━━━━━━━"
            )
            self.send_private_text(BOT_MASTER, summary)
        except Exception as e:
            self.log("[INV] role summary failed:", repr(e))

        self.invite_thread = threading.Thread(
            target=self._finish_invites,
            args=(room, [u["username"] for u in users_info]),
            name="talkin-invites",
            daemon=True,
        )
        self.invite_thread.start()

    def _request_next_inv_roster_category(self):
        state = getattr(self, "_inv_roster_pending", None)
        if not isinstance(state, dict):
            return False
        queue = state.get("queue") or []
        if not queue:
            room = state.get("room", "")
            users = list((state.get("users") or {}).values())
            if not state.get("bot_is_owner"):
                self.send_room_text(room, "⚠️ البوت ليس أونر في هذه الغرفة. لم يتم إرسال أي دعوة.")
                with self.invite_lock:
                    self.invite_pending = False
                    self.invite_silent_master = False
                self._inv_roster_pending = None
                return False
            names = [u["username"] for u in users if u.get("username") and _norm_user(u.get("username")) != _norm_user(BOT_ID)]
            if not names:
                self.send_room_text(room, "📭 لم يتم العثور على أعضاء أو مشرفين أو أونرات لإرسال الدعوات لهم.")
                with self.invite_lock:
                    self.invite_pending = False
                    self.invite_silent_master = False
                self._inv_roster_pending = None
                return False
            # Store the complete live roster before starting invitations.
            self.room_users[room] = {u["username"]: u.get("role", "member") for u in users}
            _remember_roster(room, users)
            self.invite_room = room
            self.invite_sent.clear()
            threading.Thread(
                target=self._finish_invites,
                args=(room, names),
                name="talkin-complete-room-invites",
                daemon=True,
            ).start()
            self._inv_roster_pending = None
            return True

        code = str(queue.pop(0)).lower()
        state["queue"] = queue
        state["current"] = code
        request_types = {"o": "owners_list", "a": "admins_list", "m": "members_list"}
        request_type = request_types[code]
        request_id = secrets.token_urlsafe(15)[:20]
        self._pending_room_lists[_norm_room(state["room"])] = {
            "room": state["room"], "code": code, "sender": state.get("response_to") or BOT_MASTER,
            "request_type": request_type, "request_id": request_id,
            "requested_at": time.time(), "invite_collect": True,
            "complete_users": True,
        }
        self.send_query(encode_query(
            "room_admin", type_=request_type, room=state["room"],
            to=BOT_ID, value="none", id_=request_id
        ))
        self.log("[INV] requesting complete category:", request_type, state["room"])
        return True
