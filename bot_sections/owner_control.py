"""Owner support tools, service commands, room-role checks and permissions."""

class OwnerControlSection:
    def _master_service_menu(self, username: str):
        """Greeting/menu shown once per private conversation with the master."""
        username = str(username or "").strip().lstrip("@")
        return (
            f"مرحبا عزيزي @{username}\n"
            "الماستر نائم الآن\n"
            "كيف يمكنني خدمتك؟\n"
            "\u20661.\u2069 توثيق\n"
            "\u20662.\u2069 شكاوي أو مقترحات\n"
            "\u20663.\u2069 توثيق لحساب اخر\n"
            "\u20664.\u2069 طلب توثيق VIP\n"
            "ارسل رقم 1 او 2 او 3 او 4"
        )

    def _offline_support_menu(self, username: str = ""):
        return self._master_service_menu(username)

    def _handle_master_process_command(self, sender: str, body: str, is_private: bool = False, room: str = ""):
        """Control the standalone master account from private or room chat."""
        if not _is_master_name(sender):
            return False
        low = str(body or "").strip().casefold()
        if low in ("تشغيل الماستر", "تشغيل الماستر@", "تشغيل الخدمة", "تشغيل الخدمه", "تشغيل", "start master", "master on"):
            before = _master_process_running()
            ok, msg = _start_master_process()
            if is_private or not room:
                self.send_private_text(sender, msg)
            else:
                self.send_room_text(room, msg)
            # Record only a real state change. A repeated تشغيل while already
            # running is reported as already running and does not replace .u.
            if ok and not before and not getattr(self, "_replaying_bot_action", False):
                self._remember_bot_action(self.room, body, sender, is_private=True)
            return True
        if low in ("ايقاف الماستر", "إيقاف الماستر", "ايقاف الماستر@", "إيقاف الماستر@", "إيقاف الخدمة", "ايقاف الخدمة", "إيقاف الخدمه", "ايقاف الخدمه", "إيقاف", "ايقاف", "stop master", "master off"):
            before = _master_process_running()
            ok, msg = _stop_master_process()
            if is_private or not room:
                self.send_private_text(sender, msg)
            else:
                self.send_room_text(room, msg)
            if ok and before and not getattr(self, "_replaying_bot_action", False):
                self._remember_bot_action(self.room, body, sender, is_private=True)
            return True
        if low in ("حالة الماستر", "حاله الماستر", "حالة الخدمة", "حاله الخدمة", "حالة الخدمه", "حاله الخدمه", "master status"):
            self.send_private_text(sender, "🟢 الخدمة مشغلة." if _master_process_running() else "🔴 الخدمة متوقفة.")
            return True
        return False

    def _handle_offline_master_service(self, sender: str, body: str):
        """Handle private service requests only while BOT_MASTER is absent."""
        sender = str(sender or "").strip()
        body = str(body or "").strip()
        if not sender or _norm_user(sender) in {_norm_user(BOT_MASTER), _norm_user(BOT_ID)}:
            return False
        sessions = getattr(self, "_offline_support_sessions", None)
        if not isinstance(sessions, dict):
            sessions = {}
            self._offline_support_sessions = sessions
        recent = getattr(self, "_offline_support_recent", None)
        if not isinstance(recent, dict):
            recent = {}
            self._offline_support_recent = recent
        key = _norm_user(sender)
        state = sessions.get(key, "")
        low = body.casefold()

        # Talkin may deliver the same private frame more than once. Ignore an
        # identical sender/message pair briefly so menus and acknowledgements
        # are never duplicated.
        signature = (key, body)
        now = time.time()
        if now - float(recent.get(signature, 0.0) or 0.0) < 15:
            return True
        recent[signature] = now

        if not state:
            sessions[key] = "menu"
            self.send_private_text(sender, self._master_service_menu(sender))
            return True

        if state == "complaint":
            if not body:
                self.send_private_text(sender, "❌ أرسل نص الشكوى أو المقترح.")
                return True
            if MASTER_SUPPORT_USERNAME:
                self.send_private_text(MASTER_SUPPORT_USERNAME, f"📩 شكوى أو مقترح من @{sender}:\n{body}")
            else:
                self.log("[OFFLINE-SUPPORT] MASTER_SUPPORT_USERNAME is not configured")
            self.send_private_text(sender, "✅ سيتم إبلاغ الإدارة ونبلغك قريباً.")
            sessions.pop(key, None)
            return True

        if low in ("2", "🟦2", "🟦\u20662.\u2069", "\u20662.\u2069", "شكوى", "شكاوي", "شكاوى", "مقترحات", "اقتراح"):
            sessions[key] = "complaint"
            self.send_private_text(sender, "✍️ تفضل أرسل الشكوى أو المقترح الآن.")
            return True

        if low in ("1", "🟦1", "🟦\u20661.\u2069", "\u20661.\u2069", "توثيق", "وثق", "التوثيق"):
            # The primary bot NEVER grants verification while the master is
            # away. Verification is an action performed by the master account.
            # Forward the request to the master/support account and leave the
            # verified_users store untouched until the master approves it.
            if MASTER_SUPPORT_USERNAME:
                self.send_private_text(
                    MASTER_SUPPORT_USERNAME,
                    f"📋 طلب توثيق جديد من @{sender}.\n"
                    f"يرجى توثيقه من حساب الماستر باستخدام: vi@{sender}"
                )
            sessions.pop(key, None)
            self.send_private_text(sender, "📩 تم إرسال طلب التوثيق إلى الماستر. سيتم توثيقك بعد موافقته.")
            return True

        # Do not repeat the menu for an unrecognized follow-up message.
        return True

    def _relay_private_to_owner(self, sender: str, body: str, media_url: str = ""):
        """Mirror master-account private traffic to the configured owner.

        MASTER_SUPPORT_USERNAME is the single owner/support account used for
        complaints, so it also receives private messages while the master
        service is running. Exclude the owner and local bot accounts to avoid
        forwarding loops and internal control chatter.
        """
        owner = str(MASTER_SUPPORT_USERNAME or "").strip()
        sender = str(sender or "").strip()
        if not owner or not sender:
            return False
        excluded = {_norm_user(owner), _norm_user(BOT_ID), _norm_user(PRIMARY_BOT_ID)}
        if _norm_user(sender) in excluded:
            return False
        parts = [f"📨 رسالة خاصة من @{sender}:"]
        if body:
            parts.append(str(body))
        if media_url:
            parts.append(f"📎 وسائط: {media_url}")
        return bool(self.send_private_text(owner, "\n".join(parts)))

    def _handle_master_account_service(self, sender: str, body: str):
        """Private auto-service handled by the master account itself.

        Verification is requested through the primary bot with ``vi@username``.
        The primary bot's reply is then relayed privately to the original user.
        Duplicate Talkin frames are ignored briefly so a single choice produces
        a single reply.
        """
        if not MASTER_SERVICE_ENABLED:
            return False
        sender = str(sender or "").strip()
        body = str(body or "").strip()
        if not sender:
            return False

        sessions = getattr(self, "_master_service_sessions", None)
        if not isinstance(sessions, dict):
            sessions = {}
            self._master_service_sessions = sessions
        pending = getattr(self, "_master_verify_pending", None)
        if not isinstance(pending, dict):
            pending = {}
            self._master_verify_pending = pending
        recent = getattr(self, "_master_service_recent", None)
        if not isinstance(recent, dict):
            recent = {}
            self._master_service_recent = recent

        key = _norm_user(sender)
        low = body.casefold()
        signature = (key, body)
        now = time.time()
        if now - float(recent.get(signature, 0.0) or 0.0) < 15:
            return True
        recent[signature] = now
        # Prevent this cache from growing forever.
        if len(recent) > 500:
            cutoff = now - 30
            for k, ts in list(recent.items()):
                if ts < cutoff:
                    recent.pop(k, None)

        # Result returned by the primary bot after the master sent vi@target.
        # Do not depend on extracting the username with a restrictive regex:
        # Talkin usernames can contain unusual Unicode/combining characters.
        if PRIMARY_BOT_ID and _norm_user(sender) == _norm_user(PRIMARY_BOT_ID):
            body_low = body.casefold()
            matched_key = None
            matched_info = None
            for target_key, info in list(pending.items()):
                target = str(info.get("target", "") or "").strip()
                if target and _norm_user(target) in _norm_user(body):
                    matched_key = target_key
                    matched_info = info
                    break
            # Fallback for normal "@username" replies when target matching is
            # not possible for a particular Unicode username.
            if matched_info is None and pending:
                m = re.search(r"@(.+?)(?:\.|\n|$)", body, re.S)
                if m:
                    candidate = m.group(1).strip()
                    for target_key, info in list(pending.items()):
                        target = str(info.get("target", "") or "").strip()
                        if _norm_user(candidate) == _norm_user(target):
                            matched_key = target_key
                            matched_info = info
                            break

            if matched_info is not None:
                pending.pop(matched_key, None)
                requester = str(matched_info.get("requester", "") or "").strip()
                target = str(matched_info.get("target", "") or "").strip().lstrip("@")
                if requester:
                    if "موثق سابق" in body_low or "توثيق عادي بالفعل" in body_low:
                        reply = f"⚠️ @{target} حسابه موثق سابقا."
                    elif "vip" in body_low or "توثيق VIP بالفعل" in body_low:
                        reply = f"⚠️ @{target} لديه توثيق VIP بالفعل."
                    elif "تم توثيق" in body_low:
                        reply = f"✅ تم توثيق @{target} بنجاح.\n🎉 يمكنك الآن استخدام أوامر البوت."
                    else:
                        reply = body
                    # Final notification is always sent by the master account
                    # directly to the verified user, not to a room.
                    self.send_private_text(requester, reply)
                    owner = str(MASTER_SUPPORT_USERNAME or "").strip()
                    if owner and _norm_user(owner) not in {_norm_user(requester), _norm_user(BOT_ID)}:
                        self.send_private_text(owner, f"✅ تم توثيق @{target} بنجاح.\n📩 مقدم الطلب: @{requester}")
                return True
            # Any private message from the primary bot is internal; never show
            # it to other users and never fall through to the service menu.
            return True

        if _norm_user(sender) == _norm_user(BOT_ID):
            return False

        # Option 1: verify the sender's own account.
        if low in ("1", "🟦1", "🟦\u20661.\u2069", "\u20661.\u2069", "توثيق", "وثق", "التوثيق"):
            target_bot = PRIMARY_BOT_ID.strip()
            if not target_bot:
                self.send_private_text(sender, "❌ لم يتم ضبط PRIMARY_BOT_ID للبوت الأساسي.")
                return True
            target_key = _norm_user(sender)
            pending[target_key] = {
                "requester": sender,
                "target": sender,
                "created_at": now,
            }
            if not self.send_private_text(target_bot, f"vi@{sender}"):
                pending.pop(target_key, None)
                self.send_private_text(sender, "❌ تعذر تنفيذ التوثيق الآن، حاول مرة أخرى.")
            return True

        # Option 2: complaint/suggestion. One prompt only; the actual complaint
        # is sent privately to MASTER_SUPPORT_USERNAME and not to the master
        # account unless that username is explicitly the same account.
        if low in ("2", "🟦2", "🟦\u20662.\u2069", "\u20662.\u2069", "شكوى", "شكاوي", "شكاوى", "مقترحات", "اقتراح"):
            sessions[key] = "complaint"
            self.send_private_text(sender, "✍️ تفضل أرسل الشكوى أو المقترح الآن.")
            return True

        if sessions.get(key) == "complaint":
            if not body:
                # Empty frames are silently ignored; do not spam the user.
                return True
            support = str(MASTER_SUPPORT_USERNAME or "").strip()
            sent = False
            if support and _norm_user(support) != _norm_user(BOT_ID):
                sent = bool(self.send_private_text(
                    support,
                    f"📩 شكوى أو مقترح من @{sender}:\n{body}"
                ))
            sessions.pop(key, None)
            if sent:
                self.send_private_text(sender, "✅ تم استلام الشكوى أو المقترح.")
            else:
                self.send_private_text(sender, "❌ تعذر إرسال الشكوى أو المقترح للإدارة الآن.")
            return True

        # Option 3: verify another account on behalf of the requester.
        if low in ("3", "🟦3", "🟦\u20663.\u2069", "\u20663.\u2069", "توثيق لحساب اخر", "توثيق لحساب آخر", "وثق حساب اخر", "وثق حساب آخر"):
            sessions[key] = "verify_other"
            self.send_private_text(sender, "👤 أرسل اسم المستخدم الذي تريد توثيقه الآن.")
            return True

        # Option 4: request VIP verification for another account. The master
        # forwards the requested username to the configured administration
        # account; no VIP grant is performed automatically.
        if low in ("4", "🟦4", "🟦\u20664.\u2069", "\u20664.\u2069", "طلب توثيق vip", "توثيق vip"):
            sessions[key] = "verify_vip"
            self.send_private_text(sender, "👑 أرسل اسم المستخدم المراد توثيقه VIP الآن.")
            return True

        if sessions.get(key) == "verify_vip":
            target = body.strip().lstrip("@").split()[0] if body else ""
            if not target:
                return True
            owner = str(MASTER_SUPPORT_USERNAME or "").strip()
            if owner:
                self.send_private_text(owner, f"👑 طلب توثيق VIP من @{sender}\n👤 الحساب المطلوب: @{target}")
                self.send_private_text(sender, "✅ تم إرسال طلب توثيق VIP إلى الإدارة.")
            else:
                self.send_private_text(sender, "❌ إدارة الماستر غير مضبوطة حالياً.")
            sessions.pop(key, None)
            return True

        if sessions.get(key) == "verify_other":
            target = body.strip().lstrip("@").split()[0] if body else ""
            if not target:
                # Empty frames are silently ignored; do not repeat the prompt.
                return True
            target_bot = PRIMARY_BOT_ID.strip()
            if not target_bot:
                sessions.pop(key, None)
                self.send_private_text(sender, "❌ لم يتم ضبط PRIMARY_BOT_ID للبوت الأساسي.")
                return True
            target_key = _norm_user(target)
            pending[target_key] = {
                "requester": sender,
                "target": target,
                "created_at": now,
            }
            if not self.send_private_text(target_bot, f"vi@{target}"):
                pending.pop(target_key, None)
                self.send_private_text(sender, "❌ تعذر تنفيذ التوثيق الآن، حاول مرة أخرى.")
                return True
            sessions.pop(key, None)
            return True

        # First message only: show the menu. Do not repeat it on every message.
        if not sessions.get(key):
            sessions[key] = "menu"
            self.send_private_text(sender, self._master_service_menu(sender))
        return True

    def reply_text(self, room: str, text: str, private_to: str = ""):
        return self.send_private_text(private_to, text) if private_to else self.send_room_text(room, text)

    def _bot_room_role(self, room):
        room = str(room or "").strip()
        role = str(getattr(self, "bot_room_roles", {}).get(_norm_room(room), "") or "").casefold().strip()
        if role:
            role = role.replace(" ", "_")
            return {"o": "owner", "a": "admin", "m": "member", "roomowner": "room_owner", "roomcreator": "room_creator"}.get(role, role)
        live = getattr(self, "room_users", {}).get(room, {}) or {}
        bot_keys = {_norm_user(BOT_ID), _norm_user(PRIMARY_BOT_ID), _norm_user(getattr(self, "bot_id", ""))}
        bot_keys.discard("")
        for username, value in live.items():
            if _norm_user(username) in bot_keys:
                role = str(value or "").casefold().strip().replace(" ", "_")
                return {"o": "owner", "a": "admin", "m": "member", "roomowner": "room_owner", "roomcreator": "room_creator"}.get(role, role)
        return ""

    def _handle_user_room_rank_command(self, room, username, text):
        """Fetch the command sender's actual role from the current room roster."""
        command = str(text or "").strip().casefold().rstrip("؟?!.")
        if command not in {"my rank", "rank"}:
            return False
        room = str(room or "").strip()
        requester = str(username or "").strip().lstrip("@")
        if not room or not requester:
            return True
        if not getattr(self, "ws", None):
            self.send_room_text(room, f"⚠️ لا أستطيع التحقق من رتبتك في غرفة «{room}» لأن الاتصال بالخادم غير متاح.")
            return True

        room_key = _norm_room(room)
        if not hasattr(self, "_pending_room_lists"):
            self._pending_room_lists = {}
        if room_key in self._pending_room_lists:
            self.send_room_text(room, f"⏳ يوجد طلب قائمة جارٍ في غرفة «{room}». حاول «رتبتك» بعد اكتماله.")
            return True
        if not hasattr(self, "_pending_user_room_rank_checks"):
            self._pending_user_room_rank_checks = {}
        if room_key in self._pending_user_room_rank_checks:
            self.send_room_text(room, f"⏳ ما زلت أتحقق من رتبة @{requester} في غرفة «{room}».")
            return True

        self._pending_user_room_rank_checks[room_key] = {
            "room": room, "requester": requester, "started_at": time.time(),
            "request_mode": "app",
        }
        self.send_room_text(room, f"🔎 أتحقق من رتبة @{requester} الحالية في غرفة «{room}» من الخادم...")
        if not self._request_user_room_rank(room_key):
            if not self._request_user_room_rank(room_key, fallback=True):
                self._finish_user_room_rank_check(room_key, missing=True)
        return True

    def _request_user_room_rank(self, room_key, *, fallback=False):
        state = getattr(self, "_pending_user_room_rank_checks", {}).get(room_key)
        if not isinstance(state, dict):
            return False
        mode = "legacy" if fallback else "app"
        request_id = secrets.token_urlsafe(15)[:20]
        state["request_id"] = request_id
        state["request_mode"] = mode
        self._pending_room_lists[room_key] = {
            "room": state["room"], "code": "user_room_rank",
            "sender": state["requester"], "request_type": "occupants_list",
            "request_id": request_id, "request_mode": mode,
            "requested_at": time.time(), "complete_users": True,
            "user_room_rank_check": True,
        }
        try:
            query_args = {
                "type_": "occupants_list", "room": state["room"],
                "to": BOT_ID if fallback else "username", "value": "none",
            }
            if fallback:
                query_args["id_"] = request_id
            sent = self.send_query(encode_query("room_admin", **query_args))
            if sent is False:
                raise RuntimeError("occupants query was not sent")
        except Exception as exc:
            self._pending_room_lists.pop(room_key, None)
            self.log("[USER-ROOM-RANK] server query failed:", repr(exc))
            return False

        old_timer = state.get("timer")
        if old_timer:
            try:
                old_timer.cancel()
            except Exception:
                pass
        timer = threading.Timer(
            12.0, self._user_room_rank_timeout, args=(room_key, request_id)
        )
        timer.daemon = True
        state["timer"] = timer
        timer.start()
        return True

    def _fallback_user_room_rank(self, room_key, request_id):
        state = getattr(self, "_pending_user_room_rank_checks", {}).get(room_key)
        if (
            not isinstance(state, dict)
            or state.get("request_id") != request_id
            or state.get("request_mode") != "app"
        ):
            return False
        pending = getattr(self, "_pending_room_lists", {}).get(room_key)
        if isinstance(pending, dict) and pending.get("request_id") == request_id:
            self._pending_room_lists.pop(room_key, None)
        self.log("[USER-ROOM-RANK] retrying legacy occupants request for", state.get("requester"))
        return self._request_user_room_rank(room_key, fallback=True) or self._finish_user_room_rank_check(room_key, missing=True)

    def _user_room_rank_timeout(self, room_key, request_id):
        state = getattr(self, "_pending_user_room_rank_checks", {}).get(room_key)
        if not isinstance(state, dict) or state.get("request_id") != request_id:
            return False
        if state.get("request_mode") == "app":
            return self._fallback_user_room_rank(room_key, request_id)
        pending = getattr(self, "_pending_room_lists", {}).get(room_key)
        if isinstance(pending, dict) and pending.get("request_id") == request_id:
            self._pending_room_lists.pop(room_key, None)
        return self._finish_user_room_rank_check(room_key, missing=True)

    def _finish_user_room_rank_check(self, room_key, role="", *, missing=False):
        state = getattr(self, "_pending_user_room_rank_checks", {}).pop(room_key, None)
        if not isinstance(state, dict):
            return False
        timer = state.get("timer")
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass
        room = state.get("room", "")
        requester = state.get("requester", "")
        role = str(role or "").casefold().strip().replace(" ", "_")
        role = {
            "o": "owner", "a": "admin", "m": "member",
            "none": "member", "normal": "member", "user": "member",
            "roomowner": "room_owner", "roomcreator": "room_creator",
        }.get(role, role)
        labels = {
            "owner": "أونر (Owner)", "creator": "منشئ الغرفة (Creator)",
            "room_owner": "أونر (Room Owner)", "room_creator": "منشئ الغرفة (Room Creator)",
            "host": "مالك الغرفة (Host)", "admin": "مشرف (Admin)",
            "moderator": "مشرف (Moderator)", "mod": "مشرف (Moderator)",
            "member": "عضو (Member)", "outcast": "محظور (Outcast)",
            "banned": "محظور (Banned)",
        }
        if missing or not role:
            self.send_room_text(
                room,
                f"⚠️ لم يتضمن رد الخادم رتبة @{requester} في غرفة «{room}»؛ لذلك لم أعرض رتبة مخزنة قديمة."
            )
            return True
        self.send_room_text(
            room, f"✅ رتبة @{requester} في غرفة «{room}»: {labels.get(role, role)}.\n"
                  "📡 تم التحقق من رد الخادم الحالي."
        )
        return True

    def _handle_bot_room_role_command(self, room, username, text):
        """Request current room data once and report only this bot's server role."""
        command = str(text or "").strip().casefold().rstrip("؟?!.")
        if command not in {"رتبتك", "رتبتي", "bot rank", "your rank", "رتبة البوت"}:
            return False
        room = str(room or "").strip()
        if not room:
            return False
        room_key = _norm_room(room)
        if not getattr(self, "ws", None):
            self.send_room_text(room, f"⚠️ لا أستطيع التحقق من رتبة البوت في غرفة «{room}» لأن الاتصال بالخادم غير متاح.")
            return True

        pending_lists = getattr(self, "_pending_room_lists", {})
        if room_key in pending_lists:
            self.send_room_text(room, f"⏳ يوجد طلب قوائم جارٍ في غرفة «{room}». حاول أمر «رتبتك» بعد اكتماله.")
            return True
        if not hasattr(self, "_pending_room_lists"):
            self._pending_room_lists = {}
        if not hasattr(self, "_pending_bot_room_role_checks"):
            self._pending_bot_room_role_checks = {}
        if room_key in self._pending_bot_room_role_checks:
            self.send_room_text(room, f"⏳ ما زلت أتحقق من رتبتي في غرفة «{room}».")
            return True

        self._pending_bot_room_role_checks[room_key] = {
            "room": room,
            "requester": str(username or "").strip(),
            "started_at": time.time(),
            "request_mode": "app",
        }
        self.send_room_text(room, f"🔎 أتحقق الآن من الرتبة الفعلية للبوت في غرفة «{room}» من الخادم...")
        if not self._request_bot_room_role(room_key):
            if not self._request_bot_room_role(room_key, fallback=True):
                self._abort_bot_room_role_check(room_key)
        return True

    def _request_bot_room_role(self, room_key, *, fallback=False):
        state = getattr(self, "_pending_bot_room_role_checks", {}).get(room_key)
        if not isinstance(state, dict):
            return False
        mode = "legacy" if fallback else "app"
        request_id = secrets.token_urlsafe(15)[:20]
        state["request_id"] = request_id
        state["request_mode"] = mode
        self._pending_room_lists[room_key] = {
            "room": state["room"],
            "code": "bot_role",
            "sender": state.get("requester", ""),
            "request_type": "occupants_list",
            "request_id": request_id,
            "request_mode": mode,
            "requested_at": time.time(),
            "complete_users": True,
            "bot_role_check": True,
        }
        try:
            # Match the official app's occupants request first. If the response
            # omits this bot, retry with the former BOT_ID-targeted request.
            query_args = {
                "type_": "occupants_list",
                "room": state["room"],
                "to": BOT_ID if fallback else "username",
                "value": "none",
            }
            if fallback:
                query_args["id_"] = request_id
            sent = self.send_query(encode_query("room_admin", **query_args))
            if sent is False:
                raise RuntimeError("room role query was not sent")
        except Exception as exc:
            self._pending_room_lists.pop(room_key, None)
            self.log("[ROOM-ROLE] server query failed", state.get("room"), repr(exc))
            return False

        old_timer = state.get("timer")
        if old_timer:
            try:
                old_timer.cancel()
            except Exception:
                pass
        timer = threading.Timer(12.0, self._bot_room_role_check_timeout, args=(room_key, request_id))
        timer.daemon = True
        state["timer"] = timer
        timer.start()
        return True

    def _fallback_bot_room_role_check(self, room_key, request_id):
        state = getattr(self, "_pending_bot_room_role_checks", {}).get(room_key)
        if (
            not isinstance(state, dict)
            or state.get("request_id") != request_id
            or state.get("request_mode") != "app"
        ):
            return False
        pending = getattr(self, "_pending_room_lists", {}).get(room_key)
        if isinstance(pending, dict) and pending.get("request_id") == request_id:
            self._pending_room_lists.pop(room_key, None)
        self.log(
            "[ROOM-ROLE] app-style occupants query had no bot role; retrying legacy BOT_ID query",
            state.get("room"),
        )
        if self._request_bot_room_role(room_key, fallback=True):
            return True
        return self._abort_bot_room_role_check(room_key)

    def _abort_bot_room_role_check(self, room_key):
        state = getattr(self, "_pending_bot_room_role_checks", {}).pop(room_key, None)
        pending = getattr(self, "_pending_room_lists", {}).get(room_key)
        if isinstance(pending, dict) and pending.get("bot_role_check"):
            self._pending_room_lists.pop(room_key, None)
        if isinstance(state, dict) and state.get("timer"):
            try:
                state["timer"].cancel()
            except Exception:
                pass
        if isinstance(state, dict):
            room = state.get("room", "")
            self.send_room_text(room, f"❌ تعذر إكمال التحقق من رتبة البوت من الخادم في غرفة «{room}».")
        return False

    def _bot_room_role_check_timeout(self, room_key, request_id):
        state = getattr(self, "_pending_bot_room_role_checks", {}).get(room_key)
        if not isinstance(state, dict) or state.get("request_id") != request_id:
            return False
        if state.get("request_mode") == "app":
            return self._fallback_bot_room_role_check(room_key, request_id)
        pending = getattr(self, "_pending_room_lists", {}).get(room_key)
        if isinstance(pending, dict) and pending.get("request_id") == request_id:
            self._pending_room_lists.pop(room_key, None)
        self._pending_bot_room_role_checks.pop(room_key, None)
        self.send_room_text(
            state.get("room", ""),
            f"⚠️ لم يصلني ردّ التحقق من الخادم لرتبتي في غرفة «{state.get('room', '')}». لم أعرض رتبة مخزنة قديمة.",
        )
        return True

    def _finish_bot_room_role_check(self, room_key, role="", *, missing=False):
        state = getattr(self, "_pending_bot_room_role_checks", {}).pop(room_key, None)
        if not isinstance(state, dict):
            return False
        timer = state.get("timer")
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass
        room = state.get("room", "")
        role = str(role or "").casefold().strip().replace(" ", "_")
        if missing or not role:
            getattr(self, "bot_room_roles", {}).pop(room_key, None)
            self.send_room_text(
                room,
                f"⚠️ لم يتضمن رد الخادم رتبة حساب البوت (@{BOT_ID}) في غرفة «{room}»؛ لذلك لا أستطيع تأكيد الرتبة الحالية.",
            )
            return True
        role = {
            "o": "owner", "a": "admin", "m": "member", "normal": "member", "user": "member",
            "roomowner": "room_owner", "roomcreator": "room_creator",
        }.get(role, role)
        role_labels = {
            "owner": "أونر (Owner)",
            "creator": "منشئ الغرفة (Creator)",
            "room_owner": "أونر (Room Owner)",
            "room_creator": "منشئ الغرفة (Room Creator)",
            "host": "مالك الغرفة (Host)",
            "admin": "مشرف (Admin)",
            "moderator": "مشرف (Moderator)",
            "mod": "مشرف (Moderator)",
            "member": "عضو (Member)",
        }
        roles = getattr(self, "bot_room_roles", None)
        if not isinstance(roles, dict):
            roles = self.bot_room_roles = {}
        roles[room_key] = role
        self.send_room_text(
            room,
            f"✅ رتبتي في غرفة «{room}»: {role_labels.get(role, role)}.\n📡 تم التحقق من رتبة حساب البوت مباشرةً من رد الخادم.",
        )
        return True

    def _bot_is_room_owner(self, room):
        """Return True only when the cached role is an actual room Owner.

        Live broadcast is intentionally stricter than ordinary room actions:
        unknown, member, admin, or moderator roles must not start audio.  The
        occupants response populates ``bot_room_roles`` after joining, so this
        check stays local and does not add a roster request to every command.
        """
        role = self._bot_room_role(room)
        return role in {
            "owner", "creator", "room_owner", "room_creator", "host",
            "مالك", "اونر", "أونر", "صانع", "صانع_الغرفة",
        }
