"""Talkin-native/system invite and publish-stream invitation confirmation methods."""

class SystemInvitationSection:
    def send_live_invitation_to_user(self, target, room):
        """Send the platform-native live invitation to a target.

        The `sent_invitation` frame is a server event emitted after the native
        room invitation is created; sending that frame over WebSocket does not
        create an invitation or the green system message in the room.
        """
        target = str(target or "").strip().lstrip("@")
        room = str(room or "").strip()
        if not target or not room:
            return False
        try:
            ok, detail = self.send_native_system_invite(target, room)
            if not ok:
                self.log("[STREAM] native invitation rejected:", target, room, detail)
                self.send_private_text(
                    BOT_MASTER,
                    f"❌ لم تُنشأ دعوة بث فعلية إلى @{target} في {room}: {detail}",
                )
                return False
            self.send_private_text(BOT_MASTER, f"📨 أرسلت دعوة بث إلى @{target} في {room}\n📌 action={STREAM_ROOM_ACTION} type=invite")
            return True
        except Exception as exc:
            self.log("[STREAM] manual invitation failed:", repr(exc))
            self.send_private_text(BOT_MASTER, f"❌ فشل إرسال دعوة البث إلى @{target}: {str(exc)[:180]}")
            return False

    def _stream_event_contains_publish_stream(self, value, _seen=None):
        """Return True when a direct or nested Talkin event contains publish_stream.

        Different app/server builds may place the event name at different
        protobuf fields or wrap it inside another StreamEvent/CallInfo object.
        """
        if _seen is None:
            _seen = set()
        if isinstance(value, (bytes, bytearray)):
            key = id(value)
            if key in _seen:
                return False
            _seen.add(key)
            try:
                value = decode_generic(bytes(value))
            except Exception:
                try:
                    value = bytes(value).decode("utf-8", "ignore")
                except Exception:
                    return False

        if isinstance(value, str):
            return "publish_stream" in value.casefold()

        if isinstance(value, dict):
            key = id(value)
            if key in _seen:
                return False
            _seen.add(key)
            for k, v in value.items():
                if isinstance(k, str) and "publish_stream" in k.casefold():
                    return True
                if isinstance(v, str) and "publish_stream" in v.casefold():
                    return True
                if self._stream_event_contains_publish_stream(v, _seen):
                    return True
            return False

        if isinstance(value, (list, tuple, set)):
            for item in value:
                if self._stream_event_contains_publish_stream(item, _seen):
                    return True
        return False

    def _handle_publish_stream_confirmation(self, result):
        """Handle a publish_stream event only when a live invitation is pending with distinct stage logs."""
        if not self._stream_event_contains_publish_stream(result):
            return False

        pending = getattr(self, "_pending_live_accepts", {})
        if not isinstance(pending, dict):
            pending = {}
            self._pending_live_accepts = pending

        room = ""
        event_probe = result.get("stream_event", {}) if isinstance(result, dict) else {}
        raw_room = ""
        raw_room_id = ""
        raw_event_id = ""
        raw_token = ""
        if isinstance(result, dict):
            raw_room = str(
                result.get("room", "")
                or result.get(8, "")
                or result.get("room_name", "")
                or ""
            ).strip()
            raw_room_id = str(result.get("room_id", "") or result.get(6, "") or "").strip()
        if isinstance(event_probe, dict):
            raw_room = raw_room or str(event_probe.get("room", "") or event_probe.get(8, "") or "").strip()
            raw_room_id = raw_room_id or str(event_probe.get("room_id", "") or event_probe.get(6, "") or "").strip()
            raw_event_id = str(event_probe.get("id", "") or event_probe.get(9, "") or "").strip()
            raw_token = str(event_probe.get("token", "") or event_probe.get(5, "") or "").strip()

        # Critical for multi-room operation: do not fall back to the first/only
        # room until we have tried room/id/session/token matching.
        room = self._resolve_live_room_key(raw_room, raw_room_id, raw_event_id, raw_token)

        if not room and len(pending) == 1:
            room = next(iter(pending))
        if not room:
            sessions = getattr(self, "_live_session_by_room", {})
            if len(sessions) == 1:
                room = next(iter(sessions))

        # Some server builds consume the invitation record before publish_stream
        # arrives. Rebuild a usable session from the publish event instead of
        # treating it as stale.
        accepted = pending.get(room) or getattr(self, "_live_session_by_room", {}).get(room) or {}
        stream_event_probe = result.get("stream_event", {}) if isinstance(result, dict) else {}
        if not accepted and isinstance(stream_event_probe, dict):
            probe_room_id = str(stream_event_probe.get(8, "") or stream_event_probe.get("room_id", "") or "").strip()
            probe_id = str(stream_event_probe.get(9, "") or stream_event_probe.get("id", "") or "").strip()
            probe_token = str(stream_event_probe.get(5, "") or stream_event_probe.get("token", "") or "").strip()
            if room:
                accepted = {
                    "room_id": probe_room_id if probe_room_id.isdigit() else "",
                    "session_id": probe_id,
                    "publish_id": probe_id,
                    "livekit_token": probe_token,
                    "token": probe_token,
                    "invite_id": probe_id,
                    "sent_at": time.time(),
                    "publish_confirmed": False,
                }
                pending[room] = accepted

        if not room or not accepted:
            self.log("[STREAM] ignored publish_stream: no room/session data", room)
            self._log_stream_stage(
                "3_تجاهل",
                "publish_stream_بدون_جلسة",
                room=room,
            )
            return True

        stream_event = result.get("stream_event", {}) if isinstance(result, dict) else {}

        # === المرحلة 3.1: استلام وتطابق حدث publish_stream ===
        self._log_stream_stage(
            "3.1_تأكيد_publish_stream",
            "تم_تطابق_الحدث_مع_الدعوة",
            room=room,
            room_id=accepted.get("room_id", ""),
            uid=str(result.get("uid", "") if isinstance(result, dict) else ""),
            type=str(result.get("type", "") if isinstance(result, dict) else ""),
        )

        if isinstance(stream_event, dict) and stream_event:
            self._log_stream_stage(
                "3.1_تفاصيل_stream_event",
                "بيانات_الجلسة",
                room=room,
                event_type=stream_event.get("type", ""),
                event_room=stream_event.get("room", "") or stream_event.get(8, ""),
                event_id=stream_event.get("id", "") or stream_event.get(9, ""),
                has_event_token=bool(stream_event.get("token", "") or stream_event.get(5, "")),
            )

        if accepted.get("publish_confirmed"):
            self._log_stream_stage(
                "3.1_تكرار",
                "تم_تأكيد_البث_مسبقاً",
                room=room,
            )
            return True

        self._live_ready_rooms = getattr(self, "_live_ready_rooms", set())
        self._live_ready_rooms.add(room)

        # استخراج بيانات الجلسة الحية لتثبيت المقعد
        stream_event_data = result.get("stream_event", {}) if isinstance(result, dict) else {}
        event_id = str(stream_event_data.get(9, "") or stream_event_data.get("id", "") or accepted.get("invite_id", "") or "").strip()
        event_token = str(stream_event_data.get(5, "") or stream_event_data.get("token", "") or accepted.get("token", "") or "").strip()
        event_room = str(stream_event_data.get(8, "") or stream_event_data.get("room", "") or room).strip()
        live_room_id = str(accepted.get("room_id", "") or "").strip()

        # publish_stream يحمل معرف جلسة البث الحقيقي في field 9،
        # والتوكن الخاص بـLiveKit في field 5. يجب حفظهما قبل تشغيل الصوت.
        # session_id كان يبقى فارغاً في النسخة السابقة، ولذلك كانت حزمة
        # room_stream تصل بلا جلسة مرتبطة بمقعد المتحدث.
        if event_id:
            accepted["session_id"] = event_id
            accepted["publish_id"] = event_id
        if event_token:
            accepted["livekit_token"] = event_token
        if event_room and not live_room_id and event_room.isdigit():
            live_room_id = event_room
            accepted["room_id"] = event_room
            try:
                getattr(self, "_live_room_ids", {})[room] = event_room
            except Exception:
                pass
        accepted["publish_confirmed"] = True
        accepted["publish_confirmed_at"] = time.time()
        with getattr(self, "_live_state_lock", threading.RLock()):
            pending[room] = accepted
            self._pending_live_accepts = pending
            live_sessions = getattr(self, "_live_session_by_room", {})
            if not isinstance(live_sessions, dict):
                live_sessions = {}
            live_sessions[room] = accepted
            self._live_session_by_room = live_sessions
        self._live_ready_rooms = getattr(self, "_live_ready_rooms", set())
        self._live_ready_rooms.add(room)

        # === المرحلة 3.2: تثبيت المقعد ومعالجة publish_stream ===
        # ملاحظة هامة: حدث publish_stream هو تأكيد الخادم الرسمي بأن البوت أصبح على المايك.
        # توكن publish_stream (event_token) هو توكن وسائط JWT طويل (500 بايت) مخصص لخادم الميديا.
        # إرساله في stream_accept أو كرسالة خاصة يتسبب فوراً في إغلاق السيرفر للاتصال بالرمز 1009 (رسالة كبيرة جداً).
        try:
            # إرسال ack_msg فقط إذا توفر معرف الرسالة لتأكيد الاستلام دون إرسال توكنات ضخمة
            uid_val = str(result.get("uid") or "").strip()
            if uid_val:
                self.send_query(encode_query("ack_msg", uid=uid_val))
                self._log_stream_stage(
                    "3.2_ack_msg",
                    "تم_إرسال_ack",
                    room=room,
                    uid=uid_val,
                )
        except Exception as exc:
            self.log("[STREAM] ack_msg error:", repr(exc))

        # === ربط جلسة الصوت الحية عبر خادم LiveKit للصعود الفعلي للمايك ===
        livekit_token = event_token or accepted.get("token", "")
        if livekit_token and len(livekit_token) > 50:
            def _run_livekit_client(token_jwt, r_name, r_id):
                """Connect to LiveKit as a real publisher using the official SDK."""
                loop = None
                try:
                    from livekit import rtc

                    lk_url = os.getenv("LIVEKIT_URL", "ws://chatp.net:7880").strip()
                    if lk_url.endswith("/rtc"):
                        lk_url = lk_url[:-4].rstrip("/")

                    self.log("[LIVEKIT] بدء Publisher حقيقي:", r_name, lk_url)
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    room_obj = rtc.Room()
                    setattr(self, "_livekit_loop_" + r_name, loop)
                    setattr(self, "_livekit_room_" + r_name, room_obj)

                    async def _connect_publish():
                        await room_obj.connect(lk_url, token_jwt)
                        self.log("[LIVEKIT] ✅ تم Join WebRTC:", r_name)

                        source = rtc.AudioSource(48000, 1, queue_size_ms=2000, loop=loop)
                        track = rtc.LocalAudioTrack.create_audio_track("music", source)
                        options = rtc.TrackPublishOptions(
                            source=rtc.TrackSource.SOURCE_MICROPHONE,
                            dtx=False,
                            red=True,
                        )
                        publication = await room_obj.local_participant.publish_track(track, options)
                        try:
                            track.unmute()
                        except Exception:
                            pass

                        setattr(self, "_livekit_source_" + r_name, source)
                        setattr(self, "_livekit_track_" + r_name, track)
                        setattr(self, "_livekit_publication_" + r_name, publication)

                        subscribed = False
                        try:
                            await asyncio.wait_for(
                                publication.wait_for_subscription(),
                                timeout=float(os.getenv("STREAM_FIRST_SUBSCRIPTION_TIMEOUT", "6")),
                            )
                            subscribed = True
                        except Exception as exc:
                            self.log("[LIVEKIT] no listener subscription yet; continuing publisher:", r_name, repr(exc))

                        setattr(self, "_livekit_subscribed_" + r_name, subscribed)
                        setattr(self, "_livekit_active_" + r_name, True)

                        sid = str(getattr(publication, "sid", "") or "")
                        muted = bool(getattr(track, "muted", False))
                        self.log(
                            "[LIVEKIT] 🎙️ البوت الآن Publisher/متحدث:", r_name,
                            "track_sid=", sid,
                            "muted=", muted,
                            "subscribed=", subscribed,
                        )

                        # Keep the SDK event loop alive for the entire live-seat session.
                        while not getattr(self, "_livekit_stop_requested_" + r_name, False):
                            await asyncio.sleep(2)

                    loop.run_until_complete(_connect_publish())
                except Exception as exc:
                    self.log("[LIVEKIT] ❌ فشل Publisher الحقيقي:", repr(exc))
                    setattr(self, "_livekit_active_" + r_name, False)
                finally:
                    if loop is not None:
                        try:
                            loop.close()
                        except Exception:
                            pass

            threading.Thread(
                target=_run_livekit_client,
                args=(livekit_token, room, live_room_id),
                name=f"talkin-livekit-{room}",
                daemon=True,
            ).start()

        # === المرحلة 3.4: إعلان الصعود والنجاح النهائي ===
        try:
            self.send_private_text(
                BOT_MASTER,
                f"📡 [مرحلة 3: تأكيد نهائي] وصل تأكيد publish_stream وصعد البوت للمايك بنجاح في: {room}",
            )
            self._log_stream_stage(
                "3.4_صعود_ناجح",
                "البوت_على_المايك",
                room=room,
            )
        except Exception as exc:
            self.log("[STREAM] publish confirmation report failed:", repr(exc))

        try:
            pass  # الصعود الناجح يكون صامتًا؛ تم إرسال رسالة الجاهزية عند أمر اصعد
        except Exception as exc:
            self.log("[STREAM] room live announcement failed:", repr(exc))

        # تشغيل الصوت المعلق إن وُجد
        track = getattr(self, "_pending_live_tracks", {}).pop(room, None)
        if track:
            try:
                time.sleep(float(os.getenv("STREAM_AUDIO_DELAY", "0.05")))
                room_id = str(
                    accepted.get("room_id", "")
                    or getattr(self, "_live_room_ids", {}).get(room, "")
                    or ""
                )
                session_id = str(
                    accepted.get("session_id", "")
                    or accepted.get("publish_id", "")
                    or accepted.get("id_", "")
                    or accepted.get("invite_id", "")
                    or ""
                )
                # لا نرسل الصوت قبل أن تصبح جلسة LiveKit جاهزة.
                if not getattr(self, f"_livekit_active_{room}", False):
                    wait_until = time.time() + float(os.getenv("STREAM_LIVEKIT_READY_TIMEOUT", "12"))
                    while time.time() < wait_until:
                        if getattr(self, f"_livekit_active_{room}", False):
                            break
                        time.sleep(0.10)
                self._log_stream_stage(
                    "3.5_بث_الصوت",
                    "إرسال_حزمة_الصوت",
                    room=room,
                    room_id=room_id,
                )
                wait_until = time.time() + float(os.getenv("STREAM_LIVEKIT_READY_TIMEOUT", "15"))
                while not getattr(self, f"_livekit_active_{room}", False) and time.time() < wait_until:
                    time.sleep(0.25)
                if getattr(self, f"_livekit_active_{room}", False):
                    first_frame = self._feed_livekit_audio(room, str(track["url"]), int(track.get("duration") or 0))
                    while not first_frame:
                        alternative = self._next_music_fallback_source(room, track)
                        if not alternative:
                            break
                        alternative_url = str(alternative.get("url") or "").strip()
                        if not alternative_url:
                            continue
                        track["url"] = alternative_url
                        track["duration"] = int(alternative.get("duration") or track.get("duration") or 0)
                        first_frame = self._feed_livekit_audio(
                            room, alternative_url, int(track.get("duration") or 0)
                        )
                        self.log("[MUSIC] LiveKit fallback source tested:",
                                 alternative.get("source"), "started=", first_frame, "room=", room)
                    status = getattr(self, "_last_live_play_status_by_room", {})
                    if first_frame:
                        status[room] = "started"
                        requester_name = str(track.get("requester") or "").strip()
                        music_current = getattr(self, "music_current", {})
                        if requester_name:
                            current = music_current.get(_norm_user(requester_name), {})
                            current.update(url=str(track.get("url") or ""),
                                           duration=int(track.get("duration") or 0),
                                           created_at=time.time())
                            music_current[_norm_user(requester_name)] = current
                        self.send_room_text(room, "تم تشغيل الطلب في البث")
                    else:
                        status[room] = "failed"
                        self.send_room_text(room, "تعذر تشغيل الطلب في البث؛ لم يصل صوت فعلي.")
                    self._last_live_play_status_by_room = status
                else:
                    self.log("[LIVEKIT] لم تصبح جلسة Publisher جاهزة ضمن المهلة:", room)
                self.send_query(encode_query(
                    STREAM_AUDIO_ACTION,
                    type_="audio",
                    room=room_id or room,
                    id_=session_id,
                    url=str(track["url"]),
                    length=str(max(0, int(track.get("duration") or 0))),
                ))
                try:
                    self.send_query(encode_query(
                        "room_stream",
                        type_="audio",
                        room=room_id or room,
                        id_=session_id,
                        url=str(track["url"]),
                        length=str(max(0, int(track.get("duration") or 0))),
                    ))
                except Exception:
                    pass
                try:
                    self.send_room_media(room, str(track["url"]), "audio", int(track.get("duration") or 0))
                except Exception:
                    pass
            except Exception as exc:
                self._log_stream_stage(
                    "3.5_خطأ_بث_الصوت",
                    "فشل_إرسال_الصوت",
                    room=room,
                    error=str(exc),
                )
                self._log_stream_accept_error(
                    "stage3_audio_after_publish_stream",
                    exc,
                    room,
                    accepted.get("room_id", ""),
                    accepted.get("invite_id", ""),
                )
        return True

    def send_native_system_invite(self, username: str, room: str):
        """Send the same live-seat invitation query as Talkinchat 5.8.3.

        The APK's ``C2523c.m3253q`` builds a Query with:
            action=room_stream, type=invite, to=<username>, room=<room name>
        It is not the generic ``sent_invitation`` event and does not require a
        Supabase RPC or a numeric room id.
        """
        username = str(username or "").strip().lstrip("@")
        room = str(room or "").strip()
        if not username or not room:
            return False, "username و room مطلوبان"
        try:
            self.send_query(encode_query(
                STREAM_ROOM_ACTION,
                type_="invite",
                to=username,
                room=room,
            ))
            self.log("[STREAM] app-compatible invitation sent:", username, room)
            return True, "ok"
        except Exception as e:
            detail = repr(e)
            self.log("[STREAM] invitation exception:", username, detail)
            return False, detail
