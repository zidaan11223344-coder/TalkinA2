"""YouTube/music search, source selection, download, direct-play test, sharing and live audio."""

class MusicSection:
    def _play_music_in_live_room(self, room: str, media_url: str, duration: int = 0):
        """Play immediately if already live on mic, or queue audio and join live stream."""
        room = str(room or "").strip()
        media_url = str(media_url or "").strip()
        self._last_live_play_status_by_room = getattr(self, "_last_live_play_status_by_room", {})
        self._last_live_play_status_by_room[room] = "failed"
        if not room or not media_url:
            return False
        # الملف المحلي هو المصدر الحقيقي للـLiveKit، والرابط العام يستخدم فقط
        # لحزم Talkin القديمة إذا احتاجها الخادم.
        packet_url = media_url
        try:
            local_media = Path(media_url).expanduser()
            if local_media.is_file():
                base = _public_base_url()
                if base:
                    packet_url = base.rstrip("/") + "/media/" + local_media.name
        except Exception:
            pass
        if not all((STREAM_INVITE_ACTION, STREAM_ACCEPT_ACTION, STREAM_AUDIO_ACTION)):
            self.log("[STREAM] experimental actions are not fully configured")
            return False
        try:
            ready_rooms = getattr(self, "_live_ready_rooms", set())
            pending_accepts = getattr(self, "_pending_live_accepts", {})
            live_sessions = getattr(self, "_live_session_by_room", {})
            state_room, accepted, is_active = self._live_room_is_active(room)
            if state_room and state_room != room:
                self.log("[STREAM] mapped command room to live session:", room, "->", state_room)
                room = state_room

            # إذا كان البوت صاعداً للبث والمايك بالفعل، نبث الصوت فوراً لجميع القنوات
            if is_active:
                self.log("[STREAM] البوت صاعد للمايك؛ جاري بث الصوت فوراً في:", room)
                ready_rooms.add(room)
                self._live_ready_rooms = ready_rooms

                room_id = str(
                    accepted.get("room_id", "")
                    or getattr(self, "_live_room_ids", {}).get(room, "")
                    or ""
                )
                if not room_id and getattr(self, "db", None):
                    try:
                        room_id = str(self.db.room_id(room) or "").strip()
                    except Exception:
                        pass
                if not room_id:
                    room_id = room

                session_id = str(
                    accepted.get("session_id", "")
                    or accepted.get("publish_id", "")
                    or accepted.get("id_", "")
                    or accepted.get("invite_id", "")
                    or ""
                )

                # انتظر حتى تصبح قناة LiveKit فعّالة فعلياً قبل إرسال الصوت.
                # إرسال room_stream قبل جاهزية الجلسة يجعل الخادم يعامل البوت
                # كمستمع ولا يربط الصوت بمقعد المايك.
                if not getattr(self, f"_livekit_active_{room}", False):
                    wait_until = time.time() + float(os.getenv("STREAM_LIVEKIT_READY_TIMEOUT", "8"))
                    while time.time() < wait_until:
                        if getattr(self, f"_livekit_active_{room}", False):
                            break
                        time.sleep(0.10)

                # لا نعتبر الأمر ناجحاً لمجرد وصول publish_stream؛ يجب أن تكون
                # جلسة LiveKit نفسها متصلة وبها AudioSource قبل إعلان التشغيل.
                livekit_source = getattr(self, f"_livekit_source_{room}", None)
                livekit_loop = getattr(self, f"_livekit_loop_{room}", None)
                if (not getattr(self, f"_livekit_active_{room}", False)
                        or livekit_source is None
                        or livekit_loop is None
                        or getattr(livekit_loop, "is_closed", lambda: True)()):
                    self.log("[STREAM] LiveKit publisher غير جاهز فعلياً؛ لن نعلن نجاح التشغيل:", room)
                    return False

                # بعد اتصال LiveKit، أعد تثبيت مقعد المتحدث في بوابة Talkin نفسها.
                # هذا مهم لأن بعض الإصدارات تعرض Publisher كـListener حتى يصل
                # تأكيد native publish/accept مرة أخرى بعد جاهزية WebRTC.
                self._reassert_live_speaker(room, accepted)

                # البث الحقيقي: أرسل الملف داخل مسار LiveKit المنشور فعلياً.
                # حزم room_stream أدناه تبقى للتوافق مع خوادم Talkin القديمة.
                live_audio_started = self._feed_livekit_audio(room, media_url, duration)
                self.log("[STREAM_VERIFY] LiveKit publisher first_frame=", live_audio_started, "room=", room)
                if not live_audio_started:
                    self.log("[STREAM_VERIFY] ❌ لم يتم إدخال أي إطار صوت فعلي إلى LiveKit:", room)
                    return False

                # حماية WebSocket من خطأ 1009: روابط اليوتيوب المباشرة تتجاوز 1500 بايت
                # وبث الصوت يتم عبر WebRTC/LiveKit، لذلك نرسل رابط قصير فقط إن وجد
                safe_ws_url = str(packet_url) if (
                    len(str(packet_url)) <= 250
                    and str(packet_url).lower().startswith(("https://", "http://"))
                ) else ""

                # LiveKit carries the actual audio frames. Do not send an empty
                # or local filesystem URL as a second room attachment: older
                # Talkin clients display that packet as a blank audio file.
                audio_sent = False
                if safe_ws_url:
                    try:
                        self.send_query(encode_query(
                            STREAM_AUDIO_ACTION,
                            type_="audio",
                            room=room_id,
                            id_=session_id,
                            url=safe_ws_url,
                            length=str(max(0, int(duration or 0))),
                        ))
                        self.log("[STREAM] أُرسلت حزمة STREAM_AUDIO_ACTION بنجاح إلى:", room)
                    except Exception as exc:
                        self.log("[STREAM] فشل إرسال STREAM_AUDIO_ACTION:", repr(exc))
                    try:
                        self.send_query(encode_query(
                            "room_stream",
                            type_="audio",
                            room=room_id,
                            id_=session_id,
                            url=safe_ws_url,
                            length=str(max(0, int(duration or 0))),
                        ))
                        audio_sent = True
                        self.log("[STREAM] أُرسلت حزمة room_stream audio بنجاح إلى:", room)
                    except Exception as exc:
                        self.log("[STREAM] فشل إرسال room_stream audio:", repr(exc))
                else:
                    self.log("[STREAM] skipped room audio attachment; LiveKit is carrying the audio frames:", room)

                # === اختبار آلي للتحقق من إرسال حزم الصوت وجاهزية البث ===
                lk_active = getattr(self, f"_livekit_active_{room}", False)
                lk_ws = getattr(self, f"_livekit_ws_{room}", None)
                socket_healthy = bool(lk_ws and getattr(lk_ws, "sock", None))
                self.log(f"[STREAM_VERIFY] فحص جاهزية البث في {room}: audio_sent={audio_sent}, livekit_active={lk_active}, socket_ok={socket_healthy}")

                # مراقبة EOF الحقيقي من FFmpeg، ثم إنزال البوت من البث تلقائياً.
                # رقم الجيل يمنع خيط أغنية قديمة من إيقاف بث أحدث في الغرفة نفسها.
                tokens = getattr(self, "_live_playback_tokens", None)
                if not isinstance(tokens, dict):
                    tokens = self._live_playback_tokens = {}
                playback_token = uuid.uuid4().hex
                tokens[room] = playback_token

                def _playback_lifecycle(target_room, token, track_len):
                    try:
                        self.log(f"[STREAM_PLAYBACK] مراقبة نهاية الأغنية في {target_room}.")
                        # FFmpeg sets this flag to False when the source reaches EOF.
                        # Keep a generous fallback deadline if a remote source stalls.
                        deadline = time.monotonic() + max(30, min(int(track_len or MUSIC_MAX_SECONDS) + 30, MUSIC_MAX_SECONDS + 30))
                        while time.monotonic() < deadline:
                            if getattr(self, "_live_playback_tokens", {}).get(target_room) != token:
                                self.log("[STREAM_PLAYBACK] تجاهل خيط تشغيل قديم:", target_room)
                                return
                            if not getattr(self, f"_livekit_active_{target_room}", False):
                                break
                            if not getattr(self, f"_livekit_audio_feeding_{target_room}", False):
                                break
                            time.sleep(0.5)
                        if getattr(self, "_live_playback_tokens", {}).get(target_room) != token:
                            return
                        # Let the final PCM frame drain before disconnecting the publisher.
                        time.sleep(0.7)
                        if getattr(self, "_live_playback_tokens", {}).get(target_room) == token:
                            self.log(f"[STREAM_PLAYBACK] انتهت الأغنية؛ إنزال البوت من بث {target_room}.")
                            self._stop_live_broadcast(target_room)
                    except Exception as play_err:
                        self.log("[STREAM_PLAYBACK] خطأ أثناء إنهاء البث:", repr(play_err))

                dur_sec = max(5, int(duration or MUSIC_MAX_SECONDS))
                threading.Thread(
                    target=_playback_lifecycle,
                    args=(room, playback_token, dur_sec),
                    name=f"stream-lifecycle-{room}",
                    daemon=True,
                ).start()

                self._last_live_play_status_by_room[room] = "started"
                return True

            # إذا لم يكن البوت على المايك بعد، نحفظ الصوت معلقاً ونطلب الصعود
            self._pending_live_tracks = getattr(self, "_pending_live_tracks", {})
            pending = {
                "url": media_url, "duration": int(duration or 0),
                "room_id": "", "created_at": time.time()
            }
            self._pending_live_tracks[room] = pending

            if STREAM_MANUAL_ACCEPT_ONLY:
                self.log("[STREAM] manual invitation mode: waiting for you_invited", room)
                return False

            room_id = str(getattr(self, "_live_room_ids", {}).get(room, "") or "").strip()
            if not room_id and getattr(self, "db", None):
                try:
                    room_id = str(self.db.room_id(room) or "").strip()
                except Exception as exc:
                    self.log("[STREAM] live room id lookup failed:", room, repr(exc))

            # The Talkin native room_stream(type=invite) packet is room-name based
            # on the current transport. Do not require a numeric DB id; doing so
            # silently disabled live-seat requests in rooms whose DB id is UUID/text.
            self.log("[STREAM] send native live invitation", room, "room_id=", room_id or "<name-only>")
            if not self.send_live_invitation_to_user(BOT_ID, room):
                raise RuntimeError("تعذر إنشاء دعوة البث للغرفة")
            # الطلب نجح، لكن قبول المقعد وpublish_stream غير متزامنين.
            # لا نعتبره فشلاً ولا نرسل رسالة خطأ للمستخدم؛ عند وصول
            # publish_stream سيُشغَّل المسار المعلّق تلقائياً.
            self._last_live_play_status_by_room[room] = "queued"
            return False
        except Exception as exc:
            self._last_live_play_status_by_room[room] = "failed"
            self.log("[STREAM] live play failed:", repr(exc))
            return False

    def _feed_livekit_audio(self, room: str, media_url: str, duration: int = 0):
        """Start real PCM audio feeding and return True only after first frame is captured."""
        room = str(room or "").strip()
        media_url = str(media_url or "").strip()
        if not room or not media_url:
            return False

        source = getattr(self, f"_livekit_source_{room}", None)
        loop = getattr(self, f"_livekit_loop_{room}", None)
        if source is None or loop is None or getattr(loop, "is_closed", lambda: True)():
            self.log("[LIVEKIT] audio feeder not ready:", room)
            return False

        existing = getattr(self, f"_livekit_audio_feeding_{room}", None)
        if existing:
            self.log("[LIVEKIT] audio feeder already running; waiting briefly for first frame:", room)
            ready_event = getattr(self, f"_livekit_first_frame_event_{room}", None)
            if ready_event is not None and ready_event.wait(timeout=3):
                return True
            return False

        setattr(self, f"_livekit_audio_stop_event_{room}", threading.Event())
        stop_audio = getattr(self, f"_livekit_audio_stop_event_{room}")
        setattr(self, f"_livekit_audio_proc_{room}", None)
        setattr(self, f"_livekit_audio_feeding_{room}", True)
        first_frame = threading.Event()
        setattr(self, f"_livekit_first_frame_event_{room}", first_frame)
        required_start_frames = 6
        state = {"ok": False, "frames": 0, "error": "", "natural_eof": False}

        def _run():
            proc = None
            try:
                source_path = Path(media_url).expanduser()
                is_local_file = source_path.is_file()
                # Audio transcoding is the other major CPU consumer on small
                # Raven containers. Keep ffmpeg single-threaded and lower its
                # scheduler priority so WebSocket/command handling stays
                # responsive while a live song is playing.
                ffmpeg_bin = "ffmpeg"
                ffmpeg_prefix = []
                if os.name == "posix" and shutil.which("nice") and os.getenv("FFMPEG_NICE", "10").strip() not in {"", "0"}:
                    ffmpeg_prefix = ["nice", "-n", os.getenv("FFMPEG_NICE", "10").strip()]
                ffmpeg_cmd = ffmpeg_prefix + [
                    ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-threads", os.getenv("FFMPEG_THREADS", "1"),
                    "-filter_threads", "1", "-filter_complex_threads", "1",
                    "-probesize", "32k", "-analyzeduration", "100000",
                    "-fflags", "nobuffer", "-flags", "low_delay", "-re",
                ]
                if not is_local_file:
                    ffmpeg_cmd += [
                        "-reconnect", "1", "-reconnect_streamed", "1",
                        "-reconnect_delay_max", "2",
                    ]
                ffmpeg_cmd += [
                    "-i", media_url, "-vn", "-ac", "1", "-ar", "48000",
                    "-af", "aresample=async=1:min_hard_comp=0.100:first_pts=0",
                    "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
                ]
                self.log("[LIVEKIT] starting ffmpeg audio feeder:", room, "source=", media_url if is_local_file else "remote")
                proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                setattr(self, f"_livekit_audio_proc_{room}", proc)
                bytes_per_frame = 960 * 2
                deadline = time.time() + max(5, int(duration or 900)) + 8
                while time.time() < deadline and not self.stop_event.is_set() and not stop_audio.is_set():
                    chunk = proc.stdout.read(bytes_per_frame)
                    if not chunk:
                        # An empty read after successful playback is the real
                        # end of the song, not merely a timeout.
                        state["natural_eof"] = bool(state["ok"] and not stop_audio.is_set())
                        err = b""
                        try:
                            err = proc.stderr.read() or b""
                        except Exception:
                            pass
                        state["error"] = err.decode("utf-8", "ignore")[-1000:]
                        break
                    if len(chunk) < bytes_per_frame:
                        chunk += b"\x00" * (bytes_per_frame - len(chunk))
                    from livekit import rtc
                    frame = rtc.AudioFrame(
                        data=chunk, sample_rate=48000, num_channels=1, samples_per_channel=960
                    )
                    capture = source.capture_frame(frame)
                    if inspect.isawaitable(capture):
                        fut = asyncio.run_coroutine_threadsafe(capture, loop)
                        fut.result(timeout=5)
                    state["ok"] = True
                    state["frames"] += 1
                    if state["frames"] == 1:
                        self.log("[LIVEKIT] first PCM frame accepted:", room, "bytes=", len(chunk))
                    # The caller waits for this exact burst threshold before
                    # checking state["frames"]. Signaling earlier than the
                    # success threshold caused a race: audio began playing, but
                    # the command reported that no real audio arrived.
                    if state["frames"] >= required_start_frames and not first_frame.is_set():
                        first_frame.set()
                self.log("[LIVEKIT] audio feeder finished:", room, "ok=", state["ok"], "error=", state["error"][:300])
            except FileNotFoundError as exc:
                state["error"] = "ffmpeg غير مثبت"
                self.log("[LIVEKIT] ffmpeg missing:", room, repr(exc))
            except Exception as exc:
                state["error"] = str(exc)[:1000]
                self.log("[LIVEKIT] audio feeder failed:", room, repr(exc))
            finally:
                first_frame.set()
                setattr(self, f"_livekit_audio_feeding_{room}", False)
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except Exception:
                        pass
                setattr(self, f"_livekit_audio_proc_{room}", None)
                # Do not leave the bot on the microphone when the source ends
                # naturally.  The token check avoids disconnecting a newer
                # playback if an old feeder exits late.
                if state.get("natural_eof") and not self.stop_event.is_set():
                    try:
                        tokens = getattr(self, "_live_playback_tokens", {})
                        token = tokens.get(room) if isinstance(tokens, dict) else None
                        if token is None or tokens.get(room) == token:
                            self.log("[LIVEKIT] natural EOF; leaving live broadcast:", room)
                            self._stop_live_broadcast(room)
                    except Exception as stop_exc:
                        self.log("[LIVEKIT] EOF broadcast cleanup failed:", room, repr(stop_exc))

        threading.Thread(target=_run, name=f"livekit-audio-{room}", daemon=True).start()
        # Wait for the same real PCM burst threshold that signals readiness.
        first_frame.wait(timeout=5)
        if state["frames"] >= required_start_frames:
            self.log("[LIVEKIT] real audio burst captured successfully:", room, "frames=", state["frames"])
            return True
        self.log("[LIVEKIT] no sufficient audio burst captured:", room, "frames=", state["frames"], state["error"])
        return False

    def _stop_live_song(self, room: str):
        """Stop only the current song in a live room; keep the bot on the mic."""
        room = str(room or "").strip()
        if not room:
            return False
        stopped = False
        try:
            ev = getattr(self, f"_livekit_audio_stop_event_{room}", None)
            if ev is not None:
                ev.set()
                stopped = True
        except Exception:
            pass
        try:
            proc = getattr(self, f"_livekit_audio_proc_{room}", None)
            if proc is not None and proc.poll() is None:
                proc.terminate()
                stopped = True
        except Exception:
            pass
        try:
            pending = getattr(self, "_pending_live_tracks", {}) or {}
            if room in pending:
                pending.pop(room, None)
                stopped = True
        except Exception:
            pass

        key, accepted, _ = self._live_room_is_active(room)
        room_id = str(
            accepted.get("room_id", "")
            or getattr(self, "_live_room_ids", {}).get(key or room, "")
            or getattr(self, "_live_room_ids", {}).get(room, "")
            or room
        )
        session_id = str(
            accepted.get("session_id", "")
            or accepted.get("publish_id", "")
            or accepted.get("id_", "")
            or accepted.get("invite_id", "")
            or ""
        )
        for action in (STREAM_AUDIO_ACTION, "room_stream"):
            try:
                self.send_query(encode_query(
                    action, type_="audio_stop", room=room_id, id_=session_id
                ))
            except Exception as exc:
                self.log("[STREAM] audio_stop failed:", action, room, repr(exc))
        self._last_live_play_status_by_room = getattr(self, "_last_live_play_status_by_room", {})
        self._last_live_play_status_by_room[room] = "stopped"
        return stopped or bool(accepted)

    def _audius_live_source(self, query):
        """Find a streamable Audius track for live broadcast without downloading it.

        This is deliberately separate from _music_download: normal `.sa` keeps
        the existing SoundCloud/YouTube pipeline, while `بث` can use Audius as
        a lightweight broadcast source. The returned URL is an MP3 stream that
        ffmpeg/LiveKit can consume directly, so Railway does not wait for a
        complete download/conversion before the broadcast starts.
        """
        q = str(query or "").strip()
        if not q:
            return None
        if re.match(r"^https?://", q, re.I):
            # Audius direct links can be resolved by yt-dlp/normal pipeline;
            # do not guess an Audius track id from arbitrary URLs here.
            return None

        now = time.time()
        cache = getattr(self, "_audius_live_cache", None)
        if cache is None:
            cache = {}
            setattr(self, "_audius_live_cache", cache)
        key = re.sub(r"\s+", " ", q.casefold()).strip()
        cached = cache.get(key)
        if cached and now - float(cached.get("created_at", 0)) < AUDIUS_CACHE_TTL:
            return dict(cached.get("value") or {})

        params = {"query": q, "limit": 5, "sort_method": "relevant"}
        if AUDIUS_API_KEY:
            params["api_key"] = AUDIUS_API_KEY
        headers = {"User-Agent": "TalkinBot/24"}
        try:
            r = requests.get(
                f"{AUDIUS_API_BASE}/tracks/search",
                params=params, headers=headers, timeout=AUDIUS_TIMEOUT,
            )
            r.raise_for_status()
            payload = r.json()
            tracks = payload.get("data") or []
            if not isinstance(tracks, list):
                tracks = []

            selected = None
            q_words = [w for w in re.split(r"\s+", key) if len(w) > 1]
            for track in tracks:
                if not isinstance(track, dict):
                    continue
                duration = int(track.get("duration") or 0)
                streamable = track.get("is_streamable", True)
                if streamable in (False, "false", "False", 0, "0"):
                    continue
                if duration and duration > MUSIC_MAX_SECONDS:
                    continue
                title = str(track.get("title") or "").strip()
                artist = str((track.get("user") or {}).get("name") or "Audius").strip()
                haystack = f"{title} {artist}".casefold()
                score = sum(1 for w in q_words if w in haystack)
                candidate = (score, int(track.get("play_count") or 0), track)
                if selected is None or candidate[:2] > selected[:2]:
                    selected = candidate

            if selected is None:
                return None
            track = selected[2]
            track_id = str(track.get("id") or "").strip()
            if not track_id:
                return None

            # Audius documents /tracks/{track_id}/stream as the streamable MP3
            # endpoint. Keep it as a remote URL; LiveKit's ffmpeg feeder follows
            # redirects and streams it progressively instead of downloading it.
            stream_url = f"{AUDIUS_API_BASE}/tracks/{track_id}/stream"
            if AUDIUS_API_KEY:
                stream_url += "?" + urlencode({"api_key": AUDIUS_API_KEY})

            value = {
                "id": track_id,
                "title": str(track.get("title") or q),
                "uploader": str((track.get("user") or {}).get("name") or "Audius"),
                "duration": int(track.get("duration") or 0),
                "url": stream_url,
                "source": "Audius",
                "created_at": now,
            }
            cache[key] = {"created_at": now, "value": value}
            # Keep the tiny cache bounded.
            if len(cache) > 80:
                oldest = sorted(cache.items(), key=lambda item: float(item[1].get("created_at", 0)))[:20]
                for old_key, _ in oldest:
                    cache.pop(old_key, None)
            return dict(value)
        except Exception as exc:
            self.log("[AUDIUS] live source unavailable:", repr(exc))
            return None

    def _music_live_source(self, query):
        """Resolve a direct audio URL for live broadcast without downloading it.

        Audius is still preferred by handle_music_command. This method is the
        fast fallback for YouTube/SoundCloud-style searches: yt-dlp extracts
        metadata and a playable audio URL only, then LiveKit/FFmpeg consumes the
        remote stream directly. That removes the full-download-before-play delay.
        """
        if yt_dlp is None:
            return None
        q = str(query or "").strip()
        if not q:
            return None
        target = q if re.match(r"^https?://", q, re.I) else "ytsearch5:" + q
        # Direct YouTube URLs are short-lived, so cache only briefly. This
        # avoids repeating the slow extractor step when the same song is
        # requested again while keeping the URL fresh enough to play.
        cache = getattr(self, "_music_direct_cache", None)
        if cache is None:
            cache = {}
            self._music_direct_cache = cache
        cache_key = re.sub(r"\s+", " ", q.casefold()).strip()
        cached = cache.get(cache_key)
        if cached and time.time() - float(cached.get("created_at", 0) or 0) < MUSIC_DIRECT_CACHE_TTL:
            return dict(cached.get("value") or {})
        errors = []

        clients = ("android", "web_embedded", "ios", "tv_embedded", "web", "default")
        for client in clients:
            try:
                opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "skip_download": True,
                    "format": "bestaudio[abr<=192]/bestaudio",
                    "socket_timeout": 12,
                    "retries": 2,
                    "extractor_retries": 2,
                    "cachedir": False,
                    "check_formats": False,
                    "http_headers": {
                        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
                    },
                    "js_runtimes": {"node": {}},
                    "remote_components": {"ejs": "github"},
                }
                if YOUTUBE_COOKIE_FILE:
                    opts["cookiefile"] = YOUTUBE_COOKIE_FILE
                if not (target.startswith("http") and "soundcloud.com" in target.lower()):
                    if client != "native_default":
                        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(target, download=False)
                if info and info.get("entries"):
                    entries = [x for x in (info.get("entries") or []) if x]
                    info = entries[0] if entries else None
                if not info:
                    continue

                duration = int(info.get("duration") or 0)
                if duration and duration > MUSIC_MAX_SECONDS:
                    errors.append(f"{client}: duration>{MUSIC_MAX_SECONDS}")
                    continue

                # Prefer a concrete audio-only format URL. Avoid video formats
                # and avoid extremely large audio choices.
                formats = []
                for fmt in (info.get("formats") or []):
                    if not isinstance(fmt, dict):
                        continue
                    url = str(fmt.get("url") or "").strip()
                    if not url:
                        continue
                    acodec = str(fmt.get("acodec") or "").strip()
                    vcodec = str(fmt.get("vcodec") or "none").strip()
                    if not acodec or acodec == "none" or vcodec not in ("none", ""):
                        continue
                    abr = float(fmt.get("abr") or 0)
                    size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
                    if size and int(size) > MUSIC_MAX_BYTES:
                        continue
                    if abr and abr > 192:
                        continue
                    formats.append(fmt)

                def score(fmt):
                    abr = float(fmt.get("abr") or 0)
                    size = int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)
                    proto = str(fmt.get("protocol") or "").casefold()
                    # Prefer progressive/direct HTTPS audio, then 128-192 kbps,
                    # while avoiding large files.
                    proto_bonus = 2 if proto.startswith("http") else 0
                    abr_score = -abs(160 - abr) if abr else -50
                    size_penalty = -(size / (1024 * 1024 * 10)) if size else 0
                    return (proto_bonus, abr_score, size_penalty)

                chosen = max(formats, key=score) if formats else None
                direct_url = str((chosen or {}).get("url") or info.get("url") or "").strip()
                if not direct_url:
                    continue
                source = str(info.get("extractor_key") or info.get("extractor") or "YouTube")
                value = {
                    "id": str(info.get("id") or ""),
                    "title": str(info.get("title") or q),
                    "uploader": str(info.get("uploader") or info.get("channel") or source),
                    "duration": duration,
                    "url": direct_url,
                    "source": source,
                }
                cache[cache_key] = {"created_at": time.time(), "value": value}
                if len(cache) > 80:
                    oldest = sorted(cache.items(), key=lambda item: float(item[1].get("created_at", 0) or 0))[:20]
                    for old_key, _ in oldest:
                        cache.pop(old_key, None)
                return dict(value)
            except Exception as exc:
                errors.append(f"{client}: {type(exc).__name__}: {exc}")
                continue
        if errors:
            self.log("[MUSIC] direct live source failed:", " | ".join(errors[-6:]))
        return None

    def _music_fast_sources(self, query, exclude_urls=None):
        """Yield valid source URLs in measured response-time order."""
        q = str(query or "").strip()
        if not q:
            return
        excluded = {str(url or "").strip() for url in (exclude_urls or ())}
        results = queue.Queue()
        def record_source_latency(source_label, elapsed):
            lock = getattr(self, "_music_source_latency_lock", None)
            if lock is None:
                lock = self._music_source_latency_lock = threading.Lock()
            with lock:
                stats = getattr(self, "_music_source_latency_ewma", None)
                if stats is None:
                    stats = self._music_source_latency_ewma = {}
                old_latency = stats.get(source_label)
                stats[source_label] = float(elapsed) if old_latency is None else (
                    0.7 * float(old_latency) + 0.3 * float(elapsed)
                )
        tasks = (
            ("Audius", self._audius_live_source),
            ("direct", self._music_live_source),
        )
        launched = 0
        started_at = time.monotonic()
        for label, resolver in tasks:
            if not _MUSIC_SOURCE_SEMAPHORE.acquire(blocking=False):
                continue

            def run_lookup(source_label=label, source_resolver=resolver):
                lookup_started = time.monotonic()
                try:
                    result = source_resolver(q)
                    elapsed = time.monotonic() - lookup_started
                    record_source_latency(source_label, elapsed)
                    results.put((source_label, result, None, elapsed))
                except Exception as exc:
                    elapsed = time.monotonic() - lookup_started
                    record_source_latency(source_label, elapsed)
                    results.put((source_label, None, exc, elapsed))
                finally:
                    _MUSIC_SOURCE_SEMAPHORE.release()

            try:
                _MUSIC_LOOKUP_THREAD(
                    target=run_lookup, daemon=True,
                    name=f"music-source-{label.casefold()}",
                ).start()
                launched += 1
            except Exception as exc:
                _MUSIC_SOURCE_SEMAPHORE.release()
                self.log("[MUSIC] source lookup could not start:", label, repr(exc))

        remaining_sources = launched
        deadline = started_at + max(0.5, MUSIC_FAST_LOOKUP_TIMEOUT)
        yielded_urls = set(excluded)
        while remaining_sources:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                label, result, error, elapsed = results.get(timeout=remaining)
            except queue.Empty:
                break
            remaining_sources -= 1
            if error is not None:
                self.log("[MUSIC] source lookup failed:", label, repr(error))
                continue
            if not isinstance(result, dict) or not str(result.get("url") or "").strip():
                continue
            value = dict(result)
            source_url = str(value.get("url") or "").strip()
            if source_url in yielded_urls:
                continue
            yielded_urls.add(source_url)
            value.setdefault("source", label)
            value.setdefault("uploader", label)
            value["resolver_latency_seconds"] = round(float(elapsed), 4)
            stats = getattr(self, "_music_source_latency_ewma", {})
            self.log(
                "[MUSIC] source measured:", label,
                f"resolver={float(elapsed):.2f}s, EWMA={stats[label]:.2f}s",
            )
            yield value
        if not yielded_urls - excluded:
            self.log("[MUSIC] fast source lookup timed out/empty:", q[:100])

    def _music_fast_source(self, query):
        """Backward-compatible helper returning the currently fastest source."""
        return next(self._music_fast_sources(query), None)

    def _next_music_fallback_source(self, room, track):
        """Resolve another measured source after a live track fails to start."""
        if not isinstance(track, dict):
            return None
        attempted = set(track.get("attempted_source_urls") or ())
        current = str(track.get("url") or "").strip()
        if current:
            attempted.add(current)
        sources = track.get("fallback_source_iterator")
        candidate = next(sources, None) if sources is not None else None
        if candidate is None and track.get("fallback_search_query"):
            sources = self._music_fast_sources(
                track["fallback_search_query"], exclude_urls=attempted
            )
            track["fallback_source_iterator"] = sources
            candidate = next(sources, None)
        if candidate:
            track["attempted_source_urls"] = attempted
            return candidate
        return None

    def _music_download_uncached(self,query):
        """Search/download public audio and return an MP3 ready for TalkinChat.
        Try fast YouTube and SoundCloud sources first, then Internet Archive audio.
        """
        outdir = BASE_DIR / "generated_music"
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = uuid.uuid4().hex
        out_mp3 = outdir / (stamp + ".mp3")
        errors = ["yt-dlp غير مثبت"] if yt_dlp is None else []

        def resolve_public_title(value):
            try:
                u = str(value or "").strip()
                if not re.match(r"^https?://", u, re.I):
                    return ""
                if "youtube.com" in u.lower() or "youtu.be" in u.lower():
                    r = requests.get("https://www.youtube.com/oembed", params={"url": u, "format": "json"},
                                     headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
                    if r.ok:
                        return str(r.json().get("title") or "").strip()
                if "open.spotify.com" in u.lower():
                    r = requests.get(u, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
                    if r.ok:
                        m = re.search(r'property="og:title"\s+content="([^"]+)"', r.text, re.I)
                        if not m:
                            m = re.search(r'content="([^"]+)"\s+property="og:title"', r.text, re.I)
                        if m:
                            return re.sub(r'\s*\|\s*Spotify\s*$', '', m.group(1), flags=re.I).strip()
            except Exception as exc:
                errors.append(f"Public metadata: {type(exc).__name__}: {exc}")
            return ""

        def normalize_to_mp3(source, duration=0):
            if duration and int(duration) > MUSIC_MAX_SECONDS:
                raise RuntimeError(f"الأغنية أطول من {MUSIC_MAX_SECONDS} ثانية")
            source = Path(source)
            if source.is_file() and source.stat().st_size > MUSIC_MAX_BYTES:
                raise RuntimeError(f"ملف الصوت أكبر من {int(MUSIC_MAX_MB)} ميجابايت")
            if source.suffix.lower() == ".mp3":
                # Keep the normalized MP3 outside yt-dlp's temporary folder;
                # the successful-download path removes that folder afterward.
                if source.resolve() != out_mp3.resolve():
                    shutil.copy2(source, out_mp3)
                    return out_mp3
                return source
            ffmpeg_bin = shutil.which("ffmpeg")
            if not ffmpeg_bin:
                raise RuntimeError("FFmpeg غير موجود على الاستضافة")
            proc = subprocess.run([
                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-threads", "0",
                "-i", str(source), "-vn", "-ac", "2", "-ar", "44100",
                "-codec:a", "libmp3lame", "-b:a", "192k", str(out_mp3)
            ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=90)
            if proc.returncode != 0 or not out_mp3.is_file() or out_mp3.stat().st_size <= 4096:
                detail = " | ".join((proc.stderr or "").strip().splitlines()[-4:])
                raise RuntimeError("فشل تحويل الصوت إلى MP3: " + detail[:500])
            try: source.unlink()
            except Exception: pass
            return out_mp3

        def download_with_ydl(target, label, cookies=False):
            if yt_dlp is None:
                return None
            tmpdir = outdir / f".{stamp}_{label}"
            tmpdir.mkdir(parents=True, exist_ok=True)
            template = str(tmpdir / "source.%(ext)s")
            def pre_download_filter(info, *, incomplete):
                dur = int(info.get("duration") or 0)
                if dur > MUSIC_MAX_SECONDS:
                    return f"الأغنية أطول من {MUSIC_MAX_SECONDS} ثانية"
                sz = info.get("filesize") or info.get("filesize_approx")
                if sz and int(sz) > MUSIC_MAX_BYTES:
                    return f"حجم المصدر أكبر من {int(MUSIC_MAX_MB)} ميجابايت"
                return None

            opts = {
                "quiet": True, "no_warnings": True, "noplaylist": True,
                "format": "bestaudio[ext=m4a][abr<=192]/bestaudio[ext=mp3]/bestaudio[abr<=192]/bestaudio/best",
                "outtmpl": template,
                # Brief resolver/API hiccups (including transient DNS errors)
                # were failing the request on its first network attempt.
                "socket_timeout": 25, "retries": 4, "fragment_retries": 4,
                "extractor_retries": 4, "file_access_retries": 3,
                "cachedir": False, "overwrites": True,
                "continuedl": True,
                "concurrent_fragment_downloads": 8,
                "buffersize": 4 * 1024 * 1024,
                "http_chunk_size": 8 * 1024 * 1024,
                "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"},
                "check_formats": False,
                # The previous code forced Node even when Node 22+ was not
                # installed. Deno is preferred; Node is enabled only when its
                # runtime version meets yt-dlp's current minimum.
                "remote_components": {"ejs": "github"},
                "match_filter": pre_download_filter,
            }
            js_runtime = None
            for runtime_name, executable, minimum in (
                ("deno", shutil.which("deno"), (2, 3, 0)),
                ("node", shutil.which("node"), (22, 0, 0)),
            ):
                if not executable:
                    continue
                try:
                    runtime_probe = subprocess.run(
                        [executable, "--version"], capture_output=True, text=True,
                        timeout=4, check=False,
                    )
                    version_text = (runtime_probe.stdout or runtime_probe.stderr or "").strip()
                    version_match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text)
                    if runtime_probe.returncode == 0 and version_match:
                        actual = tuple(int(part) for part in version_match.groups())
                        if actual >= minimum:
                            js_runtime = runtime_name
                            break
                except Exception:
                    continue
            if js_runtime:
                opts["js_runtimes"] = {js_runtime: {}}
            if label.startswith("youtube_"):
                # Clients not currently requiring PO tokens are attempted first;
                # if the optional bgutil plugin is installed, it can also issue
                # tokens for the later clients automatically.
                extractor_args = {"youtube": {"player_client": [label.split("_", 1)[1]]}}
                if YOUTUBE_POT_BASE_URL:
                    extractor_args["youtubepot-bgutilhttp"] = {"base_url": [YOUTUBE_POT_BASE_URL]}
                opts["extractor_args"] = extractor_args
            if cookies and YOUTUBE_COOKIE_FILE:
                opts["cookiefile"] = YOUTUBE_COOKIE_FILE
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    match_score = 0.0
                    if str(target).startswith(("ytsearch", "scsearch")):
                        search_result = ydl.extract_info(target, download=False)
                        raw_entries = search_result.get("entries") if isinstance(search_result, dict) else []
                        entries = [x for x in (raw_entries or []) if isinstance(x, dict)]
                        ranked = []
                        for entry in entries:
                            duration = int(entry.get("duration") or 0)
                            if duration and duration > MUSIC_MAX_SECONDS:
                                continue
                            score = _music_candidate_score(
                                search_query, entry.get("title"),
                                entry.get("uploader") or entry.get("channel") or entry.get("creator"),
                            )
                            ranked.append((score, entry))
                        ranked.sort(key=lambda pair: pair[0], reverse=True)
                        if not ranked:
                            raise RuntimeError("لم تظهر نتائج صوتية مناسبة في البحث")
                        match_score, selected = ranked[0]
                        selected_url = str(
                            selected.get("webpage_url") or selected.get("original_url") or selected.get("url") or ""
                        ).strip()
                        if not selected_url:
                            raise RuntimeError("نتيجة البحث لا تحتوي رابطًا قابلًا للتنزيل")
                        # Resolve and download the exact ranked result, rather
                        # than downloading result #1 and then labeling it as a
                        # different entry from the search page.
                        info = ydl.extract_info(selected_url, download=True)
                    else:
                        info = ydl.extract_info(target, download=True)
                    if not info:
                        raise RuntimeError("لم يتم العثور على الأغنية")
                candidates = [x for x in tmpdir.iterdir() if x.is_file() and x.suffix.lower() not in (".part", ".ytdl", ".temp") and x.stat().st_size > 4096]
                if not candidates:
                    raise RuntimeError("تم العثور على الأغنية لكن لم يكتمل الملف الصوتي")
                source = max(candidates, key=lambda x: x.stat().st_mtime)
                duration = int(info.get("duration") or 0)
                if duration and duration > MUSIC_MAX_SECONDS:
                    raise RuntimeError(f"الأغنية أطول من {MUSIC_MAX_SECONDS} ثانية")
                if source.stat().st_size > MUSIC_MAX_BYTES:
                    raise RuntimeError(f"ملف الصوت أكبر من {int(MUSIC_MAX_MB)} ميجابايت")
                mp3 = normalize_to_mp3(source, duration)
                return {
                    "id": str(info.get("id") or ""),
                    "title": str(info.get("title") or query),
                    "uploader": str(info.get("uploader") or info.get("channel") or ""),
                    "duration": duration,
                    "license": str(info.get("license") or info.get("license_name") or ""),
                    "source_url": str(info.get("webpage_url") or info.get("original_url") or target),
                    "source": label,
                    "match_score": match_score,
                }, mp3
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
                shutil.rmtree(tmpdir, ignore_errors=True)
                return None

        search_query = str(query or "").strip()
        if re.match(r"^https?://", search_query, re.I):
            t = resolve_public_title(search_query)
            if t: search_query = t

        # Search broadly: Arabic artist names and older tracks are often absent
        # from the first few YouTube results. Try multiple equivalent queries.
        if re.match(r"^https?://", query, re.I) and not ("soundcloud.com" in query.lower()):
            yt_targets = [query]
        else:
            variants = _music_query_variants(search_query, limit=5)
            yt_targets = list(dict.fromkeys("ytsearch10:" + value for value in variants if value))

        for target_index, yt_target in enumerate(yt_targets):
            clients = (
                ("tv_simply", "android_vr", "web_safari", "default", "mweb", "android", "ios", "web")
                if target_index == 0 else
                ("tv_simply", "android_vr", "web_safari", "default", "mweb")
            )
            for client in clients:
                res = download_with_ydl(yt_target, f"youtube_{client}", cookies=True)
                if res:
                    info, mp3 = res
                    for child in outdir.glob(f".{stamp}_*"):
                        if child.is_dir(): shutil.rmtree(child, ignore_errors=True)
                    return info, mp3

        # Fallback source: SoundCloud (1 result)
        sc_targets = []
        if re.match(r"^https?://", query, re.I) and "soundcloud.com" in query.lower():
            sc_targets = [query]
        elif search_query:
            sc_targets = ["scsearch5:" + value for value in _music_query_variants(search_query, limit=4)]

        for target in sc_targets:
            res = download_with_ydl(target, "soundcloud", cookies=False)
            if res:
                info, mp3 = res
                for child in outdir.glob(f".{stamp}_*"):
                    if child.is_dir(): shutil.rmtree(child, ignore_errors=True)
                return info, mp3

        # Last public fallback for freely available tracks, recitations and
        # prayers: Internet Archive audio items. This path is used only after
        # the faster Audius/YouTube/SoundCloud sources fail.
        archive_tmp = None
        try:
            query_text = re.sub(r"\s+", " ", search_query).strip()
            if query_text:
                search = requests.get(
                    "https://archive.org/advancedsearch.php",
                    params=[("q", f"({query_text}) AND mediatype:audio"),
                            ("fl[]", "identifier"), ("fl[]", "title"),
                            ("rows", "6"), ("output", "json")],
                    headers={"User-Agent": "TalkinBot/1.0 audio search"}, timeout=(6, 15),
                )
                search.raise_for_status()
                docs = search.json().get("response", {}).get("docs", [])
                for doc in docs[:6]:
                    identifier = str(doc.get("identifier") or "").strip()
                    if not identifier:
                        continue
                    metadata_response = requests.get(
                        f"https://archive.org/metadata/{quote(identifier, safe='')}",
                        headers={"User-Agent": "TalkinBot/1.0 audio search"}, timeout=(6, 15),
                    )
                    metadata_response.raise_for_status()
                    metadata = metadata_response.json()
                    files = metadata.get("files") or []
                    audio_files = []
                    for item in files:
                        filename = str(item.get("name") or "")
                        extension = Path(filename).suffix.lower()
                        size = int(item.get("size") or 0)
                        if extension in {".mp3", ".m4a", ".ogg", ".opus", ".flac", ".wav", ".webm"} and (not size or size <= MUSIC_MAX_BYTES):
                            audio_files.append(item)
                    if not audio_files:
                        continue
                    audio_files.sort(key=lambda item: (Path(str(item.get("name") or "")).suffix.lower() != ".mp3", -int(item.get("size") or 0)))
                    item = audio_files[0]
                    filename = str(item.get("name") or "")
                    extension = Path(filename).suffix.lower() or ".audio"
                    archive_tmp = outdir / f".{stamp}_archive{extension}"
                    audio_url = f"https://archive.org/download/{quote(identifier, safe='')}/{quote(filename, safe='')}"
                    with requests.get(audio_url, headers={"User-Agent": "TalkinBot/1.0 audio download"}, stream=True, timeout=(8, 40)) as response:
                        response.raise_for_status()
                        total = 0
                        with archive_tmp.open("wb") as handle:
                            for chunk in response.iter_content(64 * 1024):
                                if not chunk:
                                    continue
                                total += len(chunk)
                                if total > MUSIC_MAX_BYTES:
                                    raise RuntimeError(f"ملف الأرشيف أكبر من {int(MUSIC_MAX_MB)} ميجابايت")
                                handle.write(chunk)
                    if total <= 4096:
                        raise RuntimeError("ملف الأرشيف الصوتي فارغ أو غير مكتمل")
                    duration = int(float((metadata.get("metadata") or {}).get("runtime") or 0))
                    if duration > MUSIC_MAX_SECONDS:
                        raise RuntimeError(f"الأغنية أطول من {MUSIC_MAX_SECONDS} ثانية")
                    archive_mp3 = normalize_to_mp3(archive_tmp, duration)
                    if archive_mp3 != out_mp3:
                        archive_mp3.replace(out_mp3)
                    title = str(doc.get("title") or query_text)
                    creator = str((metadata.get("metadata") or {}).get("creator") or "Internet Archive")
                    self.log("[MUSIC] fallback source=Internet Archive title=", title)
                    archive_metadata = metadata.get("metadata") or {}
                    return {
                        "id": identifier, "title": title, "uploader": creator,
                        "duration": duration,
                        "license": str(archive_metadata.get("licenseurl") or archive_metadata.get("rights") or ""),
                        "source_url": f"https://archive.org/details/{quote(identifier, safe='')}",
                        "source": "Internet Archive",
                    }, out_mp3
        except Exception as exc:
            errors.append(f"Internet Archive: {type(exc).__name__}: {exc}")
            self.log("[MUSIC] Internet Archive fallback failed:", repr(exc))
        finally:
            if archive_tmp is not None:
                try:
                    archive_tmp.unlink(missing_ok=True)
                except Exception:
                    pass

        detail_items = list(dict.fromkeys(errors[:2] + errors[-8:]))
        detail = " | ".join(detail_items)
        if yt_dlp is None:
            detail += " | ثبّت yt-dlp[default] في متطلبات الاستضافة"
        if not shutil.which("ffmpeg"):
            detail += " | FFmpeg غير موجود؛ يلزم لتجهيز MP3"
        if not (shutil.which("deno") or shutil.which("node")):
            detail += " | لا يوجد Deno أو Node.js 22+ لحل تحديات YouTube الحديثة"
        if any(token in detail.lower() for token in ("403", "requested format is not available", "po token", "sign in to confirm")):
            detail += " | قد يلزم مزوّد YouTube PO Token (bgutil) أو Cookies صالحة بحسب حظر عنوان الاستضافة"
        raise RuntimeError("تعذر تنزيل الأغنية من YouTube." + (f" تفاصيل: {detail[:1500]}" if detail else ""))

    def _music_download(self, query):
        """Return a ready MP3, reusing a recent successful download when possible.

        A per-query lock prevents two simultaneous requests for the same song
        from downloading it twice. The bounded cache is stored below DATA_DIR so
        it survives process restarts when the host's persistent volume is mounted.
        Each caller gets a separate public MP3 filename so media URLs stay isolated.
        """
        query_text = str(query or "").strip()
        if not query_text:
            return self._music_download_uncached(query)

        outdir = BASE_DIR / "generated_music"
        outdir.mkdir(parents=True, exist_ok=True)
        cache_dir = MUSIC_CACHE_DIR
        cache_dir.mkdir(parents=True, exist_ok=True)
        normalized_query = _normalize_music_search_text(query_text)
        cache_basis = normalized_query or re.sub(r"\s+", " ", query_text.casefold()).strip()
        cache_key = hashlib.sha256(cache_basis.encode("utf-8")).hexdigest()
        cache_mp3 = cache_dir / (cache_key + ".mp3")
        cache_meta = cache_dir / (cache_key + ".json")
        legacy_cache_dir = outdir / ".music_cache"
        legacy_key = hashlib.sha256(re.sub(r"\s+", " ", query_text.casefold()).strip().encode("utf-8")).hexdigest()
        legacy_mp3 = legacy_cache_dir / (legacy_key + ".mp3")
        legacy_meta = legacy_cache_dir / (legacy_key + ".json")

        guard = getattr(self, "_music_download_cache_guard", None)
        if guard is None:
            guard = self._music_download_cache_guard = threading.Lock()
        with guard:
            locks = getattr(self, "_music_download_cache_locks", None)
            if locks is None:
                locks = self._music_download_cache_locks = {}
            query_lock = locks.setdefault(cache_key, threading.Lock())

        with query_lock:
            now = time.time()
            if (not cache_mp3.is_file() and legacy_mp3.is_file() and legacy_meta.is_file()
                    and now - legacy_mp3.stat().st_mtime <= MUSIC_FILE_CACHE_TTL
                    and legacy_mp3.stat().st_size > 4096):
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(legacy_mp3, cache_mp3)
                    shutil.copy2(legacy_meta, cache_meta)
                    self.log("[MUSIC] migrated recent cache into persistent DATA_DIR:", cache_key[:12])
                except Exception as exc:
                    self.log("[MUSIC] legacy cache migration failed:", type(exc).__name__)
            # First consult the durable, shared reference library. It is stored
            # outside generated_music so routine cleanup cannot remove it.
            library_dir = MUSIC_LIBRARY_DIR
            library_mp3 = library_dir / (cache_key + ".mp3")
            library_meta = library_dir / (cache_key + ".json")
            if MUSIC_LIBRARY_ENABLED and library_mp3.is_file() and library_meta.is_file():
                try:
                    if library_mp3.stat().st_size > 4096:
                        metadata = json.loads(library_meta.read_text(encoding="utf-8"))
                        cached_source = str((metadata or {}).get("source") or "").casefold()
                        if "youtube" in cached_source:
                            request_copy = outdir / (uuid.uuid4().hex + ".mp3")
                            shutil.copy2(library_mp3, request_copy)
                            self.log("[MUSIC] permanent YouTube library hit:", cache_key[:12])
                            return metadata, request_copy
                        self.log("[MUSIC] ignoring non-YouTube permanent cache:", cached_source or "unknown")
                except Exception as exc:
                    self.log("[MUSIC] library read failed:", type(exc).__name__)

            if cache_mp3.is_file() and cache_meta.is_file():
                try:
                    age = now - cache_mp3.stat().st_mtime
                    if age <= MUSIC_FILE_CACHE_TTL and cache_mp3.stat().st_size > 4096:
                        metadata = json.loads(cache_meta.read_text(encoding="utf-8"))
                        if isinstance(metadata, dict):
                            cached_source = str(metadata.get("source") or "").casefold()
                            if "youtube" in cached_source:
                                request_copy = outdir / (uuid.uuid4().hex + ".mp3")
                                shutil.copy2(cache_mp3, request_copy)
                                os.utime(cache_mp3, None)
                                os.utime(cache_dir, None)
                                return metadata, request_copy
                            self.log("[MUSIC] ignoring non-YouTube cache to honor YouTube-first playback:", cached_source or "unknown")
                except Exception as exc:
                    self.log("[MUSIC] prepared-file cache read failed:", type(exc).__name__)

            info, downloaded_path = self._music_download_uncached(query_text)
            downloaded_path = Path(downloaded_path)
            if downloaded_path.is_file() and downloaded_path.stat().st_size > 4096:
                # Default: keep a permanent copy only when the provider explicitly
                # identifies a reusable license. The optional override is intended
                # solely for tracks the operator owns or has permission to retain.
                license_text = str((info or {}).get("license") or "").strip().lower()
                is_reusable_license = any(token in license_text for token in (
                    "creativecommons.org", "creative commons", "public domain",
                    "public-domain", "cc0", "cc-by", "cc by", "cc-by-sa", "cc by-sa",
                ))
                may_store_permanently = is_reusable_license or MUSIC_LIBRARY_SAVE_AUTHORIZED
                if MUSIC_LIBRARY_ENABLED and may_store_permanently:
                    try:
                        library_dir.mkdir(parents=True, exist_ok=True)
                        max_bytes = int(MUSIC_LIBRARY_MAX_GB * 1024 * 1024 * 1024)
                        used_bytes = sum(f.stat().st_size for f in library_dir.glob("*.mp3") if f.is_file())
                        source_size = downloaded_path.stat().st_size
                        if not library_mp3.exists() and used_bytes + source_size <= max_bytes:
                            temp_library_mp3 = library_dir / (cache_key + ".tmp.mp3")
                            temp_library_meta = library_dir / (cache_key + ".tmp.json")
                            shutil.copy2(downloaded_path, temp_library_mp3)
                            temp_library_meta.write_text(json.dumps(dict(info or {}), ensure_ascii=False), encoding="utf-8")
                            os.replace(temp_library_mp3, library_mp3)
                            os.replace(temp_library_meta, library_meta)
                            self.log("[MUSIC] licensed track saved to permanent library:", cache_key[:12])
                    except Exception as exc:
                        self.log("[MUSIC] permanent library write failed:", type(exc).__name__)
                try:
                    temp_mp3 = cache_dir / (cache_key + ".tmp.mp3")
                    temp_meta = cache_dir / (cache_key + ".tmp.json")
                    shutil.copy2(downloaded_path, temp_mp3)
                    temp_meta.write_text(json.dumps(dict(info or {}), ensure_ascii=False), encoding="utf-8")
                    os.replace(temp_mp3, cache_mp3)
                    os.replace(temp_meta, cache_meta)
                    os.utime(cache_dir, None)
                    cached_items = sorted(
                        (item for item in cache_dir.glob("*.mp3") if item.is_file()),
                        key=lambda item: item.stat().st_mtime,
                    )
                    for stale in cached_items[:-MUSIC_FILE_CACHE_MAX_ITEMS]:
                        stale.unlink(missing_ok=True)
                        stale.with_suffix(".json").unlink(missing_ok=True)
                    self.log("[MUSIC] prepared-file cache stored:", cache_key[:12])
                except Exception as exc:
                    self.log("[MUSIC] prepared-file cache write failed:", type(exc).__name__)
                    for temporary in (cache_dir / (cache_key + ".tmp.mp3"), cache_dir / (cache_key + ".tmp.json")):
                        try:
                            temporary.unlink(missing_ok=True)
                        except Exception:
                            pass
            return info, downloaded_path

    def handle_youtube_direct_test_command(self, room, query, requester):
        """Search YouTube and send its direct audio URL without downloading it."""
        query = str(query or "").strip()
        if not query:
            self.send_room_text(room, "❌ اكتب: .aax اسم الأغنية")
            return True
        key = _norm_user(requester)
        last = getattr(self, "_youtube_direct_test_last", {}).get(key, 0.0)
        remaining = 10.0 - (time.time() - float(last or 0.0))
        if remaining > 0:
            self.send_room_text(room, f"⏳ انتظر {int(remaining) + 1} ثانية قبل اختبار آخر.")
            return True
        if not hasattr(self, "_youtube_direct_test_last"):
            self._youtube_direct_test_last = {}
        self._youtube_direct_test_last[key] = time.time()

        def worker():
            try:
                if yt_dlp is None:
                    raise RuntimeError("مكتبة yt-dlp غير مثبتة على الاستضافة")
                result = self._music_live_source(query)
                if not isinstance(result, dict):
                    raise RuntimeError("تعذر استخراج رابط صوت مباشر من YouTube")
                source = str(result.get("source") or "").casefold()
                direct_url = str(result.get("url") or "").strip()
                if "youtube" not in source or not re.match(r"^https://", direct_url, re.I):
                    raise RuntimeError("لم تُرجع نتائج YouTube رابط صوت مباشر صالحًا")
                duration = int(result.get("duration") or 0)
                sent = self.send_room_media(room, direct_url, "audio", duration)
                if sent is False:
                    raise RuntimeError("Talkin رفض إرسال رابط الصوت المباشر")
                title = str(result.get("title") or query)
                self.send_room_text(
                    room,
                    "🎵 اختبار تشغيل مباشر من YouTube\n"
                    f"👤 الطلب بواسطة: @{requester}\n"
                    f"🎶 الأغنية: {title}\n"
                    "▶️ استخدم زر التشغيل للاستماع من رابط YouTube المباشر.\n"
                    "ℹ️ هذا اختبار مستقل؛ لم يُنزّل البوت ملفًا صوتيًا ولم يبدأ بثًا حيًا.",
                )
                self.log("[MUSIC_TEST_DIRECT] YouTube direct audio sent:", title)
            except Exception as exc:
                self.log("[MUSIC_TEST_DIRECT] failed:", type(exc).__name__)
                self.send_room_text(
                    room,
                    "❌ فشل اختبار YouTube المباشر. تحقق من اتصال YouTube وصلاحية الكوكيز، ثم أعد المحاولة.",
                )

        self.send_room_text(room, "🔎 جاري البحث في YouTube لإرسال الصوت مباشرة دون تنزيله...")
        threading.Thread(target=worker, name="youtube-direct-music-test", daemon=True).start()
        return True

    def handle_youtube_test_command(self, room, query, requester):
        """Test YouTube search/download independently without changing .sa behavior."""
        query = str(query or "").strip()
        if not query:
            self.send_room_text(room, "❌ اكتب: تشغيل يوتيوب .aa اسم الأغنية")
            return True
        last = getattr(self, "_youtube_test_last", {}).get(_norm_user(requester), 0.0)
        remaining = 10.0 - (time.time() - float(last or 0.0))
        if remaining > 0:
            self.send_room_text(room, f"⏳ انتظر {int(remaining) + 1} ثانية قبل اختبار آخر.")
            return True
        if not hasattr(self, "_youtube_test_last"):
            self._youtube_test_last = {}
        self._youtube_test_last[_norm_user(requester)] = time.time()

        def worker():
            try:
                if yt_dlp is None:
                    raise RuntimeError("مكتبة yt-dlp غير مثبتة على الاستضافة")
                public_base = _public_base_url()
                if not public_base:
                    raise RuntimeError("رابط الوسائط العام PUBLIC_MEDIA_URL غير مضبوط")
                # Bypass the shared cross-source cache so this test proves a
                # fresh YouTube resolution rather than reusing Audius/archive.
                info, path = self._music_download_uncached(query)
                info = dict(info or {})
                source_url = str(info.get("source_url") or "").strip()
                source_name = str(info.get("source") or "").casefold()
                if "youtube" not in source_name and not re.search(r"(?:youtube\.com|youtu\.be)", source_url, re.I):
                    raise RuntimeError("لم ينجح مصدر YouTube؛ النتيجة جاءت من مصدر احتياطي")
                path = Path(path)
                if not path.is_file() or path.stat().st_size <= 4096:
                    raise RuntimeError("ملف الصوت الناتج غير مكتمل")
                duration = int(info.get("duration") or 0)
                media_url = public_base.rstrip("/") + "/media/" + path.name
                self._verify_public_media_url(media_url, media_kind="audio")
                sent = self.send_room_media(room, media_url, "audio", duration)
                if sent is False:
                    raise RuntimeError("Talkin رفض إرسال ملف الصوت")
                title = str(info.get("title") or query)
                # Keep this successful .aa track as the requester's latest song,
                # so `مشاركة username` can send the same audio privately.
                self.music_current[_norm_user(requester)] = {
                    "requester": requester,
                    "title": title,
                    "artist": str(info.get("uploader") or info.get("channel") or "YouTube"),
                    "url": media_url,
                    "local_path": str(path),
                    "query": query,
                    "duration": duration,
                    "source_url": source_url,
                    "source": "YouTube",
                    "created_at": time.time(),
                }
                caption = ("🎵 تم تشغيل أغنية من YouTube\n"
                           f"👤 الطلب بواسطة: @{requester}\n"
                           f"🎶 الأغنية: {title}\n"
                           "▶️ اضغط زر التشغيل للاستماع مباشرة داخل الشات.\n"
                           "🔒 رابط YouTube مخفي.\n"
                           "📩 للمشاركة خاص: مشاركة اسم_المستخدم")
                self.send_room_text(room, caption)
                self.log("[MUSIC_TEST] YouTube test succeeded:", title, source_url)
            except Exception as exc:
                self.log("[MUSIC_TEST] YouTube test failed:", repr(exc))
                self.send_room_text(room, "❌ فشل اختبار YouTube: " + str(exc)[:500])

        self.send_room_text(room, "🔎 جاري البحث في YouTube وتجهيز الصوت للاختبار...")
        threading.Thread(target=worker, name="youtube-music-test", daemon=True).start()
        return True

    def handle_music_command(self, room, text, requester, private_to="", broadcast_all=False,
                             with_reactions=True, room_output=True, live_stream=False):
        raw = str(text or "").strip()
        if not raw.lower().startswith(".sa "):
            return False
        query = raw[4:].strip()
        if not query:
            self.send_room_text(room, "❌ اكتب: .sa اسم الأغنية")
            return True
        role = self._bot_room_role(room) if live_stream else ""
        known_non_owner_roles = {"admin", "moderator", "mod", "member", "user", "none", "outcast", "banned", "ban", "blocked"}
        if live_stream and role in known_non_owner_roles:
            role = role or "غير معروفة"
            self.send_room_text(
                room,
                "⚠️ لم يتم تشغيل البث.\n"
                "👑 يجب أن تكون رتبة البوت Owner في الغرفة قبل تشغيل البث.\n"
                f"📌 الرتبة الحالية: {role}"
            )
            return True
        now = time.time()
        last = self.music_last.get(requester, 0)
        if now - last < MUSIC_COOLDOWN:
            self.send_room_text(room, f"⏳ انتظر {int(MUSIC_COOLDOWN - (now - last)) + 1} ثانية.")
            return True
        self.music_last[requester] = now
        response_language = self._capture_room_response_language(room, raw)

        def worker():
            try:
                public_base = _public_base_url()
                # A regular song title is already searched by the audio
                # providers; do not add a serial Spotify API round-trip first.
                spotify_link = bool(re.search(r"(?:open\.spotify\.com/|spotify:)", query, re.I))
                spotify = _spotify_track_metadata(query) if spotify_link else None
                search_query = str((spotify or {}).get("search_query") or query).strip()
                if spotify:
                    self.log("[MUSIC] Spotify metadata resolved:", spotify.get("title"), "—", spotify.get("artist"))

                fast_sources = iter(self._music_fast_sources(search_query))
                attempted_urls = set()
                info = None
                path = None
                url = ""
                duration = 0
                title = search_query
                artist = "Music"
                music_published = False
                live_started = False
                live_queued = False
                published_rooms = []

                def set_source(source_info, local_path=None):
                    nonlocal info, path, url, duration, title, artist
                    info = dict(source_info or {})
                    path = local_path
                    duration = int(info.get("duration") or 0)
                    title = str(info.get("title") or search_query)
                    artist = str(info.get("uploader") or info.get("channel") or info.get("source") or "Music")
                    if path is not None:
                        url = (public_base.rstrip("/") + "/media/" + Path(path).name) if public_base else str(path)
                    else:
                        url = str(info.get("url") or "").strip()

                if live_stream:
                    # Same primary source policy as .sa and .تشغيل: YouTube first,
                    # then SoundCloud/Archive. LiveKit receives the prepared local MP3.
                    primary_error = None
                    try:
                        info, path = self._music_download(search_query)
                        set_source(info, path)
                        live_started = bool(self._play_music_in_live_room(room, str(path), duration))
                        live_status = getattr(self, "_last_live_play_status_by_room", {}).get(room, "failed")
                        live_queued = live_status == "queued"
                        if live_started or live_queued:
                            self.log("[MUSIC] live primary pipeline source:", info.get("source"), "room=", room)
                    except Exception as exc:
                        primary_error = exc
                        self.log("[MUSIC] live primary pipeline failed:", repr(exc))

                    # Preserve the original direct-stream providers as the final
                    # fallback if the prepared track cannot be fed to LiveKit.
                    if not live_started and not live_queued:
                        for candidate in fast_sources:
                            candidate_url = str(candidate.get("url") or "").strip()
                            if not candidate_url or candidate_url in attempted_urls:
                                continue
                            attempted_urls.add(candidate_url)
                            set_source(candidate)
                            live_started = bool(self._play_music_in_live_room(room, url, duration))
                            live_status = getattr(self, "_last_live_play_status_by_room", {}).get(room, "failed")
                            live_queued = live_status == "queued"
                            if live_started or live_queued:
                                self.log("[MUSIC] live direct fallback accepted:", candidate.get("source"), "room=", room)
                                if live_queued:
                                    pending = getattr(self, "_pending_live_tracks", {}).get(room)
                                    if isinstance(pending, dict):
                                        pending["fallback_search_query"] = search_query
                                        pending["fallback_source_iterator"] = fast_sources
                                        pending["attempted_source_urls"] = set(attempted_urls)
                                        pending["requester"] = requester
                                break
                            self.log("[MUSIC] live direct fallback rejected:", candidate.get("source"), room)
                    if not live_started and not live_queued:
                        detail = f": {primary_error}" if primary_error else ""
                        raise RuntimeError("تعذر بدء تشغيل الصوت في البث من جميع المصادر" + detail)
                else:
                    if broadcast_all:
                        target_rooms = [str(item).strip() for item in (self._active_rooms() or []) if str(item).strip()]
                        if room:
                            room_name = str(room).strip()
                            target_rooms = [room_name] + [target for target in target_rooms
                                                         if target.casefold() != room_name.casefold()]
                    else:
                        target_rooms = [str(room or "").strip()] if str(room or "").strip() else []
                    if not target_rooms:
                        raise RuntimeError("لا توجد غرفة مستهدفة لإرسال الأغنية")
                    if not public_base:
                        raise RuntimeError("لا يوجد رابط عام للصوت؛ اضبط PUBLIC_MEDIA_URL مع المنفذ العام الصحيح")
                    # Room clients may accept an external stream URL packet yet
                    # fail to fetch/play that provider URL. Host a normalized
                    # MP3 on our own media server for reliable room playback.
                    downloaded_info, downloaded_path = self._music_download(search_query)
                    info = dict(downloaded_info or {})
                    path = Path(downloaded_path)
                    if not path.is_file() or path.stat().st_size <= 4096:
                        raise RuntimeError("ملف MP3 المحلي غير موجود أو غير مكتمل")
                    set_source(info, path)
                    self._verify_public_media_url(url, media_kind="audio")
                    for target_room in target_rooms:
                        try:
                            sent = self.send_room_media(target_room, url, "audio", duration)
                            if sent is False:
                                raise RuntimeError("Talkin لم يقبل حزمة MP3 المستضافة")
                            published_rooms.append(target_room)
                        except Exception as exc:
                            self.log("[MUSIC] hosted MP3 rejected:", target_room, repr(exc))
                    if not published_rooms:
                        raise RuntimeError("تعذر إرسال MP3 المستضاف إلى أي غرفة متصلة")
                    music_published = True

                self.music_current[_norm_user(requester)] = {
                    "requester": requester, "title": title, "artist": artist,
                    "url": url,
                    "local_path": str(path) if path is not None and Path(path).is_file() else "",
                    "query": search_query,
                    "duration": duration, "created_at": time.time(),
                }
                if with_reactions:
                    music_code = uuid.uuid4().hex[:4]
                    music_codes = {kind: music_code for kind in ("like", "love", "dislike", "comment", "report")}
                    caption = _message_template(
                        "music", "broadcast",
                        "🎵 تشغيل الأغنية\n👤 الناشر: @{requester_name}\n🎶 اسم الأغنية: {title}\n🏠 الغرفة: {room}\n━━━━━━━━━━━━\n👍 lk@{like}\n❤️ lv@{love}\n👎 dl@{dislike}\n💬 cm@{comment} msg\n🚨 report@{report} msg",
                        requester_name=requester, title=title, artist=artist,
                        source_label=artist or "Music", room=room,
                        like=music_codes["like"], love=music_codes["love"],
                        dislike=music_codes["dislike"], comment=music_codes["comment"],
                        report=music_codes["report"], url=url, duration=duration
                    )
                    self.reaction_targets[music_code] = {
                        "publisher": requester, "kind": "music",
                        "title": title, "description": title, "created_at": time.time(),
                    }
                elif live_stream:
                    caption = "🎶 تم تشغيل الأغنية في البث\n📡 بث مباشر"
                else:
                    caption = (f"🎶 تم تشغيل الأغنية\n━━━━━━━━━━━━\n"
                               f"🎵 العنوان: {title}\n🎤 الطلب: @{requester}\n"
                               f"📡 المصدر: {artist or 'Music'}")

                if live_stream:
                    if live_started:
                        self.send_room_text(room, "🎶 تم تشغيل الطلب في البث\n🎵 تم تشغيل الأغنية في البث فعلياً\n📡 بث مباشر")
                        music_published = True
                    elif live_queued:
                        self.send_room_text(room, "📡 بث مباشر\n⏳ جاري تشغيل الأغنية...")
                        music_published = True
                    else:
                        raise RuntimeError("تعذر بدء تشغيل البث من جميع المصادر")
                else:
                    # Audio has already been accepted; send captions afterward
                    # so source failure never produces a false success card.
                    for target_room in published_rooms:
                        try:
                            self.send_room_text(target_room, caption)
                        except Exception as exc:
                            self.log("[MUSIC] room caption send failed:", target_room, repr(exc))
                if music_published:
                    _record_media_publication("music", title, requester, room)
            except Exception as exc:
                self.log("[MUSIC] request failed:", repr(exc), "room=", room)
                if room_output:
                    failure_detail = str(exc).casefold()
                    message = "❌ تعذر جلب الأغنية."
                    if "sign in to confirm" in failure_detail or "not a bot" in failure_detail:
                        message += (
                            "\n⚙️ YouTube طلب تحققًا إضافيًا من الاستضافة."
                            " أضف YOUTUBE_COOKIES أو YOUTUBE_COOKIES_FILE في متغيرات Altare."
                        )
                    self.send_room_text(room, message)

        self.send_room_text(room, f"⏳ جاري تجهيز طلب الأغنية\n@{requester}")
        threading.Thread(
            target=self._localized_room_worker(room, response_language, worker),
            name="music-request", daemon=True,
        ).start()
        return True

    def share_last_music(self, sender: str, target: str, room: str = ""):
        """Share the requester's last prepared track as text plus a real MP3 file."""
        sender = str(sender or "").strip().lstrip("@")
        target = str(target or "").strip().lstrip("@")
        info = self.music_current.get(_norm_user(sender), {})
        if not isinstance(info, dict) or not target:
            self.send_room_text(room, "⚠️ لا توجد أغنية شغّلها المستخدم بعد لمشاركتها.") if room else self.send_private_text(sender, "⚠️ لا توجد أغنية شغّلتها بعد لمشاركتها.")
            return True

        title = str(info.get("title") or "أغنية").strip()
        artist = str(info.get("artist") or "").strip()
        duration = int(info.get("duration") or 0)
        media_url = str(info.get("url") or "").strip()
        local_path_text = str(info.get("local_path") or "").strip()
        local_path = Path(local_path_text).expanduser() if local_path_text else None
        public_base = _public_base_url()
        native_local = bool(local_path is not None and local_path.is_file() and local_path.stat().st_size > 4096)

        # Prefer the exact prepared MP3 played in the room; rebuild its short
        # hosted URL rather than sharing a provider URL that may expire.
        if native_local and public_base:
            media_url = public_base.rstrip("/") + "/media/" + local_path.name

        def verify_audio(url):
            self._verify_public_media_url(url, "audio")
            return True

        try:
            if not media_url:
                raise RuntimeError("لا يوجد رابط أو ملف صوت محفوظ للأغنية")
            try:
                if not native_local:
                    verify_audio(media_url)
            except Exception as first_error:
                # If the old hosted file expired/was cleaned, recreate it from
                # the stored search query and publish a fresh MP3.
                recovery_query = str(info.get("query") or title).strip()
                if not recovery_query or not public_base:
                    raise first_error
                fresh_info, fresh_path = self._music_download(recovery_query)
                fresh_path = Path(fresh_path)
                if not fresh_path.is_file() or fresh_path.stat().st_size <= 4096:
                    raise RuntimeError("تعذر إعادة تجهيز ملف الأغنية للمشاركة")
                local_path = fresh_path
                native_local = True
                media_url = public_base.rstrip("/") + "/media/" + fresh_path.name
                verify_audio(media_url)
                info.update({
                    "url": media_url, "local_path": str(fresh_path),
                    "duration": int((fresh_info or {}).get("duration") or duration),
                    "title": str((fresh_info or {}).get("title") or title),
                    "artist": str((fresh_info or {}).get("uploader") or artist),
                })
                duration = int(info.get("duration") or 0)
                title = str(info.get("title") or title)
                artist = str(info.get("artist") or artist)
                self.music_current[_norm_user(sender)] = info

            filename = re.sub(r'[\\/:*?"<>|]+', "_", title).strip(" ._")[:90] or "song"
            if not Path(filename).suffix:
                filename += ".mp3"

            share_text = f"🎵 تمت مشاركة أغنية معك\n🎼 العنوان: {title}\n▶️ اضغط زر التشغيل للاستماع إليها داخل الخاص."
            if artist:
                share_text += f"\n🎤 الفنان: {artist}"
            # Send text and attachment independently. A transient text-packet
            # rejection must not prevent the actual MP3 from reaching the
            # recipient, and vice versa.
            text_error = None
            try:
                text_sent = self.send_private_text(target, share_text)
                if text_sent is False:
                    text_error = RuntimeError("تعذر إرسال رسالة المشاركة النصية")
            except Exception as exc:
                text_error = exc
                self.log("[MUSIC] share text failed; continuing with audio:", repr(exc))
            # Do not batch the two private messages: let the text packet leave
            # first, then send the playable audio attachment.
            time.sleep(0.60)
            media_error = None
            try:
                media_sent = self.send_private_media(
                    target, media_url, "audio", duration, filename=filename,
                    local_path=local_path if native_local else None,
                )
                if media_sent is False:
                    media_error = RuntimeError("خادم Talkin لم يؤكد إرسال ملف الصوت")
            except Exception as exc:
                media_error = exc
                self.log("[MUSIC] share audio failed:", repr(exc))
            if text_error and media_error:
                raise RuntimeError(f"النص والملف فشلا: {text_error}; {media_error}")
            if media_error:
                raise media_error
        except Exception as exc:
            self.log(
                "[MUSIC] private share failed:",
                "sender=", _norm_user(sender), "target=", _norm_user(target),
                "error=", repr(exc),
            )
            reason = str(exc).strip().replace("\n", " ")[:300] or "سبب غير معروف"
            notice = f"⚠️ تعذر إكمال مشاركة الأغنية مع @{target}.\n🧾 سبب الفشل: {reason}"
            if room:
                self.send_room_text(room, notice)
            else:
                self.send_private_text(sender, notice)
            return True

        confirmation = f"✅ تمت مشاركة أغنية {title} مع @{target} في الخاص."
        if room:
            self.send_room_text(room, confirmation)
        else:
            self.send_private_text(sender, confirmation)
        self.log("[MUSIC] private text + audio attachment sent:", _norm_user(sender), "->", _norm_user(target), "file=", filename)
        return True
