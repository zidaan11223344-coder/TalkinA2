"""Room roster, presence, room synchronization, lists and full-room sync."""

class RoomManagementCoreSection:
    def _find_pending_join_for_message(self, sender, room=""):
        """Find the pending join-language request even when sender format differs."""
        pending_map = getattr(self, "_pending_room_language", {}) or {}
        if not pending_map:
            return None, None
        key = self._room_language_pending_key(sender)
        direct = pending_map.get(key)
        if isinstance(direct, dict):
            return key, direct
        room_key = _norm_room(room).casefold() if str(room or "").strip() else ""
        now = time.time()
        candidates = []
        for pkey, value in pending_map.items():
            if not isinstance(value, dict):
                continue
            created = float(value.get("created", 0) or 0)
            if created and now - created > 600:
                continue
            response_room = str(value.get("response_room") or "").strip()
            score = created
            if room_key and response_room and _norm_room(response_room).casefold() == room_key:
                score += 500
            candidates.append((score, pkey, value))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            _, best_key, value = candidates[0]
            return best_key, value
        for pkey, value in pending_map.items():
            if isinstance(value, dict):
                return pkey, value
        return None, None

    def _record_room_sync_result(self, sync_id, room, ok, detail=""):
        jobs = getattr(self, "_room_sync_jobs", {})
        state = jobs.get(sync_id) if isinstance(jobs, dict) else None
        if not isinstance(state, dict):
            return
        key = _norm_room(room)
        if key in state.get("results", {}):
            return
        state.setdefault("results", {})[key] = {"room": str(room), "ok": bool(ok), "detail": str(detail or "")}
        if len(state["results"]) < int(state.get("total", 0) or 0):
            return
        jobs.pop(sync_id, None)
        success = [row["room"] for row in state["results"].values() if row.get("ok")]
        failed = [row for row in state["results"].values() if not row.get("ok")]
        lines = [
            "✅ انتهى دخول غرف السيرفر",
            f"📊 النتيجة: دخل {len(success)} من أصل {state.get('total', 0)} غرفة.",
        ]
        if success:
            lines.append("🟢 تم الدخول: " + ", ".join(sorted(success, key=str.casefold)))
        if failed:
            lines.append("🔴 تعذر الدخول:")
            lines.extend(f"• {row['room']}: {row.get('detail') or 'لم يصل تأكيد من السيرفر'}" for row in failed)
        self.send_private_text(state.get("sender", ""), "\n".join(lines))

    def _start_server_room_join(self, rooms, sender):
        """Enter the last server catalog only; fetching is a separate command."""
        cleaned = sorted({_norm_room(value) for value in rooms or [] if _norm_room(value)}, key=str.casefold)
        if not cleaned:
            self.send_private_text(sender, "📭 قائمة غرف السيرفر فارغة.")
            return False
        sync_id = uuid.uuid4().hex
        jobs = getattr(self, "_room_sync_jobs", None)
        if not isinstance(jobs, dict):
            jobs = self._room_sync_jobs = {}
        jobs[sync_id] = {"sender": str(sender or ""), "total": len(cleaned), "results": {}}
        threading.Thread(
            target=self._join_rooms_serially,
            args=(cleaned, sender, False, sync_id),
            daemon=True, name="server-room-catalog-join",
        ).start()
        return True

    def _join_rooms_serially(self, rooms, sender="", fast_restore=False, sync_id=None):
        """Join rooms one at a time and wait for each server acknowledgement.

        Talkin's gateway is sensitive to a burst of room_join packets.  More
        importantly, sending the next join before the previous result arrives
        can look like an enter/leave loop.
        """
        cleaned=[]; seen=set()
        for value in rooms or []:
            room_name=str(value or "").strip()
            key=_norm_room(room_name)
            if room_name and key not in seen:
                seen.add(key); cleaned.append(room_name)
        if not cleaned:
            return {"sent": 0, "skipped": 0}
        sent=0; skipped=0
        if fast_restore:
            # Startup restore must not wait for one ACK per room. The reader
            # remains free to process all ACKs/events while joins are sent at
            # a small gateway-safe pace.
            gap=max(0.08, float(os.getenv("FAST_ROOM_RESTORE_DELAY_SECONDS", "0.15") or 0.15))
        else:
            gap=max(0.8, float(os.getenv("ROOM_BULK_DELAY_SECONDS", "1.5") or 1.5))
        timeout=max(10.0, float(os.getenv("JOIN_CONFIRM_TIMEOUT_SECONDS", "20") or 20))
        with self._room_bulk_lock:
            for target in cleaned:
                key=_norm_room(target)
                connected = any(
                    _norm_room(r).casefold() == key.casefold()
                    for r in getattr(self, "connected_rooms", set())
                )
                if connected and fast_restore:
                    # The server already confirmed this room in the current
                    # session. Never leave/rejoin it just because the master
                    # requested the saved-room list; that race used to produce
                    # a false "unable to enter" timeout after a real success.
                    if sender:
                        try:
                            self.send_private_text(sender, f"✅ البوت موجود داخل الغرفة: {target}")
                        except Exception as exc:
                            self.log("[ROOM] connected-room notice failed:", target, repr(exc))
                    skipped += 1
                    if sync_id:
                        self._record_room_sync_result(sync_id, target, True, "موجودة ومتصل بها البوت")
                    continue
                if fast_restore and sender and any(
                    _norm_room(saved).casefold() == key.casefold()
                    for saved in getattr(self, "_room_restore_candidates", set())
                ):
                    try:
                        self.send_private_text(sender, f"✅ البوت موجود داخل الغرفة: {target}")
                    except Exception as exc:
                        self.log("[ROOM] restored-room notice failed:", target, repr(exc))
                    skipped += 1
                    if sync_id:
                        self._record_room_sync_result(sync_id, target, True, "تمت استعادتها عند تشغيل البوت")
                    continue
                if not fast_restore:
                    restore_candidates = getattr(self, "_room_restore_candidates", set())
                    for saved in tuple(restore_candidates):
                        if _norm_room(saved).casefold() == key.casefold():
                            restore_candidates.discard(saved)
                if connected:
                    # A manual دخول@ request is an explicit refresh: leave
                    # first, wait briefly for the server's user_left ACK, then
                    # issue exactly one join even if that ACK never arrives.
                    pending_leaves = getattr(self, "_pending_room_leaves", None)
                    if not isinstance(pending_leaves, dict):
                        pending_leaves = self._pending_room_leaves = {}
                    leave_ack = threading.Event()
                    pending_leaves[key] = leave_ack
                    leave_sent = False
                    try:
                        leave_sent = self.leave_room(target) is not False
                    except Exception as exc:
                        self.log("[ROOM] manual leave before rejoin failed:", target, repr(exc))
                    leave_timeout = max(
                        0.2, float(os.getenv("ROOM_LEAVE_ACK_TIMEOUT_SECONDS", "3.0") or 3.0)
                    )
                    if leave_sent:
                        if leave_ack.wait(leave_timeout):
                            self.log("[ROOM] server confirmed leave before rejoin:", target)
                        else:
                            self.log("[ROOM] leave ACK timed out; continuing with one manual join:", target)
                    pending_leaves.pop(key, None)
                    rejoin_gap = max(
                        0.0, float(os.getenv("MANUAL_REJOIN_DELAY_SECONDS", "1.0") or 1.0)
                    )
                    if rejoin_gap:
                        time.sleep(rejoin_gap)
                try:
                    if self.join_room(target, force=bool(fast_restore), requested_by=sender):
                        sent += 1
                        if fast_restore:
                            getattr(self, "_room_restore_candidates", set()).add(key)
                        if not fast_restore:
                            # Manual joins wait for you_joined/you_rejoined so
                            # the next request cannot create an enter/leave loop.
                            deadline=time.time()+timeout+1.0
                            while time.time() < deadline and key in getattr(self, "_pending_room_joins", {}):
                                time.sleep(0.25)
                        if sync_id:
                            connected = any(_norm_room(r) == key for r in getattr(self, "connected_rooms", set()))
                            detail = "تم تأكيد الدخول" if connected else getattr(self, "_blocked_room_reasons", {}).get(key, "لم يصل تأكيد من السيرفر")
                            self._record_room_sync_result(sync_id, target, connected, detail)
                    else:
                        skipped += 1
                        if sync_id:
                            connected = any(_norm_room(r) == key for r in getattr(self, "connected_rooms", set()))
                            self._record_room_sync_result(sync_id, target, connected, "الغرفة متصلة مسبقاً" if connected else getattr(self, "_blocked_room_reasons", {}).get(key, "تعذر إرسال طلب الدخول"))
                except Exception as exc:
                    skipped += 1
                    self.log("[ROOM] serial join failed:", target, repr(exc))
                    if sync_id:
                        self._record_room_sync_result(sync_id, target, False, str(exc)[:180])
                if target != cleaned[-1]:
                    time.sleep(gap)
        return {"sent": sent, "skipped": skipped}

    def _add_rooms_to_saved_file(self, sender, raw_rooms):
        rooms=[]
        seen=set()
        for value in str(raw_rooms or "").split():
            room_name=value.strip()
            if not room_name:
                continue
            key=_norm_room(room_name).casefold()
            if key in seen:
                continue
            seen.add(key)
            rooms.append(room_name)
        if not rooms:
            self.send_private_text(sender,"❌ أرسل أسماء الغرف مفصولة بمسافة.\nمثال: مشاعر ادم نبض قلوب سوالف")
            return True
        blocked = getattr(self, "blocked_rooms", set())
        rooms = [room_name for room_name in rooms if _norm_room(room_name) not in blocked]
        existing=[room_name for room_name in _persistent_rooms() if _norm_room(room_name) not in blocked]
        existing_keys={_norm_room(x).casefold() for x in existing}
        added=[]
        for room_name in rooms:
            if _norm_room(room_name).casefold() not in existing_keys:
                existing.append(room_name)
                existing_keys.add(_norm_room(room_name).casefold())
                added.append(room_name)
        _save_persistent_rooms(existing)
        self.known_rooms.update(room_name for room_name in existing if _norm_room(room_name) not in blocked)
        self.send_private_text(
            sender,
            "✅ تم تحديث ملف الغرف المحفوظة.\n"
            f"➕ تمت إضافة: {len(added)}\n"
            f"📋 الجديد: {' '.join(added) if added else 'لا توجد غرف جديدة'}\n"
            f"🏠 إجمالي الغرف المحفوظة: {len(_persistent_rooms())}"
        )
        return True

    def _request_room_list_via_ws(self, room, code, sender):
        room = str(room or "").strip()
        if not room or not getattr(self, "ws", None):
            return False
        try:
            code = str(code or "m").strip().lower()
            request_types = {"m": "members_list", "a": "admins_list", "o": "owners_list", "b": "banned_list"}
            request_type = request_types.get(code, "members_list")
            request_id = secrets.token_urlsafe(15)[:20]
            self._pending_room_lists[_norm_room(room)] = {
                "room": room, "code": code, "sender": str(sender or "").strip(),
                "request_type": request_type, "request_id": request_id,
                "requested_at": time.time(), "request_mode": "app",
                # The complete APK-compatible response is ResultMessage.users[];
                # RoomEvent.text is only a 20-row preview.
                "complete_users": True,
            }
            self.send_query(encode_query(
                "room_admin", type_=request_type, room=room,
                to="username", value="none", id_=request_id
            ))
            timer = threading.Timer(10.0, self._room_list_request_timeout, args=(_norm_room(room), request_id))
            timer.daemon = True
            self._pending_room_lists[_norm_room(room)]["timer"] = timer
            timer.start()
            self.send_private_text(sender, f"⏳ جارٍ جلب إعدادات أعضاء الغرفة {room}...")
            return True
        except Exception as exc:
            self._pending_room_lists.pop(_norm_room(room), None)
            self.log("[ROOM-LIST] native occupants request failed", room, repr(exc))
            return False

    def _room_list_request_timeout(self, room_key, request_id):
        """Retry a list request with the legacy BOT_ID target once."""
        pending = getattr(self, "_pending_room_lists", {}).get(room_key)
        if not isinstance(pending, dict) or pending.get("request_id") != request_id:
            return False
        if pending.get("request_mode") != "app":
            return False
        pending["request_mode"] = "legacy"
        new_id = secrets.token_urlsafe(15)[:20]
        pending["request_id"] = new_id
        pending["requested_at"] = time.time()
        try:
            self.send_query(encode_query(
                "room_admin", type_=pending.get("request_type") or "members_list",
                room=pending.get("room", ""), to=BOT_ID, value="none", id_=new_id
            ))
            timer = threading.Timer(10.0, self._room_list_request_timeout, args=(room_key, new_id))
            timer.daemon = True
            pending["timer"] = timer
            timer.start()
            self.log("[ROOM-LIST] retrying legacy target:", pending.get("room"), pending.get("request_type"))
            return True
        except Exception as exc:
            self.log("[ROOM-LIST] legacy retry failed:", repr(exc))
            self._pending_room_lists.pop(room_key, None)
            return False

    def _start_presence_native(self, target, sender, current_room=""):
        rooms=[]; seen=set()
        candidates=list(self._active_rooms() or [])
        if current_room: candidates.insert(0, current_room)
        for room in candidates:
            room=str(room or "").strip()
            key=_norm_room(room)
            if room and key not in seen:
                seen.add(key); rooms.append(room)
        if not rooms or not getattr(self, "ws", None):
            return False
        token=f"{_norm_user(sender)}:{time.time_ns()}"
        self._pending_presence[token]={"target":str(target).strip().lstrip("@"), "sender":str(sender or "").strip(), "rooms":rooms, "results":[]}
        return self._request_next_presence(token)

    def _request_next_presence(self, token):
        state=self._pending_presence.get(token)
        if not isinstance(state, dict): return False
        rooms=state.get("rooms") or []
        if not rooms:
            target=state.get("target"); lines=[f"📍 حالة @{target} ({len(state.get('results') or [])} غرفة):"]
            for room,user in state.get("results") or []:
                present=user.get("is_present", user.get("online"))
                state_text="🟢 متصل" if present is True or str(present).lower() in {"true","1","online"} else "⚪ غير متصل"
                lines.append(f"• {room}: {state_text}")
            if len(lines)==1: lines=[f"📭 @{target} غير موجود في قوائم الغرف المحفوظة."]
            self.send_private_text(state.get("sender"), "\n".join(lines))
            self._pending_presence.pop(token, None)
            return True
        room=rooms.pop(0)
        self._pending_room_lists[_norm_room(room)]={"room":room,"code":"is","sender":state.get("sender"),"target":state.get("target"),"presence_token":token,"requested_at":time.time()}
        try:
            self.send_query(encode_query("room_admin", type_="occupants_list", room=room, to=BOT_ID, value="none"))
            return True
        except Exception as exc:
            self.log("[ROOM-PRESENCE] native request failed", room, repr(exc))
            return self._request_next_presence(token)

    def _complete_pending_room_list_text(self, room, body):
        """Consume the server's human-readable `User List:` response.

        Some TalkinChat builds answer room_admin list requests by emitting a
        RoomEvent text such as `User List: Owners 1/2 (Total: 23):` instead of
        putting the roster in ResultMessage.users[].  This is the same payload
        already visible to the monitor command, so use it as a native server
        response rather than falling back to local/database rosters.
        """
        room = str(room or "").strip()
        body = str(body or "")
        if not room or not body or "user list:" not in body.casefold():
            return False
        pending = self._pending_room_lists.get(_norm_room(room))
        if not isinstance(pending, dict) or pending.get("code") == "is":
            return False

        # Only consume a response that belongs to the request we sent.
        request_type = str(pending.get("request_type") or "").casefold()
        label = ""
        if "owners" in body.casefold():
            label = "o"
        elif "admins" in body.casefold() or "admin" in body.casefold() or "moderators" in body.casefold():
            label = "a"
        elif "members" in body.casefold() or "users" in body.casefold():
            label = "m"
        elif "banned" in body.casefold() or "outcast" in body.casefold():
            label = "b"
        expected = {"owners_list": "o", "admins_list": "a", "members_list": "m", "banned_list": "b"}.get(request_type, "")
        if expected and label and expected != label:
            return False

        # The text-form User List is only a short dashboard preview (usually
        # 20 rows). The APK's complete flow for members, admins, owners and
        # banned users uses the binary ResultMessage.users[] payload instead.
        # Do not consume/pop the pending request here, otherwise the later
        # complete binary response is discarded as unsolicited. This applies
        # equally to direct l@* commands and every category collected by inv.
        full_list_types = {
            "members_list", "admins_list", "owners_list", "banned_list"
        }
        if pending.get("user_room_rank_check"):
            room_key = _norm_room(room)
            state = getattr(self, "_pending_user_room_rank_checks", {}).get(room_key)
            if not isinstance(state, dict) or state.get("request_id") != pending.get("request_id"):
                return False
            requester_key = _norm_user(state.get("requester", ""))
            # The text response is only a preview. Use it when the requested
            # account is explicitly listed, otherwise keep waiting for the
            # complete binary roster instead of reporting a false miss.
            for line in body.splitlines():
                match = re.match(r"^\s*\d+\.\s+(.+?)\s*$", line)
                if not match:
                    continue
                name = match.group(1).strip().lstrip("@").strip()
                if _norm_user(name) == requester_key:
                    return self._finish_user_room_rank_check(
                        room_key,
                        {"o": "owner", "a": "admin", "m": "member", "b": "outcast"}.get(label, "member"),
                    )
            return False

        if pending.get("bot_role_check"):
            self.log("[ROOM-ROLE] ignored text preview; waiting for complete server list")
            return False
        if (pending.get("complete_users") or request_type in full_list_types) and os.getenv(
                "ALLOW_TEXT_ROOM_LIST_FALLBACK", "0").strip() != "1":
            self.log(
                "[ROOM-LIST] ignored truncated RoomEvent.text preview; "
                f"waiting for ResultMessage.users[] ({request_type})"
            )
            return False

        import re as _re
        names = []
        seen = set()
        for line in body.splitlines():
            line = line.strip()
            m = _re.match(r"^\s*\d+\.\s+(.+?)\s*$", line)
            if not m:
                continue
            name = m.group(1).strip().lstrip("@").strip()
            if not name:
                continue
            key = _norm_user(name)
            if key and key != _norm_user(BOT_ID) and key not in seen:
                seen.add(key)
                names.append({"username": name, "role": label or "none"})

        # A response header without numbered rows is not enough to consume the
        # pending request; let the normal binary handler continue waiting.
        if not names:
            return False

        self._pending_room_lists.pop(_norm_room(room), None)
        title = {
            "a": "🛡️ قائمة المشرفين",
            "m": "👥 قائمة الأعضاء",
            "o": "👑 قائمة الأونرات",
            "b": "🚫 قائمة المحظورين",
        }.get(label, "📋 قائمة الغرفة")
        self._send_room_list(
            room, f"{title} — {room}", names,
            "📭 لم تصل أسماء فعلية من السيرفر.", sender=pending.get("sender")
        )

        try:
            self.send_private_text(
                BOT_MASTER or pending.get("sender"),
                "📡 استلمت رد السيرفر فعليًا.\n"
                f"🏠 الغرفة: {room}\n"
                f"📋 الطلب: {request_type or 'unknown'}\n"
                f"🆔 رقم الطلب: {pending.get('request_id') or 'غير معروف'}\n"
                "📦 المصدر: RoomEvent.text / User List\n"
                f"📥 العدد الخام: {len(names)}\n"
                f"👥 العدد بعد الفلترة: {len(names)}"
            )
        except Exception as exc:
            self.log("[ROOM-LIST] text response diagnostic failed:", repr(exc))
        return True

    def _complete_pending_room_list(self, result):
        source_room = str(result.get("_occupants_room") or "").strip()
        if not source_room:
            return False
        pending = self._pending_room_lists.pop(_norm_room(source_room), None)
        if not isinstance(pending, dict):
            return False
        timer = pending.get("timer")
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass

        if pending.get("bot_role_check"):
            room_key = _norm_room(source_room)
            state = getattr(self, "_pending_bot_room_role_checks", {}).get(room_key)
            if not isinstance(state, dict) or state.get("request_id") != pending.get("request_id"):
                return False
            users = self._extract_room_list_users(result)
            bot_keys = {_norm_user(BOT_ID), _norm_user(PRIMARY_BOT_ID), _norm_user(getattr(self, "bot_id", ""))}
            bot_keys.discard("")
            bot_user = next((u for u in users if _norm_user(u.get("username")) in bot_keys), None)
            if bot_user:
                reported_role = str(
                    bot_user.get("role") or bot_user.get("rank") or bot_user.get("user_role") or bot_user.get("permission") or ""
                ).casefold().strip().replace(" ", "_")
                if reported_role not in {"none", "unknown"}:
                    return self._finish_bot_room_role_check(room_key, reported_role)
            if pending.get("request_mode") == "app":
                return self._fallback_bot_room_role_check(room_key, pending.get("request_id"))
            return self._finish_bot_room_role_check(room_key, missing=True)

        # INV roster collection: each category is fetched separately. The
        # response category is authoritative; then the next category is
        # requested. Invitations start only after owners + admins + members
        # have all been collected.
        if pending.get("list_all_collect"):
            state = getattr(self, "_list_all_pending", None)
            if not isinstance(state, dict):
                return False
            code = str(pending.get("code") or "").lower()
            users = self._extract_room_list_users(result)
            role = {"o": "owner", "a": "admin", "m": "member"}.get(code, "member")
            names=[]
            seen=set()
            for u in users:
                name=str(u.get("username") or "").strip().lstrip("@")
                if name and _norm_user(name) != _norm_user(BOT_ID) and _norm_user(name) not in seen:
                    seen.add(_norm_user(name)); names.append(name)
            state["users"][code]=names
            return self._request_next_all_list()

        if pending.get("invite_collect"):
            state = getattr(self, "_inv_roster_pending", None)
            if not isinstance(state, dict):
                return False
            code = str(pending.get("code") or "").lower()
            users = self._extract_room_list_users(result)
            if not users and result.get("users"):
                for item in result.get("users") or []:
                    if isinstance(item, dict):
                        name = str(item.get(1) or "").strip().lstrip("@")
                        if name:
                            users.append({"username": name, "role": ""})
            role = {"o": "owner", "a": "admin", "m": "member"}.get(code, "member")
            # Capture the bot's own identity BEFORE excluding it from the
            # invitation roster. This is the authoritative owner check.
            bot_keys = {
                _norm_user(BOT_ID), _norm_user(PRIMARY_BOT_ID),
                _norm_user(getattr(self, "bot_id", "")),
            }
            bot_keys.discard("")
            bot_names = {
                _norm_user(str(u.get("username") or "").strip().lstrip("@"))
                for u in users
                if isinstance(u, dict) and str(u.get("username") or "").strip()
            }
            if code == "o":
                # Owner verification is authoritative: the bot must appear in
                # the complete owners_list response. Never trust a stale local
                # role cache, because that could start invitations after the
                # bot was downgraded or removed as room owner.
                state["bot_is_owner"] = bool(bot_keys.intersection(bot_names))
                if not state["bot_is_owner"]:
                    self.log("[INV] owner verification failed:", source_room,
                             "bot=", BOT_ID, "owners=", sorted(bot_names))
                    self.send_room_text(source_room, "❌ ارفع البوت أونر أولاً.")
                    with self.invite_lock:
                        self.invite_pending = False
                        self.invite_silent_master = False
                    self._inv_roster_pending = None
                    return True

            for u in users:
                name = str(u.get("username") or "").strip().lstrip("@")
                if not name or _norm_user(name) == _norm_user(BOT_ID):
                    continue
                key = _norm_user(name)
                state.setdefault("users", {})[key] = {
                    "username": name,
                    "role": role,
                    "user_id": str(u.get("user_id") or ""),
                }
            return self._request_next_inv_roster_category()

        # Presence (is@...) keeps its old private behavior.
        if pending.get("code") == "is":
            users = self._users_from_room_admin(result.get("room_admin") or {})
            if not users and result.get("users"):
                users = [{"username": str(u.get(1) or "").strip(),
                          "role": str(u.get(6) or "none"), "online": u.get(5)}
                         for u in result.get("users") if isinstance(u, dict)]
            target = _norm_user(pending.get("target"))
            for user in users:
                if _norm_user(user.get("username")) == target:
                    token = pending.get("presence_token")
                    state = self._pending_presence.get(token)
                    if isinstance(state, dict):
                        state.setdefault("results", []).append((pending.get("room"), user))
                    break
            return self._request_next_presence(pending.get("presence_token"))

        # L@a / L@m / L@o / L@b: the APK sends the actual roster in
        # ResultMessage.users[] (or the equivalent RoomAdmin wrapper).
        code = str(pending.get("code") or "").lower()
        users = self._extract_room_list_users(result)
        inferred_role = {"a":"admin","o":"owner","m":"member","b":"banned"}.get(code,"none")
        if inferred_role != "none":
            for u in users:
                if str(u.get("role") or "").strip().lower() in {"","none"}:
                    u["role"] = inferred_role
        raw_count = 0
        try:
            raw = result.get("users") or []
            raw_count = len(raw) if isinstance(raw, (list, tuple, dict)) else 0
        except Exception:
            raw_count = 0

        # Never let the bot itself appear in a room roster.
        users = [u for u in users if _norm_user(u.get("username")) != _norm_user(BOT_ID)]
        code = str(pending.get("code") or "").lower()
        role_norm = lambda u: str(u.get("role") or "").strip().lower()
        if code == "a":
            users = [u for u in users if role_norm(u) in {"admin", "moderator", "mod"}]
        elif code == "o":
            users = [u for u in users if role_norm(u) in {"owner", "creator", "room_owner", "room_creator", "host"}]
        elif code == "m":
            users = [u for u in users if role_norm(u) not in {"owner", "creator", "room_owner", "room_creator", "host", "admin", "moderator", "mod", "outcast", "banned", "ban"}]
        elif code == "b":
            users = [u for u in users if role_norm(u) in {"outcast", "banned", "ban", "blocked"}]

        if users:
            # Category-specific lists are not complete room snapshots.
            # Merge them so L@m cannot erase owners/admins from the cache.
            self.room_users.setdefault(source_room, {}).update({u.get("username"): u.get("role", "none") for u in users if u.get("username")})
            _remember_roster(source_room, users)

        titles = {
            "a": "🛡️ قائمة المشرفين",
            "m": "👥 قائمة الأعضاء",
            "o": "👑 قائمة الأونرات",
            "b": "🚫 قائمة المحظورين",
        }
        title = f"{titles.get(code, '📋 قائمة الغرفة')} — {source_room}"

        # IMPORTANT: L@a/L@m/L@o are room commands. Show the actual list in
        # the room, not as a private message. The private message is only the
        # diagnostic requested by the master.
        self._send_room_list(
            source_room, title, users,
            "📭 لم تصل أسماء فعلية مطابقة من السيرفر.", sender=pending.get("sender")
        )

        # Private diagnostic to the command sender/master. This confirms the
        # server response without replacing the public room list.
        try:
            request_type = pending.get("request_type") or {"a": "admins_list", "m": "members_list", "o": "owners_list", "b": "banned_list"}.get(code, "unknown")
            request_id = pending.get("request_id") or "غير معروف"
            # The diagnostic belongs to the master, not to the user who typed L@a/L@m/L@o.
            # Fall back to the command sender only if no master is configured.
            diagnostic_recipient = BOT_MASTER or pending.get("sender")
            if not diagnostic_recipient:
                raise RuntimeError("BOT_MASTER/command sender is empty")
            self.send_private_text(
                diagnostic_recipient,
                "📡 استلمت رد السيرفر فعليًا.\n"
                f"🏠 الغرفة: {source_room}\n"
                f"📋 الطلب: {request_type}\n"
                f"🆔 رقم الطلب: {request_id}\n"
                "📦 المصدر: ResultMessage.users[] / RoomAdmin\n"
                f"📥 العدد الخام: {raw_count}\n"
                f"👥 العدد بعد الفلترة: {len(users)}"
            )
        except Exception as exc:
            self.log("[ROOM-LIST] private diagnostic failed:", repr(exc))
        return True

    def _send_room_list(self, room, title, users, empty="📭 لا توجد بيانات.", sender=""):
        """Send a complete room roster one safe page at a time.

        ``members_list`` can contain more users than fit in one Talkin frame.
        Keep every decoded ``users[].username`` row, build pages using the
        same byte-safe strategy as private lists, and let the requester use
        ``ns`` to advance instead of silently losing the remaining rows.
        """
        rows=[]; seen=set()
        role_labels = {
            "owner": "أونر", "creator": "صانع الغرفة", "room_owner": "أونر",
            "room_creator": "صانع الغرفة", "host": "مالك الغرفة",
            "admin": "مشرف", "moderator": "مشرف", "mod": "مشرف",
            "member": "عضو", "user": "عضو", "none": "غير محددة",
            "outcast": "محظور", "banned": "محظور",
        }
        for item in users or []:
            if isinstance(item, dict):
                name = str(item.get("username") or "").strip().lstrip("@")
                raw_role = str(item.get("role") or item.get("rank") or item.get("affiliation") or "").casefold().strip().replace(" ", "_")
                raw_role = {"o":"owner", "a":"admin", "m":"member", "normal":"member"}.get(raw_role, raw_role)
                suffix = f" — {role_labels.get(raw_role, raw_role)}" if raw_role else ""
            else:
                name = str(item or "").strip().lstrip("@")
                suffix = ""
            key=_norm_user(name)
            if key and key not in seen and key != _norm_user(BOT_ID):
                seen.add(key); rows.append((name, suffix))
        if not rows:
            self.send_room_text(room, empty)
            return
        rows.sort(key=lambda row: _norm_user(row[0]))
        limit = max(240, int(os.getenv("WS_MAX_MESSAGE_BYTES", "1008")) - 120)
        pages=[]; current=[f"{title} ({len(rows)}):"]
        for index, (name, suffix) in enumerate(rows, 1):
            line = f"{index}. @{name}{suffix}"
            candidate = "\n".join(current + [line])
            if len(candidate.encode("utf-8")) > limit and len(current) > 1:
                pages.append("\n".join(current))
                current=[f"{title} ({len(rows)}):", line]
            else:
                current.append(line)
        if len(current) > 1:
            pages.append("\n".join(current))

        # The native request is category-specific (members_list/admins_list/
        # owners_list); keep that category's pages under the room+requester
        # key so a bare ns cannot advance another user's list.
        result_pages = getattr(self, "_result_pages", {})
        result_key = ("room_message", str(room or ""), str(sender or ""))
        total = len(pages)
        result_pages[result_key] = {
            "pages": [
                f"{page}\n📄 القائمة {page_no}/{total}"
                for page_no, page in enumerate(pages, 1)
            ],
            "part": 1,
            # room_message uses Query field 6 (room), not field 4 (to).
            "kwargs": {"room": str(room or "")},
        }
        self._result_pages = result_pages
        first = result_pages[result_key]["pages"][0]
        if total > 1:
            first += "\n\n📌 للقائمة التالية اكتب Ns"
        else:
            first += "\n\n✅ انتهت القوائم."
        self.send_room_text(room, first)

    def _settings_room_users(self, room):
        """Return the room-settings roster, including offline members."""
        room = str(room or "").strip()
        # Do not return the database result immediately.  Some deployments
        # populate ``room_users`` from presence events, so that result may
        # contain online occupants only.  The persisted room-settings roster
        # is the complementary source for members who are currently offline.
        users = []
        try:
            users = self.db.room_users(room) if getattr(self, "db", None) else []
        except Exception as exc:
            self.log("[ROOM-LIST] settings roster failed", room, repr(exc))
        merged = {}
        for item in users or []:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username") or "").strip().lstrip("@")
            key = _norm_user(username)
            if key and key != _norm_user(BOT_ID):
                merged[key] = dict(item, username=username)

        data = _persistent_rosters().get("rooms", {})
        saved = data.get(room, {}) if isinstance(data, dict) else {}
        # Persistent entries fill gaps left by presence-only DB snapshots.
        for value in (saved.values() if isinstance(saved, dict) else []):
            if isinstance(value, dict):
                item = dict(value)
                username = str(item.get("username") or "").strip().lstrip("@")
                key = _norm_user(username)
                if key and key != _norm_user(BOT_ID) and key not in merged:
                    merged[key] = dict(item, username=username)

        # The native l@a/l@m/l@o responses can discover accounts that have not
        # reached the database snapshot yet. Keep that in-memory room-settings
        # cache as a third source so inv never falls back to online occupants.
        cached = (getattr(self, "room_users", {}) or {}).get(room, {})
        if isinstance(cached, dict):
            for username, role in cached.items():
                username = str(username or "").strip().lstrip("@")
                key = _norm_user(username)
                if key and key != _norm_user(BOT_ID) and key not in merged:
                    merged[key] = {"username": username, "role": str(role or "member").strip().lower() or "member"}

        result = list(merged.values())
        self.log("[ROOM-LIST] merged settings roster:", room, len(result),
                 "db=", len(users or []), "persisted=", len(saved) if isinstance(saved, dict) else 0)
        return result

    def _send_private_list(self, sender, title, users, empty="📭 لا توجد بيانات."):
        rows=[]; seen=set()
        for item in users or []:
            name = str(item.get("username") if isinstance(item, dict) else item or "").strip().lstrip("@")
            if name and _norm_user(name) not in seen and _norm_user(name) != _norm_user(BOT_ID):
                seen.add(_norm_user(name)); rows.append(name)
        rows.sort(key=_norm_user)
        if not rows:
            self.send_private_text(sender, empty)
            return
        limit = int(os.getenv("WS_MAX_MESSAGE_BYTES", "1008"))
        header = f"{title} ({len(rows)}):"
        pages=[]; current=[header]
        for index, name in enumerate(rows, 1):
            line=f"{index}. @{name}"
            candidate="\n".join(current + [line])
            probe=encode_query("chat_message", type_="text", to=str(sender or ""), body=candidate)
            # Reserve room for the page marker and protobuf envelope added
            # after the page count is known.
            if len(probe) > max(120, limit - 80) and len(current) > 1:
                pages.append("\n".join(current))
                current=[header, line]
            else:
                current.append(line)
        if len(current) > 1:
            pages.append("\n".join(current))
        total=len(pages)
        # Send only the first page. The generic `ns` command advances through
        # the remaining pages one at a time. Keep the state in the private-chat
        # result bucket because these room-list commands reply privately.
        result_pages = getattr(self, "_result_pages", {})
        result_key = ("chat_message", "", str(sender or ""))
        result_pages[result_key] = {
            "pages": [
                f"{title} ({len(rows)})\n📄 القائمة {page_no}/{total}\n" + "\n".join(page.splitlines()[1:])
                for page_no, page in enumerate(pages, 1)
            ],
            "part": 1,
            "kwargs": {"to": str(sender or "")}
        }
        self._result_pages = result_pages
        first = result_pages[result_key]["pages"][0]
        if total > 1:
            first += "\n\n📌 للقائمة التالية اكتب Ns"
        self.send_private_text(sender, first)

    def _start_inv_roster_collection_for_lists(self, room, sender):
        """Show owners/admins/members as three separate server-backed lists."""
        # Reuse the same native requests but mark them informational.
        self._list_all_pending = {
            "room": str(room or "").strip(),
            "sender": str(sender or "").strip(),
            "queue": ["o", "a", "m"],
            "users": {"o": [], "a": [], "m": []},
        }
        return self._request_next_all_list()

    def _request_next_all_list(self):
        state = getattr(self, "_list_all_pending", None)
        if not isinstance(state, dict):
            return False
        queue = state.get("queue") or []
        if not queue:
            room = state["room"]
            for code, title in [("o","👑 قائمة الأونرات"),("a","🛡️ قائمة المشرفين"),("m","👥 قائمة الأعضاء")]:
                self._send_room_list(
                    room, title, [{"username": n} for n in state["users"].get(code, [])],
                    "📭 لا توجد أسماء.", sender=state.get("sender")
                )
            self._list_all_pending = None
            return True
        code=queue.pop(0); state["queue"]=queue
        types={"o":"owners_list","a":"admins_list","m":"members_list"}
        rid=secrets.token_urlsafe(15)[:20]
        self._pending_room_lists[_norm_room(state["room"])] = {
            "room":state["room"],"code":code,"sender":state["sender"],
            "request_type":types[code],"request_id":rid,"requested_at":time.time(),
            "list_all_collect":True,"complete_users":True,
        }
        self.send_query(encode_query("room_admin",type_=types[code],room=state["room"],to=BOT_ID,value="none",id_=rid))
        return True

    def _room_list_commands(self, room, text, sender, is_private):
        raw_text = str(text or "").strip().casefold()
        arabic_codes = {
            "جلب الاعضاء": "m", "جلب الأعضاء": "m", "جلب اعضاء الغرفة": "m",
            "جلب أعضاء الغرفة": "m", "اعضاء الغرفة": "m", "أعضاء الغرفة": "m",
            "جلب المشرفين": "a", "جلب الاداريين": "a", "جلب الإداريين": "a",
            "جلب الاونرات": "o", "جلب الأونرات": "o",
        }
        if raw_text in arabic_codes:
            text = f"l@{arabic_codes[raw_text]}"
        match = re.fullmatch(r"l@([amob])", str(text or "").strip(), re.I)
        if match:
            if not room:
                self.send_private_text(sender, "⚠️ نفّذ الأمر داخل الغرفة المطلوبة."); return True
            code=match.group(1).lower()
            if getattr(self, "ws", None) and self._request_room_list_via_ws(room, code, sender):
                return True
            users=self._settings_room_users(room)
            roles={"a":{"admin","moderator","mod"},"o":{"owner","creator","room_owner","room_creator","host"}}
            if code in roles:
                users=[u for u in users if str(u.get("role") or "").lower() in roles[code]]
            elif code == "m":
                users=[u for u in users if str(u.get("role") or "").lower() not in {"owner","creator","room_owner","room_creator","host","admin","moderator","mod","outcast","banned","ban"}]
            titles={"a":"🛡️ قائمة المشرفين", "m":"👥 قائمة الأعضاء", "o":"👑 قائمة الأونرات"}
            self._send_private_list(sender, f"{titles[code]} — {room}", users, f"📭 لا توجد أسماء في {titles[code]} بغرفة {room}.")
            return True
        if str(text or "").strip().casefold() in {"l@all", "l@*", "l@x"}:
            if not room:
                self.send_private_text(sender, "⚠️ نفّذ الأمر داخل الغرفة المطلوبة."); return True
            # Fetch the three categories independently and show them as
            # separate room lists. This is informational; it does not invite.
            self._start_inv_roster_collection_for_lists(room, sender)
            return True
        if str(text or "").strip().casefold() in {"سجل الغرفه", "سجل الغرفة", "سجل الغرفه", "سجل الغرفه"}:
            if not _is_master_name(sender):
                self.send_private_text(sender, "🔒 سجل حظر الغرفة مخصص للماستر.")
                return True
            if not room:
                self.send_private_text(sender, "⚠️ نفّذ الأمر داخل الغرفة المطلوبة.")
                return True

            room_key = _norm_room(room)
            rows = [
                r for r in _bot_ban_rows()
                if isinstance(r, dict) and _norm_room(r.get("room")) == room_key
            ]

            if not rows:
                self.send_room_text(room, f"📋 سجل حظر الغرفة: {room}\n📭 لا يوجد محظورون عبر البوت.")
                return True

            # Newest first, while keeping the report compact and chunkable.
            rows = list(reversed(rows))
            lines = [
                f"📋 سجل حظر الغرفة: {room}",
                f"🚫 العدد: {len(rows)}",
            ]
            for i, row in enumerate(rows, 1):
                username = str(row.get("username") or "غير معروف").strip().lstrip("@")
                banned_by = str(row.get("banned_by") or "غير معروف").strip().lstrip("@")
                when = str(row.get("at") or "").strip()
                line = f"{i}. المحظور: @{username} | المنفذ: @{banned_by}"
                if when:
                    line += f" | {when}"
                lines.append(line)

            self.send_room_text(room, "\n".join(lines))
            return True

        if str(text or "").strip().casefold() == "l@b":
            if not _is_master_name(sender):
                self.send_private_text(sender, "🔒 قائمة حظر البوت مخصصة للماستر.")
                return True
            if not room:
                self.send_private_text(sender, "⚠️ نفّذ l@b داخل الغرفة المطلوبة.")
                return True
            if getattr(self, "ws", None) and self._request_room_list_via_ws(room, "b", sender):
                return True
            room_key = _norm_room(room)
            rows = [r for r in _bot_ban_rows() if isinstance(r, dict) and _norm_room(r.get("room")) == room_key]
            users=[{"username":r.get("username"),"role":"outcast"} for r in rows]
            self._send_private_list(sender, f"🚫 المحظورون — {room}", users, f"📭 لا يوجد مستخدمون محظورون في غرفة {room}.")
            return True
        m=re.fullmatch(r"is@(.+)", str(text or "").strip(), re.I)
        if m:
            target=m.group(1).strip().lstrip("@")
            if not getattr(getattr(self, "db", None), "client", None):
                if self._start_presence_native(target, sender, room):
                    self.send_private_text(sender, f"⏳ جارٍ فحص وجود @{target} في الغرف...")
                    return True
            rows=[]
            for r in self._settings_room_users(room) if room else []:
                if _norm_user(r.get("username")) == _norm_user(target): rows.append((room, r))
            for candidate in self._active_rooms():
                if _norm_room(candidate).casefold() == _norm_room(room).casefold(): continue
                for r in self._settings_room_users(candidate):
                    if _norm_user(r.get("username")) == _norm_user(target): rows.append((candidate,r))
            if not rows:
                self.send_private_text(sender, f"📭 @{target} غير موجود في قوائم الغرف المحفوظة.")
            else:
                lines=[f"📍 حالة @{target} ({len(rows)} غرفة):"]
                for name,r in rows:
                    present=r.get("is_present", r.get("online"))
                    state="🟢 متصل" if present is True or str(present).lower() in {"true","1","online"} else "⚪ غير متصل"
                    lines.append(f"• {name}: {state}")
                self.send_private_text(sender,"\n".join(lines))
            return True
        return False

    def _process_room_list(self, rooms):
        """Import the complete server room list and finish an explicit sync."""
        found = set()
        def walk(obj):
            if isinstance(obj, dict):
                # decode_generic exposes protobuf RoomItem fields as numeric
                # keys. The Android app reads the name field from these items,
                # while the old extractor only understood JSON-style keys and
                # consequently missed most rooms returned by load_list_new.
                numeric = {int(k): v for k, v in obj.items() if str(k).isdigit()}
                if numeric:
                    v1 = numeric.get(1)
                    v2 = numeric.get(2)
                    t1 = str(v1 or "").strip() if isinstance(v1, str) else ""
                    t2 = str(v2 or "").strip() if isinstance(v2, str) else ""
                    if t1 and not t1.startswith(("http://", "https://")):
                        found.add(t1)
                    elif t2 and not t2.startswith(("http://", "https://")):
                        found.add(t2)
                for k, v in obj.items():
                    kl = str(k).casefold()
                    if kl in ("name", "room", "room_name", "title") and isinstance(v, str) and v.strip():
                        found.add(v.strip())
                    walk(v)
            elif isinstance(obj, list):
                for v in obj: walk(v)
        walk(rooms)
        blocked = getattr(self, "blocked_rooms", set())
        found = {room for room in found if _norm_room(room) not in blocked}
        # An empty list is still a real server response. Finish an explicit
        # sync immediately instead of misreporting a timeout later.
        if not found:
            pending = getattr(self, "_pending_full_room_sync", None)
            if isinstance(pending, dict):
                self._finish_full_room_sync(
                    [], from_server=True,
                    join_after=bool(pending.get("join_after")),
                )
            return
        self.log("[ROOM-LIST] loaded", len(found), "rooms")
        self._server_room_catalog = sorted(found, key=str.casefold)
        # Keep every server-returned room so reconnect and `غرفي` use the
        # authoritative list instead of the truncated local view.
        self.known_rooms.update(found)
        _save_persistent_rooms(self.known_rooms)
        if isinstance(getattr(self, "_pending_full_room_sync", None), dict):
            pending = self._pending_full_room_sync
            self._finish_full_room_sync(
                sorted(found, key=str.casefold), from_server=True,
                join_after=bool(pending.get("join_after")),
            )

    def _send_paginated_text_pages(self, sender, room, packet_type, pages, route_fields, language="ar"):
        """Store a result list for `ns` and send its first safe page."""
        if not pages:
            return False
        result_pages = getattr(self, "_result_pages", {})
        if not isinstance(result_pages, dict):
            result_pages = {}
        key_room = "" if packet_type == "chat_message" else str(room or "")
        key = (packet_type, key_room, str(sender or ""))
        if len(pages) > 1:
            result_pages[key] = {
                "pages": pages, "part": 1, "kwargs": dict(route_fields or {}),
                "language": language,
            }
        else:
            result_pages.pop(key, None)
        self._result_pages = result_pages

        english = str(language or "ar").casefold() == "en"
        if len(pages) > 1:
            footer = "\n\n📌 Type ns for the next list." if english else "\n\n📌 اكتب ns للقائمة التالية."
        else:
            footer = "\n\n✅ End of list." if english else "\n\n✅ انتهت القوائم."
        message = pages[0] + footer
        if packet_type == "chat_message":
            if getattr(self, "ws", None):
                return self._send_text_packets(packet_type, message, **dict(route_fields or {}))
            return self.send_private_text(sender, message)
        if getattr(self, "ws", None):
            return self._send_text_packets(packet_type, message, **dict(route_fields or {}))
        return self.send_room_text(room, message)

    def _send_room_names_pages(self, sender, room, rooms, from_server=False, language=None, live_only=False):
        """Display all room names with safe pagination."""
        names = sorted({str(item).strip() for item in rooms or [] if str(item).strip()}, key=str.casefold)
        language = language or self._effective_room_language(room)
        if not names:
            if live_only:
                text = "📭 لا توجد غرف متصلة فعليًا بهذه الجلسة الآن." if language == "ar" else "📭 No rooms are currently connected to this session."
            else:
                text = "📭 لم تصل قائمة غرف من السيرفر ولا توجد غرف في الملف." if language == "ar" else "📭 The server returned no rooms and the rooms file is empty."
            if getattr(self, "ws", None):
                self._send_text_packets("chat_message", text, to=str(sender or ""))
            else:
                self.send_private_text(sender, text)
            return
        if live_only:
            title = f"📡 الغرف المتصلة الآن فعليًا ({len(names)})" if language == "ar" else f"📡 Rooms currently connected ({len(names)})"
        else:
            title = (f"🏠 غرف البوت الكاملة ({len(names)}) — من السيرفر" if from_server else f"🏠 غرف البوت — من ملف الغرف") if language == "ar" else (f"🏠 Complete bot rooms ({len(names)}) — server" if from_server else f"🏠 Bot rooms — rooms file")
        reserve = "\n📄 القائمة 999/999\n\n📌 اكتب ns للقائمة التالية." if language == "ar" else "\n📄 List 999/999\n\n📌 Type ns for the next list."

        def fits(body):
            return len(encode_query("chat_message", type_="text", to=str(sender or ""), body=body + reserve)) <= min(_ws_payload_limit(), SAFE_COMMAND_REPLY_BYTES)

        page_bodies = []
        current = [title]
        for index, name in enumerate(names, 1):
            line = f"{index}. {name}"
            if len(current) > 1 and (len(current) > 30 or not fits("\n".join(current + [line]))):
                page_bodies.append("\n".join(current))
                current = [title, line]
            else:
                current.append(line)
        if len(current) > 1:
            page_bodies.append("\n".join(current))
        total = len(page_bodies)
        pages = [f"{body}\n📄 القائمة {page_no}/{total}" if language == "ar" else f"{body}\n📄 List {page_no}/{total}"
                 for page_no, body in enumerate(page_bodies, 1)]
        key = ("chat_message", "", str(sender or ""))
        result_pages = getattr(self, "_result_pages", {})
        if not isinstance(result_pages, dict):
            result_pages = {}
        result_pages[key] = {"pages": pages, "part": 1, "kwargs": {"to": str(sender or "")}, "language": language}
        self._result_pages = result_pages
        first = pages[0]
        first += "\n\n📌 اكتب ns للقائمة التالية." if total > 1 and language == "ar" else ("\n\n📌 Type ns for the next list." if total > 1 else "\n\n✅ انتهت القوائم.")
        if total == 1:
            result_pages.pop(key, None)
            self._result_pages = result_pages
        if getattr(self, "ws", None):
            self._send_text_packets("chat_message", first, to=str(sender or ""), notify_oversize=False)
        else:
            self.send_private_text(sender, first)

    def _finish_full_room_sync(self, rooms, from_server=False, join_after=False):
        state = getattr(self, "_pending_full_room_sync", None)
        if not isinstance(state, dict):
            return False
        self._pending_full_room_sync = None
        blocked = getattr(self, "blocked_rooms", set())
        rooms = sorted({str(item).strip() for item in rooms or [] if str(item).strip() and _norm_room(item) not in blocked}, key=str.casefold)
        if not from_server and not rooms:
            rooms = sorted({str(item).strip() for item in getattr(self, "known_rooms", set()) if str(item).strip() and _norm_room(item) not in blocked}, key=str.casefold)
        self._server_room_catalog = list(rooms)
        if rooms:
            if from_server:
                # For an explicit server sync, replace the stale/truncated
                # local view with the authoritative list returned by the app
                # endpoint. Reconnect then restores the same complete set.
                self.known_rooms = set(rooms)
            else:
                self.known_rooms.update(rooms)
            _save_persistent_rooms(self.known_rooms)
        sender = state.get("sender", "")
        if from_server:
            self.send_private_text(sender, f"✅ تم جلب غرف السيرفر بنجاح\n📊 عدد الغرف: {len(rooms)} غرفة")
        self._send_room_names_pages(
            sender, state.get("room", ""), rooms,
            from_server=from_server, language=state.get("language"),
        )
        if from_server and join_after and rooms:
            self.send_private_text(sender, f"🚪 بدأ الدخول بالتتالي ({len(rooms)} غرفة)...")
            self._start_server_room_join(rooms, sender)
        elif from_server:
            if not rooms:
                self.send_private_text(sender, "❌ السيرفر لم يُرجع أي غرفة.")
        return True

    def _request_full_room_sync(self, sender, room="", language=None, join_after=False):
        """Request the server room catalog; joining is optional and explicit."""
        self.send_private_text(sender, "⏳ جاري جلب قوائم الغرف من السيرفر...")
        if not getattr(self, "ws", None):
            self._server_room_catalog = []
            self.send_private_text(sender, "❌ تعذر جلب غرف السيرفر: لا يوجد اتصال WebSocket بالسيرفر.")
            return False
        token = uuid.uuid4().hex
        self._pending_full_room_sync = {
            "token": token, "sender": str(sender or ""), "room": str(room or ""),
            "language": language or self._effective_room_language(room), "join_after": bool(join_after),
        }
        try:
            self.send_query(encode_query("load_list_new"))
            self.log("[ROOM-SYNC] requested complete room list from server")
        except Exception as exc:
            self.log("[ROOM-SYNC] server request failed:", repr(exc))
            self._pending_full_room_sync = None
            self._server_room_catalog = []
            self.send_private_text(sender, "❌ تعذر إرسال طلب قائمة غرف السيرفر.")
            return False

        def fallback():
            state = getattr(self, "_pending_full_room_sync", None)
            if isinstance(state, dict) and state.get("token") == token:
                self.log("[ROOM-SYNC] server response timeout; no local fallback for explicit server fetch")
                self._pending_full_room_sync = None
                self._server_room_catalog = []
                self.send_private_text(sender, "❌ لم يرد السيرفر بقائمة الغرف ضمن المهلة. لم يتم استخدام ملف الغرف.")
        threading.Timer(max(2.0, float(os.getenv("ROOM_LIST_SYNC_TIMEOUT_SECONDS", "4") or 4)), fallback).start()
        return True
