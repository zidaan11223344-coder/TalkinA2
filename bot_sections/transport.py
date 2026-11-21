"""Talkin protocol, connection heartbeat, room joins, message transport, chunking and broadcasts."""

class TransportSection:
    def _send_oversize_notice(self, packet_type, target="", room="", username=""):
        """Tell the affected user/room after dropping an oversized packet."""
        target = str(target or "").strip()
        room = str(room or "").strip()
        username = str(username or target or "").strip().lstrip("@")
        notice = (
            f"👤 @{username} منفّذ الأمر: تم تجاهل رسالتك بسبب رد طويل."
            if username
            else "👤 منفّذ الأمر: تم تجاهل رسالتك بسبب رد طويل."
        )
        if packet_type == "chat_message" and target:
            payload = encode_query("chat_message", type_="text", to=target, body=notice)
        elif packet_type == "room_message" and room:
            payload = encode_query("room_message", type_="text", room=room, body=notice)
        else:
            return False
        if len(payload) > _ws_payload_limit():
            self.log("[WS_OVERSIZE] oversize notice did not fit", packet_type, len(payload))
            return False
        try:
            return bool(self.send_query(payload))
        except Exception as exc:
            self.log("[WS_OVERSIZE] notice failed:", repr(exc))
            return False

    def send_query(self, payload: bytes):
        if not self.ws:
            raise RuntimeError("WebSocket is not connected")

        # Never pass an oversized protobuf packet to RawWebSocket. The server
        # closes the whole connection with 1009, so handle it locally instead.
        max_bytes = _ws_payload_limit()
        payload = bytes(payload)
        if len(payload) > max_bytes:
            packet_type = ""
            target = ""
            room = ""
            try:
                fields = decode_message(payload)
                packet_type = as_text((fields.get(1) or [b""])[0])
                target = as_text((fields.get(4) or [b""])[0])
                room = as_text((fields.get(6) or [b""])[0])
            except Exception as exc:
                self.log("[WS_OVERSIZE] decode failed:", repr(exc))
            self._report_oversize_delivery(
                packet_type, target, room, len(payload),
                f"حزمة WebSocket تجاوزت الحد الصلب ({max_bytes} بايت)؛ أُسقطت محلياً"
            )
            # User-facing notices are emitted only by _send_text_packets,
            # which has the original command executor in its context. The
            # low-level transport must only drop/log this packet; notifying
            # here would create duplicate or context-free replies.
            return False

        # Keep a compact trace of the live protocol. This is intentionally
        # limited to stream actions and never logs credentials or media URLs.
        try:
            fields = decode_message(payload)
            action = str((fields.get(1) or [b""])[0], "utf-8", "ignore")
            if action in {STREAM_INVITE_ACTION, STREAM_ACCEPT_ACTION, STREAM_AUDIO_ACTION, "sent_invitation", "check_streaming"}:
                safe = {key: (str(values[0], "utf-8", "ignore")[:120] if isinstance(values[0], bytes) else str(values[0])[:120])
                        for key, values in fields.items() if key in {1, 2, 4, 6, 8, 9, 10, 11, 13, 18}}
                if 5 in fields:
                    safe[5] = f"<token:{len(fields[5][0]) if isinstance(fields[5][0], bytes) else len(str(fields[5][0]))}>"
                self.log("[STREAM_OUT]", safe)
        except Exception as exc:
            self.log("[STREAM_OUT] decode failed:", repr(exc))
        self.ws.send_binary(payload)
        return True

    def _start_heartbeat(self):
        """Keep the realtime socket alive while the room is idle.

        The server accepts RFC6455 control pings. The old loop only answered
        incoming pings, so an idle connection could be closed with code 1000.
        """
        self._stop_heartbeat()
        stop = threading.Event()
        self._heartbeat_stop = stop
        interval = max(10.0, float(os.getenv("WS_HEARTBEAT_SECONDS", "25")))

        def run():
            while not stop.wait(interval):
                ws = self.ws
                if not ws or not ws.sock:
                    return
                try:
                    ws.send_control(0x9, b"talkin-heartbeat")
                    self.log("[WS] heartbeat ping sent")
                except Exception as exc:
                    self.log("[WS] heartbeat failed:", repr(exc))
                    return

        self._heartbeat_thread = threading.Thread(target=run, name="ws-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _stop_heartbeat(self):
        stop = self._heartbeat_stop
        if stop:
            stop.set()
        self._heartbeat_stop = None
        self._heartbeat_thread = None

    def _mark_room_blocked(self, room: str, reason: str = "", persistent: bool = False):
        room = _norm_room(room)
        if not room:
            return
        if not hasattr(self, "blocked_rooms"):
            self.blocked_rooms = set()
        if persistent:
            self.blocked_rooms.add(room)
            self._save_blocked_rooms()
        if reason:
            self._blocked_room_reasons[room] = reason
        self.known_rooms = {r for r in self.known_rooms if _norm_room(r) != room}
        self.connected_rooms = {r for r in self.connected_rooms if _norm_room(r) != room}
        self.room_users.pop(room, None)
        _save_persistent_rooms(self.known_rooms)
        self.log("[ROOM] server rejected room; removed from tracked list:", room, reason)

    def _room_failure_message(self, room: str, event_type: str):
        event_type = str(event_type or "").replace("_rejoin", "").replace("room_full _rejoin", "room_full")
        labels = {
            "room_unauthorized": "🚫 البوت محظور من الغرفة",
            "room_membership_required": "🚫 الغرفة للأعضاء/تحتاج عضوية للبوت",
            "room_full": "⚠️ الغرفة ممتلئة ولا يمكن للبوت الدخول",
            "room_wrong_password": "🔐 الغرفة تحتاج كلمة مرور أو كلمة المرور غير صحيحة",
            "room_needs_password": "🔐 الغرفة تحتاج كلمة مرور",
            "room_needs_captcha": "🤖 الغرفة تطلب تحقق CAPTCHA ولا يمكن للبوت الدخول آلياً",
        }
        return labels.get(event_type, "❌ تعذر دخول البوت إلى الغرفة")

    def _join_timeout(self, room_norm: str):
        pending = self._pending_room_joins.pop(str(room_norm or ""), None)
        if not pending:
            return
        room = str(pending.get("room") or room_norm).strip()
        requester = str(pending.get("requested_by") or "").strip()
        connected = any(
            _norm_room(item) == _norm_room(room)
            for item in getattr(self, "connected_rooms", set())
            if str(item or "").strip()
        )
        if connected:
            # The live session state is authoritative. Never send a timeout
            # failure for a room that this bot has already entered.
            self.known_rooms.add(room)
            _save_persistent_rooms(self.known_rooms)
            if requester:
                self.send_private_text(requester, f"✅ تم دخول البوت إلى الغرفة: {room}")
            return
        if (not requester and any(
                _norm_room(saved).casefold() == _norm_room(room).casefold()
                for saved in getattr(self, "_auto_rejoin_waiting_ack", set())
        )):
            self.log("[AUTO_REJOIN] join acknowledgement timed out; no retry:", room)
            return
        self.known_rooms = {r for r in self.known_rooms if _norm_room(r) != _norm_room(room)}
        self.connected_rooms = {r for r in self.connected_rooms if _norm_room(r) != _norm_room(room)}
        _save_persistent_rooms(self.known_rooms)
        self.log("[ROOM] join confirmation timeout:", room)
        if requester:
            self.send_private_text(
                requester,
                f"❌ لم يتم دخول البوت إلى الغرفة: {room}. "
                "انتهت مهلة التأكيد ولم يظهر اتصال البوت بها؛ تحقق من صلاحياته ثم أعد المحاولة.",
            )

    def _handle_unconfirmed_bot_user_left(self, room):
        """A user_left confirms absence, not a kick; never notify or auto-rejoin here."""
        room_key = _norm_room(room)
        pending_leaves = getattr(self, "_pending_room_leaves", {})
        for pending_room, leave_ack in tuple(pending_leaves.items()):
            if _norm_room(pending_room).casefold() == room_key.casefold():
                pending_leaves.pop(pending_room, None)
                try:
                    leave_ack.set()
                except Exception:
                    pass
                break

        # غرفي must reflect current membership, so remove this room as soon as
        # the server says the bot has left, whatever the reason was.
        self.connected_rooms = {
            current for current in getattr(self, "connected_rooms", set())
            if _norm_room(current).casefold() != room_key.casefold()
        }
        self.room_users.pop(room, None)
        intentional = room_key in getattr(self, "_intentional_leaves", set())
        if intentional:
            self.log(f"[USER_LEFT_INTENTIONAL] Bot left {room} by its own command; no notice and no rejoin.")
        else:
            # Keep a short-lived membership marker: some server versions send
            # user_left BEFORE role_changed:kicked. Clearing connected_rooms
            # is correct for غرفي, but must not erase proof that the bot was
            # inside the room moments before the authoritative kick event.
            recent = getattr(self, "_recent_bot_room_departures", None)
            if not isinstance(recent, dict):
                recent = {}
                self._recent_bot_room_departures = recent
            recent[room_key] = time.time()
            cutoff = time.time() - 120
            for key, stamp in tuple(recent.items()):
                if float(stamp or 0) < cutoff:
                    recent.pop(key, None)
            # user_left alone is not enough to distinguish a kick from a
            # normal disconnect. Only role_changed:kicked triggers recovery.
            self.log(f"[USER_LEFT_UNCONFIRMED] Bot left {room}; waiting for server-confirmed role_changed:kicked.")

    def _send_master_room_departure_notice(self, room, event_type, reason="", actor=""):
        """Rate-limit private notices for unexpected room departures."""
        master = str(BOT_MASTER or "").strip().lstrip("@")
        if not master:
            return False
        now = time.monotonic()
        room_key = _norm_room(room).casefold()
        last = getattr(self, "_room_departure_notice_last", None)
        if not isinstance(last, dict):
            last = self._room_departure_notice_last = {}
        previous = float(last.get(room_key, 0) or 0)
        if previous and now - previous < 60.0:
            return False
        last[room_key] = now
        notice = (
            f"⚠️ خرج البوت من الغرفة: {room}\n"
            f"📌 الحالة/السبب: {str(reason or event_type or 'غير معروف').strip()}\n"
            f"👤 الفاعل: {str(actor or 'غير معروف').strip()}\n"
            "إذا تكرر الخروج سريعًا فسيوقف المحاولات تلقائيًا. "
            "حدث user_left وحده لا يطلق إعادة الدخول التلقائية."
        )
        try:
            redact = getattr(self, "_redact_connection_secrets", lambda value: value)
            return self.send_private_text(master, redact(notice)) is not False
        except Exception as exc:
            self.log("[ROOM] departure notice failed:", type(exc).__name__)
            return False

    def _schedule_auto_rejoin(self, room: str, delay: float = 3.0):
        """Rejoin after a departure with bounded attempts and exponential backoff."""
        if not room:
            return
        room_key = _norm_room(room)
        if room_key in getattr(self, "blocked_rooms", set()):
            return
        if room_key in getattr(self, "_intentional_leaves", set()):
            return
        restore_started = float(getattr(self, "_room_restore_started_at", 0.0) or 0.0)
        if (
            restore_started
            and time.monotonic() - restore_started < 45.0
            and any(_norm_room(saved).casefold() == room_key.casefold()
                    for saved in getattr(self, "_room_restore_candidates", set()))
        ):
            self.log(f"[AUTO_REJOIN] startup restore owns this room; skip duplicate: {room}")
            return
        pending_join = getattr(self, "_pending_room_joins", {}).get(room_key)
        if pending_join:
            started = float(pending_join.get("started") or 0)
            if time.time() - started < max(30.0, float(os.getenv("JOIN_CONFIRM_TIMEOUT_SECONDS", "20")) + 5.0):
                self.log(f"[AUTO_REJOIN] room join is already pending; skip {room}")
                return
        if not hasattr(self, "_auto_rejoin_lock"):
            self._auto_rejoin_lock = threading.Lock()
        if not hasattr(self, "_auto_rejoin_pending"):
            self._auto_rejoin_pending = set()
        if not hasattr(self, "_auto_rejoin_state"):
            self._auto_rejoin_state = {}
        if not hasattr(self, "_auto_rejoin_paused"):
            self._auto_rejoin_paused = set()
        if not hasattr(self, "_auto_rejoin_waiting_ack"):
            self._auto_rejoin_waiting_ack = set()
        if any(_norm_room(saved).casefold() == room_key.casefold() for saved in self._auto_rejoin_waiting_ack):
            self.log(f"[AUTO_REJOIN] waiting for server acknowledgement; skip {room}")
            return
        now = time.monotonic()
        notify_master = False
        with self._auto_rejoin_lock:
            if room_key in self._auto_rejoin_pending:
                return
            if room_key in self._auto_rejoin_paused:
                return
            state = self._auto_rejoin_state.setdefault(
                room_key, {"attempts": [], "joined_at": 0.0, "rapid_losses": 0}
            )
            joined_at = float(state.get("joined_at", 0.0) or 0.0)
            if state.get("joined_at") and now - float(state["joined_at"]) >= 120.0:
                state["attempts"] = []
                state["rapid_losses"] = 0
            elif joined_at and now - joined_at < 120.0:
                state["rapid_losses"] = int(state.get("rapid_losses", 0) or 0) + 1
                if state["rapid_losses"] >= 3:
                    self._auto_rejoin_paused.add(room_key)
                    state["attempts"] = []
                    notify_master = True
            if notify_master:
                generation = int(getattr(self, "_connection_generation", 0))
            else:
                recent_attempts = [t for t in state.get("attempts", []) if now - float(t) < 900.0]
                if len(recent_attempts) >= 5:
                    state["attempts"] = recent_attempts
                    self.log(f"[AUTO_REJOIN] stopping after 5 attempts in 15 minutes: {room}")
                    return
                state["attempts"] = recent_attempts + [now]
                delay = max(2.0, float(delay), min(60.0, 1.2 * (2 ** len(recent_attempts))))
                generation = int(getattr(self, "_connection_generation", 0))
                self._auto_rejoin_pending.add(room_key)
        if notify_master:
            if BOT_MASTER:
                try:
                    is_en = self._effective_room_language(room) == "en"
                except Exception:
                    is_en = False
                if is_en:
                    notice = (
                        f"⏸️ Automatic rejoin to {room} paused after repeated rapid departures. "
                        "To resume, send: دخول@اسم_الغرفة"
                    )
                else:
                    notice = (
                        f"⏸️ أوقفت إعادة الدخول التلقائية إلى {room} بعد تكرر الخروج بسرعة. "
                        "لاستئنافها أرسل: دخول@اسم_الغرفة"
                    )
                try:
                    self.send_private_text(BOT_MASTER, notice)
                except Exception as exc:
                    self.log("[AUTO_REJOIN] pause notice failed:", type(exc).__name__)
            self.log(f"[AUTO_REJOIN] paused after repeated rapid departures: {room}")
            return
        def _rejoin_worker():
            try:
                time.sleep(delay)
                if generation != int(getattr(self, "_connection_generation", 0)):
                    self.log(f"[AUTO_REJOIN] stale connection worker cancelled: {room}")
                    return
                if any(_norm_room(r) == room_key for r in getattr(self, "connected_rooms", set())):
                    self.log(f"[AUTO_REJOIN] already connected; skip {room}")
                    return
                if room_key in getattr(self, "_pending_room_joins", {}):
                    self.log(f"[AUTO_REJOIN] pending join already exists; skip {room}")
                    return
                self.log(f"[AUTO_REJOIN] Attempting to rejoin {room}...")
                with self._room_bulk_lock:
                    if generation != int(getattr(self, "_connection_generation", 0)):
                        self.log(f"[AUTO_REJOIN] connection changed before join; cancel {room}")
                        return
                    self._auto_rejoin_waiting_ack.add(room_key)
                    accepted = self.join_room(room, force=True)
                    if accepted is False:
                        self._auto_rejoin_waiting_ack.discard(room_key)
            except Exception as e:
                getattr(self, "_auto_rejoin_waiting_ack", set()).discard(room_key)
                self.log(f"[AUTO_REJOIN] Rejoin failed for {room}:", repr(e))
            finally:
                with self._auto_rejoin_lock:
                    self._auto_rejoin_pending.discard(room_key)
        threading.Thread(target=_rejoin_worker, name=f"auto-rejoin-{room}", daemon=True).start()

    def join_room(self, room: str, force: bool = False, requested_by: str = ""):
        """Join a room, allowing a previously-left room to be joined again.

        ``known_rooms`` is persistent history, while ``connected_rooms`` is the
        current WebSocket session. A room that is only in known_rooms is NOT
        considered currently joined; this is important after ``خروج``.
        """
        room = str(room or "").strip()
        if not room:
            return False
        room_norm = _norm_room(room)
        pending = getattr(self, "_pending_room_joins", {}).get(room_norm)
        if pending:
            pending_age = time.time() - float(pending.get("started") or 0)
            pending_timeout = max(30.0, float(os.getenv("JOIN_CONFIRM_TIMEOUT_SECONDS", "20")) + 5.0)
            if pending_age < pending_timeout:
                self.log("[ROOM] duplicate join suppressed while acknowledgement is pending:", room)
                return False
            stale_timer = pending.get("timer")
            if stale_timer:
                try:
                    stale_timer.cancel()
                except Exception:
                    pass
            self._pending_room_joins.pop(room_norm, None)
        # Forced automatic rejoin may bypass debounce, but it must not bypass a
        # server-confirmed ban. Only a user-requested join clears that ban.
        if force or requested_by:
            stale = self._pending_room_joins.pop(room_norm, None)
            if stale and stale.get("timer"):
                try:
                    stale["timer"].cancel()
                except Exception:
                    pass
        if requested_by:
            getattr(self, "_auto_rejoin_paused", set()).discard(room_norm)
            rejoin_state = getattr(self, "_auto_rejoin_state", {}).get(room_norm)
            if isinstance(rejoin_state, dict):
                rejoin_state["rapid_losses"] = 0
                rejoin_state["attempts"] = []
            getattr(self, "_intentional_leaves", set()).discard(room_norm)
            self.blocked_rooms.discard(room_norm)
            getattr(self, "_blocked_room_reasons", {}).pop(room_norm, None)
            getattr(self, "_blocked_room_notices", set()).discard(room_norm)
            self._save_blocked_rooms()
        elif room_norm in getattr(self, "blocked_rooms", set()):
            self.known_rooms = {
                saved for saved in getattr(self, "known_rooms", set())
                if _norm_room(saved) != room_norm
            }
            self.connected_rooms = {
                saved for saved in getattr(self, "connected_rooms", set())
                if _norm_room(saved) != room_norm
            }
            self.room_users.pop(room_norm, None)
            _save_persistent_rooms(self.known_rooms)
            self.log("[ROOM] blocked room join suppressed:", room)
            return False
        known_norm = {_norm_room(r) for r in getattr(self, "known_rooms", set())}
        connected_norm = {_norm_room(r) for r in getattr(self, "connected_rooms", set())}
        already_known = _norm_room(room) in known_norm
        already_connected = _norm_room(room) in connected_norm
        # Being saved in tracked_rooms.json alone must never block a fresh join.
        # A forced join is used after an actual disconnect/kick. It must NOT
        # send a second room_join while the server already confirms us inside
        # the room; doing so can produce the visible enter/leave loop.
        if already_connected:
            self.log("[ROOM] already connected; duplicate join suppressed:", room)
            return False
        now = time.time()
        with self._join_lock:
            last = self._last_join_sent.get(room, 0.0)
            if not force and now - last < float(os.getenv("JOIN_DEBOUNCE_SECONDS", "20")):
                self.log("[ROOM] join suppressed (debounce):", room)
                return False
            self._last_join_sent[room] = now
        self.log("[ROOM] joining", room)
        self._pending_room_joins[room_norm] = {
            "room": room,
            "started": time.time(),
            "requested_by": str(requested_by or "").strip(),
            "fresh": bool(force or requested_by),
        }
        # Send the join packet first and wait for TalkinChat's result. Never
        # query occupants before confirmation; that can surface a stale
        # unauthorized state and make the bot say "محظور" after permissions
        # have already been fixed.
        self.send_query(encode_query("room_join", room=room, int_value=0, force_int_value=True))
        timer = threading.Timer(
            max(10.0, float(os.getenv("JOIN_CONFIRM_TIMEOUT_SECONDS", "20"))),
            self._join_timeout,
            args=(_norm_room(room),),
        )
        timer.daemon = True
        self._pending_room_joins[_norm_room(room)]["timer"] = timer
        timer.start()
        # A room is persisted only after TalkinChat confirms you_joined or
        # you_rejoined. A failed/blocked join therefore never becomes a saved
        # exception or an automatic rejoin target.
        return True

    def leave_room(self, room: str):
        """Leave exactly one Talkin room using the APK's room_leave packet."""
        room = str(room or "").strip()
        if not room:
            return False
        backup_ok = _flush_github_backup()
        self.log("[BACKUP_BEFORE_LEAVE]", room, "complete" if backup_ok else "not-confirmed")
        if not hasattr(self, "_intentional_leaves"):
            self._intentional_leaves = set()
        self._intentional_leaves.add(_norm_room(room))
        if _norm_room(room) in getattr(self, "blocked_rooms", set()):
            self.known_rooms = {r for r in self.known_rooms if _norm_room(r) != _norm_room(room)}
            self.connected_rooms = {r for r in self.connected_rooms if _norm_room(r) != _norm_room(room)}
            _save_persistent_rooms(self.known_rooms)
            self.log("[ROOM] leave suppressed (room is blocked):", room)
            return False
        self.send_query(encode_query("room_leave", room=room))
        room_norm = _norm_room(room)
        with self._join_lock:
            self.known_rooms = {
                saved for saved in self.known_rooms
                if _norm_room(saved) != room_norm
            }
            _save_persistent_rooms(self.known_rooms)
            self._last_join_sent = {
                joined: timestamp for joined, timestamp in self._last_join_sent.items()
                if _norm_room(joined) != room_norm
            }
        self.room_users = {
            saved: users for saved, users in self.room_users.items()
            if _norm_room(saved) != room_norm
        }
        self.connected_rooms = {
            saved for saved in self.connected_rooms
            if _norm_room(saved) != room_norm
        }
        self._server_room_catalog = [
            saved for saved in getattr(self, "_server_room_catalog", [])
            if _norm_room(saved) != room_norm
        ]
        self.log("[ROOM] left", room)
        return True

    def leave_all_rooms(self):
        """Leave all rooms temporarily while preserving the saved room list.

        The command خروج is a session pause, not deletion.  The names remain
        in known_rooms/tracked_rooms.json so bootstrap_after_connect() restores
        them after the process is restarted.
        """
        rooms = [str(r).strip() for r in self.known_rooms if str(r).strip()]
        if not hasattr(self, "_intentional_leaves"):
            self._intentional_leaves = set()
        if rooms:
            backup_ok = _flush_github_backup()
            self.log("[BACKUP_BEFORE_LEAVE_ALL]", "complete" if backup_ok else "not-confirmed")
        for room in rooms:
            try:
                room_key = _norm_room(room)
                self._intentional_leaves.add(room_key)
                pending = self._pending_room_joins.pop(room_key, None)
                if pending and pending.get("timer"):
                    try:
                        pending["timer"].cancel()
                    except Exception:
                        pass
                with self._room_bulk_lock:
                    self.send_query(encode_query("room_leave", room=room))
                    self.connected_rooms.discard(room)
                    self.room_users.pop(room, None)
                    self._last_join_sent.pop(room, None)
                self.log("[ROOM] left temporarily; preserved for restart:", room)
                if room != rooms[-1]:
                    time.sleep(max(0.25, float(os.getenv("ROOM_BULK_DELAY_SECONDS", "1.5") or 1.5)))
            except Exception as e:
                self.log("[ROOM] leave failed", room, repr(e))
        # Deliberately do not call _save_persistent_rooms with a reduced set.
        # known_rooms remains the source used by reconnect/bootstrap.
        return rooms

    def _split_talkin_text(self, text: str, limit: int = None):
        """Compatibility helper: command menus use their own <=300 splitter."""
        text = str(text or "")
        return [text] if text else [""]

    def _send_long_media_to_telegram(self, media_url: str, title="ملف كبير من البوت", media_type="file"):
        """Download an oversized Talkin media URL and upload the complete file to Telegram."""
        if not TELEGRAM_LONG_TEXT_ENABLED or not TELEGRAM_BOT_TOKEN:
            return False
        chat_id = str(getattr(self, "_telegram_chat_id", "") or "").strip()
        if not chat_id:
            return False
        media_url = str(media_url or "").strip()
        if not media_url:
            return False
        try:
            title = str(title or "ملف كبير من البوت").strip()[:900]
            parsed = urlparse(media_url)
            suffix = Path(parsed.path).suffix.lower() or ".bin"
            safe_type = re.sub(r"[^a-z0-9_-]+", "", str(media_type or "file").lower()) or "file"
            filename = f"talkin_{safe_type}_{int(time.time())}{suffix}"

            # Local file paths are supported too.
            local = Path(media_url).expanduser()
            if local.is_file():
                with local.open("rb") as fh:
                    response = requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                        data={"chat_id": chat_id, "caption": title},
                        files={"document": (local.name, fh, "application/octet-stream")},
                        timeout=120,
                    )
                data = response.json()
                if bool(data.get("ok")):
                    self.log(f"[TELEGRAM] oversized local file sent ({local.name})")
                    return True
                self.log("[TELEGRAM] local file upload failed:", str(data)[:1200])
                return False

            # Stream the remote file without sending it through Talkin again.
            with requests.get(
                media_url,
                headers={"User-Agent": "Mozilla/5.0"},
                stream=True,
                timeout=(15, 120),
            ) as source:
                source.raise_for_status()
                content_type = str(source.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0]
                content = io.BytesIO()
                total = 0
                max_upload = 49 * 1024 * 1024
                for chunk in source.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_upload:
                        self.log("[TELEGRAM] oversized media exceeds Telegram upload safety limit:", total)
                        return False
                    content.write(chunk)
                content.seek(0)
                response = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                    data={"chat_id": chat_id, "caption": title},
                    files={"document": (filename, content, content_type)},
                    timeout=120,
                )
                data = response.json()
                if bool(data.get("ok")):
                    self.log(f"[TELEGRAM] oversized media sent as file ({total} bytes)")
                    return True
                self.log("[TELEGRAM] media upload failed:", str(data)[:1200])
        except Exception as exc:
            self.log("[TELEGRAM] media download/upload failed:", repr(exc))
        return False

    def _send_long_text_to_telegram(self, text: str, title="رسالة طويلة من البوت"):
        """Send the complete oversized Talkin message to Telegram.

        Uses a normal Telegram text message when possible. If Telegram's text
        limit is exceeded, sends the complete original text as a UTF-8 .txt
        document. Never raises to the caller.
        """
        if not TELEGRAM_LONG_TEXT_ENABLED or not TELEGRAM_BOT_TOKEN:
            return False
        chat_id = str(getattr(self, "_telegram_chat_id", "") or "").strip()
        if not chat_id:
            return False
        text = str(text or "")
        if not text:
            return False
        try:
            title = str(title or "رسالة طويلة من البوت").strip()[:900]
            # Telegram text messages support up to about 4096 characters.
            if len(text) <= 4000:
                response = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    data={"chat_id": chat_id, "text": text},
                    timeout=30,
                )
                data = response.json()
                if bool(data.get("ok")):
                    self.log(f"[TELEGRAM] long text sent as message ({len(text)} chars)")
                    return True
                self.log("[TELEGRAM] sendMessage failed:", str(data)[:1200])

            stamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"talkin_long_{stamp}.txt"
            payload = text.encode("utf-8")
            response = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": chat_id, "caption": title},
                files={"document": (filename, io.BytesIO(payload), "text/plain; charset=utf-8")},
                timeout=60,
            )
            data = response.json()
            if bool(data.get("ok")):
                self.log(f"[TELEGRAM] long text sent as document ({len(text)} chars)")
                return True
            self.log("[TELEGRAM] sendDocument failed:", str(data)[:1200])
        except Exception as exc:
            self.log("[TELEGRAM] long text upload failed:", repr(exc))
        return False

    def _long_text_notice(self, packet_type, kwargs):
        if packet_type == "chat_message" and kwargs.get("to"):
            return self.send_private_text(
                str(kwargs.get("to")),\
                "📨 الرسالة طويلة؛ تم إرسالها إلى Telegram لتجنب فصل البوت."
            )
        room = str(kwargs.get("room") or "").strip()
        if room:
            return self._send_text_packets_small_notice(
                "room_message", room,
                "📨 الرسالة طويلة؛ تم إرسالها إلى Telegram لتجنب فصل البوت."
            )
        return True

    def _send_text_packets_small_notice(self, packet_type, room, text):
        payload = {"type_": "text", "body": str(text)[:220]}
        if packet_type == "room_message":
            payload["room"] = room
        try:
            self.send_query(encode_query(packet_type, **payload))
            return True
        except Exception as exc:
            self.log("[WS] long-text notice failed:", repr(exc))
            return False

    def _send_text_packets(self, packet_type: str, text: str, *, oversize_notice_user="", notify_oversize=True, **kwargs):
        """Send text in safe UTF-8 chunks that each fit the full packet limit."""
        text = str(text or "")
        if not text:
            return True

        limit = min(1008, _ws_payload_limit())

        def make_payload(value):
            payload = dict(kwargs)
            payload["type_"] = "text"
            payload["body"] = value
            return encode_query(packet_type, **payload)

        # Measure the COMPLETE WebSocket/protobuf payload, not just text bytes.
        packet = make_payload(text)
        if len(packet) <= limit:
            self.send_query(packet)
            return True

        def grapheme_units(value):
            """Keep combining marks, emoji modifiers, flags and ZWJ emoji whole."""
            units = []
            current = ""
            regional_run = 0
            for char in value:
                codepoint = ord(char)
                is_regional = 0x1F1E6 <= codepoint <= 0x1F1FF
                is_extend = (
                    unicodedata.category(char).startswith("M")
                    or 0xFE00 <= codepoint <= 0xFE0F
                    or 0xE0100 <= codepoint <= 0xE01EF
                    or 0x1F3FB <= codepoint <= 0x1F3FF
                    or 0xE0020 <= codepoint <= 0xE007F
                )
                if not current:
                    current = char
                    regional_run = 1 if is_regional else 0
                    continue
                attach = (
                    is_extend or char == "\u200d" or current.endswith("\u200d")
                    or (char == "\n" and current.endswith("\r"))
                    or (is_regional and regional_run % 2 == 1)
                )
                if attach:
                    current += char
                    if is_regional:
                        regional_run += 1
                    elif not is_extend and char != "\u200d":
                        regional_run = 0
                else:
                    units.append(current)
                    current = char
                    regional_run = 1 if is_regional else 0
            if current:
                units.append(current)
            return units

        units = grapheme_units(text)
        chunks = []
        start = 0
        while start < len(units):
            # A UTF-8 code point occupies at least one byte, so no fitting body
            # can contain more than `limit` grapheme units. Measure the actual
            # protobuf on every candidate to include routing and field overhead.
            low = start + 1
            high = min(len(units), start + limit)
            best = start
            while low <= high:
                middle = (low + high) // 2
                candidate = "".join(units[start:middle])
                if len(make_payload(candidate)) <= limit:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            if best == start:
                chunks = []
                break

            if best < len(units):
                last_newline = 0
                last_space = 0
                for index in range(start, best):
                    unit = units[index]
                    if "\n" in unit:
                        last_newline = index + 1
                    elif unit.isspace():
                        last_space = index + 1
                preferred = last_newline or last_space
                if preferred > start:
                    best = preferred

            chunks.append("".join(units[start:best]))
            start = best

        if chunks and "".join(chunks) == text:
            try:
                for chunk in chunks:
                    chunk_packet = make_payload(chunk)
                    if len(chunk_packet) > limit:
                        raise ValueError("safe text chunk exceeded packet limit")
                    if self.send_query(chunk_packet) is False:
                        raise RuntimeError("WebSocket rejected a text chunk")
            except Exception as exc:
                self.log("[WS] safe text chunk delivery failed:", repr(exc))
                return False
            self.log("[WS] split oversized text safely:", len(chunks), "packets", len(packet), "bytes")
            return True

        # If even one whole grapheme cannot fit because of routing metadata or
        # an unusually large single grapheme, use the existing fallback path.
        self._report_oversize_delivery(
            packet_type,
            str(kwargs.get("to") or ""),
            str(kwargs.get("room") or ""),
            len(packet),
            f"حزمة النص تجاوزت الحد الصلب ({limit} بايت)؛ أُسقطت محلياً",
        )
        if len(packet) > 1008:
            telegram_sent = self._send_long_text_to_telegram(
                text, title=f"رسالة Talkin طويلة ({packet_type}) — {len(packet)} بايت"
            )
            master = str(BOT_MASTER or "").strip()
            if master and telegram_sent:
                try:
                    self.send_private_text(master, f"📨 تم إرسال الرسالة الطويلة إلى Telegram لتجنب فصل البوت.\n📦 الحجم: {len(packet)} بايت\n📍 الوجهة: {kwargs.get('room') or kwargs.get('to') or 'غير محددة'}")
                except Exception as exc:
                    self.log("[TELEGRAM] master notice failed:", repr(exc))
            elif not telegram_sent:
                self.log("[TELEGRAM] oversized Talkin response was not forwarded")
            if notify_oversize:
                self._send_oversize_notice(
                    packet_type, target=str(kwargs.get("to") or ""),
                    room=str(kwargs.get("room") or ""), username=oversize_notice_user,
                )
        return False

    def _send_help_chunks(self, packet_type: str, text: str, limit: int = 320, **kwargs):
        """Send a help page in packet-safe chat bubbles before Ns continuation."""
        return self._send_text_packets(packet_type, text, **kwargs)

    def _active_rooms(self):
        blocked = getattr(self, "blocked_rooms", set())
        rooms = {
            str(r).strip() for r in getattr(self, "known_rooms", set())
            if str(r).strip() and _norm_room(r) not in blocked
        }
        if self.room and _norm_room(self.room) not in blocked:
            rooms.add(str(self.room).strip())
        rooms.update(
            str(r).strip() for r in self.room_users.keys()
            if str(r).strip() and _norm_room(r) not in blocked
        )
        rooms.update(
            str(r).strip() for r in getattr(self, "connected_rooms", set())
            if str(r).strip() and _norm_room(r) not in blocked
        )
        rooms.update(
            str(r).strip() for r in getattr(self, "_server_room_catalog", [])
            if str(r).strip() and _norm_room(r) not in blocked
        )
        rooms.update(
            str(r).strip() for r in _persistent_rooms()
            if str(r).strip() and _norm_room(r) not in blocked
        )
        return sorted(rooms)

    def _room_broadcast_worker_loop(self):
        """Deliver admin broadcasts off the realtime event/command worker."""
        work_queue = self._room_broadcast_queue
        while True:
            requester, text = work_queue.get()
            try:
                self._broadcast_room_message_worker(requester, text)
            except Exception as exc:
                self.log("[BROADCAST] background delivery failed:", repr(exc))
                if requester:
                    try:
                        self.send_private_text(requester, "❌ تعذر إكمال بث الرسالة؛ راجع سجل البوت.")
                    except Exception:
                        pass
            finally:
                work_queue.task_done()

    def _queue_room_broadcast(self, text: str, requester: str):
        work_queue = getattr(self, "_room_broadcast_queue", None)
        if work_queue is None:
            # Supports test-created/legacy instances while keeping one bounded
            # worker rather than creating a thread for each broadcast command.
            work_queue = self._room_broadcast_queue = queue.Queue(maxsize=20)
            self._room_broadcast_worker = threading.Thread(
                target=self._room_broadcast_worker_loop,
                name="room-broadcast-worker", daemon=True,
            )
            self._room_broadcast_worker.start()
        try:
            work_queue.put_nowait((str(requester or "").strip(), str(text or "")))
            return True
        except queue.Full:
            return False

    def _private_broadcast_worker_loop(self):
        """Deliver master private broadcasts away from the event worker."""
        work_queue = self._private_broadcast_queue
        while True:
            targets, payload, requester = work_queue.get()
            try:
                delay = max(0.05, float(os.getenv("PRIVATE_BROADCAST_DELAY", "0.15") or 0.15))
                sent = 0
                failed = 0
                total = len(targets)
                for username in targets:
                    try:
                        if self.send_private_text(username, payload):
                            sent += 1
                        else:
                            failed += 1
                    except Exception as exc:
                        failed += 1
                        self.log("[BROADCAST] failed", username, repr(exc))
                    if delay:
                        time.sleep(delay)
                if requester:
                    self.send_private_text(
                        requester,
                        f"📣 اكتمل البث الخاص.\n👥 المستهدفون: {total}\n"
                        f"✅ تم الإرسال: {sent}\n❌ تعذر الإرسال: {failed}",
                    )
                self.log(f"[BROADCAST] private completed total={total} sent={sent} failed={failed}")
            except Exception as exc:
                self.log("[BROADCAST] private background delivery failed:", repr(exc))
                if requester:
                    try:
                        self.send_private_text(requester, "❌ تعذر إكمال بث الرسالة الخاصة؛ راجع سجل البوت.")
                    except Exception:
                        pass
            finally:
                work_queue.task_done()

    def _queue_private_broadcast(self, targets, payload, requester):
        """Queue one private broadcast; never block the WebSocket event worker."""
        work_queue = getattr(self, "_private_broadcast_queue", None)
        if work_queue is None:
            work_queue = self._private_broadcast_queue = queue.Queue(maxsize=8)
            self._private_broadcast_worker = threading.Thread(
                target=self._private_broadcast_worker_loop,
                name="private-broadcast-worker", daemon=True,
            )
            self._private_broadcast_worker.start()
        try:
            work_queue.put_nowait((list(targets), str(payload or ""), str(requester or "").strip()))
            return True
        except queue.Full:
            return False

    def broadcast_all_rooms(self, text: str):
        """Send a room-wide announcement serially without disturbing joins."""
        return self._broadcast_room_message_worker("", text)

    def _broadcast_room_message_worker(self, requester: str, text: str):
        """Send an admin notice in the background and report per-room outcomes."""
        rooms = self._active_rooms()
        sent = 0
        delay=max(0.25, float(os.getenv("ROOM_BULK_DELAY_SECONDS", "1.5") or 1.5))
        with self._room_bulk_lock:
            for index, target_room in enumerate(rooms):
                try:
                    if self.send_room_text(target_room, text) is not False:
                        sent += 1
                except Exception as exc:
                    self.log("[BROADCAST] failed", target_room, repr(exc))
                if index+1 < len(rooms):
                    time.sleep(delay)
        if requester:
            failed = len(rooms) - sent
            if failed:
                result = f"⚠️ وصلت إلى {sent} غرفة، وفشل {failed} من {len(rooms)} غرفة."
            else:
                result = f"✅ اكتمل البث في الخلفية ووصلت الرسالة إلى {sent} غرفة."
            self.send_private_text(requester, result)
        return sent

    @staticmethod
    def _oversize_notice_exempt_command(command_text):
        low = str(command_text or "").strip().casefold().replace("ة", "ه")
        top_commands = {
            "توب", "top", "توب الالعاب", "top games", "games top", "توب النقاط",
            "توب النقاطي", "top points", "points top", "توب المليار", "توب مليار",
            "top b", "topb", "توب المليون", "توب مليون", "top m", "topm",
            "توب الماس", "توب الالماس", "top diamonds", "top gems", "اغنى المنقبين",
            "ملياري", "مليارات", "مليوني", "ملايين",
        }
        room_commands = {
            "غرفي", "myrooms", "my rooms", ".دخول غرفي", "دخول غرفي", "دخولغرفي",
            "قائمة غرف السيرفر", "جلب غرف السيرفر", "جلب الغرف من السيرفر", "get server rooms",
            "رتبتك", "رتبتي", "bot rank", "your rank",
        }
        return (
            low in top_commands or low.startswith(("توب ", "top "))
            or low in room_commands or bool(re.fullmatch(r"l@[amob]", low))
        )

    def _send_command_result_pages(self, packet_type, text, *, sender, room="", recipient="", language="ar"):
        """Paginate a long leaderboard/room-list response without a size warning."""
        route = {"to": str(recipient or "")} if packet_type == "chat_message" else {"room": str(room or "")}
        limit = min(_ws_payload_limit(), SAFE_COMMAND_REPLY_BYTES)
        reserve = "\n📄 القائمة 999/999\n\n📌 اكتب ns للقائمة التالية."

        def fits(value):
            return len(encode_query(packet_type, type_="text", body=value + reserve, **route)) <= limit

        raw_pages = []
        current = []
        for line in str(text or "").splitlines() or [str(text or "")]:
            pieces = [line]
            if not fits(line):
                # Extremely long single rows are split at Unicode code-point
                # boundaries so no individual frame can cross the hard limit.
                pieces = []
                chunk = ""
                for char in line:
                    if chunk and not fits(chunk + char):
                        pieces.append(chunk)
                        chunk = char
                    else:
                        chunk += char
                if chunk:
                    pieces.append(chunk)
            for piece in pieces:
                candidate = "\n".join(current + [piece])
                if current and not fits(candidate):
                    raw_pages.append("\n".join(current))
                    current = [piece]
                else:
                    current.append(piece)
        if current:
            raw_pages.append("\n".join(current))

        total = len(raw_pages)
        pages = [f"{page}\n📄 القائمة {index}/{total}" for index, page in enumerate(raw_pages, 1)]
        result_key = (packet_type, "" if packet_type == "chat_message" else str(room or ""), str(sender or ""))
        result_pages = getattr(self, "_result_pages", {})
        if not isinstance(result_pages, dict):
            result_pages = {}
        result_pages[result_key] = {
            "pages": pages, "part": 1, "kwargs": route, "language": language,
        }
        self._result_pages = result_pages
        first = pages[0]
        if total > 1:
            first += "\n\n📌 Type ns for the next list." if language == "en" else "\n\n📌 اكتب ns للقائمة التالية."
        else:
            result_pages.pop(result_key, None)
            self._result_pages = result_pages
            first += "\n\n✅ End of list." if language == "en" else "\n\n✅ انتهت القوائم."
        return self._send_text_packets(packet_type, first, notify_oversize=False, **route)

    def send_room_text(self, room: str, text: str):
        text = self._localize_room_text(room, text)
        if getattr(self._master_reply_local, "tracking", False):
            self._master_reply_local.replied = True
        if getattr(self._silent_master_local, "active", False):
            return True
        notice_user = getattr(self._master_reply_local, "command_sender", "")
        command_text = getattr(self._master_reply_local, "command_text", "")
        if self._oversize_notice_exempt_command(command_text):
            packet = encode_query("room_message", type_="text", body=text, room=room)
            if len(packet) > min(_ws_payload_limit(), SAFE_COMMAND_REPLY_BYTES):
                return self._send_command_result_pages(
                    "room_message", text,
                    sender=notice_user, room=room,
                    language=self._effective_room_language(room),
                )
        return self._send_text_packets(
            "room_message", text, room=room, oversize_notice_user=notice_user,
            notify_oversize=not self._oversize_notice_exempt_command(command_text),
        )

    def send_room_lines(self, room: str, lines):
        for line in lines:
            if line is not None:
                self.send_room_text(room, str(line))
        return True

    def send_room_text_chunks(self, room: str, text: str):
        """Send a long room response as complete, size-safe text messages."""
        room = str(room or "").strip()
        text = self._localize_room_text(room, str(text or ""))
        if not text:
            return True
        limit = min(_ws_payload_limit(), SAFE_COMMAND_REPLY_BYTES)

        def fits(value):
            return len(encode_query("room_message", type_="text", room=room, body=value)) <= limit

        chunks = []
        current = ""
        for line in text.splitlines() or [text]:
            pieces = [line]
            if not fits(line):
                pieces = []
                piece = ""
                for char in line:
                    if piece and not fits(piece + char):
                        pieces.append(piece)
                        piece = char
                    else:
                        piece += char
                if piece:
                    pieces.append(piece)
            for piece in pieces:
                candidate = f"{current}\n{piece}" if current else piece
                if current and not fits(candidate):
                    chunks.append(current)
                    current = piece
                else:
                    current = candidate
        if current:
            chunks.append(current)

        return all(self.send_room_text(room, chunk) for chunk in chunks)

    def _remember_bot_action(self, room, body, sender, is_private=False):
        """Remember the last command the bot actually dispatched.

        `.u` replays this command verbatim. We deliberately store the original
        sender and room so permission checks and room-scoped commands behave
        exactly as they did the first time. Replay itself is never recorded as
        a new action.
        """
        if getattr(self, "_replaying_bot_action", False):
            return
        text = str(body or "").strip()
        if not text or text.casefold() == ".u":
            return
        # Navigation/help is not a meaningful bot action to repeat.
        if _is_ns_command(text) or re.fullmatch(r"a[1-6]", text, re.I) or text.casefold() in {"اوامر", "الاوامر", "help", "مساعدة"}:
            return
        action = {
            "command": text,
            "room": str(room or ""),
            "sender": str(sender or ""),
            "is_private": bool(is_private),
            "created_at": int(time.time()),
        }
        self.last_bot_action = action
        try:
            _save_local_json(DATA_DIR / "last_action.json", action)
        except Exception as exc:
            self.log("[UNDO] save last action failed:", type(exc).__name__)
        if LOG_ROOM_MESSAGES:
            self.log("[UNDO] last action saved (room-message logging enabled)")

    def _cleanup_memory_state(self):
        """Prune bounded runtime caches and force Python GC without touching persistent data."""
        now = time.time()
        cutoff = now - CLEANUP_MEMORY_MAX_AGE_SECONDS
        changed = False

        # Transport de-duplication caches can grow during long-lived sessions.
        for attr in ("_incoming_seen", "_management_command_seen"):
            cache = getattr(self, attr, None)
            if isinstance(cache, dict):
                stale = []
                for key, value in list(cache.items()):
                    try:
                        stamp = float(value if isinstance(value, (int, float)) else (value.get("created_at", 0) if isinstance(value, dict) else 0))
                    except Exception:
                        stamp = 0
                    if stamp and stamp < cutoff:
                        stale.append(key)
                for key in stale:
                    cache.pop(key, None)
                if stale:
                    changed = True
                # Hard cap as a safety valve.
                if len(cache) > 3000:
                    for key in list(cache)[:len(cache) - 3000]:
                        cache.pop(key, None)
                    changed = True

        # Message/repeat history is deliberately short-lived.
        for mapping_name in ("last_messages", "_room_repeat_state", "_joinleave_state"):
            mapping = getattr(self, mapping_name, None)
            if isinstance(mapping, dict) and len(mapping) > 500:
                for key in list(mapping)[:len(mapping) - 500]:
                    mapping.pop(key, None)
                changed = True

        for mapping_name in ("game_cooldown", "board_game_cooldown"):
            mapping = getattr(self, mapping_name, None)
            if isinstance(mapping, dict):
                stale = []
                for key, value in list(mapping.items()):
                    try:
                        if float(value or 0) < cutoff:
                            stale.append(key)
                    except Exception:
                        stale.append(key)
                for key in stale:
                    mapping.pop(key, None)
                if stale:
                    changed = True

        # Never let a completed/old diagnostic command survive indefinitely.
        inflight = getattr(self, "_inflight_command", None)
        if isinstance(inflight, dict):
            try:
                if float(inflight.get("created_at", 0) or 0) < cutoff:
                    self._inflight_command = None
                    changed = True
            except Exception:
                self._inflight_command = None
                changed = True

        gc.collect()
        return changed

    def _remember_inflight_command(self, room, body, sender, is_private=False):
        """Record the command currently entering the dispatcher.

        Unlike last_action.json, this is diagnostic state: it identifies the
        command that was in flight when the socket crashed, so the master can
        reproduce and fix the failure after reconnect.
        """
        text = str(body or "").strip()
        if not text:
            return
        low = text.casefold()
        if low in {".u", "help", "مساعدة", "اوامر", "الأوامر", "ns", "n", "التالي"}:
            return
        # IMPORTANT: this diagnostic record must contain only actual bot commands.
        # Previously every normal room sentence was recorded here, so after a
        # WebSocket 1009 reconnect the bot falsely reported ordinary chat text
        # as the command that caused the disconnect.
        is_command = bool(
            _looks_like_admin_command(text)
            or re.match(r"^(?:بث|\.تشغيل|\.sa|sher@|is@|vip@|\+vip@|-vip@|mf@|\+mf@|-mf@)\s*.+", text, re.I | re.S)
            or re.match(r"^(?:sa|هدايا|gifts|gv|نقاطي|points|نقاط\s+@?.+|points\s+@?.+|صعود|اصعد|إصعد|\.صعود)$", text, re.I)
            or re.fullmatch(r"a[1-6]", text, re.I)
        )
        if not is_command:
            return
        self._inflight_command = {
            "command": text[:800],
            "room": str(room or "")[:200],
            "sender": str(sender or "")[:120],
            "is_private": bool(is_private),
            "created_at": int(time.time()),
        }

    def _replay_last_bot_action(self, requester):
        """Replay the last stored bot command once, without replacing it."""
        action = getattr(self, "last_bot_action", None)
        if not isinstance(action, dict) or not str(action.get("command") or "").strip():
            action = _load_local_json(DATA_DIR / "last_action.json", {})
        if not isinstance(action, dict) or not str(action.get("command") or "").strip():
            self.send_private_text(requester, "📭 لا يوجد أمر سابق نفذه البوت لإعادته.")
            return True

        command = str(action.get("command") or "").strip()
        if command.casefold() == ".u":
            self.send_private_text(requester, "📭 لا يوجد أمر سابق صالح لإعادته.")
            return True
        target_room = str(action.get("room") or "").strip()
        original_sender = str(action.get("sender") or "").strip() or str(requester or "").strip()
        is_private = bool(action.get("is_private", False))

        self.send_private_text(
            requester,
            f"↻ إعادة تنفيذ آخر أمر للبوت:\n"
            f"📌 الأمر: {command}\n"
            f"🏠 الغرفة: {target_room or 'غير محددة'}"
        )

        old_flag = getattr(self, "_replaying_bot_action", False)
        self._replaying_bot_action = True
        try:
            # Use the same TalkinChat command pipelines used by normal incoming
            # messages. Service on/off lives one layer above management, so it
            # must also be replayable by .u.
            if self._handle_master_process_command(original_sender, command, is_private=is_private):
                return True
            if self._handle_management_command(target_room, command, original_sender, is_private=is_private):
                return True
            low = command.casefold()
            if re.match(r"^sa@[^@]+@.+$", command, re.I):
                return bool(self.handle_gift_command(target_room, command, original_sender))
            if low in ("هدايا", "gifts", "gv"):
                self.gift_help(target_room, original_sender)
                return True
            if re.fullmatch(r"sher@(.+)", command, re.I):
                self.share_last_music(original_sender, command.split("@",1)[1], target_room)
                return True
            youtube_direct_test = re.fullmatch(r"\.aax\s*(.+)", command.strip(), re.I)
            if youtube_direct_test:
                return bool(self.handle_youtube_direct_test_command(target_room, youtube_direct_test.group(1), original_sender))
            youtube_test = re.fullmatch(r"(?:تشغيل\s+يوتيوب\s+)?\.aa\s*(.+)", command.strip(), re.I)
            if youtube_test:
                return bool(self.handle_youtube_test_command(target_room, youtube_test.group(1), original_sender))
            if low.startswith(".sa "):
                return bool(self.handle_music_command(target_room, command, original_sender, broadcast_all=True, with_reactions=True))
            if low in ("نقاطي", "points"):
                self.send_room_text(target_room, _points_summary_text(original_sender))
                return True
            named_points = re.fullmatch(r"(?:نقاط|points)\s+@?(.+?)", command, re.I)
            if named_points:
                self.send_room_text(target_room, _named_points_text(named_points.group(1)))
                return True
            if self._run_game_command_async(target_room, command, original_sender):
                return True
            self.send_private_text(requester, "⚠️ لم يعد الأمر السابق معروفاً في النسخة الحالية من البوت.")
            return True
        except Exception as exc:
            self.log("[UNDO] replay failed:", repr(exc))
            self.send_private_text(requester, f"❌ تعذر إعادة تنفيذ الأمر: {exc}")
            return True
        finally:
            self._replaying_bot_action = old_flag

    def ack(self, uid: str):
        if uid:
            self.send_query(encode_query("ack_msg", uid=uid))

    def send_private_text(self, username: str, text: str):
        """Send the complete private text in safe sequential chunks.

        The private chat may display long reports, but one oversized protobuf
        can make Talkin close the entire WebSocket with code 1009. Chunking
        preserves every character while keeping each packet below the safe
        room/server limit.
        """
        tracking = getattr(self._master_reply_local, "tracking", False)
        # Management responses addressed to the master follow the channel
        # where the command arrived: room command -> room reply, private
        # command -> private reply. Notifications addressed to other users
        # remain private and are not rerouted.
        if (tracking and not getattr(self._master_reply_local, "command_private", True)
                and _norm_user(username) == _norm_user(getattr(self._master_reply_local, "command_sender", ""))
                and not getattr(self._master_reply_local, "rerouting", False)):
            self._master_reply_local.rerouting = True
            try:
                return self.send_room_text(getattr(self._master_reply_local, "command_room", ""), text)
            finally:
                self._master_reply_local.rerouting = False
        if tracking:
            self._master_reply_local.replied = True
            self._master_reply_local.private_replied = True
        if getattr(self._silent_master_local, "active", False):
            return True
        username = str(username or "").strip()
        if not username or username == BOT_ID:
            return False
        command_text = getattr(self._master_reply_local, "command_text", "")
        exempt = self._oversize_notice_exempt_command(command_text)
        if exempt:
            packet = encode_query("chat_message", type_="text", to=username, body=str(text or ""))
            if len(packet) > min(_ws_payload_limit(), SAFE_COMMAND_REPLY_BYTES):
                return self._send_command_result_pages(
                    "chat_message", text,
                    sender=getattr(self._master_reply_local, "command_sender", username),
                    recipient=username,
                    language=_detect_text_language(command_text) or "ar",
                )
        return self._send_text_packets(
            "chat_message", text, to=username, notify_oversize=not exempt
        )

    def _deliver_user_private_message(self, sender, target, message_text):
        """Deliver a user's message privately and keep both sides out of rooms."""
        sender = str(sender or "").strip().lstrip("@")
        target = str(target or "").strip().lstrip("@")
        message_text = str(message_text or "").strip()
        if not sender or not target or not message_text:
            return False
        payload = f"📩 لديك رسالة من @{sender}\n{message_text}"
        try:
            sent = bool(self.send_private_text(target, payload))
        except Exception as exc:
            self.log("[DIRECT-MESSAGE] failed", target, repr(exc))
            sent = False
        reply_context = getattr(self, "_master_reply_local", None)
        old_rerouting = getattr(reply_context, "rerouting", False)
        try:
            if reply_context is not None:
                reply_context.rerouting = True
            self.send_private_text(
                sender,
                f"✅ تم إرسال الرسالة إلى @{target} بنجاح." if sent
                else f"❌ فشل إرسال الرسالة إلى @{target}.",
            )
        except Exception as exc:
            self.log("[DIRECT-MESSAGE] sender confirmation failed", repr(exc))
        finally:
            if reply_context is not None:
                reply_context.rerouting = old_rerouting
        return sent

    def send_private_media(self, username: str, media_url: str, media_type: str,
                           duration: int = 0, filename: str = "", local_path=None):
        """Upload then send a native Talkinchat private audio/image attachment."""
        username = str(username or "").strip().lstrip("@")
        media_url = str(media_url or "").strip()
        requested_type = str(media_type or "").strip().lower()
        uploaded_official = bool(local_path)
        if local_path:
            media_url = self._upload_private_media_official(local_path, username, requested_type)
        if not username or _norm_user(username) == _norm_user(BOT_ID) or not media_url:
            return False
        if requested_type not in {"audio", "image", "video", "file"}:
            raise ValueError(f"unsupported private media type: {requested_type}")

        requested_audio = requested_type == "audio"
        if requested_audio:
            if not uploaded_official:
                self._verify_public_media_url(media_url, "audio")
        elif requested_type in {"image", "video"}:
            if not uploaded_official:
                self._verify_public_media_url(media_url, requested_type)

        parsed_path = urlparse(media_url).path
        clean_name = str(filename or Path(parsed_path).name or "").strip()
        clean_name = re.sub(r'[\\/:*?"<>|]+', "_", clean_name).strip(" .")[:120]
        if requested_audio:
            if not clean_name:
                clean_name = "song.mp3"
            elif not Path(clean_name).suffix:
                clean_name += ".mp3"
            if not media_url.lower().split("?", 1)[0].endswith((".mp3", ".m4a", ".ogg", ".wav", ".webm")):
                self.log("[MEDIA] private audio uses explicit filename; URL path has no audio extension")

        # The official Android client uploads first, then sends one
        # chat_message with type=audio/image and the returned URL.  Do not
        # substitute type=file: that path is acknowledged but rendered as
        # text-only by the current private gateway.
        selected_type = requested_type
        payload = encode_query(
            "chat_message", type_=selected_type, to=username, uid=_talkin_message_uid(), url=media_url,
            body="",
            length=str(max(0, int(duration or 0))) if selected_type == "audio" else None,
        )
        if len(payload) > _ws_payload_limit():
            raise RuntimeError(
                f"حزمة ملف الصوت الخاص أكبر من حد Talkin ({len(payload)} > {_ws_payload_limit()} بايت)"
            )
        last_error = None
        for attempt in range(2):
            try:
                result = self.send_query(payload)
                if result is not False:
                    self.log(
                        "[MEDIA] private attachment packet sent:", username,
                        "type=", selected_type, "name=", clean_name[:80],
                        "bytes=", len(payload),
                    )
                    return result
                last_error = RuntimeError(f"Talkin rejected private media type {selected_type}")
            except Exception as exc:
                last_error = exc
                self.log("[MEDIA] private media send failed:", username, selected_type, repr(exc))
            if attempt == 0:
                time.sleep(0.8)
        raise last_error or RuntimeError("تعذر إرسال ملف الصوت الخاص")
