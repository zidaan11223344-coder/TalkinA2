"""Room media delivery, avatar refresh, upload and public media verification."""

class RoomMediaSection:
    def _cache_stream_member_event(self, stream_event):
        """Persist member identity carried by Talkin STREAM events.

        Some Talkin builds do not return the complete room roster through
        RoomAdmin. Instead, STREAM records contain the member identity in
        fields 2/8/13/14/42: username, role, room, user_id and presence.
        These records are safe to merge into the append/update-only roster.
        They are NOT treated as the complete roster by themselves.
        """
        if not isinstance(stream_event, dict):
            return False
        try:
            username = str(stream_event.get(2, "") or "").strip()
            room = str(stream_event.get(13, "") or stream_event.get("room", "") or "").strip()
            role = str(stream_event.get(8, "") or stream_event.get("role", "") or "none").strip().lower() or "none"
            user_id = str(stream_event.get(14, "") or stream_event.get("user_id", "") or "").strip()
            online = stream_event.get(42, stream_event.get("online", stream_event.get("is_present")))
            if not username or not room or _norm_user(username) == _norm_user(BOT_ID):
                return False
            # Only accept the known STREAM member shape. This prevents an
            # unrelated StreamEvent from being mistaken for a room member.
            if not user_id or not user_id.isdigit():
                return False
            if role not in {"owner", "admin", "moderator", "mod", "member", "user", "none"}:
                return False
            item = {
                "username": username,
                "role": role,
                "user_id": user_id,
                "online": online,
                "is_present": online,
            }
            # STREAM packets can repeat frequently while a live room is active.
            # Persisting the entire room_users.json on every packet causes heavy
            # JSON serialization + state-db/GitHub work and can saturate CPU.
            cache = getattr(self, "_stream_member_cache", {})
            cache_key = (_norm_room(room), _norm_user(username))
            previous = cache.get(cache_key)
            current = (role, user_id)
            self.room_users.setdefault(room, {})[username] = role
            if user_id:
                profile_ids = getattr(self, "user_profile_ids", None)
                if not isinstance(profile_ids, dict):
                    profile_ids = self.user_profile_ids = {}
                profile_ids[username.casefold()] = user_id
                profile_ids[_norm_user(username)] = user_id
            if previous != current:
                cache[cache_key] = current
                self._stream_member_cache = cache
                _remember_roster(room, [item])
            return True
        except Exception as exc:
            self.log("[ROSTER] STREAM member cache failed:", repr(exc))
            return False

    def _queue_bot_avatar_room_refresh(self, expected_url=""):
        """Refresh native room rosters after a bot profile-photo change.

        The profile_update request changes the account profile. Talkin room
        clients also consume the native occupants/UserItem roster, so request
        a fresh roster for every room the bot is currently connected to.
        This does not leave/rejoin rooms or alter membership/permissions.
        """
        rooms = sorted(
            {str(r).strip() for r in getattr(self, "connected_rooms", set()) if str(r).strip()},
            key=str.casefold,
        )
        if not rooms:
            fallback = str(getattr(self, "last_joined_room", "") or getattr(self, "room", "") or "").strip()
            if fallback:
                rooms = [fallback]
        if not rooms:
            self.log("[PROFILE-PHOTO] no connected rooms available for avatar roster refresh")
            return False

        # Keep the new value available immediately.  The native roster response
        # may arrive late (or may omit the bot item), while the profile page is
        # updated synchronously by profile_update.
        expected_url = str(expected_url or "").strip()
        if expected_url.startswith(("http://", "https://")):
            self.bot_profile_photo_url = expected_url

        def refresh_all():
            try:
                time.sleep(max(1.0, float(os.getenv("BOT_AVATAR_ROOM_REFRESH_DELAY", "2.5"))))
                self.log("[PROFILE-PHOTO] starting bot avatar room refresh:", "rooms=", len(rooms))
                for room_name in rooms:
                    if getattr(self, "stop_event", None) is not None and self.stop_event.is_set():
                        break
                    # Talkin builds disagree on the `to` field for this
                    # request.  The current APK uses `username`, while older
                    # gateways (and the normal room refresh path) require the
                    # bot username.  Try both so the room UserItem is rebuilt
                    # instead of leaving the old avatar cached in the room.
                    sent_app = self._request_native_profile_roster(room_name, legacy=False)
                    time.sleep(max(0.15, float(os.getenv("BOT_AVATAR_ROOM_REFRESH_VARIANT_GAP", "0.25"))))
                    sent_legacy = self._request_native_profile_roster(room_name, legacy=True)
                    self.log(
                        "[PROFILE-PHOTO] bot avatar roster refresh:", room_name,
                        "app=", bool(sent_app), "legacy=", bool(sent_legacy),
                    )
                    time.sleep(max(0.2, float(os.getenv("BOT_AVATAR_ROOM_REFRESH_GAP", "0.7"))))
                self.log("[PROFILE-PHOTO] room refresh requests completed; waiting for native roster response")

                # A number of Talkin gateways return the room roster
                # successfully but omit the bot's own UserItem (the log then
                # shows fewer photos than users).  In that case the room UI
                # keeps its old avatar cache.  A short leave/rejoin cycle is
                # the same refresh used by the native client and makes the
                # server send a fresh UserItem containing the new photo.
                if os.getenv("BOT_AVATAR_FORCE_ROOM_REJOIN", "0").strip().casefold() not in {
                    "0", "false", "no", "off"
                }:
                    rejoin_gap = max(1.0, float(os.getenv("BOT_AVATAR_ROOM_REJOIN_DELAY", "1.5")))
                    time.sleep(rejoin_gap)
                    for room_name in rooms:
                        if getattr(self, "stop_event", None) is not None and self.stop_event.is_set():
                            break
                        try:
                            self.send_query(encode_query("room_leave", room=room_name))
                            self.connected_rooms = {
                                saved for saved in getattr(self, "connected_rooms", set())
                                if _norm_room(saved) != _norm_room(room_name)
                            }
                            time.sleep(rejoin_gap)
                            joined = self.join_room(
                                room_name, force=True, requested_by="bot-avatar-refresh"
                            )
                            self.log(
                                "[PROFILE-PHOTO] bot avatar room rejoin:",
                                room_name, "sent=", bool(joined),
                            )
                            time.sleep(rejoin_gap)
                        except Exception as exc:
                            self.log("[PROFILE-PHOTO] bot avatar room rejoin failed:", room_name, repr(exc))
            except Exception as exc:
                self.log("[PROFILE-PHOTO] bot avatar room refresh failed:", repr(exc))

        threading.Thread(target=refresh_all, name="bot-avatar-room-refresh", daemon=True).start()
        return True

    def _upload_private_media_official(self, media_path, target, media_type):
        """Upload a private audio/image using Talkinchat's Android flow."""
        media_path = Path(media_path)
        target = str(target or "").strip().lstrip("@")
        media_type = str(media_type or "").strip().lower()
        if not media_path.is_file() or media_type not in {"audio", "image"} or not target:
            raise ValueError("بيانات رفع الوسائط الخاصة غير صالحة")
        fields = {
            "jid": str(BOT_ID or "").strip(),
            "to_user": target,
            "is_private": "yes",
            "type": media_type,
            "device_id": "null",
            "s": os.getenv("TALKINCHAT_UPLOAD_SESSION", ""),
        }
        if not fields["jid"]:
            raise RuntimeError("BOT_ID غير مضبوط")
        def post_upload(path, strict=False):
            mime_type = "audio/mpeg" if media_type == "audio" else (
                mimetypes.guess_type(path.name)[0] or "image/jpeg"
            )
            with path.open("rb") as media_file:
                response = requests.post(
                    "https://talkinchat.com/upload",
                    data=fields,
                    files={"file": (path.name, media_file, mime_type)},
                    headers={"User-Agent": "Talkinchat/5.8.3", "Accept": "text/plain"},
                    timeout=(10, 90),
                )
            response.raise_for_status()
            raw_response = str(response.text or "").strip().strip('"')
            uploaded_url = raw_response
            if uploaded_url.startswith("{"):
                try:
                    payload = response.json()
                    uploaded_url = str(
                        payload.get("url") or payload.get("media_url")
                        or payload.get("file_url") or payload.get("result") or ""
                    ).strip()
                except Exception:
                    uploaded_url = ""
            if not uploaded_url or uploaded_url.casefold() in {"failed", "error", "none", "null"}:
                self.log("[MEDIA] Talkin upload rejected:", media_type,
                         "status=", response.status_code, "body=", repr(raw_response[:300]))
                if strict:
                    raise RuntimeError("رفض خادم Talkinchat رفع ملف الخاص")
                return ""
            if uploaded_url.startswith("/"):
                uploaded_url = "https://talkinchat.com" + uploaded_url
            if not uploaded_url.startswith(("http://", "https://")):
                if strict:
                    raise RuntimeError("استجابة رفع ملف الخاص ليست رابطاً صالحاً")
                return ""
            return uploaded_url

        normalized_path = None
        try:
            upload_path = media_path
            if media_type == "audio" and shutil.which("ffmpeg"):
                normalized_path = BASE_DIR / "generated_music" / f"coffee_{uuid.uuid4().hex}.mp3"
                proc = subprocess.run(
                    [shutil.which("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                     "-i", str(media_path), "-vn", "-ac", "2", "-ar", "44100",
                     "-codec:a", "libmp3lame", "-b:a", "128k", "-map_metadata", "-1",
                     str(normalized_path)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=90,
                )
                if proc.returncode == 0 and normalized_path.is_file() and normalized_path.stat().st_size > 4096:
                    upload_path = normalized_path
                else:
                    normalized_path = None
            uploaded_url = post_upload(upload_path)
            if not uploaded_url and upload_path != media_path:
                uploaded_url = post_upload(media_path, strict=True)
            if not uploaded_url:
                raise RuntimeError("رفض خادم Talkinchat رفع ملف الخاص")
            self.log("[MEDIA] official private upload completed:", media_type, target, uploaded_url[:180])
            return uploaded_url
        finally:
            if normalized_path is not None:
                try:
                    normalized_path.unlink(missing_ok=True)
                except Exception:
                    pass

    def send_room_media(self, room: str, media_url: str, media_type: str, duration: int = 0):
        """Send room media using Query's normal room/url fields.

        Text messages already prove that Query field ``room`` (field 6) is
        the room identifier. Media uses the same field; ``length`` (field 3)
        carries the optional audio duration. Putting the room in ``password``
        made Talkin accept the packet but discard the image/audio payload.
        """
        if media_type == "audio":
            packet = encode_query(
                "room_message", type_="audio",
                length=str(max(0, int(duration or 0))), room=room, url=media_url
            )
        else:
            packet = encode_query(
                "room_message", type_=media_type, room=room, url=media_url
            )
        limit = _ws_payload_limit()
        if len(packet) > limit:
            self.log("[WS] media packet dropped before hard payload limit:", media_type, len(packet), limit)
            return False
        return self.send_query(packet)

    def _send_room_result_pages(self, room, sender, pages, language="ar"):
        """Store a room result list so `ns` can continue it, 30 items at a time."""
        room_text = str(room or "")
        sender_text = str(sender or "")
        page_list = [str(page or "") for page in (pages or []) if str(page or "").strip()]
        if not page_list:
            return False
        key = ("room_message", room_text, sender_text)
        result_pages = getattr(self, "_result_pages", {})
        if not isinstance(result_pages, dict):
            result_pages = {}
        result_pages[key] = {
            "pages": page_list, "part": 1,
            "kwargs": {"room": room_text}, "language": "en" if language == "en" else "ar",
        }
        self._result_pages = result_pages
        # A newly opened room list takes precedence over an older master-only
        # transfer log when the same user next types `ns`.
        transfer_key = _norm_user(sender_text)
        transfer_pages = getattr(self, "_transfer_log_pages", None)
        if isinstance(transfer_pages, dict):
            transfer_pages.pop(transfer_key, None)
        transfer_languages = getattr(self, "_transfer_log_languages", None)
        if isinstance(transfer_languages, dict):
            transfer_languages.pop(transfer_key, None)
        first = page_list[0]
        first += (
            "\n\n📌 Type ns for the next list." if language == "en"
            else "\n\n📌 اكتب ns للانتقال إلى القائمة التالية."
        )
        return self._send_text_packets("room_message", first, room=room_text)

    def _verify_public_media_url(self, url: str, media_kind: str = "image"):
        try:
            r=requests.get(url,headers={"Range":"bytes=0-4095","User-Agent":"TalkinBot/22"},timeout=15,stream=True)
            ctype=(r.headers.get("Content-Type") or "").lower()
            if r.status_code not in (200,206):
                raise RuntimeError(f"الرابط العام أعاد HTTP {r.status_code}")
            if media_kind=="image" and not ctype.startswith("image/"):
                raise RuntimeError(f"نوع الصورة غير صحيح: {ctype or 'unknown'}")
            if media_kind=="video" and not (ctype.startswith("video/") or "octet-stream" in ctype):
                raise RuntimeError(f"نوع الفيديو غير صحيح: {ctype or 'unknown'}")
            if media_kind=="audio" and not (ctype.startswith("audio/") or "octet-stream" in ctype):
                raise RuntimeError(f"نوع الصوت غير صحيح: {ctype or 'unknown'}")
            return True
        except Exception as exc:
            self.log("[MEDIA] public URL check failed:",repr(exc))
            raise RuntimeError(f"الرابط العام للوسائط غير قابل للوصول: {exc}") from exc
