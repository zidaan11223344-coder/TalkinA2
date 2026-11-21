"""WebSocket callbacks, reconnect/bootstrap, run loop and process startup."""

class LifecycleSection:
    def on_open(self, ws):
        # Room join is deliberately performed once by bootstrap_after_connect(),
        # after the server handshake/bootstrap frame. Joining here as well can
        # cause duplicate join/leave events on some server sessions.
        self.log("[WS] connected:", self.websocket_url())

    def on_error(self, ws, error):
        self.last_error = str(error)
        self.log("[WS] error:", error)

    def _reset_room_connection_state(self):
        """Discard acknowledgements and delayed join workers from an old socket."""
        self._connection_generation = int(getattr(self, "_connection_generation", 0)) + 1
        getattr(self, "connected_rooms", set()).clear()
        pending = getattr(self, "_pending_room_joins", {})
        for item in list(pending.values()):
            timer = item.get("timer") if isinstance(item, dict) else None
            if timer:
                try:
                    timer.cancel()
                except Exception:
                    pass
        pending.clear()
        auto_pending = getattr(self, "_auto_rejoin_pending", None)
        lock = getattr(self, "_auto_rejoin_lock", None)
        getattr(self, "_auto_rejoin_waiting_ack", set()).clear()
        if auto_pending is not None:
            if lock is None:
                auto_pending.clear()
            else:
                with lock:
                    auto_pending.clear()

    def on_close(self, ws, code, msg):
        # These are rooms connected to THIS WebSocket session. Once it closes,
        # none of them may be reported by "غرفي" until the server confirms
        # them again after reconnect. The historical list remains on disk.
        self._reset_room_connection_state()
        threading.Thread(
            target=_flush_github_backup,
            kwargs={"timeout": GITHUB_BACKUP_WAIT_SECONDS},
            daemon=True,
            name="backup-before-reconnect",
        ).start()
        self.log("[WS] closed:", code, msg)

    def bootstrap_after_connect(self):
        """Wait briefly for the server bootstrap, then request the room lists."""
        deadline = time.time() + float(os.getenv("BOOTSTRAP_WAIT", "2"))
        got_server_frame = False

        while time.time() < deadline:
            remaining = max(0.2, deadline - time.time())
            old_timeout = getattr(self.ws, "timeout", 20)
            try:
                self.ws.sock.settimeout(min(remaining, 1.0))
                kind, message = self.ws.recv()
            except socket.timeout:
                continue
            finally:
                try:
                    self.ws.sock.settimeout(old_timeout)
                except Exception:
                    pass

            if kind == "ping":
                continue
            if kind == "pong":
                continue
            if kind == "oversize":
                self.log("[WS_DROP_OVERSIZE_INBOUND] تم تجاهل حزمة واردة كبيرة:", message)
                continue
            if kind == "close":
                raise ConnectionError(f"WebSocket closed during server bootstrap: {message}")
            if kind != "binary":
                continue

            got_server_frame = True
            self.on_message(self.ws, message)

            # First valid server result is the synchronization point used
            # before room-list loading.
            try:
                # Newer TalkinChat builds expose this request name directly.
                self.send_query(encode_query("load_list_new"))
                self.log("[BOOTSTRAP] sent load_list_new")
            except Exception as e:
                self.log("[BOOTSTRAP] load_list_new failed:", repr(e))
            break

        if not got_server_frame:
            # Do not hang forever if a build/server does not send an initial
            # unsolicited frame. Still request the list exactly once.
            try:
                self.send_query(encode_query("load_list_new"))
                self.log("[BOOTSTRAP] no unsolicited frame; sent load_list_new")
            except Exception as e:
                self.log("[BOOTSTRAP] list request failed:", repr(e))

        # Give the server a short window to return list data before joining.
        list_deadline = time.time() + float(os.getenv("LIST_BOOTSTRAP_WAIT", "0.5"))
        while time.time() < list_deadline:
            remaining = min(0.8, max(0.1, list_deadline - time.time()))
            old_timeout = getattr(self.ws, "timeout", 20)
            try:
                self.ws.sock.settimeout(remaining)
                kind, message = self.ws.recv()
            except socket.timeout:
                continue
            finally:
                try:
                    self.ws.sock.settimeout(old_timeout)
                except Exception:
                    pass
            if kind == "binary":
                self.on_message(self.ws, message)
            elif kind == "oversize":
                self.log("[WS_DROP_OVERSIZE_INBOUND] تم تجاهل حزمة واردة كبيرة:", message)
            elif kind == "ping":
                continue
            elif kind == "close":
                raise ConnectionError(f"WebSocket closed during room-list bootstrap: {message}")

        # Startup/reconnect room policy:
        # OFF = only the configured GROUP_TO_JOIN/FIRST_ROOM.
        # ON  = all rooms saved in tracked_rooms.json plus GROUP_TO_JOIN.
        # Manual `.دخول غرفي` is independent and always joins the saved list.
        if MASTER_SERVICE_ENABLED:
            rooms_to_restore = set()
        else:
            # tracked_rooms.json is the authoritative startup source. The
            # in-memory known_rooms set may be a truncated/stale view after a
            # reconnect and was the reason only a subset was restored.
            rooms_to_restore = {
                str(r).strip() for r in _persistent_rooms()
                if str(r).strip() and _norm_room(r) not in getattr(self, "blocked_rooms", set())
            }
            if self.room:
                rooms_to_restore.add(str(self.room).strip())
        if rooms_to_restore:
            self._room_restore_candidates = {
                _norm_room(room_name) for room_name in rooms_to_restore if _norm_room(room_name)
            }
            threading.Thread(
                target=self._join_rooms_serially,
                args=(sorted(rooms_to_restore), "", True),
                daemon=True, name="fast-room-restore",
            ).start()

    def run_once(self):
        # Start a fresh live-room view for this WebSocket session.
        self._reset_room_connection_state()
        self.authenticate()
        # Android saves AuthResult.server into SharedPreferences and then
        # Client uses that saved server for the WebSocket. Do the same:
        # authenticated server first, configured default only as fallback.
        ports = []
        # AuthResult.server is the server selected by TalkinChat for this
        # account/session. Prefer it and only use SOCKET_PORT if auth did not
        # provide a server.
        auth_port = getattr(self, "auth_server", "")
        if auth_port:
            ports.append(auth_port)
        elif DEFAULT_PORT:
            ports.append(DEFAULT_PORT)
        if not ports:
            ports = ["5335"]

        # Exact default 5.8.3 Client.smali branch: Client reads `default/m`
        # with default `n`, then calls M9/c.c(), which supplies only `b`.
        # This is also the branch that previously reached HTTP 101 on this server.
        header_lines = self.app_ws_headers()

        last_error = None
        # Client.smali constructs only chatp.net:<server>/server. The CDN entry
        # seen in the connection monitor is not used by this Talkin Client path,
        # so do not treat it as a WebSocket fallback.
        for port in ports:
            self.port = port
            for host in WS_HOSTS:
                self.ws_host = host
                for path in WS_PATHS:
                    self.ws_path = path
                    url = self.websocket_url(path)
                    self.log("[WS] trying host/port/path:", host, port, path, url)
                    try:
                        # Y9/v Client.smali does not copy the HTTP auth Session
                        # cookies into the WebSocket handshake. Keep the WS request
                        # limited to the headers actually built by the APK.
                        self.ws = RawWebSocket(url, list(header_lines), timeout=20, debug=RAW_DIAGNOSTIC)
                        self.ws.connect()
                        self._start_heartbeat()
                        self.log("[WS] CONNECTED:", url)
                        self.log("[WS] custom headers:", [x.split(":",1)[0] + ": <redacted>" if x.lower().startswith(("username:","password:")) else x for x in header_lines])
                        self.bootstrap_after_connect()
                        if self._db_success_notice_pending and BOT_MASTER:
                            notice_sent = self.send_private_text(
                                BOT_MASTER,
                                f"✅ تم ربط قاعدة البيانات {RIVEN_DATABASE_KIND.upper()} بنجاح.\n🗄️ قاعدة البيانات: {RIVEN_DATABASE_NAME}",
                            )
                            if notice_sent:
                                self._db_success_notice_pending = False
                        # Publish the profile status once after the first fully
                        # established connection. A reconnect restores the
                        # base status only when no temporary gift is active.
                        if not self._set_first_connection_status():
                            with self._profile_status_lock:
                                gift_active = bool(self._profile_status_timer)
                            if not gift_active:
                                self._set_profile_status(self._profile_base_status or BOT_BASE_STATUS)
                                self._profile_current_status = self._profile_base_status or BOT_BASE_STATUS
                        now = time.time()
                        reason = self._pending_reconnect_reason
                        self._pending_reconnect_reason = ""
                        reconnect_command = self._pending_reconnect_command
                        self._pending_reconnect_command = None
                        cooldown = float(getattr(self, "_connection_notice_cooldown", 300.0))
                        if reason and now - self._last_connection_notice >= cooldown:
                            cmd_age = now - float(reconnect_command.get("created_at", 0)) if isinstance(reconnect_command, dict) else 999999
                            if not (isinstance(reconnect_command, dict) and reconnect_command.get("command") and cmd_age <= 45.0):
                                reconnect_command = None
                            threading.Thread(
                                target=self._send_reconnect_notice,
                                args=(reason, reconnect_command),
                                daemon=True,
                                name="connection-recovery-notice",
                            ).start()
                            self._last_connection_notice = now
                        elif reason:
                            self.log("[CONNECTION_NOTICE] suppressed by cooldown; reason=", reason[:500])
                        self._had_connection = True

                        while not self.stop_event.is_set():
                            try:
                                kind, message = self.ws.recv()
                            except socket.timeout:
                                # An idle room is normal. Do not reconnect just
                                # because no WebSocket frame arrived during the
                                # read timeout.
                                continue
                            if kind == "binary":
                                self.on_message(self.ws, message)
                            elif kind == "oversize":
                                self.log("[WS_DROP_OVERSIZE_INBOUND] تم تجاهل حزمة واردة كبيرة:", message)
                            elif kind == "ping":
                                continue
                            elif kind == "pong":
                                continue
                            elif kind == "text":
                                self.log("[WS] unexpected text frame received")
                            elif kind == "close":
                                raise ConnectionError(f"WebSocket closed by server: {message}")
                        self._stop_heartbeat()
                        return
                    except Exception as e:
                        self._stop_heartbeat()
                        last_error = e
                        self.log("[WS] host/path failed:", host, port, path, repr(e))
                        if self._had_connection:
                            threading.Thread(
                                target=_flush_github_backup,
                                kwargs={"timeout": GITHUB_BACKUP_WAIT_SECONDS},
                                daemon=True,
                                name="backup-before-reconnect",
                            ).start()
                        try:
                            if self.ws:
                                self.ws.close()
                        except Exception:
                            pass
                        self.ws = None
        raise last_error

    def start(self):
        if DEBUG and not QUIET_MODE:
            print("=== Talkinchat Bot V22 - Talkin + YouTube Cookies + Giant Gift Cards ===", flush=True)
        missing = []
        if not BOT_ID:
            missing.append("BOT_ID (or BOT_USERNAME)")
        if not BOT_PWD:
            missing.append("BOT_PWD (or BOT_PASSWORD)")
        if not self.room and not MASTER_SERVICE_ENABLED:
            missing.append("GROUP_TO_JOIN (or FIRST_ROOM)")
        if DEBUG and not QUIET_MODE:
            print(f"[ROOM_POLICY] startup={'ALL_SAVED_ROOMS' if AUTO_JOIN_ALL_ROOMS else 'FIRST_ROOM_ONLY'} verification={'ON' if VERIFICATION_ENABLED else 'OFF'}", flush=True)
        if missing:
            raise SystemExit(
                "Missing required deployment variables: " + ", ".join(missing) + ". "
                "Add them to Railway Variables (not the source code) and redeploy."
            )
        if TELEGRAM_BOT_TOKEN and TELEGRAM_POLL_ENABLED:
            threading.Thread(target=self._telegram_poll_loop, name="telegram-poll", daemon=True).start()
        elif TELEGRAM_BOT_TOKEN:
            self.log("[TELEGRAM] polling disabled for this process; another instance owns getUpdates")
        else:
            self.log("[TELEGRAM] TELEGRAM_BOT_TOKEN is not configured; Telegram log upload disabled")
        self._start_cpu_guard()
        self._cleanup_thread = start_runtime_cleanup(self.stop_event, self)
        self.asset_server = start_asset_server()
        try:
            ytdlp_version = getattr(getattr(yt_dlp, "version", None), "__version__", "installed") if yt_dlp else "MISSING"
            js_runtime_status = []
            for runtime_name in ("deno", "node"):
                runtime_path = shutil.which(runtime_name)
                if runtime_path:
                    try:
                        probe = subprocess.run([runtime_path, "--version"], capture_output=True, text=True, timeout=3, check=False)
                        js_runtime_status.append(f"{runtime_name}={((probe.stdout or probe.stderr).strip() or 'unknown')}")
                    except Exception:
                        js_runtime_status.append(f"{runtime_name}=unavailable")
            self.log(
                "[MEDIA_DIAGNOSTICS]",
                f"yt_dlp={ytdlp_version}",
                f"ffmpeg={'yes' if shutil.which('ffmpeg') else 'NO'}",
                f"Pillow={'yes' if PIL_AVAILABLE else 'NO'}",
                f"JS={','.join(js_runtime_status) if js_runtime_status else 'NO Deno/Node'}",
                f"public_base={'set' if _public_base_url() else 'MISSING'}",
                f"asset_server={'started' if self.asset_server else 'FAILED/DISABLED'}",
                f"port={ASSET_HTTP_PORT}",
                f"pot_provider={'set:'+YOUTUBE_POT_BASE_URL if YOUTUBE_POT_BASE_URL else 'localhost:4416/default-if-plugin-installed'}",
            )
        except Exception as diagnostic_exc:
            self.log("[MEDIA_DIAGNOSTICS] failed:", type(diagnostic_exc).__name__)
        while not self.stop_event.is_set():
            try:
                self.run_once()
                # run_once normally blocks until stop_event or a socket error;
                # if it returns normally, the connection was not rejected.
                self._reconnect_delay = 10.0
            except Exception as e:
                self.last_error = str(e)
                raw_reason = self._redact_connection_secrets(e)
                normal_transport_close = _is_normal_ws_close_reason(raw_reason)
                if "code': 1000" in raw_reason or '"code": 1000' in raw_reason:
                    raw_reason = "الخادم أغلق WebSocket إغلاقًا طبيعياً (1000): " + raw_reason
                if "1009" in raw_reason:
                    raw_reason = "الخادم أغلق WebSocket بسبب حجم الرسالة (1009): " + raw_reason
                if normal_transport_close:
                    # A peer can close an idle TCP/WebSocket connection without
                    # a close frame. Reconnect, but do not report it as a bot
                    # failure or send a noisy "connection returned" alert.
                    self.log("[WS] socket closed normally; reconnecting quietly")
                    self._pending_reconnect_reason = ""
                    self._reconnect_delay = 10.0
                else:
                    if self.room:
                        raw_reason = f"{raw_reason[:850]} | آخر غرفة: {self.room}"
                    self._pending_reconnect_reason = raw_reason[:1200]
                if isinstance(getattr(self, "_inflight_command", None), dict):
                    self._pending_reconnect_command = dict(self._inflight_command)
                if DEBUG and not QUIET_MODE:
                    print("[BOT] error:", repr(e), flush=True)
            if not self.stop_event.is_set():
                delay = self._reconnect_delay
                if DEBUG and not QUIET_MODE:
                    print(f"[BOT] reconnecting in {int(delay)}s...", flush=True)
                if self.stop_event.wait(delay):
                    break
                self._reconnect_delay = min(self._reconnect_delay * 2.0, self._reconnect_delay_max)
