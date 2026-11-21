"""Runtime health, logging, Telegram integration, authentication helpers and safe diagnostics."""

class RuntimeServicesSection:
    def _load_social_features(self):
        self.auto_replies_file = REPLIES_FILE
        self.custom_welcomes_file = DATA_DIR / "custom_welcomes.json"
        try:
            data = _ensure_replies_file()
            self.auto_replies_enabled = bool(data.get("auto_replies_enabled", True))
            raw = data.get("auto_replies", {})
            fixed = {}
            if isinstance(raw, dict):
                for k,v in raw.items():
                    key=_auto_reply_key(k)
                    if isinstance(v, dict):
                        replies=v.get("replies")
                        if replies is None and "reply" in v:
                            replies=v.get("reply")
                        if isinstance(replies, str):
                            replies=[replies]
                        fixed[key]={"trigger":str(v.get("trigger") or k),"replies":[str(x).strip() for x in (replies or []) if str(x).strip()]}
                    elif isinstance(v, str):
                        fixed[key]={"trigger":str(k),"replies":[v]}
                self.auto_replies = fixed
            else:
                self.auto_replies = {}
        except Exception:
            self.auto_replies_enabled, self.auto_replies = True, {}
        try:
            data = _load_local_json(self.custom_welcomes_file, {})
            self.custom_welcome_enabled = bool(data.get("enabled", True))
            raw = data.get("welcomes", {})
            self.custom_welcomes = raw if isinstance(raw, dict) else {}
        except Exception:
            self.custom_welcome_enabled, self.custom_welcomes = True, {}

    def _save_social_features(self):
        data = _ensure_replies_file()
        data["auto_replies_enabled"] = bool(self.auto_replies_enabled)
        data["auto_replies"] = self.auto_replies
        _save_local_json(self.auto_replies_file, data)
        _save_local_json(self.custom_welcomes_file, {"enabled": self.custom_welcome_enabled, "welcomes": self.custom_welcomes})

    def _load_departure_replies(self):
        """Load departure-trigger replies from their own file, separate from auto replies."""
        self.departure_replies_file = DATA_DIR / "departure_replies.json"
        data = _load_local_json(self.departure_replies_file, {})
        self.departure_farewell_enabled = bool(
            data.get("farewell_enabled", data.get("enabled", True))
        ) if isinstance(data, dict) else True
        raw = data.get("replies", {}) if isinstance(data, dict) else {}
        fixed = {}
        if isinstance(raw, dict):
            for trigger, replies in raw.items():
                key = _departure_trigger_key(trigger)
                values = [str(item).strip() for item in (replies if isinstance(replies, list) else [replies]) if str(item).strip()]
                if key and values:
                    fixed[key] = values
        self.departure_replies = fixed

    def _save_departure_replies(self):
        _save_local_json(
            getattr(self, "departure_replies_file", DATA_DIR / "departure_replies.json"),
            {
                "farewell_enabled": bool(getattr(self, "departure_farewell_enabled", True)),
                "replies": getattr(self, "departure_replies", {}),
            },
        )

    def _auto_reply_variants(self, trigger):
        item = self.auto_replies.get(_auto_reply_key(trigger))
        if isinstance(item, dict):
            raw = item.get("replies")
            if isinstance(raw, list):
                values = [str(x).strip() for x in raw if str(x).strip()]
                if values:
                    return values
            one = str(item.get("reply") or "").strip()
            return [one] if one else []
        if isinstance(item, list):
            return [str(x).strip() for x in item if str(x).strip()]
        if isinstance(item, str) and item.strip():
            return [item.strip()]
        return []

    def _render_auto_reply(self, template, username, room):
        # Automatic replies always show the username as plain text, never as
        # a mention. This also handles templates that were saved previously
        # as "@{username}".
        clean_name = str(username or "").strip().lstrip("@")
        text = str(template or "")
        has_username_placeholder = "{username}" in text
        text = text.replace("@{username}", "{username}")
        text = text.replace("{username}", clean_name)
        text = text.replace("{room}", str(room or ""))
        text = text.replace("@@", "@").strip()
        if clean_name and text and not has_username_placeholder:
            text = f"{clean_name} {text}"
        return text

    def _choose_auto_reply(self, trigger, username, room):
        """Return exactly ONE saved reply per trigger event.

        Replies are consumed from a shuffled cycle so the bot does not dump the
        whole reply list and does not immediately repeat one response. A new
        cycle is generated only after every saved response has been used.
        """
        variants = self._auto_reply_variants(trigger)
        if not variants:
            return ""
        key = _auto_reply_key(trigger)
        state = getattr(self, "_auto_reply_cycle", {})
        signature = tuple(variants)
        entry = state.get(key) if isinstance(state, dict) else None

        if not isinstance(entry, dict) or tuple(entry.get("signature", ())) != signature:
            order = list(range(len(variants)))
            random.shuffle(order)
            entry = {"signature": signature, "order": order, "index": 0}

        order = entry.get("order") or []
        index = int(entry.get("index", 0) or 0)
        if len(order) != len(variants) or index >= len(order):
            order = list(range(len(variants)))
            random.shuffle(order)
            index = 0

        reply = variants[order[index]]
        entry["signature"] = signature
        entry["order"] = order
        entry["index"] = index + 1
        state[key] = entry
        self._auto_reply_cycle = state
        return self._render_auto_reply(reply, username, room)

    def _departure_reply_text(self, trigger, username, room):
        defaults = {
            "ب": "🌀 في الخلاط", "ت": "⚰️ في التابوت",
            "ق": "❤️ بقلبي", "ك": "👑 ملك الكركيت",
        }
        choices = getattr(self, "departure_replies", {}).get(trigger, [])
        if isinstance(choices, str):
            choices = [choices]
        if choices:
            template = secrets.choice(choices)
        elif trigger in defaults:
            # The established single-letter reactions remain active even when
            # the generic farewell is switched off.
            template = defaults[trigger]
        elif getattr(self, "departure_farewell_enabled", True):
            template = "👋 غادرنا بالسلامة"
        else:
            return ""
        name = str(username or "").strip().lstrip("@")
        text = str(template).replace("{username}", name).replace("{room}", str(room or "")).strip()
        if "{username}" not in str(template) and name:
            text = f"👤 {name} {text}"
        return text

    def _handle_departure_reply_command(self, room, body):
        trigger = _departure_trigger_key(body)
        if not trigger:
            return False
        last = getattr(self, "_last_departed_user_by_room", {})
        username = last.get(_norm_room(room), "") if isinstance(last, dict) else ""
        if not username:
            self.send_room_text(room, "📭 لا يوجد اسم مغادر مسجل الآن؛ لا أختلق مغادرة.")
            return True
        reply = self._departure_reply_text(trigger, username, room)
        if reply:
            self.send_room_text(room, reply)
        return True

    def _cpu_guard_snapshot(self, cpu_percent):
        """Collect cheap clues about CPU pressure without inspecting or stopping the WS."""
        queue_size = -1
        try:
            queue_size = self._event_queue.qsize()
        except Exception:
            pass
        active_threads = [thread.name for thread in threading.enumerate() if thread.is_alive()]
        return (
            f"cpu={cpu_percent:.1f}% threads={len(active_threads)} "
            f"event_queue={queue_size} log_room_messages={'on' if LOG_ROOM_MESSAGES else 'off'} "
            f"log_suppressed={getattr(self, '_cpu_log_suppressed', 0)} "
            f"rooms={len(getattr(self, 'connected_rooms', set()))} "
            f"music={len(getattr(self, 'music_current', {}))} "
            f"crops={len(getattr(self, 'crop_plots', {}))} "
            f"workers={','.join(active_threads[:18])}"
        )

    def _cpu_guard_loop(self):
        """Reduce diagnostic/logging pressure when this process uses over 50% CPU.

        This is deliberately cooperative: no socket close, room leave, process
        restart, or server stop is performed by the guard.
        """
        last_wall = time.monotonic()
        last_cpu = time.process_time()
        was_cooling = False
        while not self._cpu_guard_stop.wait(CPU_GUARD_INTERVAL_SECONDS):
            now_wall = time.monotonic()
            now_cpu = time.process_time()
            wall_delta = max(0.001, now_wall - last_wall)
            cpu_percent = max(0.0, (now_cpu - last_cpu) * 100.0 / wall_delta)
            last_wall, last_cpu = now_wall, now_cpu
            self._cpu_last_percent = cpu_percent
            if cpu_percent >= CPU_GUARD_HIGH_PERCENT and not was_cooling:
                was_cooling = True
                self._cpu_cooling = True
                self._cpu_cooling_since = time.time()
                snapshot = self._cpu_guard_snapshot(cpu_percent)
                # This diagnosis distinguishes log pressure from event/music
                # pressure; room messages are not logged by default.
                self.log("[CPU-GUARD] cooling enabled;", snapshot)
                if BOT_MASTER:
                    try:
                        self.send_private_text(
                            BOT_MASTER,
                            "⚠️ تبريد CPU تلقائي فعّال بدون فصل البوت أو السيرفر.\n"
                            f"{snapshot}\n"
                            "تم تخفيف السجل التشخيصي فقط؛ الاتصال والغرف لم يتم لمسها.",
                        )
                    except Exception:
                        pass
            elif was_cooling and cpu_percent <= CPU_GUARD_RECOVER_PERCENT:
                was_cooling = False
                self._cpu_cooling = False
                self.log(f"[CPU-GUARD] cooling disabled; cpu={cpu_percent:.1f}%")

    def _start_cpu_guard(self):
        if not CPU_GUARD_ENABLED or self._cpu_guard_thread is not None:
            return
        self._cpu_guard_stop.clear()
        self._cpu_guard_thread = threading.Thread(
            target=self._cpu_guard_loop, name="cpu-guard", daemon=True
        )
        self._cpu_guard_thread.start()

    def _stop_cpu_guard(self):
        self._cpu_guard_stop.set()
        thread = self._cpu_guard_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._cpu_guard_thread = None

    def log(self, *args):
        """Optional diagnostics. Quiet mode avoids stdout and persistent log growth."""
        if QUIET_MODE and not DEBUG:
            return
        try:
            # A room-message body is user content, not a diagnostic.  The
            # normal room handler does not pass it here; this guard also keeps
            # future diagnostic calls from accidentally filling Riven logs.
            if not LOG_ROOM_MESSAGES and any(
                isinstance(value, dict) and value.get("_room_message_body")
                for value in args
            ):
                return
            line = " ".join(str(x) for x in args)
            self._cpu_log_suppressed = int(getattr(self, "_cpu_log_suppressed", 0) or 0)
            if getattr(self, "_cpu_cooling", False):
                now = time.monotonic()
                critical = any(
                    marker in line.upper()
                    for marker in ("ERROR", "FAILED", "EXCEPTION", "RECONNECT", "AUTH")
                )
                if not critical and line == getattr(self, "_cpu_last_log_line", "") and now - getattr(self, "_cpu_last_log_at", 0.0) < CPU_GUARD_LOG_COOLDOWN_SECONDS:
                    self._cpu_log_suppressed += 1
                    return
                self._cpu_last_log_line = line
                self._cpu_last_log_at = now
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            record = f"[{stamp}] {line}"
            try:
                TELEGRAM_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                lock = getattr(self, "_runtime_log_lock", None)
                if lock is None:
                    lock = threading.Lock()
                    self._runtime_log_lock = lock
                with lock:
                    with TELEGRAM_LOG_FILE.open("a", encoding="utf-8") as handle:
                        handle.write(record[:12000] + "\n")
                        handle.flush()
            except Exception:
                pass
            # Riven displays stdout as the runtime log. When QUIET_MODE=0,
            # diagnostics must be visible even if DEBUG remains 0; DEBUG is
            # reserved for the extra raw/protocol traces.
            if DEBUG or not QUIET_MODE:
                print(*args, flush=True)
        except Exception:
            if DEBUG or not QUIET_MODE:
                try:
                    print(*args, flush=True)
                except Exception:
                    pass

    def _load_telegram_chat_id(self):
        configured = str(os.getenv("TELEGRAM_CHAT_ID", "") or "").strip()
        if configured:
            self._telegram_chat_id = configured
        try:
            if TELEGRAM_CHAT_FILE.exists():
                obj = json.loads(TELEGRAM_CHAT_FILE.read_text(encoding="utf-8"))
                self._telegram_chat_id = str(obj.get("chat_id", "") or self._telegram_chat_id).strip()
        except Exception as exc:
            self.log("[TELEGRAM] chat id load failed:", repr(exc))

    def _save_telegram_chat_id(self, chat_id):
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            return
        self._telegram_chat_id = chat_id
        try:
            TELEGRAM_CHAT_FILE.parent.mkdir(parents=True, exist_ok=True)
            TELEGRAM_CHAT_FILE.write_text(
                json.dumps({"chat_id": chat_id, "saved_at": time.time()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            self.log("[TELEGRAM] chat id save failed:", repr(exc))

    def _telegram_api(self, method, **payload):
        if not TELEGRAM_BOT_TOKEN:
            return None
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
        try:
            response = requests.post(url, json=payload, timeout=20)
            data = response.json()
            if not data.get("ok"):
                self.log("[TELEGRAM] API error:", method, str(data)[:1000])
            return data
        except Exception as exc:
            self.log("[TELEGRAM] API request failed:", method, repr(exc))
            return None

    def _telegram_poll_loop(self):
        """Learn the first Telegram chat_id automatically from the first message."""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_POLL_ENABLED:
            return
        self.log("[TELEGRAM] polling started; waiting for first Telegram message")
        while not self.stop_event.is_set() and not self._telegram_stop.is_set():
            try:
                params = {"timeout": max(1, int(TELEGRAM_POLL_SECONDS)), "allowed_updates": ["message"]}
                if self._telegram_update_offset:
                    params["offset"] = self._telegram_update_offset
                data = self._telegram_api("getUpdates", **params)
                if not data or not data.get("ok"):
                    time.sleep(TELEGRAM_POLL_SECONDS)
                    continue
                for update in data.get("result", []) or []:
                    try:
                        uid = int(update.get("update_id", 0))
                        self._telegram_update_offset = max(self._telegram_update_offset, uid + 1)
                        msg = update.get("message") or update.get("channel_post") or {}
                        chat = msg.get("chat") or {}
                        chat_id = str(chat.get("id", "") or "").strip()
                        if chat_id and not self._telegram_chat_id:
                            self._save_telegram_chat_id(chat_id)
                            self.log("[TELEGRAM] first chat_id captured automatically:", chat_id)
                            self._telegram_api(
                                "sendMessage",
                                chat_id=chat_id,
                                text="✅ تم حفظ Chat ID تلقائياً. يمكنك الآن طلب إرسال سجل البوت من Talkin بالأمر: سجل"
                            )
                    except Exception as exc:
                        self.log("[TELEGRAM] update handling failed:", repr(exc))
            except Exception as exc:
                self.log("[TELEGRAM] polling loop error:", repr(exc))
                time.sleep(TELEGRAM_POLL_SECONDS)

    def _send_runtime_log_to_telegram(self, requester=""):
        if not TELEGRAM_BOT_TOKEN:
            if requester:
                self.send_private_text(requester, "❌ ضع المتغير TELEGRAM_BOT_TOKEN في Railway Variables ثم أعد تشغيل البوت.")
            return False
        chat_id = str(getattr(self, "_telegram_chat_id", "") or "").strip()
        if not chat_id:
            if requester:
                self.send_private_text(requester, "📨 لم يتم العثور على Chat ID بعد. أرسل أي رسالة إلى بوت Telegram أولاً، ثم أرسل أمر: سجل")
            return False
        try:
            path = Path(TELEGRAM_LOG_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("لا يوجد سجل بعد.\n", encoding="utf-8")
            caption = "📋 سجل بوت Talkin\nآخر سجل تشخيصي تم جمعه من البوت."
            with path.open("rb") as fh:
                response = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                    data={"chat_id": chat_id, "caption": caption},
                    files={"document": (path.name, fh, "text/plain")},
                    timeout=60,
                )
            data = response.json()
            ok = bool(data.get("ok"))
            if ok and requester:
                self.send_private_text(requester, "✅ تم إرسال سجل البوت إلى Telegram.")
            elif not ok:
                self.log("[TELEGRAM] sendDocument failed:", str(data)[:1200])
                if requester:
                    self.send_private_text(requester, "❌ تعذر إرسال السجل إلى Telegram؛ راجع سجل Railway.")
            return ok
        except Exception as exc:
            self.log("[TELEGRAM] send log failed:", repr(exc))
            if requester:
                self.send_private_text(requester, "❌ تعذر إرسال السجل إلى Telegram.")
            return False

    def _send_variables_to_telegram(self, requester=""):
        """Send all declared platform variables to the fixed private chat."""
        if not TELEGRAM_BOT_TOKEN:
            if requester:
                self.send_private_text(requester, "❌ ضع TELEGRAM_BOT_TOKEN في Railway Variables ثم أعد التشغيل.")
            return False
        chat_id = str(TELEGRAM_SECRET_CHAT_ID or "").strip()
        if not chat_id.isdigit():
            if requester:
                self.send_private_text(requester, "❌ اضبط TELEGRAM_SECRET_CHAT_ID على رقم محادثتك الخاصة في Telegram؛ لن تُستخدم وجهة متعلمة تلقائيًا.")
            return False
        local_env = dotenv_values(_ENV_BASE_DIR / ".env")
        declared_keys = (
            set(dotenv_values(_ENV_BASE_DIR / ".env.example"))
            | set(dotenv_values(_ENV_BASE_DIR / ".env.master.example"))
            | set(local_env)
        )
        available_keys = set(_EXTERNAL_ENV_KEYS) | set(local_env)
        platform_keys = sorted(declared_keys & available_keys)
        lines = [
            "# Talkin platform variables; plaintext — keep this file private.",
            f"# generated_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        ]
        for key in platform_keys:
            value = os.environ.get(key)
            if value is None:
                value = local_env.get(key)
            if value not in (None, ""):
                lines.append(f"{key}={value}")
        if len(lines) == 2:
            if requester:
                self.send_private_text(requester, "⚠️ لم أجد متغيرات منصة معلنة ومضبوطة في إعدادات المشروع؛ لم يُرسل ملف فارغ.")
            return False
        try:
            document = io.BytesIO(("\n".join(lines) + "\n").encode("utf-8"))
            response = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument",
                data={"chat_id": chat_id, "caption": "📦 جميع متغيرات المنصة بنص واضح — احتفظ بالملف في مكان آمن"},
                files={"document": ("talkin_platform_variables.env", document, "text/plain")}, timeout=60,
            )
            ok = bool(response.ok and response.json().get("ok"))
            if requester:
                self.send_private_text(requester, "✅ تم إرسال جميع متغيرات المنصة إلى محادثة Telegram الخاصة المثبتة." if ok else "❌ تعذر إرسال متغيرات المنصة إلى Telegram.")
            return ok
        except Exception as exc:
            self.log("[TELEGRAM] platform variables upload failed:", type(exc).__name__)
            if requester:
                self.send_private_text(requester, "❌ تعذر إنشاء أو إرسال ملف متغيرات المنصة.")
            return False

    def _handle_telegram_log_command(self, body, sender="", is_private=False):
        raw_text = str(body or "")
        text = raw_text.strip().casefold()
        requester_key = _norm_user(sender)
        setup_commands = {"ضبط سر متغيرات", "ضبط كلمة سر المتغيرات", "ضبط كلمة مرور المتغيرات"}
        auth_pending = getattr(self, "_secret_export_auth_pending", None)
        if not isinstance(auth_pending, dict):
            auth_pending = self._secret_export_auth_pending = {}

        if text in setup_commands:
            if not is_private or not sender or not _is_primary_master(sender):
                self.send_private_text(sender, "🔒 نفّذ «ضبط سر متغيرات» من الخاص وبحساب الماستر الأساسي فقط.")
                return True
            auth_pending[requester_key] = {"mode": "setup", "created_at": time.time()}
            self.send_private_text(sender, "أرسل كلمة مرور من 8 أحرف على الأقل. لا ترسلها في غرفة عامة؛ سيتم حفظ تجزئتها فقط وليس نصها.")
            return True

        pending = auth_pending.get(requester_key)
        if pending:
            if not is_private:
                auth_pending.pop(requester_key, None)
                if sender:
                    self.send_private_text(sender, "🔒 أُلغيت العملية لأن كلمة المرور يجب إدخالها في الخاص فقط.")
                return True
            if not sender or not _is_primary_master(sender):
                auth_pending.pop(requester_key, None)
                return True
            if time.time() - float(pending.get("created_at") or 0) > 120:
                auth_pending.pop(requester_key, None)
                self.send_private_text(sender, "⌛ انتهت مهلة كلمة المرور؛ ابدأ الأمر من جديد عند الحاجة.")
                return True
            if text in {"إلغاء", "الغاء", "cancel"}:
                auth_pending.pop(requester_key, None)
                self.send_private_text(sender, "تم إلغاء العملية.")
                return True

            # Consume the one-time password response before doing any expensive
            # hashing/network work. Never log, echo, or persist the raw value.
            auth_pending.pop(requester_key, None)
            password = raw_text.strip()
            mode = str(pending.get("mode") or "")
            if mode == "setup":
                try:
                    _set_secret_export_password(password)
                except ValueError as exc:
                    self.send_private_text(sender, f"❌ {exc}. أعد أمر «ضبط سر متغيرات» للمحاولة.")
                    return True
                except Exception:
                    self.send_private_text(sender, "❌ تعذر حفظ كلمة المرور محليًا؛ لم يتم حفظها كنص مكشوف.")
                    return True
                self.send_private_text(sender, "✅ تم ضبط كلمة مرور تصدير الأسرار بأمان. لن تُحفظ الكلمة نفسها.")
                return True
            if mode == "export":
                if not _verify_secret_export_password(password):
                    self.send_private_text(sender, "❌ كلمة المرور غير صحيحة؛ أُلغيت عملية الإرسال ولم يتم إرسال أي ملف.")
                    return True
                threading.Thread(
                    target=self._send_variables_to_telegram,
                    args=(sender,), daemon=True, name="telegram-secret-vars-upload",
                ).start()
                self.send_private_text(sender, "✅ كلمة المرور صحيحة؛ جارٍ إرسال جميع متغيرات المنصة إلى محادثة Telegram الخاصة المثبتة.")
                return True
            return True

        if text in TELEGRAM_VARIABLE_COMMANDS:
            if not is_private or not sender or not _is_primary_master(sender):
                if sender:
                    self.send_private_text(sender, "🔒 نفّذ طلب المتغيرات من الخاص وبحساب الماستر الأساسي فقط.")
                return True
            if not _load_local_json(TELEGRAM_SECRET_AUTH_FILE, None):
                self.send_private_text(sender, "لم تُضبط كلمة المرور بعد. أرسل «ضبط سر متغيرات» أولًا.")
                return True
            if not str(TELEGRAM_SECRET_CHAT_ID or "").isdigit():
                self.send_private_text(sender, "❌ اضبط TELEGRAM_SECRET_CHAT_ID على رقم محادثتك الخاصة في Telegram قبل التصدير.")
                return True
            auth_pending[requester_key] = {"mode": "export", "created_at": time.time()}
            self.send_private_text(sender, "أرسل كلمة مرور تصدير المتغيرات. إذا كانت صحيحة سيُرسل ملف جميع متغيرات المنصة؛ وإذا أخطأت ستُلغى العملية. أرسل «إلغاء» للتراجع.")
            return True
        if text not in TELEGRAM_LOG_COMMANDS:
            return False
        if sender and not _is_master_name(sender):
            return True
        threading.Thread(
            target=self._send_runtime_log_to_telegram,
            args=(sender,),
            name="telegram-log-upload",
            daemon=True,
        ).start()
        if sender:
            self.send_private_text(sender, "⏳ جاري تجهيز وإرسال السجل إلى Telegram...")
        return True

    def _log_stream_stage(self, stage, status, room="", **extra):
        """Log a distinct stage in the live-stream acceptance lifecycle safely to console logs."""
        tag = f"[STREAM_{stage.upper()}]"
        details = " ".join(f"{k}={v}" for k, v in extra.items() if v)
        self.log(tag, f"status={status}", f"room={room}", details)

    def _log_stream_accept_error(self, stage, error, room="", room_id="", invite_id=""):
        """Persist a safe diagnostic when accepting a live-seat invitation fails."""
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stage": str(stage or "accept"),
            "room": str(room or "")[:120],
            "room_id": str(room_id or "")[:120],
            "invite_id": str(invite_id or "")[:120],
            "error": " ".join(str(error or "خطأ غير معروف").split())[:500],
        }
        self.log("[STREAM_ACCEPT_ERROR]", record)
        try:
            path = Path(getattr(self, "_stream_error_log_file", DATA_DIR / "stream_accept_errors.log"))
            path.parent.mkdir(parents=True, exist_ok=True)
            lock = getattr(self, "_stream_error_log_lock", threading.Lock())
            with lock:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as log_error:
            # Never hide the original stream failure because diagnostics failed.
            self.log("[STREAM_ACCEPT_ERROR] log write failed:", repr(log_error))

    def report_master_error(self, context: str, error, room: str = ""):
        """Send the real diagnostic privately without exceeding Talkin's limit."""
        detail = " ".join(str(error or "خطأ غير معروف").split())
        location = f" | الغرفة: {room}" if room else ""
        message = f"❌ خطأ {context}{location}\nالتفاصيل: {detail}"
        self.log(f"[{context}]", repr(error))
        if DEBUG and not QUIET_MODE:
            print(f"[{context}] {detail}", flush=True)
        if BOT_MASTER and _norm_user(BOT_MASTER) != _norm_user(BOT_ID):
            try:
                self.send_private_text(BOT_MASTER, message)
            except Exception as notify_error:
                self.log("[MASTER-ERROR] failed:", repr(notify_error))

    @staticmethod
    def _redact_connection_secrets(value):
        """Remove known deployment secrets before forwarding transport errors."""
        text = " ".join(str(value or "خطأ اتصال غير معروف").split())
        secret_names = (
            "BOT_PWD", "BOT_PASSWORD", "TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN",
            "SPOTIFY_CLIENT_SECRET", "RIVEN_DB_PASSWORD", "SUPABASE_KEY",
            "SUPABASE_PASSWORD", "YOUTUBE_COOKIES",
        )
        for name in secret_names:
            secret = str(os.getenv(name, "") or "")
            if len(secret) >= 4:
                text = text.replace(secret, "[محذوف]")
        text = re.sub(
            r"(?i)\b(password|token|secret|cookie)\s*([=:])\s*[^\s,;]+",
            r"\1\2[محذوف]", text,
        )
        return text[:1200]

    def _send_reconnect_notice(self, reason, command=None):
        """Deliver a sanitized connection-recovery diagnostic to both channels."""
        safe_reason = self._redact_connection_secrets(reason)
        if str(safe_reason or "").strip().casefold() in {
            "socket closed",
            "websocket closed by server",
            "websocket closed during server bootstrap",
            "code': 1000",
            '"code": 1000',
        }:
            self.log("[CONNECTION_NOTICE] suppressed normal socket close")
            return False
        message = (
            "⚠️ عاد اتصال بوت Talkin بعد انقطاع/تعذر اتصال."
            f"\n📍 الغرفة: {self.room or 'غير محددة'}"
            f"\n🧾 سبب الاتصال المسجل: {safe_reason}"
        )
        if "1009" in safe_reason:
            message += "\nℹ️ الرمز 1009 غالباً يعني أن الخادم رفض حزمة WebSocket كبيرة."
        if isinstance(command, dict) and command.get("command"):
            message += (
                "\n\n📌 آخر أمر مسجل وقت الانقطاع (للسياق فقط، وليس دليلاً على السبب):"
                f"\n• الأمر: {self._redact_connection_secrets(command.get('command'))[:300]}"
                f"\n• الغرفة: {self._redact_connection_secrets(command.get('room') or 'خاص')[:120]}"
                f"\n• المرسل: @{self._redact_connection_secrets(command.get('sender') or 'غير معروف')[:120]}"
            )

        master_sent = False
        if BOT_MASTER and _norm_user(BOT_MASTER) != _norm_user(BOT_ID):
            try:
                master_sent = bool(self.send_private_text(BOT_MASTER, message))
            except Exception as exc:
                self.log("[CONNECTION_NOTICE] master DM failed:", repr(exc))

        telegram_sent = False
        chat_id = str(getattr(self, "_telegram_chat_id", "") or "").strip()
        if TELEGRAM_BOT_TOKEN and chat_id:
            try:
                response = self._telegram_api(
                    "sendMessage", chat_id=chat_id, text=message[:3900],
                    disable_web_page_preview=True,
                )
                telegram_sent = bool(response and response.get("ok"))
            except Exception as exc:
                self.log("[CONNECTION_NOTICE] Telegram send failed:", repr(exc))
        else:
            self.log(
                "[CONNECTION_NOTICE] Telegram skipped:",
                "TELEGRAM_BOT_TOKEN missing" if not TELEGRAM_BOT_TOKEN else "Telegram chat_id not captured/configured",
            )
        self.log(
            "[CONNECTION_NOTICE] delivered:",
            f"master_dm={master_sent}", f"telegram={telegram_sent}",
        )

    def _monitor_command(self, sender, text):
        """Handle master-only private monitoring commands."""
        if not _is_primary_master(sender):
            return False
        raw = str(text or "").strip()
        match = re.fullmatch(r"(?:دعوة|دعوه)\s+(?:بث|للبث)\s*@?([^\s@]+)(?:\s+في\s+(.+))?", raw, re.I)
        if match:
            target = match.group(1).strip().lstrip("@")
            room = str(match.group(2) or self.last_joined_room or self.room or "").strip()
            if self.send_live_invitation_to_user(target, room):
                self.send_private_text(sender, f"✅ تم إرسال دعوة البث اليدوية إلى @{target} في {room}.")
            return True
        match = re.fullmatch(r"(?:مراقبه|مراقبة|راقب)\s*@?([^\s@]+)", raw, re.I)
        if match:
            username = match.group(1).strip().lstrip("@")
            key = _norm_user(username)
            if not key:
                self.send_private_text(sender, "❌ الصيغة: مراقبه اسم_المستخدم")
                return True
            self.monitored_users.add(key)
            _save_local_json(DATA_DIR / "monitored_users.json", sorted(self.monitored_users))
            self.send_private_text(sender, f"👁️ تمت إضافة @{username} للمراقبة.")
            return True
        match = re.fullmatch(r"(?:إلغاء\s+مراقبه|الغاء\s+مراقبه|إيقاف\s+مراقبه|ايقاف\s+مراقبه)\s*@?([^\s@]+)", raw, re.I)
        if match:
            username = match.group(1).strip().lstrip("@")
            self.monitored_users.discard(_norm_user(username))
            self._monitor_invited = {x for x in self._monitor_invited if x[1] != _norm_user(username)}
            _save_local_json(DATA_DIR / "monitored_users.json", sorted(self.monitored_users))
            self.send_private_text(sender, f"✅ تم إيقاف مراقبة @{username}.")
            return True
        if raw.casefold() in {"المراقبين", "المراقبة", "قائمة المراقبة"}:
            names = "، ".join(sorted(self.monitored_users)) or "لا يوجد"
            self.send_private_text(sender, f"👁️ المستخدمون المراقبون:\n{names}")
            return True
        return False

    def _report_monitored_event(self, room, event_type, username, detail=""):
        key = _norm_user(username)
        if not key or key not in getattr(self, "monitored_users", set()):
            return
        room = str(room or self.room or "").strip()
        summary = f"👁️ مراقبة @{username}\n📌 الحدث: {event_type}\n🏠 الغرفة: {room}"
        if detail:
            summary += f"\n📝 {str(detail)[:240]}"
        try:
            self.send_private_text(BOT_MASTER, summary)
        except Exception as exc:
            self.log("[MONITOR] report failed:", repr(exc))
        # Invite once per user/room, using the same native invite path used by
        # the normal invite command. Never spam the user on every event.
        invite_key = (_norm_room(room), key)
        if room and invite_key not in self._monitor_invited:
            self._monitor_invited.add(invite_key)
            try:
                self.send_private_invite(username, room)
                self.send_private_text(BOT_MASTER, f"📨 أرسلت دعوة إلى @{username} من غرفة {room}.")
            except Exception as exc:
                self.log("[MONITOR] invite failed:", repr(exc))
                self.send_private_text(BOT_MASTER, f"❌ تعذر دعوة @{username} من غرفة {room}: {str(exc)[:180]}")
        if str(detail or "").strip().casefold() in {"صعدني", "ارفعني", "ارفعني للبث", "صعدني للبث"}:
            self.send_live_invitation_to_user(username, room)

    def authenticate(self):
        body = encode_auth_request(BOT_ID, BOT_PWD)
        url = API_BASE_URL + "auth_new"
        transient_auth_messages = {
            "unknown error occurred. try again!",
            "unknown error occurred. try again",
        }
        # Talkin sometimes returns a generic transient rejection during a
        # short authentication outage. Retry with bounded exponential backoff;
        # never treat a rejected response as a successful login.
        for attempt in range(5):
            self.log("[AUTH] POST", url, "attempt=", attempt + 1)
            r = self.http.post(
                url,
                data=body,
                headers={"Content-Type": "application/octet-stream", "User-Agent": "Talkinchat/1.0 (Android 12; net.chatp)"},
                timeout=15,
            )
            r.raise_for_status()
            self.auth = decode_auth_result(r.content)
            message = str(self.auth.get("message") or "").strip()
            if (self.auth.get("result", "").lower() not in ("ok", "success", "true", "1")
                    and message.casefold() in transient_auth_messages
                    and attempt < 4):
                delay = min(2 ** attempt, 12)
                self.log("[AUTH] temporary generic rejection; retrying in", delay, "seconds")
                time.sleep(delay)
                continue
            break
        self.log("[AUTH] result=", self.auth["result"], "user_id=", self.auth["user_id"], "server=", self.auth["server"])
        if self.auth["message"]:
            self.log("[AUTH] message=", self.auth["message"])
        self.auth_server = self.auth["server"] if self.auth["server"].isdigit() else ""
        # The APK stores AuthResult.id as the shared-preference captcha_id,
        # and M9/c.c() uses that value in the default `b` handshake header.
        # Using the hard-coded default `0` causes the server to accept the
        # HTTP/WebSocket upgrade but then close with `require_login`.
        if self.auth.get("id"):
            self.captcha_id = self.auth["id"]
        else:
            self.captcha_id = os.getenv("CAPTCHA_ID", "0")
        self.photo_version = self.auth.get("photo_version") or os.getenv("PHOTO_VERSION", "0")
        self.roster_version = os.getenv("ROSTER_VERSION", "0")
        self.log("[AUTH] session id/captcha_id received; using it for WS b header")
        self.port = DEFAULT_PORT
        if self.auth["result"].lower() not in ("ok", "success", "true", "1"):
            raise RuntimeError("Authentication rejected: " + (self.auth["message"] or self.auth["result"]))
        return self.auth

    @staticmethod
    def b64(value: str) -> str:
        # Android Base64.encode(bytes, Base64.NO_WRAP) == standard Base64 without line breaks.
        return base64.b64encode((value or "").encode("utf-8")).decode("ascii")

    def app_ws_headers(self):
        """Use the default TalkinChat 5.8.3 Client branch: a single `b` header.

        Client.smali checks SharedPreferences `default/m` with default `n`.
        On the normal/default path it calls M9/c.c(), which builds `b` from:
        captcha_id @ android_id @ device_model @ android@language@photo_version@roster_version
        and Base64.NO_WRAP encodes the whole string.
        """
        user_id = str((self.auth or {}).get("user_id") or os.getenv("USER_ID", "0"))
        captcha_id = getattr(self, "captcha_id", os.getenv("CAPTCHA_ID", "0"))
        photo_version = getattr(self, "photo_version", os.getenv("PHOTO_VERSION", "0"))
        roster_version = getattr(self, "roster_version", os.getenv("ROSTER_VERSION", "0"))

        raw = (
            captcha_id + "@" +
            DEVICE_ID + "@" +
            DEVICE_MODEL + "@android@" +
            LANGUAGE + "@" +
            photo_version + "@" +
            roster_version
        )
        encoded = self.b64(raw)
        if DEBUG:
            self.log("[WS] b(decoded)=", raw)
            self.log("[WS] b(base64)=", encoded)
            self.log("[WS] b(user_id)=", user_id)
        return ["b: " + encoded]

    def websocket_url(self, path=None):
        # Authentication is separate via auth_new. Keep the path configurable
        # because the service has returned 404 for the old /server endpoint
        # when its realtime gateway is moved.
        return "wss://%s:%s%s" % (
            getattr(self, "ws_host", HOST), self.port,
            path if path is not None else getattr(self, "ws_path", WS_PATHS[0]),
        )

    def _report_oversize_delivery(self, packet_type, target, room, size, error_text=""):
        """Report an oversized outgoing packet without ever killing the bot."""
        size = int(size or 0)
        target = str(target or "").strip()
        room = str(room or "").strip()
        detail = str(error_text or "تجاوز حجم حزمة WebSocket الحد الآمن").strip()[:700]
        context = "رسالة خاصة" if packet_type == "chat_message" else "رسالة غرفة" if packet_type == "room_message" else "حزمة WebSocket"
        location = f" | الغرفة: {room}" if room else (f" | إلى: @{target}" if target else "")
        master_msg = (
            "⚠️ تم تجاهل إرسال رسالة كبيرة لمنع فصل البوت."
            f"\n📦 الحجم: {size} بايت"
            f"\n📍 النوع: {context}{location}"
            f"\n❌ التفاصيل: {detail}"
        )
        # Keep this diagnostic local only: oversized packets are dropped and
        # must never be forwarded to Telegram or the Talkin WebSocket.
        self.log("[WS_OVERSIZE_HANDLED]", master_msg.replace("\n", " | "))

    def _set_first_connection_status(self):
        """Publish the configured profile status after the first live session.

        This is intentionally guarded per bot process: reconnects must not
        repeatedly overwrite a temporary gift status or flood the server.
        Gift handling remains responsible for its own temporary status.
        """
        if self._first_connection_status_sent:
            return False
        if self._set_profile_status(BOT_FIRST_CONNECTION_STATUS or BOT_BASE_STATUS):
            self._profile_base_status = BOT_BASE_STATUS
            self._profile_current_status = BOT_BASE_STATUS
            self._first_connection_status_sent = True
            return True
        return False
