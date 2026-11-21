"""Live-room discovery, speaker state, stream invitations and live broadcast lifecycle."""

class LiveAudioSection:
    def _find_live_room_state(self, room: str):
        """Return the canonical in-memory live-room key matching ``room``.

        Talkin can report the same room with small formatting/case differences
        between chat messages and stream events. In a multi-room session that
        used to make an already-live room look inactive, causing ``بث`` to send
        a second invitation and then display a false "تعذر صعود البوت" error.
        """
        target = str(room or "").strip()
        if not target:
            return ""
        target_key = _norm_room(target).casefold()
        candidates = []
        candidates.extend(list(getattr(self, "_live_ready_rooms", set()) or set()))
        candidates.extend(list((getattr(self, "_pending_live_accepts", {}) or {}).keys()))
        candidates.extend(list((getattr(self, "_live_session_by_room", {}) or {}).keys()))
        for key in candidates:
            key_s = str(key or "").strip()
            if key_s and _norm_room(key_s).casefold() == target_key:
                return key_s
        # Finally inspect the per-room LiveKit publisher flags.
        for key in candidates:
            key_s = str(key or "").strip()
            if key_s and getattr(self, f"_livekit_active_{key_s}", False):
                if _norm_room(key_s).casefold() == target_key:
                    return key_s
        return target

    def _live_room_is_active(self, room: str):
        """Return (canonical_key, accepted_session, active) for one room."""
        key = self._find_live_room_state(room)
        pending = getattr(self, "_pending_live_accepts", {}) or {}
        sessions = getattr(self, "_live_session_by_room", {}) or {}
        accepted = pending.get(key, {}) or sessions.get(key, {}) or {}
        if not accepted and key != room:
            accepted = pending.get(room, {}) or sessions.get(room, {}) or {}
        active = bool(
            key in (getattr(self, "_live_ready_rooms", set()) or set())
            or accepted.get("publish_confirmed")
            or getattr(self, f"_livekit_active_{key}", False)
            or getattr(self, f"_livekit_active_{room}", False)
            or accepted.get("livekit_active")
        )
        return key, accepted, active

    def _reassert_live_speaker(self, room: str, accepted: dict):
        """Re-send the native Talkin publish/accept packets after LiveKit is ready.

        Some Talkin server builds acknowledge publish_stream and connect LiveKit,
        yet leave the seat visually/semantically as listener until the native
        room_stream/stream_accept pair is asserted again. Never use the large
        LiveKit JWT here; use the original invitation token captured in stage 2.
        """
        if not STREAM_REASSERT_SPEAKER or not isinstance(accepted, dict):
            return False
        room = str(room or "").strip()
        if not room:
            return False
        token = str(accepted.get("token", "") or "").strip()
        invite_id = str(
            accepted.get("invite_id", "")
            or accepted.get("session_id", "")
            or accepted.get("publish_id", "")
            or ""
        ).strip()
        room_id = str(
            accepted.get("room_id", "")
            or getattr(self, "_live_room_ids", {}).get(room, "")
            or ""
        ).strip()
        if not token:
            self.log("[STREAM] cannot reassert speaker: original invitation token is missing", room)
            return False
        ok = False
        try:
            # Re-assert the native publish seat. Keep the JWT out of diagnostics.
            self.send_query(encode_query(
                STREAM_ROOM_ACTION,
                type_=STREAM_ROOM_TYPE,
                to=token,
                token=token,
                id_=invite_id,
                room=room,
            ))
            ok = True
            self._log_stream_stage("3.4_إعادة_تثبيت_المتحدث", "publish_تم_إعادة_الإرسال", room=room)
        except Exception as exc:
            self.log("[STREAM] speaker publish reassert failed:", room, repr(exc))

        try:
            # Re-assert accept by room name.
            self.send_query(encode_query(
                STREAM_ACCEPT_ACTION,
                type_="accept",
                to=token,
                token=token,
                id_=invite_id,
                room=room,
                state=STREAM_ACCEPT_STATE,
                value=STREAM_ACCEPT_STATE,
            ))
            ok = True
        except Exception as exc:
            self.log("[STREAM] speaker accept reassert failed:", room, repr(exc))

        if room_id and room_id != room and room_id.isdigit():
            try:
                self.send_query(encode_query(
                    STREAM_ACCEPT_ACTION,
                    type_="accept",
                    to=token,
                    token=token,
                    id_=invite_id,
                    room=room_id,
                    state=STREAM_ACCEPT_STATE,
                    value=STREAM_ACCEPT_STATE,
                ))
                ok = True
            except Exception as exc:
                self.log("[STREAM] numeric speaker accept reassert failed:", room, room_id, repr(exc))

        if STREAM_REASSERT_DELAY:
            time.sleep(STREAM_REASSERT_DELAY)
        return ok

    def _stop_live_broadcast(self, room: str):
        """Stop the song and disconnect the bot's LiveKit publisher for one room."""
        room = str(room or "").strip()
        if not room:
            return False
        key, accepted, active = self._live_room_is_active(room)
        target = key or room
        tokens = getattr(self, "_live_playback_tokens", None)
        if isinstance(tokens, dict):
            tokens.pop(target, None)
            tokens.pop(room, None)
        self._stop_live_song(target)
        stopped = bool(active or accepted)

        setattr(self, f"_livekit_stop_requested_{target}", True)
        lk_room = getattr(self, f"_livekit_room_{target}", None)
        lk_loop = getattr(self, f"_livekit_loop_{target}", None)
        if lk_room is not None and lk_loop is not None and not getattr(lk_loop, "is_closed", lambda: True)():
            try:
                async def _disconnect():
                    try:
                        await lk_room.disconnect()
                    except TypeError:
                        lk_room.disconnect()
                fut = asyncio.run_coroutine_threadsafe(_disconnect(), lk_loop)
                fut.result(timeout=5)
                stopped = True
            except Exception as exc:
                self.log("[LIVEKIT] disconnect failed:", target, repr(exc))

        # Do not let the old session make the next "بث" look already active.
        try:
            getattr(self, "_live_ready_rooms", set()).discard(target)
        except Exception:
            pass
        for mapping_name in ("_pending_live_accepts", "_live_session_by_room"):
            try:
                getattr(self, mapping_name, {}).pop(target, None)
            except Exception:
                pass
        try:
            getattr(self, "_live_room_ids", {}).pop(target, None)
        except Exception:
            pass

        setattr(self, f"_livekit_active_{target}", False)
        setattr(self, f"_livekit_stop_requested_{target}", False)
        self._last_live_play_status_by_room = getattr(self, "_last_live_play_status_by_room", {})
        self._last_live_play_status_by_room[target] = "broadcast_stopped"
        return stopped

    def request_live_room(self, room: str):
        """Request a live seat using the app-compatible native invitation flow."""
        room = str(room or "").strip()
        if not room:
            return False
        try:
            self._live_ready_rooms = getattr(self, "_live_ready_rooms", set())
            self._live_ready_rooms.discard(room)
            self.log("[STREAM] request live seat", room, STREAM_INVITE_ACTION)

            if STREAM_MANUAL_ACCEPT_ONLY:
                self.send_room_text(room, "🎙️ جاهز، أرسل: بث اسم الأغنية لتشغيلها")
                return True

            # A self-seat request must use the same real invitation packet as
            # the manual flow: room_id, room name, Token, and invitation_id.
            # The old generic sent_invitation(room/to) packet was ignored by
            # the gateway and never produced a usable you_invited callback.
            if not self.send_live_invitation_to_user(BOT_ID, room):
                raise RuntimeError("تعذر إرسال دعوة البث الفعلية إلى البوت")
            # Do not send a guessed accept packet here. The real acceptance
            # requires token/room_id/room_name/session_id from `you_invited`.
            self.send_room_text(room, "🎙️ جاهز، أرسل: بث اسم الأغنية لتشغيلها")
        except Exception as exc:
            self._live_ready_rooms.discard(room)
            self.log("[STREAM] manual live join failed:", repr(exc))
            self.send_room_text(room, f"❌ تعذر صعود البوت للبث: {str(exc)[:180]}")
        return True

    def _master_is_online(self):
        """Return the latest presence state known by this bot connection."""
        return bool(getattr(self, "master_online", False))

    def _resolve_live_room_key(self, candidate_room="", candidate_room_id="", candidate_invite_id="", candidate_token=""):
        """Resolve a live event to the correct room when multiple rooms are active."""
        candidate_room = str(candidate_room or "").strip()
        candidate_room_id = str(candidate_room_id or "").strip()
        candidate_invite_id = str(candidate_invite_id or "").strip()
        candidate_token = str(candidate_token or "").strip()
        pending = getattr(self, "_pending_live_accepts", {}) or {}
        sessions = getattr(self, "_live_session_by_room", {}) or {}

        def norm(v):
            try:
                return _norm_room(v).casefold()
            except Exception:
                return str(v or "").strip().casefold()

        if candidate_room:
            n = norm(candidate_room)
            for key in list(pending.keys()) + [k for k in sessions.keys() if k not in pending]:
                if norm(key) == n:
                    return key

        # Match by room id, invitation/session id, or the original (small)
        # invitation token. Never compare/log the large LiveKit JWT itself.
        for key, data in pending.items():
            if not isinstance(data, dict):
                continue
            if candidate_room_id and str(data.get("room_id", "")) == candidate_room_id:
                return key
            if candidate_invite_id and candidate_invite_id in {
                str(data.get("invite_id", "")),
                str(data.get("session_id", "")),
                str(data.get("publish_id", "")),
            }:
                return key
            if candidate_token and len(candidate_token) < 256 and str(data.get("token", "")) == candidate_token:
                return key

        for key, data in sessions.items():
            if not isinstance(data, dict):
                continue
            if candidate_room_id and str(data.get("room_id", "")) == candidate_room_id:
                return key
            if candidate_invite_id and candidate_invite_id in {
                str(data.get("invite_id", "")),
                str(data.get("session_id", "")),
                str(data.get("publish_id", "")),
            }:
                return key
            if candidate_token and len(candidate_token) < 256 and str(data.get("token", "")) == candidate_token:
                return key

        return candidate_room

    def _handle_stream_event(self, event):
        """Accept a real you_invited event, then publish the queued track with distinct stage logging."""
        if not isinstance(event, dict):
            return False
        event_type = str(event.get(1, "") or event.get("type", "") or "").strip().casefold()
        if event_type not in {"you_invited", "invited", "stream_invite", "live_invite"} and not any(token in event_type for token in ("invite", "invitation", "دعوه", "دعوة")):
            return False
        room_name = str(event.get(8, "") or event.get(2, "") or event.get("room", "") or getattr(self, "room", "") or "").strip()
        room_id = str(event.get(6, "") or event.get(3, "") or event.get("room_id", "") or room_name).strip()
        if room_name and room_id.isdigit():
            getattr(self, "_live_room_ids", {}).update({room_name: room_id})
        invitation_stream_id = str(event.get(9, "") or event.get("stream_id", "") or event.get("id", "") or "").strip()
        invite_id = invitation_stream_id
        if event_type in {"sent_invitation", "invitation_sent", "sent_invite", "دعوة_مرسلة", "دعوه_مرسله"}:
            # This is an outgoing invitation notification, not an incoming
            # seat invitation. Ignore it silently so the master is not spammed.
            self.log("[STREAM] ignoring outgoing sent_invitation event", room_name)
            return False

        stream_token = str(event.get(5, "") or event.get("token", "") or "").strip()
        room_name = self._resolve_live_room_key(room_name, room_id, invitation_stream_id, stream_token) or room_name
        if not room_id:
            room_id = str(getattr(self, "_live_room_ids", {}).get(room_name, "") or room_name).strip()

        # === المرحلة 1: استقبال الدعوة وتحليل حقولها ===
        fields_summary = ",".join(str(k) for k in sorted(event.keys(), key=str) if str(k).isdigit())[:80]
        self._log_stream_stage(
            "1_دعوة",
            "استلام_الدعوة",
            room=room_name,
            room_id=room_id,
            invite_id=invite_id,
            token_present=bool(stream_token),
            fields=fields_summary,
        )

        if not stream_token:
            self._log_stream_stage(
                "1_خطأ_دعوة",
                "فقدان_التوكن",
                room=room_name,
                room_id=room_id,
                invite_id=invite_id,
                hint="الحقل 5 / token غير متوفر في الدعوة",
            )
            self._log_stream_accept_error(
                "stage1_missing_token", "you_invited has no field-5 token", room_name, room_id, invite_id
            )
            return False

        try:
            if BOT_MASTER and _norm_user(BOT_MASTER) != _norm_user(BOT_ID):
                pass
        except Exception as exc:
            self.log("[STREAM] invite event report failed:", repr(exc))

        pending_tracks = getattr(self, "_pending_live_tracks", {})
        pending = pending_tracks.get(room_name)
        if not pending and room_id:
            pending_key = next((key for key, item in pending_tracks.items()
                                if str(item.get("room_id", "")) == room_id), None)
            pending = pending_tracks.get(pending_key) if pending_key else None
            room_name = pending_key or room_name

        if not room_id:
            self._log_stream_stage(
                "1_خطأ_دعوة",
                "فقدان_معرف_الغرفة",
                room=room_name,
                invite_id=invite_id,
            )
            self._log_stream_accept_error(
                "stage1_missing_room_id", "دعوة البث لا تحتوي على room_id", room_name, room_id, invite_id
            )
            return False

        try:
            accepted_invites = getattr(self, "_accepted_live_invites", set())
            invitation_key = (room_name, invitation_stream_id or room_id)
            if invitation_key in accepted_invites:
                self._log_stream_stage(
                    "1_تجاهل",
                    "دعوة_مكررة",
                    room=room_name,
                    invite_id=invitation_stream_id,
                )
                return True

            # === المرحلة 2.1: إرسال حزمة room_stream (type=publish) ===
            self._log_stream_stage(
                "2.1_إرسال_publish",
                "بدء_الإرسال",
                room=room_name,
                action=STREAM_ROOM_ACTION,
                type=STREAM_ROOM_TYPE,
                to=stream_token[:20] + "...",
                id=invitation_stream_id,
            )
            try:
                self.send_query(encode_query(
                    STREAM_ROOM_ACTION, type_=STREAM_ROOM_TYPE, to=stream_token,
                    token=stream_token, id_=invitation_stream_id,
                    room=room_name,
                ))
                self._log_stream_stage(
                    "2.1_تم_إرسال_publish",
                    "نجاح_الإرسال",
                    room=room_name,
                )
            except Exception as exc_pub:
                self._log_stream_stage(
                    "2.1_خطأ_publish",
                    "فشل_الإرسال",
                    room=room_name,
                    error=str(exc_pub),
                )
                self._log_stream_accept_error("stage2_publish_send_failed", exc_pub, room_name, room_id, invite_id)
                raise

            # === المرحلة 2.2: إرسال حزم stream_accept لحجز المايك ===
            self._log_stream_stage(
                "2.2_إرسال_stream_accept",
                "بدء_الإرسال",
                room=room_name,
                action=STREAM_ACCEPT_ACTION,
                state="accept",
            )
            try:
                # إرسال باسم الغرفة
                self.send_query(encode_query(
                    STREAM_ACCEPT_ACTION, type_="accept", to=stream_token,
                    token=stream_token, id_=invitation_stream_id,
                    room=room_name, state="accept", value="accept",
                ))
                self._log_stream_stage(
                    "2.2_تم_stream_accept",
                    "تم_الإرسال_باسم_الغرفة",
                    room=room_name,
                )

                # إرسال برقم الغرفة (إذا كان مختلفاً ورقمياً)
                if room_id and room_id != room_name and room_id.isdigit():
                    self.send_query(encode_query(
                        STREAM_ACCEPT_ACTION, type_="accept", to=stream_token,
                        token=stream_token, id_=invitation_stream_id,
                        room=room_id, state="accept", value="accept",
                    ))
                    self._log_stream_stage(
                        "2.2_تم_stream_accept",
                        "تم_الإرسال_برقم_الغرفة",
                        room=room_name,
                        room_id=room_id,
                    )
            except Exception as exc_acc:
                self._log_stream_stage(
                    "2.2_خطأ_stream_accept",
                    "فشل_stream_accept",
                    room=room_name,
                    error=str(exc_acc),
                )
                self.log("[STREAM] stage2 stream_accept error:", repr(exc_acc))

            # محاولة احتياطية مجدولة برقم الغرفة في حال تأخر publish_stream
            if room_id and room_id != room_name and room_id.isdigit():
                def _live_room_id_fallback():
                    try:
                        time.sleep(float(os.getenv("STREAM_ROOM_ID_FALLBACK_DELAY", "1.2")))
                        current = getattr(self, "_pending_live_accepts", {}).get(room_name, {})
                        if current.get("publish_confirmed"):
                            return
                        self._log_stream_stage(
                            "2.3_إعادة_محاولة_room_id",
                            "تأخر_التأكيد_إرسال_بالرقم",
                            room=room_name,
                            room_id=room_id,
                        )
                        self.send_query(encode_query(
                            STREAM_ROOM_ACTION,
                            type_=STREAM_ROOM_TYPE,
                            to=stream_token,
                            room=room_id,
                        ))
                    except Exception as exc:
                        self.log("[STREAM] stage2 room_id fallback failed:", repr(exc))

                threading.Thread(
                    target=_live_room_id_fallback,
                    name="talkin-live-room-id-fallback",
                    daemon=True,
                ).start()

            accepted_invites.add(invitation_key)
            self._accepted_live_invites = accepted_invites
            with getattr(self, "_live_state_lock", threading.RLock()):
                pending_accepts = getattr(self, "_pending_live_accepts", None)
                if not isinstance(pending_accepts, dict):
                    pending_accepts = {}
                    self._pending_live_accepts = pending_accepts
                pending_accepts[room_name] = {
                "room_id": room_id,
                "session_id": "",
                "publish_id": "",
                "livekit_token": "",
                "token": stream_token,
                "invite_id": invitation_stream_id,
                "sent_at": time.time(),
                    "publish_confirmed": False,
                }
            try:
                msg2 = "\n".join([
                    f"📤 [مرحلة 2: إرسال قبول] أرسلت حزم room_stream و stream_accept في {room_name}",
                    f"📌 token={stream_token[:20]}... | room={room_name} | id={room_id}",
                    "⏳ بانتظار تأكيد الخادم (publish_stream)."
                ])
                self.send_private_text(BOT_MASTER, msg2)
            except Exception as exc:
                self.log("[STREAM] stage2 notification failed:", repr(exc))

            if not pending:
                self.log("[STREAM] accepted seat invitation; waiting for بث command", room_name)
                return True
            self.log("[STREAM] audio queued until server confirms live seat", room_name)
            return True
        except Exception as exc:
            self._log_stream_stage(
                "2_خطأ_عام",
                "فشل_المرحلة_2",
                room=room_name,
                room_id=room_id,
                error=str(exc),
            )
            self._log_stream_accept_error(
                "stage2_accept_general_error", exc, room_name, room_id, invite_id
            )
            self.log("[STREAM] stage2 invitation accept failed:", repr(exc))
            return False

    def _live_master_debug(self, title, **data):
        """Safely record live-stream diagnostics in console logs without spamming private chat."""
        try:
            parts = [f"[STREAM_DEBUG] {title}"]
            for key, value in data.items():
                if value is None:
                    continue
                v_str = str(value).replace("\n", " ").strip()
                if len(v_str) > 60:
                    v_str = v_str[:57] + "..."
                parts.append(f"{key}={v_str}")
            self.log(" | ".join(parts))
        except Exception:
            pass

    def _handle_stream_result_ack(self, result):
        """Report the server's acceptance/failure after stream_accept."""
        if not isinstance(result, dict):
            return False
        kind = str(result.get("type", "") or result.get("value", "") or "").strip().casefold()
        if not any(word in kind for word in ("stream", "live", "check", "accept", "رفض", "فشل")):
            return False
        pending = getattr(self, "_pending_live_accepts", {})
        room = str(result.get("room", "") or result.get(8, "") or "").strip()
        if not room and len(pending) == 1:
            room = next(iter(pending))
        if not room or room not in pending:
            self._live_master_debug(
                "رد بث غير مطابق",
                type=kind,
                room=room,
                pending_rooms=",".join(str(x) for x in pending.keys())[:100],
            )
            return False
        positive = any(word in kind for word in ("started", "accepted", "accept", "streaming", "live_ok", "success", "ok", "تم"))
        negative = any(word in kind for word in ("reject", "رفض", "failed", "فشل", "denied", "error"))
        if negative:
            self._live_master_debug(
                "الخادم رفض البث",
                type=kind,
                room=room,
                result=str(result),
            )

        if positive and not negative:
            self._live_master_debug(
                "الخادم أكد البث",
                type=kind,
                room=room,
            )
            accepted = pending.pop(room, None) or {}
            live_sessions = getattr(self, "_live_session_by_room", {})
            if accepted:
                live_sessions[room] = accepted
                self._live_session_by_room = live_sessions
            self._live_ready_rooms = getattr(self, "_live_ready_rooms", set())
            self._live_ready_rooms.add(room)
            self.send_private_text(BOT_MASTER, f"✅ أكد الخادم صعود البوت للبث في الغرفة: {room}")
            track = getattr(self, "_pending_live_tracks", {}).pop(room, None)
            if track:
                try:
                    time.sleep(float(os.getenv("STREAM_AUDIO_DELAY", "0.05")))
                    self.log("[STREAM] publish queued audio", STREAM_AUDIO_ACTION, accepted.get("room_id", ""))
                    self.send_query(encode_query(
                        STREAM_AUDIO_ACTION,
                        type_="audio",
                        room=str(accepted.get("room_id", "") or ""),
                        id_=str(accepted.get("session_id", "") or ""),
                        url=str(track["url"]),
                        length=str(max(0, int(track.get("duration") or 0))),
                    ))
                except Exception as exc:
                    self._log_stream_accept_error(
                        "audio_after_accept", exc, room, accepted.get("room_id", ""), ""
                    )
            return True
        if negative:
            detail = str(result.get("value", "") or result.get("type", "") or "غير معروف")[:180]
            pending.pop(room, None)
            self.send_private_text(BOT_MASTER, f"❌ رفض الخادم صعود البوت في {room}: {detail}")
            return True
        return False
